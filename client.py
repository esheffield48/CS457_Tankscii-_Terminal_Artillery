"""Authoritative TCP client and 80x24 ASCII terminal UI for Tankscii.

This module implements the Sprint 3 CLI contract (`-h`, `-i`, `-p`), graceful
DNS hostname resolution via `socket.gethostbyname()` with explicit
`socket.gaierror` handling, persistent `bytearray` newline-delimited JSON
stream framing, and the interactive 80x24 ASCII battlefield terminal UI.
"""

from __future__ import annotations

import argparse
import copy
import selectors
import socket
import sys
import time
from typing import Any

from tankscii import (
    DEFAULT_CLIENT_VERSION,
    DEFAULT_P1_NICKNAME,
    DEFAULT_P2_NICKNAME,
    MAX_PORT,
    MIN_PORT,
    TURN_DURATION_SECONDS,
)
from tankscii.framing import (
    ConnectionClosedError,
    FrameDecodeError,
    create_tls_context,
    decode_json_line,
    receive_and_extract_raw_lines,
    send_frame_safely,
)
from tankscii.protocol import (
    MAX_NICKNAME_LENGTH,
    MIN_NICKNAME_LENGTH,
    build_connect_message,
    build_disconnect_message,
    build_fire_message,
    build_move_message,
    validate_frame_envelope,
)
from tankscii.renderer import (
    DEFAULT_SCANLINE_DELAY,
    DEFAULT_SECTION_PAUSE,
    DEFAULT_TUTORIAL_LINE_DELAY,
    animate_fire_trajectory_in_place,
    enable_ansi_terminal_colors,
    format_action_summary,
    format_game_over_banner,
    format_hud_header,
    format_state_resume_banner,
    format_tutorial_banner,
    print_scanline_block,
    redraw_hud_and_board_in_place,
    render_battlefield_ascii,
)

DEFAULT_NICKNAME: str = DEFAULT_P1_NICKNAME


def parse_client_arguments(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse and validate command-line arguments for the Tankscii client.

    Enforces the Sprint 3 CLI specification requiring `-i <hostname_or_ip>` and
    `-p <port>` within the unprivileged port range `1024-65535`.

    Args:
        argv: Optional argument list for unit testing; defaults to `sys.argv[1:]`.

    Returns:
        Parsed `argparse.Namespace` with `hostname`, `port`, and `nickname`.
    """
    parser = argparse.ArgumentParser(
        prog="client.py",
        description=(
            "Tankscii : Terminal Artillery — IPv4 TCP Client & 80x24 ASCII UI."
        ),
        epilog=(
            "Example: python3 client.py -i server.sheffield.edu -p 8080"
        ),
    )
    parser.add_argument(
        "-i",
        "--ip",
        "--hostname",
        dest="hostname",
        required=True,
        metavar="<server_hostname_or_ip>",
        help="Server DNS hostname (e.g., server.sheffield.edu) or IPv4 address.",
    )
    parser.add_argument(
        "-p",
        "--port",
        dest="port",
        required=True,
        type=int,
        metavar="<port>",
        help=f"Server TCP port ({MIN_PORT}-{MAX_PORT}).",
    )
    parser.add_argument(
        "-n",
        "--nickname",
        dest="nickname",
        default=None,
        metavar="<nickname>",
        help="Optional player nickname (1-16 chars); prompts interactively if omitted.",
    )
    parser.add_argument(
        "-tls",
        "--tls",
        action="store_true",
        default=False,
        help="Enable opt-in TLS encryption (cleartext TCP is default for Wireshark).",
    )
    parser.add_argument(
        "--cert",
        default="cert.pem",
        help="Path to trusted server TLS certificate PEM file (used with -tls).",
    )

    args = parser.parse_args(argv)
    if not (MIN_PORT <= args.port <= MAX_PORT):
        parser.error(f"Port must be between {MIN_PORT} and {MAX_PORT}.")

    return args


def resolve_server_hostname(hostname: str) -> str:
    """Resolve a server DNS hostname or IPv4 literal to an IPv4 address string.

    Explicitly catches `socket.gaierror` when DNS resolution fails so the client
    never crashes with a raw Python traceback, instead guiding the user to
    verify their Cisco router DNS configuration (`ip dns server` / `ip host`).

    Args:
        hostname: Target server DNS hostname or IPv4 address string.

    Returns:
        Resolved dotted-quad IPv4 address string.
    """
    try:
        server_ip: str = socket.gethostbyname(hostname)
        return server_ip
    except socket.gaierror:
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print(
            "Please check your Cisco router DNS configuration "
            "('ip dns server' / 'ip host') or verify domain spelling."
        )
        sys.exit(1)


def sanitize_or_prompt_nickname(configured_nickname: str | None = None) -> str:
    """Validate a pre-configured nickname or interactively prompt the player.

    Args:
        configured_nickname: Optional nickname passed via CLI or constructor.

    Returns:
        Validated player nickname between 1 and 16 characters.
    """
    if configured_nickname is not None:
        cleaned = configured_nickname.strip()
        if MIN_NICKNAME_LENGTH <= len(cleaned) <= MAX_NICKNAME_LENGTH:
            return cleaned
        if len(cleaned) > MAX_NICKNAME_LENGTH:
            return cleaned[:MAX_NICKNAME_LENGTH]
        return "DEFAULT"

    prompt_text = (
        f"Enter nickname (1-16 chars) "
        f"[default: {DEFAULT_P1_NICKNAME} / {DEFAULT_P2_NICKNAME}]: "
    )
    while True:
        try:
            raw_entry = input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "DEFAULT"

        if not raw_entry:
            return "DEFAULT"
        if MIN_NICKNAME_LENGTH <= len(raw_entry) <= MAX_NICKNAME_LENGTH:
            return raw_entry
        print(
            f"[INPUT ERROR] Nickname must be between {MIN_NICKNAME_LENGTH} "
            f"and {MAX_NICKNAME_LENGTH} characters."
        )


def parse_turn_command_input(
    raw_command: str,
    player_id: str,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Parse a raw CLI turn command into an outbound protocol message dictionary.

    Supported commands:
    - `MOVE LEFT <1-5>` or `MOVE RIGHT <1-5>`
    - `FIRE <angle 0-180> <power 1-100>`
    - `FORFEIT` or `QUIT`

    Args:
        raw_command: User-entered command string.
        player_id: Local player's assigned canonical ID (`"Player_1"` or `"Player_2"`).

    Returns:
        Tuple of `(outbound_frame, error_message, is_disconnect)`.
    """
    tokens = raw_command.strip().split()
    if not tokens:
        return None, "Please enter a command (MOVE, FIRE, FORFEIT, or QUIT).", False

    verb = tokens[0].upper()

    if verb in ("FORFEIT", "QUIT", "EXIT"):
        reason = "FORFEIT" if verb == "FORFEIT" else "USER_QUIT"
        frame = build_disconnect_message(player_id=player_id, reason=reason)
        return frame, None, True

    if verb == "MOVE":
        if len(tokens) != 3:
            return (
                None,
                "Usage: MOVE <LEFT|RIGHT> <distance 1-5> (e.g., MOVE RIGHT 3).",
                False,
            )
        direction = tokens[1].upper()
        try:
            distance = int(tokens[2])
        except ValueError:
            return (
                None,
                "Move distance must be an integer between 1 and 5.",
                False,
            )
        frame = build_move_message(
            player_id=player_id,
            direction=direction,
            distance=distance,
        )
        return frame, None, False

    if verb == "FIRE":
        if len(tokens) != 3:
            return (
                None,
                "Usage: FIRE <angle 0-180> <power 1-100> (e.g., FIRE 45 75).",
                False,
            )
        try:
            angle = int(tokens[1])
            power = int(tokens[2])
        except ValueError:
            return (
                None,
                "Fire angle (0-180) and power (1-100) must be integers.",
                False,
            )
        frame = build_fire_message(
            player_id=player_id,
            angle=angle,
            power=power,
        )
        return frame, None, False

    return (
        None,
        "Unrecognized command. Valid commands: MOVE <LEFT|RIGHT> <1-5>, "
        "FIRE <angle> <power>, FORFEIT, QUIT.",
        False,
    )


class TanksciiClient:
    """Stateful IPv4 TCP client and ASCII terminal interface for Tankscii.

    Maintains the persistent `bytearray` receive buffer required for
    newline-delimited UTF-8 JSON framing over TCP and synchronizes local display
    state exclusively from authoritative server broadcasts (`LOBBY_WAIT`,
    `GAME_START`, `STATE_UPDATE`, `ERROR`, `GAME_OVER`).
    """

    def __init__(
        self,
        hostname: str,
        port: int,
        nickname: str | None = None,
        scripted_commands: list[str] | None = None,
        selector: selectors.BaseSelector | None = None,
        use_color: bool | None = None,
        scanline_delay: float | None = None,
        use_tls: bool = False,
        tls_certfile: str | None = None,
    ) -> None:
        """Initialize the client with target server address and optional commands.

        Args:
            hostname: Server DNS hostname or IPv4 address string.
            port: Validated server TCP port (`1024-65535`).
            nickname: Optional pre-selected player nickname (`1-16` chars).
            scripted_commands: Optional queue of turn command strings for
                automated testing or deterministic demo execution.
            selector: Optional custom `selectors.BaseSelector` for testing.
            use_color: Optional override for ANSI color rendering; auto-detects
                interactive TTY when `None`.
            scanline_delay: Optional line-by-line scanline reveal delay in
                seconds; defaults to `DEFAULT_SCANLINE_DELAY` for interactive
                color terminals and `0.0` for scripted/test runs.
        """
        self.hostname: str = hostname
        self.port: int = port
        self.configured_nickname: str | None = nickname
        self.used_default_nickname: bool = not bool(
            nickname and nickname.strip()
        )
        self.nickname: str = (
            nickname.strip()
            if nickname and nickname.strip()
            else DEFAULT_P1_NICKNAME
        )
        self.server_ip: str = "127.0.0.1"
        self.connected_banner_printed: bool = False
        self.last_fire_rendered_in_place: bool = False
        self.scripted_commands: list[str] = (
            list(scripted_commands) if scripted_commands is not None else []
        )
        self.use_color: bool = (
            enable_ansi_terminal_colors() if use_color is None else use_color
        )
        if scanline_delay is not None:
            self.scanline_delay: float = float(scanline_delay)
        else:
            self.scanline_delay = (
                DEFAULT_SCANLINE_DELAY
                if (self.use_color and not self.scripted_commands)
                else 0.0
            )
        self.tutorial_line_delay: float = (
            DEFAULT_TUTORIAL_LINE_DELAY if (self.scanline_delay > 0.0) else 0.0
        )
        self.section_pause: float = (
            DEFAULT_SECTION_PAUSE if (self.scanline_delay > 0.0) else 0.0
        )
        self.tutorial_shown: bool = False
        self.use_tls: bool = use_tls
        self.tls_certfile: str | None = tls_certfile

        self.sock: socket.socket | None = None
        self.selector: selectors.BaseSelector = (
            selector if selector is not None else selectors.DefaultSelector()
        )
        # Persistent per-connection bytearray accumulator for \n TCP framing.
        self.stream_buffer: bytearray = bytearray()

        self.assigned_id: str = "Player_1"
        self.opponent_nickname: str = "Opponent"
        self.tanks: dict[str, dict[str, int]] = {
            "Player_1": {"x": 10, "y": 6, "hp": 100},
            "Player_2": {"x": 70, "y": 6, "hp": 100},
        }
        self.craters: list[dict[str, int]] = []
        self.active_turn: str = "Player_1"
        self.time_remaining: int = TURN_DURATION_SECONDS
        self.current_round: int = 1
        self.move_used: bool = False
        self.is_running: bool = False
        self.needs_turn_prompt: bool = False

    def connect_to_server(self) -> None:
        """Resolve server hostname, open the TCP connection, and send `CONNECT`.

        Catches `(ConnectionRefusedError, TimeoutError, OSError)` during socket
        connection so that unreachable server endpoints exit cleanly with code 1
        instead of printing an unhandled traceback.
        """
        server_ip = resolve_server_hostname(self.hostname)
        self.server_ip = server_ip
        prompted_name = sanitize_or_prompt_nickname(self.configured_nickname)
        if prompted_name == "DEFAULT":
            self.used_default_nickname = True
            self.nickname = DEFAULT_P1_NICKNAME
            handshake_nickname = "DEFAULT"
        else:
            self.used_default_nickname = False
            self.nickname = prompted_name
            handshake_nickname = prompted_name

        tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            tcp_socket.connect((server_ip, self.port))
            if self.use_tls:
                tls_context = create_tls_context(
                    is_server=False,
                    certfile=self.tls_certfile,
                )
                tcp_socket = tls_context.wrap_socket(
                    tcp_socket,
                    server_hostname=self.hostname,
                )
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            tcp_socket.close()
            print(
                f"[ERROR] Unable to connect to server '{self.hostname}' "
                f"({server_ip}:{self.port}): {exc}"
            )
            sys.exit(1)

        # Switch to non-blocking mode and multiplex reads via DefaultSelector so
        # we can inspect readable server frames before and after user input.
        tcp_socket.setblocking(False)
        self.sock = tcp_socket
        self.selector.register(tcp_socket, selectors.EVENT_READ)
        self.is_running = True

        if not self.used_default_nickname:
            print(
                f"[CONNECTED] Radio link established with {self.hostname} "
                f"({server_ip}:{self.port}) — Armor Crewman '{self.nickname}' "
                "reporting for duty."
            )
            self.connected_banner_printed = True

        connect_frame = build_connect_message(
            nickname=handshake_nickname,
            client_version=DEFAULT_CLIENT_VERSION,
            player_id=self.assigned_id,
        )
        if not send_frame_safely(self.sock, connect_frame):
            print("[ERROR] Failed to transmit initial CONNECT handshake frame.")
            self.close_connection()
            sys.exit(1)

    def run_client_loop(self) -> None:
        """Run the main client receive-render-prompt loop until match end."""
        try:
            while self.is_running and self.sock is not None:
                # Wait for incoming server frames. If a turn prompt is pending,
                # poll with timeout=0.0 first to drain any coalesced frames
                # (such as GAME_OVER immediately following STATE_UPDATE). Cap at
                # 0.5s so Windows delivers Ctrl+C promptly while waiting.
                select_timeout: float = (
                    0.0 if self.needs_turn_prompt else 0.5
                )
                ready_events = self.selector.select(timeout=select_timeout)

                if ready_events:
                    self._read_and_process_available_frames()
                    continue

                if self.needs_turn_prompt and self.is_running:
                    self.needs_turn_prompt = False
                    self.prompt_and_send_turn_action()
        except KeyboardInterrupt:
            print("\n[CLIENT] Interrupted by user. Disconnecting cleanly...")
            if self.sock is not None and self.is_running:
                disconnect_frame = build_disconnect_message(
                    player_id=self.assigned_id,
                    reason="USER_QUIT",
                )
                send_frame_safely(self.sock, disconnect_frame)
        finally:
            self.close_connection()

    def _has_unread_socket_data(self) -> bool:
        """Return True if the server socket currently has unread bytes ready."""
        if (
            self.sock is None
            or not self.is_running
            or not self.selector.get_map()
        ):
            return False
        ready_events = self.selector.select(timeout=0.0)
        return bool(ready_events)

    def _read_and_process_available_frames(self) -> None:
        """Read available TCP bytes into `stream_buffer` and dispatch frames."""
        if self.sock is None:
            return

        try:
            raw_lines = receive_and_extract_raw_lines(
                self.sock, self.stream_buffer
            )
        except ConnectionClosedError as exc:
            if self.is_running:
                print(f"[DISCONNECTED] Server closed the connection: {exc}")
            self.is_running = False
            return

        for raw_line in raw_lines:
            if not self.is_running:
                break
            try:
                frame = decode_json_line(raw_line)
            except FrameDecodeError as exc:
                print(f"[PROTOCOL WARNING] Ignored malformed server frame: {exc}")
                continue

            is_valid, detail = validate_frame_envelope(frame)
            if not is_valid:
                print(
                    f"[PROTOCOL WARNING] Invalid server frame envelope: {detail}"
                )
                continue

            self.handle_server_frame(frame)

    def handle_server_frame(self, frame: dict[str, Any]) -> None:
        """Dispatch a validated server message envelope by its `msg_type`.

        Args:
            frame: Validated 4-key Tankscii message dictionary.
        """
        msg_type = str(frame["msg_type"])
        payload: dict[str, Any] = frame["payload"]

        if msg_type == "LOBBY_WAIT":
            self.handle_lobby_wait(payload)
        elif msg_type == "GAME_START":
            self.handle_game_start(payload)
        elif msg_type == "STATE_RESUME":
            self.handle_state_resume(payload)
        elif msg_type == "STATE_UPDATE":
            self.handle_state_update(payload)
        elif msg_type == "ERROR":
            self.handle_error_frame(payload)
        elif msg_type == "GAME_OVER":
            self.handle_game_over(payload)

    def get_player_names_map(self) -> dict[str, str]:
        """Return a mapping of `'Player_1'` and `'Player_2'` to nicknames."""
        if self.assigned_id == "Player_1":
            return {
                "Player_1": self.nickname,
                "Player_2": self.opponent_nickname,
            }
        return {
            "Player_1": self.opponent_nickname,
            "Player_2": self.nickname,
        }

    def _ensure_connected_banner_printed(self) -> None:
        """Print the Armor Crewman connection banner once the nickname is finalized."""
        if not self.connected_banner_printed and self.sock is not None:
            print(
                f"[CONNECTED] Radio link established with {self.hostname} "
                f"({self.server_ip}:{self.port}) — Armor Crewman "
                f"'{self.nickname}' reporting for duty."
            )
            self.connected_banner_printed = True

    def handle_lobby_wait(self, payload: dict[str, Any]) -> None:
        """Process a `LOBBY_WAIT` notification while waiting for Player 2.

        Args:
            payload: `LOBBY_WAIT` payload dictionary.
        """
        self.assigned_id = str(payload.get("assigned_id", "Player_1"))
        if self.used_default_nickname:
            self.nickname = (
                DEFAULT_P1_NICKNAME
                if self.assigned_id == "Player_1"
                else DEFAULT_P2_NICKNAME
            )
        self._ensure_connected_banner_printed()
        lobby_message = str(
            payload.get("message", "Waiting for opponent...")
        )
        print(
            f"[LOBBY] Armor Crewman '{self.nickname}' deployed — "
            "Holding position in the staging area while waiting for "
            f"opposing tank crew... ({lobby_message})"
        )

    def handle_game_start(self, payload: dict[str, Any]) -> None:
        """Process `GAME_START`, initialize match state, and render the board.

        Args:
            payload: `GAME_START` payload dictionary.
        """
        self.assigned_id = str(payload.get("assigned_id", self.assigned_id))
        if self.used_default_nickname:
            self.nickname = (
                DEFAULT_P1_NICKNAME
                if self.assigned_id == "Player_1"
                else DEFAULT_P2_NICKNAME
            )
        self._ensure_connected_banner_printed()
        self.opponent_nickname = str(
            payload.get("opponent_nickname", "Opponent")
        )
        self.tanks = dict(payload.get("tanks", self.tanks))
        self.active_turn = str(payload.get("first_turn", "Player_1"))
        self.time_remaining = int(
            payload.get("turn_duration", TURN_DURATION_SECONDS)
        )
        self.current_round = 1
        self.craters = []
        self.move_used = False
        self.tutorial_shown = True

        print()
        tutorial_text = format_tutorial_banner(
            assigned_id=self.assigned_id,
            opponent_nickname=self.opponent_nickname,
            use_color=self.use_color,
            player_nickname=self.nickname,
        )
        print_scanline_block(tutorial_text, line_delay=self.tutorial_line_delay)
        if self.section_pause > 0.0:
            time.sleep(self.section_pause)

        print()
        hud_text = format_hud_header(
            assigned_id=self.assigned_id,
            opponent_nickname=self.opponent_nickname,
            current_round=self.current_round,
            tanks=self.tanks,
            active_turn=self.active_turn,
            time_remaining=self.time_remaining,
            use_color=self.use_color,
            player_nickname=self.nickname,
        )
        battlefield_text = render_battlefield_ascii(
            tanks=self.tanks,
            craters=self.craters,
            use_color=self.use_color,
        )
        print_scanline_block(hud_text, line_delay=self.scanline_delay)
        print_scanline_block(battlefield_text, line_delay=self.scanline_delay)

        self.needs_turn_prompt = self.active_turn == self.assigned_id

    def handle_state_resume(self, payload: dict[str, Any]) -> None:
        """Process `STATE_RESUME`, restore full match state, and render the board.

        Args:
            payload: `STATE_RESUME` payload dictionary.
        """
        self.assigned_id = str(payload.get("assigned_id", self.assigned_id))
        if self.used_default_nickname:
            self.nickname = (
                DEFAULT_P1_NICKNAME
                if self.assigned_id == "Player_1"
                else DEFAULT_P2_NICKNAME
            )
        self._ensure_connected_banner_printed()
        self.opponent_nickname = str(
            payload.get("opponent_nickname", self.opponent_nickname)
        )
        self.current_round = int(payload.get("round", self.current_round))
        self.tanks = dict(payload.get("tanks", self.tanks))
        self.craters = list(payload.get("craters") or self.craters)
        self.active_turn = str(payload.get("active_turn", self.active_turn))
        self.time_remaining = int(
            payload.get("time_remaining", TURN_DURATION_SECONDS)
        )
        self.move_used = bool(payload.get("move_used", False))
        self.last_fire_rendered_in_place = False

        print()
        print(
            format_state_resume_banner(payload, use_color=self.use_color),
            flush=True,
        )
        hud_text = format_hud_header(
            assigned_id=self.assigned_id,
            opponent_nickname=self.opponent_nickname,
            current_round=self.current_round,
            tanks=self.tanks,
            active_turn=self.active_turn,
            time_remaining=self.time_remaining,
            use_color=self.use_color,
            player_nickname=self.nickname,
        )
        battlefield_text = render_battlefield_ascii(
            tanks=self.tanks,
            craters=self.craters,
            use_color=self.use_color,
        )
        print_scanline_block(hud_text, line_delay=self.scanline_delay)
        print_scanline_block(battlefield_text, line_delay=self.scanline_delay)

        self.needs_turn_prompt = self.active_turn == self.assigned_id

    def handle_state_update(self, payload: dict[str, Any]) -> None:
        """Process `STATE_UPDATE`, render trajectory/craters, and update turn state.

        Args:
            payload: `STATE_UPDATE` payload dictionary.
        """
        pre_shot_tanks = copy.deepcopy(self.tanks)
        pre_shot_craters = copy.deepcopy(self.craters)

        self.current_round = int(payload.get("round", self.current_round))
        self.tanks = dict(payload.get("tanks", self.tanks))
        self.craters = list(payload.get("craters") or self.craters)
        self.active_turn = str(payload.get("next_turn", self.active_turn))
        self.time_remaining = int(
            payload.get("time_remaining", TURN_DURATION_SECONDS)
        )

        action = str(payload.get("action", ""))
        actor_id = str(payload.get("actor_id", ""))
        if action == "PLAYER_DISCONNECTED":
            self.last_fire_rendered_in_place = False
            print()
            print(
                format_action_summary(
                    payload,
                    use_color=self.use_color,
                    player_names=self.get_player_names_map(),
                ),
                flush=True,
            )
            self.needs_turn_prompt = False
            return

        if action == "MOVE" and actor_id == self.assigned_id:
            self.move_used = True
        else:
            self.move_used = False

        any_tank_eliminated = any(
            int(tank.get("hp", 0)) <= 0 for tank in self.tanks.values()
        )
        self.needs_turn_prompt = (
            self.active_turn == self.assigned_id and not any_tank_eliminated
        )

        move_path = payload.get("move_path")
        trajectory = payload.get("trajectory")
        impact = payload.get("impact")

        # When the server sends the clean TURN_START state update without
        # trajectory for the next player's turn, clear the old trajectory in
        # place on interactive terminals without duplicating the board log.
        if action == "TURN_START":
            if (
                self.use_color
                and self.scanline_delay > 0.0
                and self.last_fire_rendered_in_place
            ):
                hud_text = format_hud_header(
                    assigned_id=self.assigned_id,
                    opponent_nickname=self.opponent_nickname,
                    current_round=self.current_round,
                    tanks=self.tanks,
                    active_turn=self.active_turn,
                    time_remaining=self.time_remaining,
                    use_color=self.use_color,
                    player_nickname=self.nickname,
                )
                battlefield_text = render_battlefield_ascii(
                    tanks=self.tanks,
                    craters=self.craters,
                    trajectory=None,
                    impact=None,
                    move_path=None,
                    use_color=self.use_color,
                )
                redraw_hud_and_board_in_place(hud_text, battlefield_text)
            self.last_fire_rendered_in_place = False
            return

        if (
            action == "FIRE"
            and self.use_color
            and self.scanline_delay > 0.0
            and trajectory
        ):
            animate_fire_trajectory_in_place(
                payload=payload,
                assigned_id=self.assigned_id,
                player_nickname=self.nickname,
                opponent_nickname=self.opponent_nickname,
                pre_shot_tanks=pre_shot_tanks,
                pre_shot_craters=pre_shot_craters,
                post_shot_tanks=self.tanks,
                post_shot_craters=self.craters,
                current_round=self.current_round,
                active_turn=self.active_turn,
                time_remaining=self.time_remaining,
                player_names=self.get_player_names_map(),
                use_color=self.use_color,
            )
            self.last_fire_rendered_in_place = True
            return

        self.last_fire_rendered_in_place = False
        print()
        print(
            format_action_summary(
                payload,
                use_color=self.use_color,
                player_names=self.get_player_names_map(),
            ),
            flush=True,
        )
        if self.section_pause > 0.0:
            time.sleep(self.section_pause)

        hud_text = format_hud_header(
            assigned_id=self.assigned_id,
            opponent_nickname=self.opponent_nickname,
            current_round=self.current_round,
            tanks=self.tanks,
            active_turn=self.active_turn,
            time_remaining=self.time_remaining,
            use_color=self.use_color,
            player_nickname=self.nickname,
        )
        battlefield_text = render_battlefield_ascii(
            tanks=self.tanks,
            craters=self.craters,
            trajectory=trajectory,
            impact=impact,
            move_path=move_path,
            use_color=self.use_color,
        )
        print_scanline_block(hud_text, line_delay=self.scanline_delay)
        print_scanline_block(battlefield_text, line_delay=self.scanline_delay)

    def handle_error_frame(self, payload: dict[str, Any]) -> None:
        """Process a structured `ERROR` frame from the server.

        Args:
            payload: `ERROR` payload dictionary (`error_code`, `detail`,
                `retry_allowed`).
        """
        error_code = str(payload.get("error_code", "UNKNOWN_ERROR"))
        detail = str(payload.get("detail", "Server rejected the request."))
        retry_allowed = bool(payload.get("retry_allowed", False))

        print(f"[SERVER ERROR] ({error_code}): {detail}")

        if error_code == "LOBBY_FULL":
            self.is_running = False
            self.needs_turn_prompt = False
            return

        if retry_allowed and self.active_turn == self.assigned_id:
            self.needs_turn_prompt = True

    def handle_game_over(self, payload: dict[str, Any]) -> None:
        """Process `GAME_OVER`, render the final match summary banner, and exit.

        Args:
            payload: `GAME_OVER` payload dictionary.
        """
        self.needs_turn_prompt = False
        print()
        print_scanline_block(
            format_game_over_banner(
                payload,
                self.assigned_id,
                use_color=self.use_color,
                player_names=self.get_player_names_map(),
            ),
            line_delay=self.scanline_delay,
        )
        self.is_running = False

    def prompt_and_send_turn_action(self) -> None:
        """Prompt the active player for a turn command and transmit the frame.

        Displays aim guidance and accepted commands. Before transmitting a user's
        typed command, checks whether the server socket became readable while
        waiting at `input()` (for example, if the 60-second turn timer expired
        and the server broadcast a `TIMEOUT` `STATE_UPDATE` or `GAME_OVER`).
        """
        if not self.is_running or self.sock is None:
            return

        print(
            "\n[AIM GUIDE] 10-85 deg aims forward toward your opponent "
            "(45 deg = max distance | 90 deg = straight up)"
        )
        if not self.move_used:
            print(
                "[COMMANDS] MOVE LEFT <1-5> | MOVE RIGHT <1-5> | "
                "FIRE <angle 0-180> <power 1-100> | FORFEIT | QUIT"
            )
        else:
            print(
                "[COMMANDS] (MOVE already used this turn) "
                "FIRE <angle 0-180> <power 1-100> | FORFEIT | QUIT"
            )

        while self.is_running and self.sock is not None:
            # If the server sent a frame (e.g. TIMEOUT or GAME_OVER) before or
            # during input, return to the event loop immediately to process it.
            if self._has_unread_socket_data():
                return

            if self.scripted_commands:
                raw_command = self.scripted_commands.pop(0)
                print(f"[{self.nickname} Command] > {raw_command}")
            else:
                try:
                    raw_command = input(f"[{self.nickname} Command] > ")
                except EOFError:
                    raw_command = "QUIT"

            # Re-check immediately after blocking input() in case the 60-second
            # turn timer expired on the server while the user was typing.
            if self._has_unread_socket_data():
                print(
                    "[NOTICE] Server state updated while waiting for input; "
                    "refreshing battlefield..."
                )
                return

            outbound_frame, error_message, is_disconnect = (
                parse_turn_command_input(
                    raw_command=raw_command,
                    player_id=self.assigned_id,
                )
            )
            if error_message is not None or outbound_frame is None:
                print(f"[INPUT ERROR] {error_message}")
                if self.scripted_commands:
                    continue
                continue

            if not send_frame_safely(self.sock, outbound_frame):
                print("[ERROR] Connection lost while sending command.")
                self.is_running = False
                return

            if is_disconnect:
                print(
                    f"[DISCONNECT] Sent {outbound_frame['payload']['reason']} "
                    "notification to server."
                )
                self.is_running = False
            return

    def close_connection(self) -> None:
        """Unregister and close the client TCP socket and selector cleanly."""
        self.is_running = False
        self.needs_turn_prompt = False
        if self.sock is not None:
            try:
                self.selector.unregister(self.sock)
            except (KeyError, ValueError, OSError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            self.selector.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for launching the Tankscii client application.

    Args:
        argv: Optional command-line argument list; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code (`0` on normal match completion).
    """
    args = parse_client_arguments(argv)
    client = TanksciiClient(
        hostname=args.hostname,
        port=args.port,
        nickname=args.nickname,
        use_tls=args.tls,
        tls_certfile=args.cert if args.tls else None,
    )
    client.connect_to_server()
    client.run_client_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
