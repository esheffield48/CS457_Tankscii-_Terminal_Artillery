---
name: server-fsm-concurrency
description: >-
  Use this skill when implementing or modifying the authoritative Tankscii game
  server (`server.py`), CLI `-h`/`-p` port parsing and `0.0.0.0` binding, the
  8-state Finite State Machine (FSM), the `selectors.DefaultSelector` event
  loop, turn timer deadlines, ballistic hit resolution, or client
  disconnect/cleanup logic.
---

# Server State Machine & Concurrency Logic

Follow these exact state transitions and concurrency rules from `docs/fsm_specification.md`, `docs/concurrency_design.md`, `docs/sequence_diagram.md`, and `docs/ai_prompts.md`.

## 1. 8-State Finite State Machine (FSM)

`INIT` -> `WAITING_FOR_PLAYERS` -> `GAME_START` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` -> `GAME_OVER` -> `CLEANUP`

### Phase 1: `INIT` -> `WAITING_FOR_PLAYERS`
1. Parse CLI arguments (`-h`, `-p <port>` required, validated in range `1024..65535`).
2. Create IPv4 TCP socket (`socket.AF_INET`, `socket.SOCK_STREAM`), set `SO_REUSEADDR = 1`, `bind(("0.0.0.0", port))`, `listen(5)`, `listen_sock.setblocking(False)`.
3. Register with `selectors.DefaultSelector()`: `sel.register(listen_sock, selectors.EVENT_READ, data=None)`.
4. Transition `fsm_state = "WAITING_FOR_PLAYERS"`.

### Phase 2: `WAITING_FOR_PLAYERS` -> `GAME_START` -> `PLAYER_TURN`
1. On `listen_sock` readable: `conn, addr = listen_sock.accept()`, `conn.setblocking(False)`, register `ClientSession`.
2. **Capacity Check (`MAX_PLAYERS = 2`)**: If `len(clients) >= 2` (or match active), immediately send `ERROR ("LOBBY_FULL", retry_allowed=False)`, unregister, and close `conn` without altering match state.
3. **Client 1 `CONNECT`**: Store `clients["Player_1"] = session`, send `LOBBY_WAIT` (`assigned_id: "Player_1"`), remain in `WAITING_FOR_PLAYERS`.
   - If `Player_1` disconnects (0-byte EOF or socket error) before `Player_2` joins: unregister, close socket, `clients.clear()`, stay in `WAITING_FOR_PLAYERS`.
4. **Client 2 `CONNECT`**: Store `clients["Player_2"] = session`, transition to `GAME_START`.
5. Initialize `board_matrix` (`80x24`), `terrain_heights` (length 80), spawn `Player_1` (`x=10, hp=100`) and `Player_2` (`x=70, hp=100`), randomly pick `first_turn`, set `round = 1`, `move_used = False`, `turn_deadline = time.monotonic() + 60.0`.
6. Send individualized `GAME_START` frames to both clients and transition to `PLAYER_TURN`.

### Phase 3: Active Match Loop (`PLAYER_TURN` <-> `EVALUATE_MOVE` <-> `CHECK_WIN_DRAW`)
1. **Dynamic Selector Timeout & Race Ordering**:
   - In `PLAYER_TURN`, compute `timeout = max(0.0, turn_deadline - time.monotonic())` (or `disconnect_deadline - time.monotonic()` while a player is `TEMPORARILY_DISCONNECTED`).
   - Always process all readable socket events from `sel.select(timeout)` **before** checking `time.monotonic() >= turn_deadline` (prevents Race 1 where an arriving `FIRE` packet races with timer expiration).
2. **Turn Enforcement**:
   - If `frame["player_id"] != active_turn`, immediately send `ERROR ("OUT_OF_TURN", retry_allowed=False)` to sender. Leave `turn_deadline` and state untouched.
3. **`MOVE` Handling**:
   - Check `move_used == False` (else `ERROR "MOVE_ALREADY_USED", retry_allowed=True`) and `direction in ("LEFT", "RIGHT")`, `1 <= distance <= 5`, new `x in 0..79` (else `ERROR "INVALID_PARAMS", retry_allowed=True`).
   - Transition `PLAYER_TURN` -> `EVALUATE_MOVE`. Update tank `x`/`y`, record `move_path`, set `move_used = True`, compute `time_remaining = max(1, int(ceil(turn_deadline - time.monotonic())))`.
   - Broadcast `STATE_UPDATE (action="MOVE", next_turn=active_turn, time_remaining=...)` and transition back to `PLAYER_TURN`.
4. **`FIRE` Handling**:
   - Validate `0 <= angle <= 180` and `1 <= power <= 100` (else `ERROR "INVALID_PARAMS", retry_allowed=True`).
   - Transition `PLAYER_TURN` -> `EVALUATE_MOVE`. Simulate ballistic trajectory across `board_matrix`, record crater in `craters`, calculate damage (**25 HP** direct hit; **10 HP** shrapnel hit within 2 distance units), and deduct tank HP (clamped to `>= 0`).
   - Transition `EVALUATE_MOVE` -> `CHECK_WIN_DRAW`:
     - If any tank `hp <= 0` or `round > 20` (after completing the round): broadcast final `STATE_UPDATE (action="FIRE")` followed immediately by `GAME_OVER` (`winner`: `"Player_1" | "Player_2" | "DRAW"`, `reason`: `"HP_ELIMINATION" | "ROUND_LIMIT"`), then transition to `CLEANUP`.
     - Otherwise: switch `active_turn` to opponent, reset `move_used = False`, reset `turn_deadline = time.monotonic() + 60.0`, increment `round` after both players have completed their turns in the round, broadcast `STATE_UPDATE (action="FIRE", next_turn=active_turn, time_remaining=60)` followed by a clean `STATE_UPDATE (action="TURN_START", trajectory=None, impact=None)`, and transition to `PLAYER_TURN`.
5. **60-Second Turn Timeout**:
   - When `time.monotonic() >= turn_deadline` in `PLAYER_TURN`: send `ERROR ("TIMEOUT", retry_allowed=False)` to `active_turn`, flip `active_turn` to opponent, reset `move_used = False` and `turn_deadline = time.monotonic() + 60.0`, broadcast `STATE_UPDATE (action="TIMEOUT", next_turn=active_turn, time_remaining=60)`, and remain in `PLAYER_TURN`.

### Phase 4: Disconnections, 60s State Preservation & `CLEANUP` -> `WAITING_FOR_PLAYERS`
1. **Snapshot Broadcast Isolation**:
   - Iterate over `list(clients.items())`. Wrap each `sendall()` in `try/except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)`, collecting failed IDs in `disconnected_ids` and handling them *after* the broadcast loop completes.
2. **Mid-Game State Preservation, Reconnection & Forfeit**:
   - Explicit `DISCONNECT` (`reason = "FORFEIT"`): transition immediately to `GAME_OVER (reason="FORFEIT")` and `CLEANUP`.
   - Unexpected socket drop (`0-byte EOF`, `ConnectionResetError`, `BrokenPipeError`) or `DISCONNECT (reason="USER_QUIT")` during an active match: close only the dropped socket, mark the session `connection_status = "TEMPORARILY_DISCONNECTED"` with `disconnect_deadline = time.monotonic() + 60.0`, preserve all match state, and notify the opponent with `STATE_UPDATE (action="PLAYER_DISCONNECTED")`.
   - When the returning player reconnects via `CONNECT` within 60s using their nickname/alias or ID, re-attach the new socket and send `STATE_RESUME` to both clients. If the 60s grace period expires, award `GAME_OVER (reason="DISCONNECT")` to the survivor and enter `CLEANUP`.
3. **`CLEANUP` State**:
   - Unregister (`sel.unregister(sock)`) and close (`sock.close()`) all client sockets safely.
   - Clear `clients`, reset `board_matrix`, `tanks`, `craters`, `round = 1`, `move_used = False`, and transition to `WAITING_FOR_PLAYERS`.
