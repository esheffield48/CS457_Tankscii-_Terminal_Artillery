"""Authoritative Tankscii : Terminal Artillery TCP game server.

Implements the single-threaded, non-blocking `selectors.DefaultSelector` event
loop and the 8-state Finite State Machine (`INIT` -> `WAITING_FOR_PLAYERS` ->
`GAME_START` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` ->
`GAME_OVER` -> `CLEANUP`) specified in `docs/fsm_specification.md` and
`docs/concurrency_design.md`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import copy
import logging
import math
import random
import selectors
import socket
import sys
import time
from typing import Any

from tankscii import (
    DEFAULT_P1_NICKNAME,
    DEFAULT_P2_NICKNAME,
    MAX_PLAYERS,
    MAX_PORT,
    MAX_ROUNDS,
    MIN_PORT,
    RECONNECT_GRACE_SECONDS,
    TURN_DURATION_SECONDS,
)
from tankscii.framing import (
    ClientSession,
    ConnectionClosedError,
    FrameDecodeError,
    decode_json_line,
    receive_and_extract_raw_lines,
    send_frame_safely,
)
from tankscii.physics import (
    apply_tank_move,
    build_board_matrix,
    create_initial_tanks,
    create_initial_terrain,
    simulate_ballistic_shot,
)
from tankscii.protocol import (
    build_error_message,
    build_game_over_message,
    build_game_start_message,
    build_lobby_wait_message,
    build_state_resume_message,
    build_state_update_message,
    validate_connect_payload,
    validate_disconnect_payload,
    validate_fire_payload,
    validate_frame_envelope,
    validate_move_payload,
)


def parse_server_arguments(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse and validate command-line arguments for `server.py`.

    Requires `-p` / `--port` in the unprivileged port range `1024..65535` and
    provides `-h` / `--help` usage documentation.

    Args:
        argv: Optional list of CLI argument strings (defaults to `sys.argv[1:]`).

    Returns:
        Parsed `argparse.Namespace` with validated integer `port`.
    """
    parser = argparse.ArgumentParser(
        prog="server.py",
        description=(
            "Tankscii : Terminal Artillery authoritative TCP game server. "
            "Binds to 0.0.0.0 on the specified port (1024-65535)."
        ),
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        required=True,
        help=f"Listening TCP port ({MIN_PORT}-{MAX_PORT}).",
    )

    parsed_args = parser.parse_args(argv)
    if not (MIN_PORT <= parsed_args.port <= MAX_PORT):
        parser.error(f"Port must be between {MIN_PORT} and {MAX_PORT}.")

    return parsed_args


def configure_server_logging(
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return the standard library logger for the Tankscii server.

    Args:
        level: Logging verbosity level (defaults to `logging.INFO`).

    Returns:
        Configured `logging.Logger` instance (`"tankscii.server"`).
    """
    logger = logging.getLogger("tankscii.server")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="%(asctime)s [SERVER] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class TanksciiServer:
    """Authoritative single-threaded non-blocking Tankscii TCP server.

    Multiplexes the listening socket and all client sockets on a single
    `selectors.DefaultSelector` event loop without spawning per-client threads,
    eliminating shared-memory race conditions across match state transitions.
    """

    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        selector: selectors.BaseSelector | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        first_turn_choice: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the authoritative game server in the `INIT` FSM state.

        Args:
            port: Validated listening TCP port (`1024..65535`).
            host: Interface address to bind (defaults to `"0.0.0.0"`).
            selector: Optional custom selector instance for testing.
            clock_fn: Monotonic clock callable returning seconds as `float`.
            first_turn_choice: Optional deterministic starting player ID
                (`"Player_1"` or `"Player_2"`) for testing.
            logger: Optional `logging.Logger` instance for server diagnostics.
        """
        self.host: str = host
        self.port: int = port
        self.selector: selectors.BaseSelector = (
            selector if selector is not None else selectors.DefaultSelector()
        )
        self.clock_fn: Callable[[], float] = clock_fn
        self.first_turn_choice: str | None = first_turn_choice
        self.logger: logging.Logger = (
            logger
            if logger is not None
            else logging.getLogger("tankscii.server")
        )
        self.listen_sock: socket.socket | None = None

        # Canonical shared state variables defined in docs/concurrency_design.md
        self.fsm_state: str = "INIT"
        self.clients: dict[str, ClientSession] = {}
        self.board_matrix: list[list[str]] = []
        self.terrain_heights: list[int] = []
        self.craters: list[dict[str, int]] = []
        self.tanks: dict[str, dict[str, int]] = {}
        self.active_turn: str = "Player_1"
        self.turn_deadline: float = 0.0
        self.move_used: bool = False
        self.round: int = 1
        self.final_scores: dict[str, int] = {
            "Player_1_hp": 100,
            "Player_2_hp": 100,
            "rounds": 0,
        }

        # Accepted TCP connections awaiting their application-layer CONNECT frame
        self.unassigned_sessions: dict[int, ClientSession] = {}
        self.turns_completed_in_round: int = 0
        self.paused_turn_remaining: float = float(TURN_DURATION_SECONDS)

    def initialize_listening_socket(self) -> None:
        """Create, bind (`0.0.0.0`), and register the non-blocking listen socket.

        Transitions `fsm_state` from `"INIT"` to `"WAITING_FOR_PLAYERS"`.
        """
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Allow immediate port rebinding on server restart (avoids TIME_WAIT bind errors).
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind((self.host, self.port))
        listen_sock.listen(5)
        listen_sock.setblocking(False)

        # Register with data=None to distinguish new connection events from client I/O.
        self.selector.register(listen_sock, selectors.EVENT_READ, data=None)
        self.listen_sock = listen_sock
        self.fsm_state = "WAITING_FOR_PLAYERS"
        self.logger.info(
            "Listening socket bound to %s:%d; FSM transitioned INIT -> %s.",
            self.host,
            self.port,
            self.fsm_state,
        )

    def accept_client_connection(self) -> ClientSession | None:
        """Accept an incoming TCP connection on `listen_sock` and register it.

        Returns:
            The registered `ClientSession`, or `None` if rejected or unavailable.
        """
        if self.listen_sock is None:
            return None

        try:
            client_sock, client_addr = self.listen_sock.accept()
        except (BlockingIOError, OSError):
            return None

        return self.register_accepted_socket(client_sock, client_addr)

    def register_accepted_socket(
        self,
        client_sock: socket.socket,
        client_addr: tuple[str, int] = ("127.0.0.1", 0),
    ) -> ClientSession | None:
        """Configure a newly accepted client socket and enforce lobby capacity.

        If 2 players are already registered (`len(self.clients) >= MAX_PLAYERS`)
        or a match is currently active (`self.fsm_state != "WAITING_FOR_PLAYERS"`),
        immediately sends `ERROR ("LOBBY_FULL", retry_allowed=False)` and closes
        the socket without perturbing the active match.

        Args:
            client_sock: Accepted TCP socket.
            client_addr: Remote `(host, port)` address tuple.

        Returns:
            Registered `ClientSession` when accepted into the lobby, or `None`
            when rejected due to full capacity.
        """
        client_sock.setblocking(False)

        has_disconnected_slot = self._has_temporarily_disconnected_player()
        if (
            len(self.clients) >= MAX_PLAYERS
            or self.fsm_state != "WAITING_FOR_PLAYERS"
        ) and not has_disconnected_slot:
            self.logger.warning(
                "Rejected connection from %s:%d with ERROR(LOBBY_FULL) "
                "(active_players=%d/%d, state=%s).",
                client_addr[0],
                client_addr[1],
                len(self.clients),
                MAX_PLAYERS,
                self.fsm_state,
            )
            lobby_full_error = build_error_message(
                error_code="LOBBY_FULL",
                detail="Server match in progress (2/2 players active).",
                retry_allowed=False,
            )
            send_frame_safely(client_sock, lobby_full_error)
            self._close_socket_safely(client_sock)
            return None

        session = ClientSession(
            sock=client_sock,
            addr=client_addr,
            stream_buffer=bytearray(),
        )
        self.unassigned_sessions[id(session)] = session
        self.selector.register(client_sock, selectors.EVENT_READ, data=session)
        self.logger.info(
            "Accepted TCP connection from %s:%d (awaiting CONNECT handshake).",
            client_addr[0],
            client_addr[1],
        )
        return session

    # Alias supporting external test harnesses that register socketpair endpoints.
    register_external_client_socket = register_accepted_socket

    def compute_select_timeout(self) -> float | None:
        """Compute the dynamic `selectors.select()` timeout for the current state.

        Returns:
            `None` in `"WAITING_FOR_PLAYERS"` (blocks indefinitely until socket I/O),
            the remaining reconnection grace window when a player is temporarily
            disconnected, or `max(0.0, turn_deadline - clock_fn())` in `"PLAYER_TURN"`.
        """
        if self._has_temporarily_disconnected_player():
            grace_deadlines = [
                c.disconnect_deadline
                for c in self.clients.values()
                if c.connection_status == "TEMPORARILY_DISCONNECTED"
                and c.disconnect_deadline is not None
            ]
            if grace_deadlines:
                return max(0.0, min(grace_deadlines) - self.clock_fn())
        if self.fsm_state == "PLAYER_TURN":
            return max(0.0, self.turn_deadline - self.clock_fn())
        return None

    def run_event_loop_once(
        self,
        timeout_override: float | None = None,
    ) -> None:
        """Execute a single deterministic iteration of the `selectors` event loop.

        Readable socket events returned by `selector.select()` are always
        serviced BEFORE checking `clock_fn() >= turn_deadline`. This ordering
        resolves Race 1 (`FIRE` packet vs. 60-second turn timer expiration) in
        favor of an in-flight command that arrived at the socket buffer prior
        to selector wakeup.

        Args:
            timeout_override: Optional explicit timeout in seconds for testing.
        """
        select_timeout = (
            timeout_override
            if timeout_override is not None
            else self.compute_select_timeout()
        )
        # Guard against calling select.select([], [], []) on Windows when no sockets
        # are registered (e.g., in headless unit tests without a listening socket).
        ready_events = (
            self.selector.select(timeout=select_timeout)
            if self.selector.get_map()
            else []
        )

        # 1. Service all ready socket I/O events first.
        for key, _ in ready_events:
            if key.data is None:
                self.accept_client_connection()
            else:
                session = key.data
                if isinstance(session, ClientSession):
                    self.service_client_session(session)

        # 2. Check turn timer expiration only after all readable sockets are processed.
        self.check_turn_timeout()

    def run_forever(self) -> None:
        """Initialize the listening socket and run the main event loop until interrupted."""
        if self.listen_sock is None:
            self.initialize_listening_socket()

        try:
            while True:
                computed_timeout = self.compute_select_timeout()
                # Cap blocking interval at 0.5s so Windows delivers Ctrl+C
                # (KeyboardInterrupt) promptly even when idle in the lobby.
                effective_timeout = (
                    0.5
                    if computed_timeout is None
                    else min(computed_timeout, 0.5)
                )
                self.run_event_loop_once(timeout_override=effective_timeout)
        except KeyboardInterrupt:
            self.logger.info(
                "Received KeyboardInterrupt (Ctrl+C); shutting down server cleanly."
            )
        finally:
            self.shutdown_server()

    def service_client_session(self, session: ClientSession) -> None:
        """Read available bytes from a client socket and process complete frames.

        Uses `receive_and_extract_raw_lines` to accumulate incoming bytes into
        `session.stream_buffer` and partition on `b"\\n"`, handling arbitrary TCP
        fragmentation and coalescing. Immediately traps 0-byte EOF and socket
        errors via `ConnectionClosedError`.

        Args:
            session: The readable `ClientSession` triggered by `selector.select()`.
        """
        try:
            raw_lines = receive_and_extract_raw_lines(
                session.sock, session.stream_buffer
            )
        except (
            ConnectionClosedError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ):
            self.handle_unexpected_disconnect(session)
            return

        for raw_line in raw_lines:
            # Stop processing coalesced frames if a prior frame closed this session.
            if not self._is_session_registered(session):
                break

            try:
                frame = decode_json_line(raw_line)
            except FrameDecodeError as exc:
                retry_allowed = (
                    self.fsm_state == "PLAYER_TURN"
                    and session.player_id == self.active_turn
                )
                self.logger.warning(
                    "Malformed frame from %s: %s (retry_allowed=%s).",
                    session.player_id or f"{session.addr[0]}:{session.addr[1]}",
                    exc,
                    retry_allowed,
                )
                error_msg = build_error_message(
                    error_code="MALFORMED_FRAME",
                    detail=str(exc),
                    retry_allowed=retry_allowed,
                )
                send_frame_safely(session.sock, error_msg)
                continue

            is_valid_env, env_detail = validate_frame_envelope(frame)
            if not is_valid_env:
                retry_allowed = (
                    self.fsm_state == "PLAYER_TURN"
                    and session.player_id == self.active_turn
                )
                self.logger.warning(
                    "Invalid frame envelope from %s: %s (retry_allowed=%s).",
                    session.player_id or f"{session.addr[0]}:{session.addr[1]}",
                    env_detail,
                    retry_allowed,
                )
                error_msg = build_error_message(
                    error_code="MALFORMED_FRAME",
                    detail=env_detail,
                    retry_allowed=retry_allowed,
                )
                send_frame_safely(session.sock, error_msg)
                continue

            if (
                session.player_id is not None
                and self.clients.get(session.player_id) is not None
            ):
                session = self.clients[session.player_id]

            self.dispatch_client_frame(session, frame)

    def dispatch_client_frame(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Route a validated message envelope to its state-specific handler.

        Args:
            session: Sending client session.
            frame: Validated 4-key message envelope dictionary.
        """
        msg_type: str = frame["msg_type"]

        if msg_type == "CONNECT":
            self.handle_connect_frame(session, frame)
            return

        if msg_type == "DISCONNECT":
            self.handle_disconnect_frame(session, frame)
            return

        # Any other command requires an assigned player slot in an active match.
        if session.player_id is None:
            if (
                len(self.clients) >= MAX_PLAYERS
                or self.fsm_state != "WAITING_FOR_PLAYERS"
            ) and not self._has_temporarily_disconnected_player():
                lobby_full_error = build_error_message(
                    error_code="LOBBY_FULL",
                    detail="Server match in progress (2/2 players active).",
                    retry_allowed=False,
                )
                send_frame_safely(session.sock, lobby_full_error)
            else:
                unregistered_error = build_error_message(
                    error_code="MALFORMED_FRAME",
                    detail="Client must complete CONNECT handshake first.",
                    retry_allowed=False,
                )
                send_frame_safely(session.sock, unregistered_error)
            self._unregister_and_close_session(session)
            return

        if self.fsm_state != "PLAYER_TURN":
            out_of_turn_error = build_error_message(
                error_code="OUT_OF_TURN",
                detail="Action not permitted outside PLAYER_TURN state.",
                retry_allowed=False,
            )
            send_frame_safely(session.sock, out_of_turn_error)
            return

        if msg_type in ("MOVE", "FIRE"):
            if self._has_temporarily_disconnected_player():
                paused_error = build_error_message(
                    error_code="OUT_OF_TURN",
                    detail=(
                        "Match paused waiting for opponent to reconnect "
                        "(60s grace period)."
                    ),
                    retry_allowed=True,
                )
                send_frame_safely(session.sock, paused_error)
                return

            # Enforce turn ownership before touching any shared match state or clock.
            sender_id = frame["player_id"]
            if (
                session.player_id != self.active_turn
                or sender_id != self.active_turn
            ):
                self.logger.warning(
                    "Out-of-turn %s attempt by %s while active_turn=%s.",
                    msg_type,
                    session.player_id,
                    self.active_turn,
                )
                out_of_turn_error = build_error_message(
                    error_code="OUT_OF_TURN",
                    detail=f"{self.active_turn}'s turn.",
                    retry_allowed=False,
                )
                send_frame_safely(session.sock, out_of_turn_error)
                return

            if msg_type == "MOVE":
                self.handle_move_frame(session, frame)
            else:
                self.handle_fire_frame(session, frame)
            return

        # Reject server-only message types sent by a client.
        unexpected_type_error = build_error_message(
            error_code="MALFORMED_FRAME",
            detail=f"Unsupported client command msg_type: {msg_type}.",
            retry_allowed=(session.player_id == self.active_turn),
        )
        send_frame_safely(session.sock, unexpected_type_error)

    def handle_connect_frame(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Process a `CONNECT` handshake frame for initial lobby join or reconnection.

        In `WAITING_FOR_PLAYERS`, assigns the first client to `"Player_1"`
        (replying with `LOBBY_WAIT`) and the second client to `"Player_2"`
        (triggering `start_match()`). During an active match when a player slot
        is `TEMPORARILY_DISCONNECTED`, validates the returning player's identity,
        rebinds their new TCP socket to the preserved `ClientSession`, and
        transmits `STATE_RESUME`.

        Args:
            session: Connecting client session.
            frame: Validated `CONNECT` frame envelope.
        """
        if session.player_id is not None:
            duplicate_error = build_error_message(
                error_code="INVALID_PARAMS",
                detail="Session is already registered in the lobby.",
                retry_allowed=False,
            )
            send_frame_safely(session.sock, duplicate_error)
            return

        has_disconnected_slot = self._has_temporarily_disconnected_player()
        if (
            len(self.clients) >= MAX_PLAYERS
            or self.fsm_state != "WAITING_FOR_PLAYERS"
        ) and not has_disconnected_slot:
            lobby_full_error = build_error_message(
                error_code="LOBBY_FULL",
                detail="Server match in progress (2/2 players active).",
                retry_allowed=False,
            )
            send_frame_safely(session.sock, lobby_full_error)
            self._unregister_and_close_session(session)
            return

        payload = frame["payload"]
        is_valid, error_code, detail, retry_allowed = validate_connect_payload(
            payload
        )
        if not is_valid:
            error_msg = build_error_message(
                error_code=error_code,
                detail=detail,
                retry_allowed=retry_allowed,
            )
            send_frame_safely(session.sock, error_msg)
            self._unregister_and_close_session(session)
            return

        cleaned_nickname = payload["nickname"].strip()

        if has_disconnected_slot and self.fsm_state != "WAITING_FOR_PLAYERS":
            self._handle_reconnect_handshake(session, cleaned_nickname)
            return

        self.unassigned_sessions.pop(id(session), None)

        if "Player_1" not in self.clients:
            if cleaned_nickname in ("DEFAULT", "Commander"):
                cleaned_nickname = DEFAULT_P1_NICKNAME
            session.player_id = "Player_1"
            session.nickname = cleaned_nickname
            self.clients["Player_1"] = session
            self.logger.info(
                "Registered Player_1 ('%s', v%s) from %s:%d; sending LOBBY_WAIT.",
                cleaned_nickname,
                payload["client_version"],
                session.addr[0],
                session.addr[1],
            )

            lobby_wait_msg = build_lobby_wait_message(
                assigned_id="Player_1",
                message="Waiting for opponent...",
            )
            if not send_frame_safely(session.sock, lobby_wait_msg):
                self.handle_unexpected_disconnect(session)
            return

        if cleaned_nickname in ("DEFAULT", "Commander"):
            cleaned_nickname = DEFAULT_P2_NICKNAME
        session.player_id = "Player_2"
        session.nickname = cleaned_nickname
        self.clients["Player_2"] = session
        self.logger.info(
            "Registered Player_2 ('%s', v%s) from %s:%d; lobby full (2/2).",
            cleaned_nickname,
            payload["client_version"],
            session.addr[0],
            session.addr[1],
        )
        self.start_match()

    def _handle_reconnect_handshake(
        self,
        session: ClientSession,
        cleaned_nickname: str,
    ) -> None:
        """Match a reconnecting client to a `TEMPORARILY_DISCONNECTED` slot and resume."""
        disconnected_sessions = [
            c
            for c in self.clients.values()
            if c.connection_status == "TEMPORARILY_DISCONNECTED"
        ]
        matched_session: ClientSession | None = None
        for candidate in disconnected_sessions:
            if (
                cleaned_nickname.lower() == candidate.nickname.lower()
                or (
                    cleaned_nickname in ("Player_1", "Player_2")
                    and cleaned_nickname == candidate.player_id
                )
            ):
                matched_session = candidate
                break

        if matched_session is None and cleaned_nickname in (
            "DEFAULT",
            "Commander",
            DEFAULT_P1_NICKNAME,
            DEFAULT_P2_NICKNAME,
        ):
            matched_session = disconnected_sessions[0]

        if matched_session is None:
            self.logger.warning(
                "Rejected reconnect attempt with unmatched nickname '%s' from %s:%d.",
                cleaned_nickname,
                session.addr[0],
                session.addr[1],
            )
            lobby_full_error = build_error_message(
                error_code="LOBBY_FULL",
                detail=(
                    "Match in progress; reconnect using your registered "
                    "player nickname/ID."
                ),
                retry_allowed=False,
            )
            send_frame_safely(session.sock, lobby_full_error)
            self._unregister_and_close_session(session)
            return

        self.unassigned_sessions.pop(id(session), None)
        matched_session.sock = session.sock
        matched_session.addr = session.addr
        matched_session.stream_buffer = session.stream_buffer
        matched_session.connection_status = "CONNECTED"
        matched_session.disconnect_deadline = None
        session.player_id = matched_session.player_id
        session.nickname = matched_session.nickname

        try:
            self.selector.modify(
                session.sock, selectors.EVENT_READ, data=matched_session
            )
        except (KeyError, ValueError, OSError):
            try:
                self.selector.register(
                    session.sock, selectors.EVENT_READ, data=matched_session
                )
            except (KeyError, ValueError, OSError):
                pass

        self.logger.info(
            "Reconnected %s ('%s') from %s:%d; restoring session state.",
            matched_session.player_id,
            matched_session.nickname,
            matched_session.addr[0],
            matched_session.addr[1],
        )

        remaining_seconds = max(
            1,
            min(
                TURN_DURATION_SECONDS,
                int(math.ceil(self.paused_turn_remaining)),
            ),
        )
        self.final_scores = {
            "Player_1_hp": self.tanks["Player_1"]["hp"],
            "Player_2_hp": self.tanks["Player_2"]["hp"],
            "rounds": self.round,
        }

        if not self._has_temporarily_disconnected_player():
            self.turn_deadline = self.clock_fn() + self.paused_turn_remaining
            p1_session = self.clients["Player_1"]
            p2_session = self.clients["Player_2"]
            resume_text = (
                f"{matched_session.nickname} reconnected — match resumed!"
            )
            resume_p1 = build_state_resume_message(
                assigned_id="Player_1",
                opponent_nickname=p2_session.nickname,
                round_num=self.round,
                tanks=copy.deepcopy(self.tanks),
                craters=copy.deepcopy(self.craters),
                active_turn=self.active_turn,
                time_remaining=remaining_seconds,
                move_used=self.move_used,
                scores=copy.deepcopy(self.final_scores),
                message=resume_text,
            )
            resume_p2 = build_state_resume_message(
                assigned_id="Player_2",
                opponent_nickname=p1_session.nickname,
                round_num=self.round,
                tanks=copy.deepcopy(self.tanks),
                craters=copy.deepcopy(self.craters),
                active_turn=self.active_turn,
                time_remaining=remaining_seconds,
                move_used=self.move_used,
                scores=copy.deepcopy(self.final_scores),
                message=resume_text,
            )
            send_frame_safely(p1_session.sock, resume_p1)
            send_frame_safely(p2_session.sock, resume_p2)
        else:
            opponent_id = (
                "Player_2"
                if matched_session.player_id == "Player_1"
                else "Player_1"
            )
            opponent_session = self.clients.get(opponent_id)
            opponent_nickname = (
                opponent_session.nickname if opponent_session else opponent_id
            )
            partial_resume = build_state_resume_message(
                assigned_id=str(matched_session.player_id),
                opponent_nickname=opponent_nickname,
                round_num=self.round,
                tanks=copy.deepcopy(self.tanks),
                craters=copy.deepcopy(self.craters),
                active_turn=self.active_turn,
                time_remaining=remaining_seconds,
                move_used=self.move_used,
                scores=copy.deepcopy(self.final_scores),
                message=(
                    f"{matched_session.nickname} reconnected — waiting for "
                    "opponent to reconnect..."
                ),
            )
            send_frame_safely(matched_session.sock, partial_resume)

    def start_match(self) -> None:
        """Initialize match state in `GAME_START` and transition to `PLAYER_TURN`.

        Generates the `80x24` terrain and board matrix, spawns `Player_1` at
        `x=10` and `Player_2` at `x=70` with `100 HP`, selects `first_turn`,
        sends individualized `GAME_START` frames to both players, and starts
        the 60-second turn timer.
        """
        self.fsm_state = "GAME_START"

        # Reject any extra unassigned sockets still lingering in the lobby.
        for lingering_session in list(self.unassigned_sessions.values()):
            lobby_full_error = build_error_message(
                error_code="LOBBY_FULL",
                detail="Server match in progress (2/2 players active).",
                retry_allowed=False,
            )
            send_frame_safely(lingering_session.sock, lobby_full_error)
            self._unregister_and_close_session(lingering_session)

        self.terrain_heights = create_initial_terrain()
        self.craters = []
        self.tanks = create_initial_tanks(self.terrain_heights)
        self.board_matrix = build_board_matrix(
            self.terrain_heights, self.tanks, self.craters
        )

        if self.first_turn_choice in ("Player_1", "Player_2"):
            first_turn = self.first_turn_choice
        else:
            first_turn = random.choice(["Player_1", "Player_2"])

        self.active_turn = first_turn
        self.round = 1
        self.turns_completed_in_round = 0
        self.move_used = False
        self.turn_deadline = self.clock_fn() + float(TURN_DURATION_SECONDS)
        self.final_scores = {
            "Player_1_hp": self.tanks["Player_1"]["hp"],
            "Player_2_hp": self.tanks["Player_2"]["hp"],
            "rounds": 0,
        }

        player_1_session = self.clients["Player_1"]
        player_2_session = self.clients["Player_2"]

        game_start_p1 = build_game_start_message(
            assigned_id="Player_1",
            opponent_nickname=player_2_session.nickname,
            tanks=copy.deepcopy(self.tanks),
            first_turn=self.active_turn,
            turn_duration=TURN_DURATION_SECONDS,
        )
        game_start_p2 = build_game_start_message(
            assigned_id="Player_2",
            opponent_nickname=player_1_session.nickname,
            tanks=copy.deepcopy(self.tanks),
            first_turn=self.active_turn,
            turn_duration=TURN_DURATION_SECONDS,
        )

        disconnected_ids: list[str] = []
        if not send_frame_safely(player_1_session.sock, game_start_p1):
            disconnected_ids.append("Player_1")
        if not send_frame_safely(player_2_session.sock, game_start_p2):
            disconnected_ids.append("Player_2")

        self.fsm_state = "PLAYER_TURN"
        self.logger.info(
            "FSM transitioned WAITING_FOR_PLAYERS -> GAME_START -> PLAYER_TURN | "
            "Round 1/%d | First turn: %s | Player_1 ('%s') vs Player_2 ('%s').",
            MAX_ROUNDS,
            self.active_turn,
            player_1_session.nickname,
            player_2_session.nickname,
        )

        if disconnected_ids:
            self._handle_broadcast_disconnections(disconnected_ids)

    def handle_move_frame(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Validate and execute a tank `MOVE` command during `PLAYER_TURN`.

        Transitions `PLAYER_TURN` -> `EVALUATE_MOVE`, updates the acting tank's
        position on `board_matrix`, sets `move_used = True`, broadcasts
        `STATE_UPDATE (action="MOVE")`, and returns to `PLAYER_TURN` with the
        remaining turn timer intact.

        Args:
            session: Active player's `ClientSession`.
            frame: Validated `MOVE` frame envelope.
        """
        payload = frame["payload"]
        actor_id = self.active_turn
        current_x = self.tanks[actor_id]["x"]

        is_valid, error_code, detail, retry_allowed = validate_move_payload(
            payload=payload,
            current_x=current_x,
            move_used=self.move_used,
        )
        if not is_valid:
            self.logger.warning(
                "Invalid MOVE from %s: %s (%s).",
                actor_id,
                error_code,
                detail,
            )
            error_msg = build_error_message(
                error_code=error_code,
                detail=detail,
                retry_allowed=retry_allowed,
            )
            send_frame_safely(session.sock, error_msg)
            return

        self.fsm_state = "EVALUATE_MOVE"

        direction: str = payload["direction"]
        distance: int = payload["distance"]
        move_path = apply_tank_move(
            tanks=self.tanks,
            terrain_heights=self.terrain_heights,
            player_id=actor_id,
            direction=direction,
            distance=distance,
        )
        self.board_matrix = build_board_matrix(
            self.terrain_heights, self.tanks, self.craters
        )
        self.move_used = True

        remaining_seconds = max(
            1,
            min(
                TURN_DURATION_SECONDS,
                int(math.ceil(self.turn_deadline - self.clock_fn())),
            ),
        )
        self.logger.info(
            "[Round %d] %s executed MOVE %s %d: (%d, %d) -> (%d, %d) | "
            "time_remaining=%ds.",
            self.round,
            actor_id,
            direction,
            distance,
            move_path[0][0],
            move_path[0][1],
            move_path[-1][0],
            move_path[-1][1],
            remaining_seconds,
        )
        state_update_msg = build_state_update_message(
            round_num=self.round,
            action="MOVE",
            actor_id=actor_id,
            tanks=copy.deepcopy(self.tanks),
            move_path=move_path,
            trajectory=None,
            impact=None,
            damage_dealt=0,
            craters=copy.deepcopy(self.craters),
            next_turn=self.active_turn,
            time_remaining=remaining_seconds,
        )

        self.fsm_state = "PLAYER_TURN"
        self.broadcast_message(state_update_msg)

    def handle_fire_frame(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Validate and execute a ballistic `FIRE` command during `PLAYER_TURN`.

        Transitions `PLAYER_TURN` -> `EVALUATE_MOVE` to simulate the projectile
        trajectory, terrain cratering, and HP damage (`25 HP` direct / `10 HP`
        shrapnel), then transitions to `CHECK_WIN_DRAW` to broadcast
        `STATE_UPDATE` and evaluate victory/draw conditions.

        Args:
            session: Active player's `ClientSession`.
            frame: Validated `FIRE` frame envelope.
        """
        payload = frame["payload"]
        is_valid, error_code, detail, retry_allowed = validate_fire_payload(
            payload
        )
        if not is_valid:
            self.logger.warning(
                "Invalid FIRE from %s: %s (%s).",
                self.active_turn,
                error_code,
                detail,
            )
            error_msg = build_error_message(
                error_code=error_code,
                detail=detail,
                retry_allowed=retry_allowed,
            )
            send_frame_safely(session.sock, error_msg)
            return

        self.fsm_state = "EVALUATE_MOVE"

        actor_id = self.active_turn
        opponent_id = "Player_2" if actor_id == "Player_1" else "Player_1"
        angle: int = payload["angle"]
        power: int = payload["power"]

        shot_result = simulate_ballistic_shot(
            shooter_id=actor_id,
            angle_degrees=angle,
            power_percent=power,
            tanks=self.tanks,
            terrain_heights=self.terrain_heights,
            craters=self.craters,
        )
        self.board_matrix = build_board_matrix(
            self.terrain_heights, self.tanks, self.craters
        )
        impact_coord = shot_result["impact"]["coord"]
        self.logger.info(
            "[Round %d] %s executed FIRE (angle=%d deg, power=%d%%): "
            "impact=(%d, %d) result=%s damage=%d HP | P1 HP=%d, P2 HP=%d.",
            self.round,
            actor_id,
            angle,
            power,
            impact_coord[0],
            impact_coord[1],
            shot_result["impact"]["result"],
            shot_result["damage_dealt"],
            self.tanks["Player_1"]["hp"],
            self.tanks["Player_2"]["hp"],
        )

        self.fsm_state = "CHECK_WIN_DRAW"
        self.evaluate_win_draw_after_fire(
            actor_id=actor_id,
            opponent_id=opponent_id,
            shot_result=shot_result,
        )

    def evaluate_win_draw_after_fire(
        self,
        actor_id: str,
        opponent_id: str,
        shot_result: dict[str, Any],
    ) -> None:
        """Evaluate win/draw conditions in `CHECK_WIN_DRAW` after a `FIRE` action.

        Broadcasts the synchronous `STATE_UPDATE (action="FIRE")` frame first.
        If a tank's HP reached `0` or the 20-round limit expired, transitions to
        `GAME_OVER`, broadcasts `GAME_OVER`, and cleans up the lobby. Otherwise,
        hands off `active_turn` to `opponent_id` and returns to `PLAYER_TURN`.

        Args:
            actor_id: Player ID who fired the shot.
            opponent_id: Next player ID in turn order.
            shot_result: Dictionary returned by `simulate_ballistic_shot`.
        """
        self.turns_completed_in_round += 1
        completed_round = self.round

        player_1_hp = self.tanks["Player_1"]["hp"]
        player_2_hp = self.tanks["Player_2"]["hp"]

        winner: str | None = None
        reason: str = ""
        game_over_text: str = ""

        if player_1_hp <= 0 and player_2_hp <= 0:
            winner = "DRAW"
            reason = "HP_ELIMINATION"
            game_over_text = "Mutual destruction! Both tanks reached 0 HP."
        elif player_2_hp <= 0 < player_1_hp:
            winner = "Player_1"
            reason = "HP_ELIMINATION"
            game_over_text = "Player 1 wins by destroying Player 2's tank!"
        elif player_1_hp <= 0 < player_2_hp:
            winner = "Player_2"
            reason = "HP_ELIMINATION"
            game_over_text = "Player 2 wins by destroying Player 1's tank!"
        elif (
            self.round >= MAX_ROUNDS and self.turns_completed_in_round >= 2
        ) or self.round > MAX_ROUNDS:
            winner = "DRAW"
            reason = "ROUND_LIMIT"
            game_over_text = (
                f"Match ended in a draw after {MAX_ROUNDS} completed rounds."
            )

        state_update_msg = build_state_update_message(
            round_num=completed_round,
            action="FIRE",
            actor_id=actor_id,
            tanks=copy.deepcopy(self.tanks),
            move_path=None,
            trajectory=shot_result["trajectory"],
            impact=shot_result["impact"],
            damage_dealt=shot_result["damage_dealt"],
            craters=copy.deepcopy(self.craters),
            next_turn=opponent_id,
            time_remaining=TURN_DURATION_SECONDS,
        )

        if winner is not None:
            self.final_scores = {
                "Player_1_hp": player_1_hp,
                "Player_2_hp": player_2_hp,
                "rounds": completed_round,
            }
            # Broadcast final STATE_UPDATE before transitioning to GAME_OVER.
            self.broadcast_message(
                state_update_msg, handle_disconnects_on_failure=False
            )
            self.fsm_state = "GAME_OVER"
            self.logger.info(
                "FSM transitioned CHECK_WIN_DRAW -> GAME_OVER | Winner: %s | "
                "Reason: %s | Final Scores: P1=%d HP, P2=%d HP, Rounds=%d.",
                winner,
                reason,
                player_1_hp,
                player_2_hp,
                completed_round,
            )
            game_over_msg = build_game_over_message(
                winner=winner,
                reason=reason,
                final_scores=copy.deepcopy(self.final_scores),
                message=game_over_text,
            )
            self.broadcast_message(
                game_over_msg, handle_disconnects_on_failure=False
            )
            self.cleanup_and_reset_lobby()
            return

        # Match continues: advance round counter if both players have acted.
        if self.turns_completed_in_round >= 2:
            self.round += 1
            self.turns_completed_in_round = 0

        self.active_turn = opponent_id
        self.move_used = False
        self.turn_deadline = self.clock_fn() + float(TURN_DURATION_SECONDS)
        self.final_scores = {
            "Player_1_hp": player_1_hp,
            "Player_2_hp": player_2_hp,
            "rounds": completed_round,
        }
        self.fsm_state = "PLAYER_TURN"
        # 1. Broadcast the FIRE state update with trajectory and impact coordinates.
        self.broadcast_message(state_update_msg)

        # 2. If both clients remain connected, broadcast a clean TURN_START state
        # update without the trajectory or impact marker for the next player's turn.
        if (
            self.fsm_state == "PLAYER_TURN"
            and len(self.clients) == MAX_PLAYERS
            and not self._has_temporarily_disconnected_player()
        ):
            clean_turn_state_msg = build_state_update_message(
                round_num=self.round,
                action="TURN_START",
                actor_id=opponent_id,
                tanks=copy.deepcopy(self.tanks),
                move_path=None,
                trajectory=None,
                impact=None,
                damage_dealt=0,
                craters=copy.deepcopy(self.craters),
                next_turn=opponent_id,
                time_remaining=TURN_DURATION_SECONDS,
            )
            self.broadcast_message(clean_turn_state_msg)

    def check_reconnect_grace_timeout(self) -> bool:
        """Check whether any `TEMPORARILY_DISCONNECTED` player's 60s grace window expired.

        If `clock_fn() >= session.disconnect_deadline`, transitions to `GAME_OVER`,
        awards victory (`reason="DISCONNECT"`) to the surviving opponent, and
        resets the lobby.

        Returns:
            `True` if a reconnection grace period expired and ended the match.
        """
        for player_id, session in list(self.clients.items()):
            if (
                session.connection_status == "TEMPORARILY_DISCONNECTED"
                and session.disconnect_deadline is not None
                and self.clock_fn() >= session.disconnect_deadline
            ):
                self.logger.warning(
                    "60-second reconnection grace period expired for %s ('%s'); "
                    "awarding DISCONNECT victory to opponent.",
                    player_id,
                    session.nickname,
                )
                self._award_survivor_victory(
                    departing_id=player_id,
                    reason="DISCONNECT",
                    message=(
                        f"{player_id} disconnected from the server. "
                        "You win by forfeit!"
                    ),
                )
                return True
        return False

    def check_turn_timeout(self) -> bool:
        """Enforce the 60-second turn deadline when in `PLAYER_TURN`.

        While a player is `TEMPORARILY_DISCONNECTED`, pauses the active turn
        timer and instead checks the 60-second reconnection grace deadline.

        Returns:
            `True` if a turn or reconnection timeout occurred and was handled.
        """
        if self._has_temporarily_disconnected_player():
            return self.check_reconnect_grace_timeout()

        if self.fsm_state != "PLAYER_TURN":
            return False

        if self.clock_fn() < self.turn_deadline:
            return False

        timed_out_player = self.active_turn
        next_player = (
            "Player_2" if timed_out_player == "Player_1" else "Player_1"
        )
        self.logger.warning(
            "[Round %d] 60-second turn timer expired for %s; handing turn to %s.",
            self.round,
            timed_out_player,
            next_player,
        )

        timed_out_session = self.clients.get(timed_out_player)
        if timed_out_session is not None:
            timeout_error = build_error_message(
                error_code="TIMEOUT",
                detail="Turn timer expired (60s).",
                retry_allowed=False,
            )
            send_frame_safely(timed_out_session.sock, timeout_error)

        self.turns_completed_in_round += 1
        completed_round = self.round
        round_limit_reached = (
            self.round >= MAX_ROUNDS and self.turns_completed_in_round >= 2
        ) or self.round > MAX_ROUNDS

        state_update_msg = build_state_update_message(
            round_num=completed_round,
            action="TIMEOUT",
            actor_id=timed_out_player,
            tanks=copy.deepcopy(self.tanks),
            move_path=None,
            trajectory=None,
            impact=None,
            damage_dealt=0,
            craters=copy.deepcopy(self.craters),
            next_turn=next_player,
            time_remaining=TURN_DURATION_SECONDS,
        )

        if round_limit_reached:
            self.final_scores = {
                "Player_1_hp": self.tanks["Player_1"]["hp"],
                "Player_2_hp": self.tanks["Player_2"]["hp"],
                "rounds": completed_round,
            }
            self.broadcast_message(
                state_update_msg, handle_disconnects_on_failure=False
            )
            self.fsm_state = "GAME_OVER"
            self.logger.info(
                "FSM transitioned PLAYER_TURN -> GAME_OVER | Winner: DRAW | "
                "Reason: ROUND_LIMIT | Final Scores: P1=%d HP, P2=%d HP, Rounds=%d.",
                self.tanks["Player_1"]["hp"],
                self.tanks["Player_2"]["hp"],
                completed_round,
            )
            game_over_msg = build_game_over_message(
                winner="DRAW",
                reason="ROUND_LIMIT",
                final_scores=copy.deepcopy(self.final_scores),
                message=(
                    f"Match ended in a draw after {MAX_ROUNDS} completed rounds."
                ),
            )
            self.broadcast_message(
                game_over_msg, handle_disconnects_on_failure=False
            )
            self.cleanup_and_reset_lobby()
            return True

        if self.turns_completed_in_round >= 2:
            self.round += 1
            self.turns_completed_in_round = 0

        self.active_turn = next_player
        self.move_used = False
        self.turn_deadline = self.clock_fn() + float(TURN_DURATION_SECONDS)
        self.fsm_state = "PLAYER_TURN"
        self.broadcast_message(state_update_msg)
        return True

    def broadcast_message(
        self,
        message: dict[str, Any],
        handle_disconnects_on_failure: bool = True,
    ) -> list[str]:
        """Broadcast a frame to all connected players using snapshot iteration.

        Iterates over a static snapshot `list(self.clients.items())` so that
        an abrupt mid-broadcast socket failure (`BrokenPipeError` / `TCP RST`)
        never mutates `self.clients` during iteration. Skips any session whose
        `connection_status` is `"TEMPORARILY_DISCONNECTED"`. Failed client IDs
        are collected in `disconnected_ids` and processed after the loop completes.

        Args:
            message: Complete message envelope dictionary to broadcast.
            handle_disconnects_on_failure: Whether to trigger mid-game
                disconnect resolution if a client write fails.

        Returns:
            List of `player_id` strings whose sockets failed during `sendall()`.
        """
        disconnected_ids: list[str] = []
        for player_id, session in list(self.clients.items()):
            if session.connection_status == "TEMPORARILY_DISCONNECTED":
                continue
            if not send_frame_safely(session.sock, message):
                disconnected_ids.append(player_id)

        if disconnected_ids and handle_disconnects_on_failure:
            self._handle_broadcast_disconnections(disconnected_ids)

        return disconnected_ids

    def handle_disconnect_frame(
        self,
        session: ClientSession,
        frame: dict[str, Any],
    ) -> None:
        """Process a graceful `DISCONNECT` frame from a client.

        If received during `WAITING_FOR_PLAYERS`, removes the departing client
        and keeps the lobby open. During an active match:
        - `reason == "FORFEIT"` immediately awards victory to the opponent and
          transitions through `GAME_OVER` -> `CLEANUP` -> `WAITING_FOR_PLAYERS`.
        - `reason == "USER_QUIT"` preserves match state and places the player
          into a 60-second `TEMPORARILY_DISCONNECTED` reconnection grace window.

        Args:
            session: Departing client's `ClientSession`.
            frame: Validated `DISCONNECT` envelope.
        """
        payload = frame["payload"]
        is_valid, error_code, detail, retry_allowed = (
            validate_disconnect_payload(payload)
        )
        if not is_valid:
            error_msg = build_error_message(
                error_code=error_code,
                detail=detail,
                retry_allowed=retry_allowed,
            )
            send_frame_safely(session.sock, error_msg)
            return

        if (
            self.fsm_state == "WAITING_FOR_PLAYERS"
            or session.player_id is None
        ):
            self._remove_lobby_session(session)
            return

        departing_id = session.player_id
        reason = payload["reason"]
        self.logger.info(
            "Received graceful DISCONNECT (reason=%s) from %s.",
            reason,
            departing_id,
        )
        if (
            reason == "USER_QUIT"
            and self.fsm_state not in ("INIT", "WAITING_FOR_PLAYERS", "GAME_OVER", "CLEANUP")
            and departing_id in ("Player_1", "Player_2")
        ):
            self._enter_reconnect_grace_period(session)
            return

        self._award_survivor_victory(
            departing_id=departing_id,
            reason="FORFEIT",
            message=f"{departing_id} forfeited the match.",
        )

    def handle_unexpected_disconnect(self, session: ClientSession) -> None:
        """Handle 0-byte EOF or abrupt socket errors (`RST` / `BrokenPipeError`).

        In `WAITING_FOR_PLAYERS`, unregisters and closes the dropped socket and
        resets the lobby slot. During an active match, preserves match state
        (`board_matrix`, `tanks`, `craters`, `final_scores`, `active_turn`) and
        marks the player `TEMPORARILY_DISCONNECTED` for a 60-second reconnection
        grace period.

        Args:
            session: Disconnected `ClientSession`.
        """
        self.logger.warning(
            "Detected unexpected disconnect (0-byte EOF / socket error) from %s "
            "in state %s.",
            session.player_id or f"{session.addr[0]}:{session.addr[1]}",
            self.fsm_state,
        )
        if (
            self.fsm_state in ("INIT", "WAITING_FOR_PLAYERS")
            or session.player_id is None
        ):
            self._remove_lobby_session(session)
            return

        if self.fsm_state in ("GAME_OVER", "CLEANUP"):
            self._unregister_and_close_session(session)
            return

        if session.player_id in ("Player_1", "Player_2"):
            self._enter_reconnect_grace_period(session)
            return

        departing_id = session.player_id
        self._award_survivor_victory(
            departing_id=departing_id,
            reason="DISCONNECT",
            message=f"{departing_id} disconnected from the server. You win by forfeit!",
        )

    def _enter_reconnect_grace_period(self, session: ClientSession) -> None:
        """Close the dropped socket while holding the player's slot for 60s reconnection."""
        player_id = str(session.player_id)
        if not self._has_temporarily_disconnected_player():
            self.paused_turn_remaining = max(
                1.0, self.turn_deadline - self.clock_fn()
            )

        self._close_socket_only(session.sock)
        session.stream_buffer.clear()
        session.connection_status = "TEMPORARILY_DISCONNECTED"
        session.disconnect_deadline = self.clock_fn() + float(
            RECONNECT_GRACE_SECONDS
        )
        self.final_scores = {
            "Player_1_hp": self.tanks.get("Player_1", {}).get("hp", 100),
            "Player_2_hp": self.tanks.get("Player_2", {}).get("hp", 100),
            "rounds": self.round,
        }
        self.logger.warning(
            "%s ('%s') marked TEMPORARILY_DISCONNECTED for %ds reconnection "
            "grace period; preserving match state (round=%d, active_turn=%s).",
            player_id,
            session.nickname,
            RECONNECT_GRACE_SECONDS,
            self.round,
            self.active_turn,
        )

        opponent_id = "Player_2" if player_id == "Player_1" else "Player_1"
        opponent_session = self.clients.get(opponent_id)
        if (
            opponent_session is not None
            and opponent_session.connection_status == "CONNECTED"
        ):
            disconnect_notice = build_state_update_message(
                round_num=self.round,
                action="PLAYER_DISCONNECTED",
                actor_id=player_id,
                tanks=copy.deepcopy(self.tanks),
                move_path=None,
                trajectory=None,
                impact=None,
                damage_dealt=0,
                craters=copy.deepcopy(self.craters),
                next_turn=self.active_turn,
                time_remaining=RECONNECT_GRACE_SECONDS,
            )
            send_frame_safely(opponent_session.sock, disconnect_notice)

    def cleanup_and_reset_lobby(self) -> None:
        """Transition through `CLEANUP`, close all client sockets, and reset lobby.

        Safely unregisters and closes every socket in `self.clients` and
        `self.unassigned_sessions`, resets all match state variables, and
        transitions `self.fsm_state` back to `"WAITING_FOR_PLAYERS"` so the
        server is ready to host the next match without restarting.
        """
        self.fsm_state = "CLEANUP"

        for _, session in list(self.clients.items()):
            self._unregister_and_close_session(session)
        for _, session in list(self.unassigned_sessions.items()):
            self._unregister_and_close_session(session)

        self.clients.clear()
        self.unassigned_sessions.clear()
        self.board_matrix = []
        self.terrain_heights = []
        self.craters = []
        self.tanks = {}
        self.active_turn = "Player_1"
        self.turn_deadline = 0.0
        self.paused_turn_remaining = float(TURN_DURATION_SECONDS)
        self.move_used = False
        self.round = 1
        self.turns_completed_in_round = 0
        self.fsm_state = "WAITING_FOR_PLAYERS"
        self.logger.info(
            "FSM transitioned CLEANUP -> WAITING_FOR_PLAYERS; reclaimed client "
            "sockets and reset lobby for next match."
        )

    def shutdown_server(self) -> None:
        """Clean up all client sessions, close the listening socket, and close selector."""
        self.cleanup_and_reset_lobby()
        if self.listen_sock is not None:
            try:
                self.selector.unregister(self.listen_sock)
            except (KeyError, ValueError, OSError):
                pass
            self._close_socket_safely(self.listen_sock)
            self.listen_sock = None
        try:
            self.selector.close()
        except OSError:
            pass

    def _award_survivor_victory(
        self,
        departing_id: str,
        reason: str,
        message: str,
    ) -> None:
        """Transition to `GAME_OVER`, notify the surviving client, and clean up.

        Args:
            departing_id: `"Player_1"` or `"Player_2"` who forfeited or dropped.
            reason: `"FORFEIT"` or `"DISCONNECT"`.
            message: Human-readable summary message for `GAME_OVER`.
        """
        survivor_id = "Player_2" if departing_id == "Player_1" else "Player_1"
        player_1_hp = self.tanks.get("Player_1", {}).get("hp", 100)
        player_2_hp = self.tanks.get("Player_2", {}).get("hp", 100)
        completed_rounds = max(0, self.round - 1)

        self.final_scores = {
            "Player_1_hp": player_1_hp,
            "Player_2_hp": player_2_hp,
            "rounds": completed_rounds,
        }
        self.fsm_state = "GAME_OVER"
        self.logger.info(
            "FSM transitioned -> GAME_OVER | Winner: %s | Reason: %s | "
            "Final Scores: P1=%d HP, P2=%d HP, Rounds=%d.",
            survivor_id,
            reason,
            player_1_hp,
            player_2_hp,
            completed_rounds,
        )

        survivor_session = self.clients.get(survivor_id)
        if (
            survivor_session is not None
            and survivor_session.connection_status == "CONNECTED"
        ):
            game_over_msg = build_game_over_message(
                winner=survivor_id,
                reason=reason,
                final_scores=copy.deepcopy(self.final_scores),
                message=message,
            )
            send_frame_safely(survivor_session.sock, game_over_msg)

        self.cleanup_and_reset_lobby()

    def _handle_broadcast_disconnections(
        self,
        disconnected_ids: list[str],
    ) -> None:
        """Resolve client socket failures detected during a snapshot broadcast."""
        if self.fsm_state in ("GAME_OVER", "CLEANUP", "WAITING_FOR_PLAYERS"):
            for player_id in disconnected_ids:
                session = self.clients.get(player_id)
                if session is not None:
                    self._remove_lobby_session(session)
            return

        for player_id in disconnected_ids:
            session = self.clients.get(player_id)
            if session is not None:
                self.handle_unexpected_disconnect(session)

    def _has_temporarily_disconnected_player(self) -> bool:
        """Return True if any registered player session is `TEMPORARILY_DISCONNECTED`."""
        return any(
            session.connection_status == "TEMPORARILY_DISCONNECTED"
            for session in self.clients.values()
        )

    def _close_socket_only(self, sock: socket.socket) -> None:
        """Unregister a socket from `self.selector` and close it while keeping session state."""
        try:
            self.selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass
        self._close_socket_safely(sock)

    def _remove_lobby_session(self, session: ClientSession) -> None:
        """Unregister and close a session during `WAITING_FOR_PLAYERS`."""
        self._unregister_and_close_session(session)
        if session.player_id and self.clients.get(session.player_id) is session:
            del self.clients[session.player_id]
        self.fsm_state = "WAITING_FOR_PLAYERS"

    def _unregister_and_close_session(self, session: ClientSession) -> None:
        """Unregister a client's socket from `self.selector` and close it safely."""
        self.unassigned_sessions.pop(id(session), None)
        try:
            self.selector.unregister(session.sock)
        except (KeyError, ValueError, OSError):
            pass
        self._close_socket_safely(session.sock)

    def _is_session_registered(self, session: ClientSession) -> bool:
        """Return True if `session` is still an active lobby or match session."""
        if id(session) in self.unassigned_sessions:
            return True
        if session.player_id is not None:
            registered = self.clients.get(session.player_id)
            if registered is session or (
                registered is not None and registered.sock is session.sock
            ):
                return True
        return False

    @staticmethod
    def _close_socket_safely(sock: socket.socket) -> None:
        """Close a TCP socket while suppressing any OS-level close errors."""
        try:
            sock.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the authoritative Tankscii server."""
    args = parse_server_arguments(argv)
    logger = configure_server_logging()
    server = TanksciiServer(port=args.port, host="0.0.0.0", logger=logger)
    server.initialize_listening_socket()
    server.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
