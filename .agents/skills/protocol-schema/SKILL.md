---
name: protocol-schema
description: >-
  Use this skill when implementing, validating, or updating the 9 canonical
  application-layer JSON message types, frame headers, numerical boundary
  checks, or structured ERROR responses for Tankscii.
---

# Message Serialization & Schema Validation

Follow these exact schemas and validation rules from `docs/protocol_blueprint.md`, `docs/sequence_diagram.md`, and `docs/ai_prompts.md`.

## 1. Universal Frame Envelope

Every message on the wire must be a JSON object with exactly 4 top-level keys:
- `msg_type` (`str`): One of the 9 canonical message types below.
- `player_id` (`str`): `"Player_1"`, `"Player_2"`, or `"SERVER"`.
- `timestamp` (`int`): UNIX epoch timestamp (`int(time.time())`).
- `payload` (`dict`): Message-specific dictionary.

If any envelope field is missing, wrong type, or unparseable JSON, respond with `ERROR` (`error_code: "MALFORMED_FRAME"`, `retry_allowed: True` if in active turn, else `False`).

## 2. The Canonical Message Schemas

1. **`CONNECT` (Client -> Server)**
   - `payload`: `{"nickname": str (1..16 chars, defaults "CobraKing"/"Thunderbolt"), "client_version": str (SemVer, e.g. "1.0.0")}`
2. **`LOBBY_WAIT` (Server -> Client)**
   - `payload`: `{"assigned_id": "Player_1", "message": str (non-empty)}`
3. **`GAME_START` (Server -> Clients)**
   - `payload`: `{"assigned_id": "Player_1"|"Player_2", "opponent_nickname": str (1..16 chars), "tanks": {"Player_1": {"x": int, "y": int, "hp": 100}, "Player_2": {"x": int, "y": int, "hp": 100}}, "first_turn": "Player_1"|"Player_2", "turn_duration": 60}`
4. **`MOVE` (Client -> Server)**
   - `payload`: `{"direction": "LEFT"|"RIGHT", "distance": int (1 <= distance <= 5)}`
   - Additional validation: `move_used == False` (else `"MOVE_ALREADY_USED"`, `retry_allowed: True`) and target `x` within `0..79` (else `"INVALID_PARAMS"`, `retry_allowed: True`).
5. **`FIRE` (Client -> Server)**
   - `payload`: `{"angle": int (0 <= angle <= 180), "power": int (1 <= power <= 100)}`
   - Out-of-range values return `ERROR` (`"INVALID_PARAMS"`, `retry_allowed: True`).
6. **`STATE_UPDATE` (Server -> Clients)**
   - `payload`:
     - `round`: `int` (`1..20`)
     - `action`: `"MOVE" | "FIRE" | "TIMEOUT" | "TURN_START" | "PLAYER_DISCONNECTED"`
     - `actor_id`: `"Player_1" | "Player_2"`
     - `tanks`: `{"Player_1": {"x": int, "y": int, "hp": int}, "Player_2": {"x": int, "y": int, "hp": int}}`
     - `move_path`: `[[x, y], ...]` on `"MOVE"`, `None` (`null`) otherwise
     - `trajectory`: `[[x, y], ...]` on `"FIRE"`, `None` (`null`) otherwise
     - `impact`: `{"coord": [x, y], "result": "HIT"|"MISS"|"GROUND"}` on `"FIRE"`, `None` (`null`) otherwise
     - `damage_dealt`: `0 | 10 | 25` (`int`)
     - `craters`: `[{"x": int, "radius": int}, ...]`
     - `next_turn`: `"Player_1" | "Player_2"`
     - `time_remaining`: `int` (`1..60`)
7. **`STATE_RESUME` (Server -> Clients)**
   - `payload`: `{"assigned_id": "Player_1"|"Player_2", "opponent_nickname": str, "round": int (1..20), "tanks": dict, "craters": list, "active_turn": "Player_1"|"Player_2", "time_remaining": int (1..60), "move_used": bool, "scores": {"Player_1_hp": int, "Player_2_hp": int, "rounds": int}, "message": str}`
8. **`ERROR` (Server -> Client)**
   - `payload`:
     - `error_code`: `"OUT_OF_TURN" | "INVALID_PARAMS" | "MOVE_ALREADY_USED" | "TIMEOUT" | "LOBBY_FULL" | "MALFORMED_FRAME"`
     - `detail`: `str` (non-empty explanation)
     - `retry_allowed`: `bool` (`False` for `"OUT_OF_TURN"`, `"TIMEOUT"`, `"LOBBY_FULL"`; `True` for `"INVALID_PARAMS"`, `"MOVE_ALREADY_USED"`)
9. **`DISCONNECT` (Client -> Server)**
   - `payload`: `{"reason": "FORFEIT"|"USER_QUIT"}`
10. **`GAME_OVER` (Server -> Clients)**
    - `payload`:
      - `winner`: `"Player_1" | "Player_2" | "DRAW"`
      - `reason`: `"HP_ELIMINATION" | "ROUND_LIMIT" | "FORFEIT" | "DISCONNECT"`
      - `final_scores`: `{"Player_1_hp": int, "Player_2_hp": int, "rounds": int}`
      - `message`: `str` (non-empty summary)
