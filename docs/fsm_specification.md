# TANKSCII : Game State Machine (FSM) Specification

**Author :** Ethan Sheffield  
**Date :** 2026-10-04  
**Course :** CS 457 - Computer Networks  
**Target Server Domain :** `server.sheffield.edu`  

---

```mermaid
stateDiagram-v2
    [*] --> INIT
    
    INIT --> WAITING_FOR_PLAYERS: Server Started & Listening
    
    WAITING_FOR_PLAYERS --> WAITING_FOR_PLAYERS: Client 1 Sends CONNECT (Send LOBBY_WAIT)/<br> Client 1 Disconnects / EOF Rule (Reset Lobby)
    WAITING_FOR_PLAYERS --> GAME_START: Client 2 Sends CONNECT (Assign Roles, Spawn Assets, Random Select First Turn)
    
    GAME_START --> PLAYER_TURN: Broadcast GAME_START
    
    PLAYER_TURN --> PLAYER_TURN: Out-of-Turn or Invalid Input (Send ERROR)/<br> Timer Expires (Send ERROR and STATE_UPDATE)
    
    PLAYER_TURN --> EVALUATE_MOVE: Active Player Sends<br> MOVE or FIRE

    EVALUATE_MOVE --> CHECK_WIN_DRAW: Simulate Trajectory & Damage (Send STATE_UPDATE)
    
    CHECK_WIN_DRAW --> PLAYER_TURN: Both Tanks Alive & Round <= 20 (Switch Turn & Reset 30s Timer)
    CHECK_WIN_DRAW --> GAME_OVER: Tank HP <= 0 or Round Limit > 20 (Broadcast GAME_OVER)
    
    PLAYER_TURN --> GAME_OVER: Client sends DISCONNECT / Abrupt Disconnect<br> (Send GAME_OVER)
    EVALUATE_MOVE --> GAME_OVER: Abrupt Disconnect<br> (Send GAME_OVER)
    
    GAME_OVER --> CLEANUP: Broadcast Results (Send GAME_OVER)
    
    CLEANUP --> WAITING_FOR_PLAYERS: Reset Match State for Next Lobby
    CLEANUP --> [*]: Server Shutdown (SIGINT)
```

---

## State Handling Logic & Edge Cases

### 1. Valid Moves
- **Tank Repositioning (`MOVE`) :** 
    - The active player may optionally submit one `MOVE` command per turn (`direction: "LEFT" | "RIGHT"`, `distance: 1–5`). The server validates distance limits and terrain bounds, updates tank coordinates, and broadcasts `STATE_UPDATE` (`action: "MOVE"`). The state machine loops back to `PLAYER_TURN` so the active player can aim and fire with their remaining time.
- **Projectile Launch (`FIRE`) :** 
    - The active player submits launch values (`angle: 0–180`, `power: 1–100`). State transitions to `EVALUATE_MOVE`, where the server simulates trajectory physics, creates terrain craters, and deducts damage (Direct Hit: 25 HP; Shrapnel Hit within 2 units: 10 HP). The server then broadcasts `STATE_UPDATE` (`action: "FIRE"`), and transitions to `CHECK_WIN_DRAW`.

### 2. Invalid Input & Turn Timeouts
- **Out-of-Turn Actions :** 
    - Commands from the inactive player are rejected immediately with `ERROR` (`error_code: "OUT_OF_TURN"`). The active player's turn and timer continue uninterrupted.
- **Illegal Parameters :** 
    - Out-of-bounds firing values, distance > 5, or multiple `MOVE` attempts in a single turn return `ERROR` (`error_code: "INVALID_PARAMS"` or `"MOVE_ALREADY_USED"`) with `retry_allowed: true`. The active player's turn and timer continue uninterrupted.
- **30-Second Turn Timeout :** 
    - If the countdown expires before a shot is fired, the server sends `ERROR` (`error_code: "TIMEOUT", retry_allowed: false`) to the player who did not fire. The server then broadcasts `STATE_UPDATE` (`action: "TIMEOUT", time_remaining: 30`) to hand off the turn to the opponent, and remains in `PLAYER_TURN`.
- **Full Lobby Rejection (`LOBBY_FULL`) :**
    - If a 3rd client connects while both player slots (`2/2`) are filled, the server sends `ERROR` (`error_code: "LOBBY_FULL", retry_allowed: false`), unregisters and closes that socket, and leaves the active FSM state untouched.

### 3. Unexpected Client Disconnections & Teardown
- **Exception Trapping :** 
    - Non-blocking socket `recv()` and `sendall()` handlers catch `ConnectionResetError` (TCP RST), `BrokenPipeError` (EPIPE during broadcast), `TimeoutError`, and `OSError` without crashing the server process.
- **0-Byte EOF Detection :** 
    - Receive handlers evaluate `if not chunk:` (`b""`) to detect TCP FIN teardowns and avoid 100% CPU infinite spin loops.
- **Lobby Drop (`WAITING_FOR_PLAYERS`) :** 
    - If Client 1 departs before Client 2 joins, the server unregisters (`sel.unregister(sock)`) and closes the socket, resetting the lobby state in `WAITING_FOR_PLAYERS`.
- **Mid-Game Drop (`PLAYER_TURN`, `EVALUATE_MOVE`) :** 
    - An abrupt drop or `DISCONNECT` frame immediately routes to `GAME_OVER`. The server declares the surviving player the victor (`reason: "FORFEIT"` for graceful surrender, `reason: "DISCONNECT"` for abrupt drop/RST/EOF). In `CLEANUP`, all client sockets are unregistered from `selectors` and closed (`sock.close()`), and the server resets match state to `WAITING_FOR_PLAYERS`.