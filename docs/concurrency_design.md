# TANKSCII : Concurrency Architecture

**Author :** Ethan Sheffield  
**Date :** 2026-10-18  
**Course :** CS 457 - Computer Networks  
**Target Server Domain :** `server.sheffield.edu`  

## 1. Server Concurrency Architecture & Connection Lifecycle

### 1.1 Concurrency Model : 
---
- **Non-Blocking I/O Multiplexing** (`selectors` / `select.select`)
   - The authoritative server implements Python's standard library `selectors` module `selectors.DefaultSelector`.
   - Rather than spawning a dedicated OS worker thread per client socket (`threading.Thread`), the server multiplexes the listening TCP socket and all connected client sockets within a single deterministic event loop.

### 1.2 Connection Lifecycle & Socket Servicing Mechanics
---

The authoritative server manages every client socket through four lifecycle phases:

#### **Phase 1** : Listening Socket `accept()` (`INIT` -> `WAITING_FOR_PLAYERS`)
1. **Socket Creation :** 
   - On startup, the server creates an IPv4 TCP listening socket (`socket.socket(socket.AF_INET, socket.SOCK_STREAM)`) and sets ` setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)` to allow immediate port rebinding during server restarts.
2. **Non-Blocking Bind & Listen :** 
   - The socket binds to the configured host/port, calls `listen(5)`, and is explicitly set to non-blocking mode via `listen_sock.setblocking(False)`.
3. **Selector Registration :** 
   - The listening socket is registered with the selector using `sel.register(listen_sock, selectors.EVENT_READ, data=None)`. The `data=None` parameter differentiates incoming connection requests on the listening socket from readable data events on established client sockets.
4. **Accepting Connections (`accept_wrapper`) :** 
   - When `sel.select()` signals `EVENT_READ` on `listen_sock`, the server calls `conn, addr = listen_sock.accept()`, immediately sets the new client socket to non-blocking mode (`conn.setblocking(False)`), instantiates a `ClientSession(sock=conn, addr=addr, stream_buffer=bytearray())`, and registers it via `sel.register(conn, selectors.EVENT_READ, data=session)`.

#### **Phase 2** : Client Registration (`WAITING_FOR_PLAYERS` -> `GAME_START`)
1. **Application-Layer Registration (`CONNECT`) :** 
   - Accepting a TCP connection does not immediately assign a player slot; the client must transmit a valid newline-delimited `CONNECT` JSON frame containing `nickname` and `client_version`.
2. **Player Slot Assignment :**
   - **First Client (`Player_1`) :** 
      - Registered in `clients["Player_1"] = session`. The server replies with `LOBBY_WAIT` (`assigned_id: "Player_1"`, `message: "Waiting for opponent..."`) and remains in `WAITING_FOR_PLAYERS`.
   - **Second Client (`Player_2`) :** 
      - Registered in `clients["Player_2"] = session`. The server transitions to `GAME_START`, initializes the map (`board_matrix`), spawns both tanks, randomly selects `first_turn`, broadcasts individualized `GAME_START` frames to both clients, sets `turn_deadline = time.monotonic() + 30.0`, and transitions to `PLAYER_TURN`.
3. **Multi-Client Capacity :**
   - Match instances are strictly limited to 2 active players (`MAX_PLAYERS = 2`).
   - If a 3rd client connects while a match is in progress (or after both client slots are filled), the server accepts the socket in non-blocking mode, sends an immediate `ERROR` frame (`error_code: "LOBBY_FULL"`, `detail: "Server match in progress (2/2 players active)."`, `retry_allowed: false`), unregisters the socket, and closes it cleanly without disturbing the active game loop or changing shared match state.

#### **Phase 3** : Active Game Loop Servicing (`PLAYER_TURN` <-> `EVALUATE_MOVE` <-> `CHECK_WIN_DRAW`)
1. **Dynamic Timeout Multiplexing :** 
   - At the top of each event loop iteration, the server calculates the `select()` timeout:
      - In `WAITING_FOR_PLAYERS`: `timeout = None` (blocks until a socket event occurs).
      - In `PLAYER_TURN`: `timeout = max(0.0, turn_deadline - time.monotonic())` to enforce the 30-second turn limit.
2. **Non-Blocking Byte Accumulation (`service_connection`) :** 
   - When a client socket triggers `EVENT_READ`, the server reads available bytes via `chunk = session.sock.recv(4096)`.
3. **Newline Frame Extraction (`\n`) :** 
   - Incoming bytes are appended to `session.stream_buffer`. A `while b"\n" in session.stream_buffer:` loop extracts every complete UTF-8 line, handling both **TCP fragmentation** (partial JSON waits in `stream_buffer` until the next `EVENT_READ`) and **TCP coalescing** (multiple back-to-back commands processed sequentially in arrival order).
4. **Synchronous State Evaluation & Broadcast :** 
   - Each complete JSON message is validated against the protocol schema and current FSM state:
      - **Out-of-Turn / Invalid Move:** Returns `ERROR` directly to the sender (`session.sock.sendall(...)`); game state and turn clock remain untouched.
      - **Valid `MOVE`:** Updates tank coordinates on `board_matrix`, sets `move_used = True`, broadcasts `STATE_UPDATE (action="MOVE")` to both clients in `clients.values()`, and returns to `PLAYER_TURN`.
      - **Valid `FIRE`:** Simulates ballistic trajectory across `board_matrix`, applies terrain cratering and HP deductions (`25 HP` direct hit / `10 HP` shrapnel hit), increments `round` / updates `scores`, checks win/draw conditions in `CHECK_WIN_DRAW`, and broadcasts `STATE_UPDATE (action="FIRE")` (followed by `GAME_OVER` if a tank reached `0 HP` or `round > 20`).

#### **Phase 4** : Client Socket Teardown & Lobby Reset (`GAME_OVER` -> `CLEANUP` -> `WAITING_FOR_PLAYERS`)
1. **Graceful Application Disconnect (`DISCONNECT`) :** 
   - If a client sends a `DISCONNECT` frame (`reason: "FORFEIT" or "USER_QUIT"`), the server transitions immediately to `GAME_OVER` and broadcasts `GAME_OVER (winner=opponent, reason="FORFEIT", final_scores=...)` to the surviving client.
2. **0-Byte EOF Detection :** 
   - If `session.sock.recv(4096)` returns `b""` (0 bytes), the peer initiated a TCP `FIN` closure.
      - If in `WAITING_FOR_PLAYERS`: The server unregisters and closes `Player_1`'s socket, removes `Player_1` from `clients`, and remains in `WAITING_FOR_PLAYERS` waiting for a new connection.
      - If in an active match (`PLAYER_TURN` / `EVALUATE_MOVE`): The server transitions to `GAME_OVER`, notifies the surviving client with `GAME_OVER (reason="DISCONNECT")`, and proceeds to `CLEANUP`.
3. **Abrupt Drop Exception Trapping (`TCP RST` / `EPIPE`) :** 
   - All `recv()` and `sendall()` calls are wrapped in `try/except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)` blocks so an abrupt client crash never terminates the server process.
4. **Reclaiming Resources (`CLEANUP`) :** 
   - For every socket in `clients` :
      - The server calls `sel.unregister(sock)` (ignoring `KeyError`/`ValueError` if already unregistered).
      - Calls `sock.close()` inside a `try/except OSError` block.
      - Clears `clients.clear()`, resets `board_matrix`, `tanks`, `craters`, `round = 1`, `move_used = False`, and transitions cleanly back to `WAITING_FOR_PLAYERS` to accept the next match without restarting the server process.

---

## 2. Thread Safety, Lock Acquisition & State Isolation Strategy

### 2.1 Shared Resources & State Variables
---

| Variable Name | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `fsm_state` | str | Current server FSM state | `"INIT"`, `"WAITING_FOR_PLAYERS"`, `"GAME_START"`, `"PLAYER_TURN"`, `"EVALUATE_MOVE"`, `"CHECK_WIN_DRAW"`, `"GAME_OVER"`, or `"CLEANUP"` |
| `clients` | dict | Connected client socket and session registry | `{player_id: ClientSession}` (max 2 players) |
| `stream_buffer` | bytearray | Per-client TCP receive buffer | UTF-8 bytes delimited by `\n` (`0x0A`) |
| `board_matrix` | list | 2D ASCII battlefield grid | `80 x 24` character matrix |
| `terrain_heights` | list | Surface elevation per column | `list[int]`, length 80 (`0 <= y < 24`) |
| `craters` | list | List of terrain craters | `[{"x": int, "radius": int}, ...]` |
| `tanks` | dict | Tank positions & HP | `{id: {x, y, hp}}`, (`0 <= hp <= 100`) |
| `active_turn` | str | Active player for current turn | `"Player_1"` or `"Player_2"` |
| `turn_deadline` | float | Turn expiration timestamp | `time.monotonic() + 30.0` |
| `move_used` | bool | Whether optional move was used this turn | `true` or `false` |
| `round` | int | Current match round | 1–20 |
| `final_scores` | dict | Final HP and completed rounds | `{"Player_1_hp": int, "Player_2_hp": int, "rounds": int}` |

### 2.2 Race Condition Prevention
---
- **Serialized Single-Thread Execution :** Because all socket reads, turn timer checks, and state transitions execute on a single thread within the `selectors` event loop, two handlers can never access or mutate shared state (`board_matrix`, `tanks`, `active_turn`, `round`) concurrently.
- **Atomic Run-to-Completion Handlers :** Each extracted JSON frame is validated, applied to game state, and broadcast to all clients before the event loop processes the next frame or socket event, eliminating read-modify-write and Time-of-Check to Time-of-Use race conditions.
- **Snapshot Iteration on Broadcasts :** Broadcast loops iterate over a static copy of connected clients (`list(clients.items())`) and defer socket removal until after the broadcast finishes, preventing `RuntimeError` dictionary mutation hazards if a client disconnects mid-broadcast.

### 2.3 Server-Side State Safety : Single-Threaded Event-Driven Isolation (`selectors`)
---

By designing the server around a single-threaded `selectors` event loop, the server achieves Event-Driven State Isolation:

1. **Critical Section per Event :**  
   - Every callback invoked by the event loop (`accept_wrapper`, `service_connection`, and `check_turn_timeout`) executes on the single main thread. When a complete `\n`-delimited JSON frame is popped from `session.stream_buffer`, the server synchronously executes the following before returning control to `sel.select()` :
      1) Parse JSON
      2) Validate Turn and Bounds
      3) Mutate Board/HP/Scores
      4) Transition FSM
      5) Broadcast `STATE_UPDATE`
2. **Isolation of Per-Client Stream Buffer :**  
   - Each `ClientSession` maintains an independent `stream_buffer = bytearray()`. If `Player_1` sends a fragmented packet (e.g., first half of a `FIRE` JSON string without `\n`), the event loop appends those bytes to `clients["Player_1"].stream_buffer` and immediately returns to `sel.select()` without blocking `Player_2` or delaying the turn timer.
3. **Deterministic Boundary Race Resolution :**
   - **Race 1 : `FIRE` Packet vs. 30-Second Turn Timer Expiration :**
     - In the event loop, readable socket events returned by `sel.select(timeout)` are serviced first, followed immediately by the monotonic clock check. If `Player_1`'s `FIRE` frame arrived before `select()` returned, `FIRE` is processed first, `active_turn` flips to `"Player_2"`, and `turn_deadline` resets to `now + 30.0`, so the subsequent clock check sees `clock < turn_deadline` and does not trigger a false timeout. Conversely, if the timeout already fired on the prior loop iteration, `active_turn` is already `"Player_2"`, so `Player_1`'s late `FIRE` frame is deterministically rejected with `ERROR ("OUT_OF_TURN")`.
   - **Race 2 : Simultaneous Out-of-Turn Spam During Active Turn :**
     - If `Player_2` transmits `MOVE` or `FIRE` while `active_turn == "Player_1"`, the server validates `frame["player_id"] != self.active_turn` prior to touching `board_matrix`, `tanks`, `move_used`, or `turn_deadline`, and sends `ERROR ("OUT_OF_TURN", retry_allowed=False)` only to `Player_2`.
   - **Race 3 : Client Disconnect Mid-Broadcast (`BrokenPipeError` / `ConnectionResetError`) :**
     - When broadcasting `STATE_UPDATE` or `GAME_OVER` across `list(self.clients.items())`, each socket `sendall()` is isolated in its own `try/except OSError` block. Failed client IDs are appended to a `disconnected_ids` list and processed *after* the broadcast loop finishes, preventing dictionary-mutation-during-iteration errors and guaranteeing the surviving player receives the resulting `GAME_OVER (reason="DISCONNECT")` frame.