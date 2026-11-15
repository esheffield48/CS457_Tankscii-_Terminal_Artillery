"""Application-layer protocol message builders and strict schema validators.

This module defines the 9 canonical Tankscii message types (`CONNECT`,
`LOBBY_WAIT`, `GAME_START`, `MOVE`, `FIRE`, `STATE_UPDATE`, `ERROR`,
`DISCONNECT`, and `GAME_OVER`), envelope constructors, and boundary validators
specified in `docs/protocol_blueprint.md`.
"""

from __future__ import annotations

import re
import time
from typing import Any

from tankscii import DEFAULT_CLIENT_VERSION, TURN_DURATION_SECONDS

CANONICAL_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "CONNECT",
        "LOBBY_WAIT",
        "GAME_START",
        "MOVE",
        "FIRE",
        "STATE_UPDATE",
        "ERROR",
        "DISCONNECT",
        "GAME_OVER",
    }
)

VALID_PLAYER_IDS: frozenset[str] = frozenset({"Player_1", "Player_2", "SERVER"})
VALID_MOVE_DIRECTIONS: frozenset[str] = frozenset({"LEFT", "RIGHT"})
VALID_DISCONNECT_REASONS: frozenset[str] = frozenset({"FORFEIT", "USER_QUIT"})
VALID_ERROR_CODES: frozenset[str] = frozenset(
    {
        "OUT_OF_TURN",
        "INVALID_PARAMS",
        "MOVE_ALREADY_USED",
        "TIMEOUT",
        "LOBBY_FULL",
        "MALFORMED_FRAME",
    }
)

MIN_NICKNAME_LENGTH: int = 1
MAX_NICKNAME_LENGTH: int = 16
MIN_MOVE_DISTANCE: int = 1
MAX_MOVE_DISTANCE: int = 5
MIN_FIRE_ANGLE: int = 0
MAX_FIRE_ANGLE: int = 180
MIN_FIRE_POWER: int = 1
MAX_FIRE_POWER: int = 100
DEFAULT_BOARD_WIDTH: int = 80

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _is_strict_int(value: Any) -> bool:
    """Return True if `value` is an `int` and explicitly NOT a `bool`.

    In Python, `bool` is a subclass of `int` (`isinstance(True, int) == True`),
    so protocol validators must reject booleans before checking integer bounds.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def build_message_envelope(
    msg_type: str,
    player_id: str,
    payload: dict[str, Any],
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Construct a canonical 4-key Tankscii message envelope.

    Args:
        msg_type: One of the 9 canonical message type strings.
        player_id: Sender identifier (`"Player_1"`, `"Player_2"`, or `"SERVER"`).
        payload: Message-specific dictionary body.
        timestamp: Optional UNIX epoch timestamp in seconds; defaults to current
            epoch time (`int(time.time())`).

    Returns:
        Dictionary containing `msg_type`, `player_id`, `timestamp`, and `payload`.
    """
    effective_timestamp = int(time.time()) if timestamp is None else timestamp
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "timestamp": effective_timestamp,
        "payload": payload,
    }


def build_connect_message(
    nickname: str,
    client_version: str = DEFAULT_CLIENT_VERSION,
    player_id: str = "Player_1",
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `CONNECT` message sent by a client to join the match lobby."""
    payload: dict[str, Any] = {
        "nickname": nickname,
        "client_version": client_version,
    }
    return build_message_envelope("CONNECT", player_id, payload, timestamp)


def build_lobby_wait_message(
    assigned_id: str = "Player_1",
    message: str = "Waiting for opponent...",
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `LOBBY_WAIT` message sent by the server to Player 1."""
    payload: dict[str, Any] = {
        "assigned_id": assigned_id,
        "message": message,
    }
    return build_message_envelope("LOBBY_WAIT", "SERVER", payload, timestamp)


def build_game_start_message(
    assigned_id: str,
    opponent_nickname: str,
    tanks: dict[str, dict[str, int]],
    first_turn: str,
    turn_duration: int = TURN_DURATION_SECONDS,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `GAME_START` message sent by the server to each player (60s turn timer)."""
    payload: dict[str, Any] = {
        "assigned_id": assigned_id,
        "opponent_nickname": opponent_nickname,
        "tanks": tanks,
        "first_turn": first_turn,
        "turn_duration": turn_duration,
    }
    return build_message_envelope("GAME_START", "SERVER", payload, timestamp)


def build_move_message(
    player_id: str,
    direction: str,
    distance: int,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `MOVE` message sent by the active client (`LEFT`/`RIGHT`, 1-5)."""
    payload: dict[str, Any] = {
        "direction": direction,
        "distance": distance,
    }
    return build_message_envelope("MOVE", player_id, payload, timestamp)


def build_fire_message(
    player_id: str,
    angle: int,
    power: int,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `FIRE` message sent by the active client (`angle` 0-180, `power` 1-100)."""
    payload: dict[str, Any] = {
        "angle": angle,
        "power": power,
    }
    return build_message_envelope("FIRE", player_id, payload, timestamp)


def build_state_update_message(
    round_num: int,
    action: str,
    actor_id: str,
    tanks: dict[str, dict[str, int]],
    move_path: list[list[int]] | None,
    trajectory: list[list[int]] | None,
    impact: dict[str, Any] | None,
    damage_dealt: int,
    craters: list[dict[str, int]],
    next_turn: str,
    time_remaining: int,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `STATE_UPDATE` broadcast message from the authoritative server."""
    payload: dict[str, Any] = {
        "round": round_num,
        "action": action,
        "actor_id": actor_id,
        "tanks": tanks,
        "move_path": move_path,
        "trajectory": trajectory,
        "impact": impact,
        "damage_dealt": damage_dealt,
        "craters": craters,
        "next_turn": next_turn,
        "time_remaining": time_remaining,
    }
    return build_message_envelope("STATE_UPDATE", "SERVER", payload, timestamp)


def build_error_message(
    error_code: str,
    detail: str,
    retry_allowed: bool,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a structured `ERROR` message sent by the server to a client."""
    payload: dict[str, Any] = {
        "error_code": error_code,
        "detail": detail,
        "retry_allowed": retry_allowed,
    }
    return build_message_envelope("ERROR", "SERVER", payload, timestamp)


def build_disconnect_message(
    player_id: str,
    reason: str = "FORFEIT",
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `DISCONNECT` message sent by a departing client."""
    payload: dict[str, Any] = {
        "reason": reason,
    }
    return build_message_envelope("DISCONNECT", player_id, payload, timestamp)


def build_game_over_message(
    winner: str,
    reason: str,
    final_scores: dict[str, int],
    message: str,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build a `GAME_OVER` broadcast message summarizing the match outcome."""
    payload: dict[str, Any] = {
        "winner": winner,
        "reason": reason,
        "final_scores": final_scores,
        "message": message,
    }
    return build_message_envelope("GAME_OVER", "SERVER", payload, timestamp)


def validate_frame_envelope(frame: Any) -> tuple[bool, str]:
    """Validate that a parsed JSON object conforms to the universal 4-key envelope.

    Checks that `frame` is a `dict` containing `"msg_type"` (str in the 9
    canonical types), `"player_id"` (`"Player_1"`, `"Player_2"`, or `"SERVER"`),
    `"timestamp"` (strict `int`, not `bool`), and `"payload"` (`dict`).

    Args:
        frame: Deserialized JSON value to inspect.

    Returns:
        Tuple of `(is_valid, detail_message)`.
    """
    if not isinstance(frame, dict):
        return False, "Frame envelope must be a JSON object."

    required_keys = {"msg_type", "player_id", "timestamp", "payload"}
    missing_keys = required_keys - set(frame.keys())
    if missing_keys:
        sorted_missing = ", ".join(sorted(missing_keys))
        return False, f"Missing required envelope field(s): {sorted_missing}."

    msg_type = frame.get("msg_type")
    if not isinstance(msg_type, str) or msg_type not in CANONICAL_MESSAGE_TYPES:
        return False, f"Unsupported or invalid msg_type: {msg_type!r}."

    player_id = frame.get("player_id")
    if not isinstance(player_id, str) or player_id not in VALID_PLAYER_IDS:
        return False, f"Invalid player_id: {player_id!r}."

    timestamp = frame.get("timestamp")
    if not _is_strict_int(timestamp):
        return False, "Field 'timestamp' must be an integer."

    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return False, "Field 'payload' must be a JSON object."

    return True, "Valid envelope."


def validate_connect_payload(
    payload: dict[str, Any],
) -> tuple[bool, str, str, bool]:
    """Validate a `CONNECT` message payload (`nickname` and `client_version`).

    Args:
        payload: The `"payload"` dictionary from a `CONNECT` frame.

    Returns:
        Tuple of `(is_valid, error_code, detail, retry_allowed)`.
    """
    if not isinstance(payload, dict):
        return False, "MALFORMED_FRAME", "Payload must be a JSON object.", False

    nickname = payload.get("nickname")
    if not isinstance(nickname, str):
        return (
            False,
            "INVALID_PARAMS",
            "Field 'nickname' must be a string (1-16 characters).",
            False,
        )

    stripped_nickname = nickname.strip()
    if not (
        MIN_NICKNAME_LENGTH <= len(stripped_nickname) <= MAX_NICKNAME_LENGTH
    ) or len(nickname) > MAX_NICKNAME_LENGTH:
        return (
            False,
            "INVALID_PARAMS",
            "Field 'nickname' must be between 1 and 16 non-empty characters.",
            False,
        )

    client_version = payload.get("client_version")
    if (
        not isinstance(client_version, str)
        or not _SEMVER_PATTERN.match(client_version.strip())
    ):
        return (
            False,
            "INVALID_PARAMS",
            "Field 'client_version' must be a valid SemVer string (e.g. '1.0.0').",
            False,
        )

    return True, "", "", False


def validate_move_payload(
    payload: dict[str, Any],
    current_x: int,
    move_used: bool,
    board_width: int = DEFAULT_BOARD_WIDTH,
) -> tuple[bool, str, str, bool]:
    """Validate a `MOVE` message payload against turn state and board boundaries.

    Args:
        payload: The `"payload"` dictionary from a `MOVE` frame.
        current_x: Acting tank's current horizontal column (`0 <= x < board_width`).
        move_used: Whether the active player has already moved during this turn.
        board_width: Width of the battlefield grid (default `80`).

    Returns:
        Tuple of `(is_valid, error_code, detail, retry_allowed)`.
    """
    if move_used:
        return (
            False,
            "MOVE_ALREADY_USED",
            "Move already used this turn.",
            True,
        )

    if not isinstance(payload, dict):
        return False, "MALFORMED_FRAME", "Payload must be a JSON object.", True

    direction = payload.get("direction")
    if not isinstance(direction, str) or direction not in VALID_MOVE_DIRECTIONS:
        return (
            False,
            "INVALID_PARAMS",
            "Field 'direction' must be 'LEFT' or 'RIGHT'.",
            True,
        )

    distance = payload.get("distance")
    if not _is_strict_int(distance) or not (
        MIN_MOVE_DISTANCE <= distance <= MAX_MOVE_DISTANCE
    ):
        return (
            False,
            "INVALID_PARAMS",
            f"Field 'distance' must be an integer between {MIN_MOVE_DISTANCE} and {MAX_MOVE_DISTANCE}.",
            True,
        )

    target_x = (
        current_x - distance if direction == "LEFT" else current_x + distance
    )
    if not (0 <= target_x < board_width):
        return (
            False,
            "INVALID_PARAMS",
            f"Move out of bounds: target x={target_x} outside 0..{board_width - 1}.",
            True,
        )

    return True, "", "", False


def validate_fire_payload(
    payload: dict[str, Any],
) -> tuple[bool, str, str, bool]:
    """Validate a `FIRE` message payload (`angle` 0-180, `power` 1-100).

    Args:
        payload: The `"payload"` dictionary from a `FIRE` frame.

    Returns:
        Tuple of `(is_valid, error_code, detail, retry_allowed)`.
    """
    if not isinstance(payload, dict):
        return False, "MALFORMED_FRAME", "Payload must be a JSON object.", True

    angle = payload.get("angle")
    if not _is_strict_int(angle) or not (
        MIN_FIRE_ANGLE <= angle <= MAX_FIRE_ANGLE
    ):
        return (
            False,
            "INVALID_PARAMS",
            f"Field 'angle' must be an integer between {MIN_FIRE_ANGLE} and {MAX_FIRE_ANGLE}.",
            True,
        )

    power = payload.get("power")
    if not _is_strict_int(power) or not (
        MIN_FIRE_POWER <= power <= MAX_FIRE_POWER
    ):
        return (
            False,
            "INVALID_PARAMS",
            f"Field 'power' must be an integer between {MIN_FIRE_POWER} and {MAX_FIRE_POWER}.",
            True,
        )

    return True, "", "", False


def validate_disconnect_payload(
    payload: dict[str, Any],
) -> tuple[bool, str, str, bool]:
    """Validate a `DISCONNECT` message payload (`reason` in `FORFEIT`/`USER_QUIT`).

    Args:
        payload: The `"payload"` dictionary from a `DISCONNECT` frame.

    Returns:
        Tuple of `(is_valid, error_code, detail, retry_allowed)`.
    """
    if not isinstance(payload, dict):
        return False, "MALFORMED_FRAME", "Payload must be a JSON object.", False

    reason = payload.get("reason")
    if not isinstance(reason, str) or reason not in VALID_DISCONNECT_REASONS:
        return (
            False,
            "INVALID_PARAMS",
            "Field 'reason' must be 'FORFEIT' or 'USER_QUIT'.",
            False,
        )

    return True, "", "", False
