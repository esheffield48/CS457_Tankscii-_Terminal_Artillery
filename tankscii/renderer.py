"""Pure 80x24 ASCII battlefield, trajectory, HUD, and banner terminal renderer."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from tankscii.physics import (
    BASE_TERRAIN_HEIGHT,
    BOARD_HEIGHT,
    BOARD_WIDTH,
    CRATER_RADIUS,
    build_board_matrix,
    stamp_tank_sprites,
)

MAX_ROUNDS: int = 20

DEFAULT_SCANLINE_DELAY: float = 0.02
DEFAULT_TUTORIAL_LINE_DELAY: float = 0.14
DEFAULT_SECTION_PAUSE: float = 0.25

# Standard ANSI escape codes (zero external dependencies)
ANSI_RESET: str = "\033[0m"
ANSI_PLAYER_1: str = "\033[1;96m"        # Bold Bright Cyan ([1>)
ANSI_PLAYER_2: str = "\033[1;95m"        # Bold Bright Magenta (<2])
ANSI_SURFACE_INTACT: str = "\033[32m"    # Green (=)
ANSI_CRATER_FLOOR: str = "\033[33m"      # Yellow / Amber (_)
ANSI_SUBSOIL: str = "\033[90m"           # Dim Gray (:)
ANSI_TRAJECTORY: str = "\033[1;93m"      # Bright Yellow (*)
ANSI_IMPACT: str = "\033[1;91m"          # Bold Bright Red (X)
ANSI_MOVE_TRAIL: str = "\033[36m"        # Cyan (.)

CELL_ANSI_COLORS: dict[str, str] = {
    "[": ANSI_PLAYER_1,
    "1": ANSI_PLAYER_1,
    ">": ANSI_PLAYER_1,
    "<": ANSI_PLAYER_2,
    "2": ANSI_PLAYER_2,
    "]": ANSI_PLAYER_2,
    "=": ANSI_SURFACE_INTACT,
    "_": ANSI_CRATER_FLOOR,
    ":": ANSI_SUBSOIL,
    "*": ANSI_TRAJECTORY,
    "X": ANSI_IMPACT,
    ".": ANSI_MOVE_TRAIL,
}


def enable_ansi_terminal_colors() -> bool:
    """Detect interactive TTY and enable native ANSI colors on Windows/POSIX.

    On Windows 10/11 consoles, invoking ``os.system("")`` activates Virtual
    Terminal Processing (`ENABLE_VIRTUAL_TERMINAL_PROCESSING`) using only the
    Python standard library. Returns ``False`` when ``sys.stdout`` is redirected
    to a file or pipe so plain-text logs never contain raw escape sequences.

    Returns:
        ``True`` if ``sys.stdout`` is an interactive TTY with ANSI support.
    """
    if os.environ.get("NO_COLOR"):
        return False
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not is_tty:
        return False
    if os.name == "nt":
        os.system("")
    return True


def colorize_battlefield_row(row_chars: list[str], use_color: bool) -> str:
    """Format a single 80-column battlefield row with optional ANSI colors.

    Args:
        row_chars: List of 80 single-character strings for one row.
        use_color: Whether to wrap cells in ANSI color sequences.

    Returns:
        Formatted row string (excluding outer ``|`` borders).
    """
    if not use_color:
        return "".join(row_chars)

    colored_segments: list[str] = []
    for ch in row_chars:
        ansi_code = CELL_ANSI_COLORS.get(ch)
        if ansi_code is not None:
            colored_segments.append(f"{ansi_code}{ch}{ANSI_RESET}")
        else:
            colored_segments.append(ch)
    return "".join(colored_segments)


def reconstruct_terrain_heights(
    craters: list[dict[str, int]],
    width: int = BOARD_WIDTH,
    base_height: int = BASE_TERRAIN_HEIGHT,
) -> list[int]:
    """Reconstruct the 80-column terrain elevation list from cumulative craters.

    Allows the thin client to deterministically render terrain deformations
    solely from the ``craters`` list received in ``STATE_UPDATE`` frames.

    Args:
        craters: Cumulative list of crater dicts ``{"x": int, "radius": int}``.
        width: Number of horizontal board columns (default 80).
        base_height: Initial un-cratered surface elevation (default 6).

    Returns:
        List of ``width`` integers representing column surface heights.
    """
    terrain_heights = [base_height] * width
    for crater in craters:
        impact_x = int(crater.get("x", -1))
        radius = int(crater.get("radius", CRATER_RADIUS))
        start_col = max(0, impact_x - radius)
        end_col = min(width - 1, impact_x + radius)
        for col in range(start_col, end_col + 1):
            distance_from_center = abs(col - impact_x)
            depth_drop = 2 if distance_from_center == 0 else 1
            terrain_heights[col] = max(1, terrain_heights[col] - depth_drop)
    return terrain_heights


def render_battlefield_ascii(
    tanks: dict[str, dict[str, int]],
    craters: list[dict[str, int]] | None = None,
    trajectory: list[list[int]] | None = None,
    impact: dict[str, Any] | None = None,
    move_path: list[list[int]] | None = None,
    use_color: bool = False,
) -> str:
    """Render the 80x24 ASCII battlefield with terrain, craters, arcs, and tanks.

    Row ``y = 23`` is rendered at the top of the frame (sky) and ``y = 0`` at
    the bottom of the frame (subterranean ground). Visual markers:
    - ``=``: Intact ground surface crust
    - ``_``: Stepped crater floor surface
    - ``:``: Subterranean earth beneath the surface
    - ``.``: Tank movement trail
    - ``*``: Projectile trajectory arc
    - ``X``: Projectile impact point
    - ``[1>`` and ``<2]``: Player_1 and Player_2 armored tanks

    Args:
        tanks: Dictionary mapping ``"Player_1"`` and ``"Player_2"`` to ``{x, y, hp}``.
        craters: Optional list of crater dictionaries.
        trajectory: Optional list of ``[x, y]`` projectile coordinates.
        impact: Optional dictionary ``{"coord": [x, y], "result": str}``.
        move_path: Optional list of ``[x, y]`` movement trail coordinates.
        use_color: Whether to apply ANSI color escape codes to cells.

    Returns:
        Multi-line string of the framed 80x24 ASCII battlefield.
    """
    active_craters = craters if craters is not None else []
    terrain_heights = reconstruct_terrain_heights(active_craters)
    board_matrix = build_board_matrix(
        terrain_heights=terrain_heights,
        tanks=tanks,
        craters=active_craters,
    )

    if move_path:
        for point in move_path:
            if len(point) >= 2:
                col, row = int(point[0]), int(point[1])
                if 0 <= col < BOARD_WIDTH and 0 <= row < BOARD_HEIGHT:
                    if board_matrix[row][col] not in ("1", "2"):
                        board_matrix[row][col] = "."

    if trajectory:
        for point in trajectory:
            if len(point) >= 2:
                col, row = int(point[0]), int(point[1])
                if 0 <= col < BOARD_WIDTH and 0 <= row < BOARD_HEIGHT:
                    if board_matrix[row][col] not in ("1", "2"):
                        board_matrix[row][col] = "*"

    # Re-stamp living tank sprites ([1> and <2]) on top
    stamp_tank_sprites(board_matrix, tanks)

    if impact and isinstance(impact.get("coord"), list) and len(impact["coord"]) >= 2:
        impact_x = int(impact["coord"][0])
        impact_y = int(impact["coord"][1])
        if 0 <= impact_x < BOARD_WIDTH and 0 <= impact_y < BOARD_HEIGHT:
            board_matrix[impact_y][impact_x] = "X"

    horizontal_border = "+" + ("-" * BOARD_WIDTH) + "+"
    lines: list[str] = [horizontal_border]
    for row in range(BOARD_HEIGHT - 1, -1, -1):
        rendered_row = colorize_battlefield_row(
            board_matrix[row], use_color=use_color
        )
        lines.append("|" + rendered_row + "|")
    lines.append(horizontal_border)
    return "\n".join(lines)


def print_scanline_block(text: str, line_delay: float = 0.0) -> None:
    """Print a multi-line string line-by-line with optional CRT scanline pacing.

    Args:
        text: Multi-line string to render to standard output.
        line_delay: Seconds to pause between lines (``0.0`` disables sleep).
    """
    for line in text.splitlines():
        print(line, flush=True)
        if line_delay > 0.0:
            time.sleep(line_delay)


def _format_hp_bar(hp: int) -> str:
    """Format a 10-cell ASCII health bar (`[##########]`) for `0..100` HP."""
    clamped_hp = max(0, min(100, int(hp)))
    filled_cells = max(0, min(10, (clamped_hp + 9) // 10))
    return "[" + ("#" * filled_cells) + ("-" * (10 - filled_cells)) + "]"


def format_tutorial_banner(
    assigned_id: str,
    opponent_nickname: str,
    use_color: bool = False,
    player_nickname: str | None = None,
) -> str:
    """Format an 82-column boxed pre-game tactical briefing before Round 1.

    Covers:
    1. Mission Objective (`0 HP` elimination or higher HP after `20 rounds`).
    2. Turn Rules (`60s` timer, optional `MOVE LEFT/RIGHT <1-5>`, `FIRE <0-180> <1-100>`).
    3. Unified Aiming & Damage (`10-85 deg` forward for both players, `25 HP` / `10 HP`).

    Args:
        assigned_id: Local player identifier (``"Player_1"`` or ``"Player_2"``).
        opponent_nickname: Display nickname of the remote opponent.
        use_color: Whether to apply ANSI color escape codes after padding.
        player_nickname: Optional local player's display nickname.

    Returns:
        Multi-line 82-column boxed tutorial banner string.
    """
    commander_label = (
        player_nickname if player_nickname else assigned_id
    )
    horizontal_border = "+" + ("-" * BOARD_WIDTH) + "+"
    inner_rows = [
        (
            f" TACTICAL BRIEFING // {commander_label} vs "
            f"{opponent_nickname}"
        ),
        (
            " 1. MISSION OBJECTIVE: Destroy the enemy tank (0 HP) or hold "
            "higher HP"
        ),
        "    when the 20-round artillery campaign concludes.",
        (
            " 2. TURN RULES (60s timer): Optional MOVE LEFT <1-5> or "
            "MOVE RIGHT <1-5>"
        ),
        (
            "    (once per turn), then FIRE <angle 0-180> <power 1-100> to "
            "launch shell."
        ),
        (
            " 3. UNIFIED AIMING & DAMAGE: 10-85 deg aims forward toward your "
            "opponent"
        ),
        (
            "    for BOTH players (45 deg = max distance). Direct Hit = 25 HP, "
            "Crater"
        ),
        "    Shrapnel (<=2 cols) = 10 HP.",
    ]

    lines: list[str] = [horizontal_border]
    for idx, raw_row in enumerate(inner_rows):
        padded_row = f"{raw_row:<{BOARD_WIDTH}}"[:BOARD_WIDTH]
        if use_color:
            if idx == 0:
                padded_row = f"{ANSI_TRAJECTORY}{padded_row}{ANSI_RESET}"
            elif idx in (1, 3, 5):
                padded_row = f"{ANSI_SURFACE_INTACT}{padded_row}{ANSI_RESET}"
        lines.append(f"|{padded_row}|")
    lines.append(horizontal_border)
    return "\n".join(lines)


def format_hud_header(
    assigned_id: str,
    opponent_nickname: str,
    current_round: int,
    tanks: dict[str, dict[str, int]],
    active_turn: str,
    time_remaining: int,
    use_color: bool = False,
    player_nickname: str | None = None,
) -> str:
    """Format the stylized 82-column boxed scoreboard HUD and turn status banner.

    Pads each inner row to 80 visible characters before applying optional ANSI
    colors so the right ``|`` border aligns at column 82 even when ``use_color=True``.

    Args:
        assigned_id: Local player identifier (``"Player_1"`` or ``"Player_2"``).
        opponent_nickname: Display nickname of the remote opponent.
        current_round: Active match round number (1 to 20).
        tanks: Dictionary mapping player IDs to ``{"x": int, "y": int, "hp": int}``.
        active_turn: Player ID whose turn is currently active.
        time_remaining: Turn countdown seconds remaining.
        use_color: Whether to apply ANSI color escape codes to tank labels.
        player_nickname: Optional local player's display nickname.

    Returns:
        Formatted 82-column boxed HUD header string.
    """
    p1_tank = tanks.get("Player_1", {"x": 10, "y": 6, "hp": 100})
    p2_tank = tanks.get("Player_2", {"x": 70, "y": 6, "hp": 100})
    p1_hp = int(p1_tank.get("hp", 100))
    p2_hp = int(p2_tank.get("hp", 100))
    p1_x = int(p1_tank.get("x", 10))
    p2_x = int(p2_tank.get("x", 70))

    horizontal_border = "+" + ("-" * BOARD_WIDTH) + "+"

    if assigned_id == "Player_1":
        p1_name = player_nickname if player_nickname else "Player_1"
        p2_name = opponent_nickname if opponent_nickname else "Player_2"
    else:
        p1_name = opponent_nickname if opponent_nickname else "Player_1"
        p2_name = player_nickname if player_nickname else "Player_2"

    # Line 1: Title and Round XX/20 (centered to 80 visible chars)
    line_1_raw = (
        f"TANKSCII : TERMINAL ARTILLERY | Round {current_round:02d}/{MAX_ROUNDS}"
    )
    line_1_padded = f"{line_1_raw:^{BOARD_WIDTH}}"[:BOARD_WIDTH]

    # Line 2: Nickname + tank sprite + 10-cell HP bar + HP (centered to 80 chars)
    p1_bar = _format_hp_bar(p1_hp)
    p2_bar = _format_hp_bar(p2_hp)
    p1_label = f"{p1_name[:14]} [1>"
    p2_label = f"{p2_name[:14]} <2]"
    p1_segment_raw = f"{p1_label} {p1_bar} {p1_hp:3d}HP"
    p2_segment_raw = f"{p2_label} {p2_bar} {p2_hp:3d}HP"
    line_2_raw = f"{p1_segment_raw} | {p2_segment_raw}"
    line_2_padded = f"{line_2_raw:^{BOARD_WIDTH}}"[:BOARD_WIDTH]

    # Line 3: Narrative turn status indicator (centered to 80 visible chars)
    if active_turn == assigned_id:
        line_3_raw = (
            f"Gunner awaiting orders ({time_remaining}s remaining)"
        )
    else:
        line_3_raw = (
            f"STANDBY — Enemy crew {opponent_nickname} is aiming "
            f"({time_remaining}s remaining)"
        )
    line_3_padded = f"{line_3_raw:^{BOARD_WIDTH}}"[:BOARD_WIDTH]

    if use_color:
        line_1_padded = f"{ANSI_TRAJECTORY}{line_1_padded}{ANSI_RESET}"
        line_2_padded = line_2_padded.replace(
            p1_label,
            f"{ANSI_PLAYER_1}{p1_label}{ANSI_RESET}",
            1,
        ).replace(
            p2_label,
            f"{ANSI_PLAYER_2}{p2_label}{ANSI_RESET}",
            1,
        )
        status_color = (
            ANSI_SURFACE_INTACT
            if active_turn == assigned_id
            else ANSI_CRATER_FLOOR
        )
        line_3_padded = f"{status_color}{line_3_padded}{ANSI_RESET}"

    return "\n".join(
        [
            horizontal_border,
            f"|{line_1_padded}|",
            f"|{line_2_padded}|",
            f"|{line_3_padded}|",
            horizontal_border,
        ]
    )


def format_action_summary(
    payload: dict[str, Any],
    use_color: bool = False,
    player_names: dict[str, str] | None = None,
) -> str:
    """Format an engaging artillery combat narrative from a ``STATE_UPDATE`` payload.

    Args:
        payload: Validated ``STATE_UPDATE`` payload dictionary.
        use_color: Whether to wrap the narrative line in ANSI color codes.
        player_names: Optional mapping of ``"Player_1"`` / ``"Player_2"`` to nicknames.

    Returns:
        A descriptive combat narrative string summarizing the move, shot, or timeout.
    """
    action = str(payload.get("action", "UNKNOWN"))
    actor_id = str(payload.get("actor_id", "SERVER"))
    actor_name = (
        player_names.get(actor_id, actor_id) if player_names else actor_id
    )
    target_id = "Player_2" if actor_id == "Player_1" else "Player_1"
    target_name = (
        player_names.get(target_id, "the enemy tank")
        if player_names
        else "the enemy tank"
    )

    if action == "MOVE":
        move_path = payload.get("move_path") or []
        if move_path:
            start_coord = move_path[0]
            end_coord = move_path[-1]
            narrative = (
                f">> TREADS GRINDING: {actor_name} rolls across the ridge from "
                f"({start_coord[0]}, {start_coord[1]}) to "
                f"({end_coord[0]}, {end_coord[1]}) to take a new firing position."
            )
        else:
            narrative = (
                f">> TREADS GRINDING: {actor_name} repositions across the ridge "
                "to take a new firing position."
            )
        return f"{ANSI_MOVE_TRAIL}{narrative}{ANSI_RESET}" if use_color else narrative

    if action == "FIRE":
        impact = payload.get("impact") or {}
        coord = impact.get("coord") or [0, 0]
        result = str(impact.get("result", "MISS"))
        damage_dealt = int(payload.get("damage_dealt", 0))

        if damage_dealt >= 25:
            narrative = (
                f">> DIRECT HIT! {actor_name}'s shell screams across the sky and "
                f"slams squarely into {target_name} at ({coord[0]}, {coord[1]}) "
                f"for {damage_dealt} HP damage!"
            )
            return f"{ANSI_IMPACT}{narrative}{ANSI_RESET}" if use_color else narrative

        if 0 < damage_dealt < 25:
            narrative = (
                f">> SHRAPNEL BLAST! {actor_name}'s shell detonates near "
                f"({coord[0]}, {coord[1]}), rattling {target_name}'s hull with "
                f"{damage_dealt} HP of concussive shrapnel!"
            )
            return (
                f"{ANSI_TRAJECTORY}{narrative}{ANSI_RESET}"
                if use_color
                else narrative
            )

        if result == "GROUND":
            narrative = (
                f">> CRATER IMPACT! {actor_name}'s shell gouges the earth at "
                f"({coord[0]}, {coord[1]}) — no hull damage as the smoke clears."
            )
            return (
                f"{ANSI_CRATER_FLOOR}{narrative}{ANSI_RESET}"
                if use_color
                else narrative
            )

        narrative = (
            f">> WIDE SHOT! {actor_name}'s shell sails beyond the battlefield "
            f"perimeter at ({coord[0]}, {coord[1]}) without effect."
        )
        return f"{ANSI_SUBSOIL}{narrative}{ANSI_RESET}" if use_color else narrative

    if action == "TIMEOUT":
        next_turn_id = str(payload.get("next_turn", "opponent"))
        next_turn_name = (
            player_names.get(next_turn_id, next_turn_id)
            if player_names
            else next_turn_id
        )
        narrative = (
            f">> COMMS TIMEOUT! {actor_name}'s 60-second firing window expired in "
            f"radio silence — turn authority passes to {next_turn_name}."
        )
        return f"{ANSI_IMPACT}{narrative}{ANSI_RESET}" if use_color else narrative

    narrative = f">> FIELD REPORT: {actor_name} executed {action}."
    return narrative


def format_game_over_banner(
    payload: dict[str, Any],
    assigned_id: str,
    use_color: bool = False,
    player_names: dict[str, str] | None = None,
) -> str:
    """Format the end-of-match summary banner from a ``GAME_OVER`` payload.

    Args:
        payload: Validated ``GAME_OVER`` payload dictionary.
        assigned_id: Local player's assigned ID (``"Player_1"`` or ``"Player_2"``).
        use_color: Whether to apply optional ANSI colors to the banner.
        player_names: Optional mapping of ``"Player_1"`` / ``"Player_2"`` to nicknames.

    Returns:
        Multi-line ASCII banner summarizing winner, reason, and final scores.
    """
    winner = str(payload.get("winner", "DRAW"))
    reason = str(payload.get("reason", "UNKNOWN"))
    message = str(payload.get("message", "Match concluded."))
    final_scores = payload.get("final_scores") or {}
    p1_hp = int(final_scores.get("Player_1_hp", 0))
    p2_hp = int(final_scores.get("Player_2_hp", 0))
    rounds = int(final_scores.get("rounds", 0))

    p1_name = (
        player_names.get("Player_1", "Player_1") if player_names else "Player_1"
    )
    p2_name = (
        player_names.get("Player_2", "Player_2") if player_names else "Player_2"
    )
    winner_name = (
        player_names.get(winner, winner) if player_names else winner
    )
    message = (
        message.replace("Player 1", p1_name)
        .replace("Player_1", p1_name)
        .replace("Player 2", p2_name)
        .replace("Player_2", p2_name)
    )

    if winner == "DRAW":
        outcome_header = "MATCH RESULT: DRAW"
        header_color = ANSI_TRAJECTORY
    elif winner == assigned_id:
        outcome_header = f"MATCH RESULT: VICTORY ({winner_name} WINS!)"
        header_color = ANSI_SURFACE_INTACT
    else:
        outcome_header = f"MATCH RESULT: DEFEAT ({winner_name} WINS)"
        header_color = ANSI_IMPACT

    if use_color:
        outcome_header = f"{header_color}{outcome_header}{ANSI_RESET}"

    border = "=" * 82
    return "\n".join(
        [
            border,
            f"  {outcome_header}",
            f"  Reason : {reason} — {message}",
            f"  Scores : {p1_name} HP: {p1_hp} | {p2_name} HP: {p2_hp} | Rounds: {rounds}",
            border,
        ]
    )


def _overwrite_terminal_lines_in_place(lines: list[str]) -> None:
    """Move cursor up `len(lines)` rows and overwrite each row atomically."""
    num_lines = len(lines)
    buffer_parts: list[str] = [f"\033[{num_lines}A\r"]
    for line in lines:
        buffer_parts.append(f"\033[2K{line}\n")
    sys.stdout.write("".join(buffer_parts))
    sys.stdout.flush()


def animate_fire_trajectory_in_place(
    payload: dict[str, Any],
    assigned_id: str,
    player_nickname: str,
    opponent_nickname: str,
    pre_shot_tanks: dict[str, dict[str, int]],
    pre_shot_craters: list[dict[str, int]],
    post_shot_tanks: dict[str, dict[str, int]],
    post_shot_craters: list[dict[str, int]],
    current_round: int,
    active_turn: str,
    time_remaining: int,
    player_names: dict[str, str],
    use_color: bool = True,
    frame_delay: float = 0.06,
    impact_pause: float = 0.85,
) -> None:
    """Animate a projectile arc across the 80x24 battlefield in place via ANSI cursor control.

    1. Renders progressive slices of ``trajectory`` on top of ``pre_shot_tanks`` and
       ``pre_shot_craters`` while redrawing in place using ``\\033[<N>A``.
    2. On impact, reveals the ``X`` impact marker, deformed ``post_shot_craters``,
       updated tank HP bars, and the narrative combat report, pausing briefly
       before the next-turn state update clears the trajectory.
    """
    trajectory: list[list[int]] = list(payload.get("trajectory") or [])
    impact = payload.get("impact")
    actor_id = str(payload.get("actor_id", "Player_1"))
    actor_name = player_names.get(actor_id, actor_id)

    flight_line = (
        f">> ARTILLERY IN FLIGHT: {actor_name} fires a shell screaming "
        "across the sky..."
    )
    if use_color:
        flight_line = f"{ANSI_TRAJECTORY}{flight_line}{ANSI_RESET}"

    pre_hud = format_hud_header(
        assigned_id=assigned_id,
        opponent_nickname=opponent_nickname,
        current_round=current_round,
        tanks=pre_shot_tanks,
        active_turn=actor_id,
        time_remaining=time_remaining,
        use_color=use_color,
        player_nickname=player_nickname,
    )

    total_points = len(trajectory)
    max_flight_frames = 10
    if total_points <= 1:
        step_lengths = [total_points]
    else:
        num_steps = min(max_flight_frames, total_points)
        step_lengths = [
            max(1, int(round((step_idx + 1) * total_points / num_steps)))
            for step_idx in range(num_steps)
        ]

    print()
    for idx, slice_len in enumerate(step_lengths):
        partial_trajectory = trajectory[:slice_len]
        board_frame = render_battlefield_ascii(
            tanks=pre_shot_tanks,
            craters=pre_shot_craters,
            trajectory=partial_trajectory,
            impact=None,
            move_path=None,
            use_color=use_color,
        )
        frame_lines = [flight_line] + pre_hud.splitlines() + board_frame.splitlines()
        if idx == 0:
            sys.stdout.write("\n".join(frame_lines) + "\n")
            sys.stdout.flush()
        else:
            time.sleep(frame_delay)
            _overwrite_terminal_lines_in_place(frame_lines)

    # Final impact frame: show impact X, deformed crater, updated HP bar, and narrative
    time.sleep(frame_delay)
    impact_narrative = format_action_summary(
        payload,
        use_color=use_color,
        player_names=player_names,
    )
    post_hud = format_hud_header(
        assigned_id=assigned_id,
        opponent_nickname=opponent_nickname,
        current_round=current_round,
        tanks=post_shot_tanks,
        active_turn=active_turn,
        time_remaining=time_remaining,
        use_color=use_color,
        player_nickname=player_nickname,
    )
    impact_board = render_battlefield_ascii(
        tanks=post_shot_tanks,
        craters=post_shot_craters,
        trajectory=trajectory,
        impact=impact,
        move_path=None,
        use_color=use_color,
    )
    impact_lines = (
        [impact_narrative] + post_hud.splitlines() + impact_board.splitlines()
    )
    _overwrite_terminal_lines_in_place(impact_lines)
    if impact_pause > 0.0:
        time.sleep(impact_pause)


def redraw_hud_and_board_in_place(
    hud_text: str,
    battlefield_text: str,
) -> None:
    """Overwrite the HUD and 80x24 battlefield in place (preserving the narrative line above)."""
    combined_lines = hud_text.splitlines() + battlefield_text.splitlines()
    _overwrite_terminal_lines_in_place(combined_lines)
