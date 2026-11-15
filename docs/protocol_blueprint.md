# TANKSCII : Application-Layer Protocol Blueprint

**Author :** Ethan Sheffield  
**Date :** 2026-10-04
**Course :** CS 457 - Computer Networks  
**Target Server Domain :** `server.sheffield.edu`

## 1. Transport Layer & Packet Framing Mechanism

### 1.1 Transport Protocol : 
---
- **TCP**

### 1.2 Framing Mechanism :
---
- **Newline-Delimited JSON :** `\n` (`0x0A`)
- **Framing Rule :** Every JSON object is UTF-8 encoded and terminated by a newline character `\n` (`0x0A`).
- **Receiver Extraction Logic :**
  Each client session accumulates incoming non-blocking `recv(4096)` bytes into a per-client `stream_buffer` (`bytearray`). While a `\n` delimiter is present in `stream_buffer`, each complete UTF-8 line is extracted and deserialized into a JSON object, while any trailing partial frame bytes remain buffered until the next readable socket event.

### 1.3 CLI Invocation & DNS Resolution :
---
- **Server Invocation (`server.py`) :**
  - Parses `-h` (usage help) and `-p <port>` (required listening port in range `1024–65535`).
  - Binds its IPv4 TCP listening socket (`socket.AF_INET`, `socket.SOCK_STREAM`) to `0.0.0.0`.
- **Client Invocation (`client.py`) :**
  - Parses `-h` (usage help), `-i <hostname_or_ip>` (required server DNS hostname or IP), and `-p <port>` (required server port in range `1024–65535`).
- **Graceful DNS Resolution (`socket.gethostbyname`) :**
  - The client resolves `-i <hostname_or_ip>` via `socket.gethostbyname()` without hardcoded IP addresses.
  - Explicitly catches `socket.gaierror` if DNS resolution fails or the DNS server is unreachable, printing a user-friendly troubleshooting message (`'ip dns server'` / `'ip host'`) and exiting cleanly with status code `1` without a raw traceback.

## 2. Application Message Schema :

### 2.1 Message Framing Structure : 
---
### Frame Header Fields :
| Field Name | Description |
| :--- | :--- | 
| `msg_type` : | Identifies the operation.
| `player_id` : | Identifies who sent the frame.
| `timestamp` : | UNIX epoch timestamp.

### Frame Body :
| Field Name | Description |
| :--- | :--- | 
| `payload` : | Contains message specific parameters.

### 2.2 Message Types :
---
| Message (`msg_type`) | Direction | Description |
| :--- | :--- | :--- |
| `CONNECT` | Client -> Server | Client requests to join game room with player nickname. |
| `LOBBY_WAIT` | Server -> Client | Server notifies Client 1 that it is waiting for Player 2 to connect. |
| `GAME_START` | Server -> Clients | Server notifies both clients that the game has started, assigns player roles, and spawns the map and tanks. |
| `MOVE` | Client -> Server | Active player submits move direction and distance value (optional). |
| `FIRE` | Client -> Server | Active player submits firing angle and power. |
| `STATE_UPDATE` | Server -> Clients | Server broadcasts updated map state, players health, and active player turn. |
| `STATE_RESUME` | Server -> Clients | Server restores preserved board state, scores, and active turn to both clients when a temporarily disconnected player reconnects within the 60-second grace period. |
| `ERROR` | Server -> Client | Server notifies client of out-of-turn move, invalid input values, turn timeout, full lobby, or malformed messages. | 
| `DISCONNECT` | Client -> Server | Client notifies server of intentional departure/quit. |
| `GAME_OVER` | Server -> Clients | Server broadcasts final game outcome (Winner / Draw / Forfeit), number of rounds completed, and final health values. |

### 2.3 Message Type Payload Details :
---
### `CONNECT`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `nickname` | str | Player's nickname (or reconnect alias/ID) | 1–16 characters (defaults: `"CobraKing"` / `"Thunderbolt"`) |
| `client_version` | str | Version string | SemVer format (e.g., `"1.0.0"`) |

- **Payload Example :** "payload":{"nickname":"CobraKing","client_version":"1.0.0"}

### `LOBBY_WAIT`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `assigned_id` | str | Assigned player identifier | `"Player_1"` |
| `message` | str | Status notification text | Non-empty string |

- **Payload Example :** "payload":{"assigned_id":"Player_1","message":"Waiting for opponent..."}

### `GAME_START`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `assigned_id` | str | Assigned player identifier | `"Player_1"` or `"Player_2"` |
| `opponent_nickname` | str | Opponent's nickname | 1–16 characters |
| `tanks` | dict | Initial tank positions & HP | `{id: {x, y, hp}}`, (initial hp = 100) |
| `first_turn` | str | Starting turn | `"Player_1"` or `"Player_2"` |
| `turn_duration` | int | Turn countdown limit in seconds | 60 |

- **Payload Example :** "payload":{"assigned_id":"Player_1","opponent_nickname":"Thunderbolt","tanks":{"Player_1":{"x":10,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":100}},"first_turn":"Player_1","turn_duration":60}

### `MOVE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `direction` | str | Movement direction | `"LEFT"` or `"RIGHT"` |
| `distance` | int | Distance to travel | 1–5 |

- **Payload Example :** "payload":{"direction":"RIGHT","distance":3}

### `FIRE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `angle` | int | Forward-relative launch angle in degrees (`0–89` forward, `90` vertical, `91–180` backward) | 0 <= angle <= 180 |
| `power` | int | Launch power percentage | 1 <= power <= 100 |

- **Payload Example :** "payload":{"angle":45,"power":75}

### `STATE_UPDATE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `round` | int | Current match round | 1–20 |
| `action` | str | Triggering action | `"MOVE"`, `"FIRE"`, `"TIMEOUT"`, `"TURN_START"`, or `"PLAYER_DISCONNECTED"` |
| `actor_id` | str | Active player performing action | `"Player_1"` or `"Player_2"` |
| `tanks` | dict | Tank positions & HP | `{id: {x, y, hp}}` |
| `move_path` | list | Coordinates traversed during movement | `[[x, y], ...]` (`null` on `"FIRE"`, `"TURN_START"`, `"PLAYER_DISCONNECTED"`) |
| `trajectory` | list | Path of the projectile | `[[x, y], ...]` (`null` on `"MOVE"`, `"TURN_START"`, `"PLAYER_DISCONNECTED"`) |
| `impact` | dict | Impact point & result | `{"coord": [x, y], "result": "HIT" or "MISS" or "GROUND"}` (`null` on `"MOVE"`, `"TURN_START"`) |
| `damage_dealt` | int | HP damage inflicted on opponent | `0`, `10` (shrapnel), or `25` (direct hit) |
| `craters` | list | List of terrain craters | `[{"x": int, "radius": int}, ...]` |
| `next_turn` | str | Active player for next turn | `"Player_1"` or `"Player_2"` |
| `time_remaining` | int | Turn countdown timer in seconds | 1–60 |

- **Payload Example :** "payload":{"round":1,"action":"MOVE","actor_id":"Player_1","tanks":{"Player_1":{"x":13,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":100}},"move_path":[[10,6],[11,6],[12,6],[13,6]],"trajectory":null,"impact":null,"damage_dealt":0,"craters":[],"next_turn":"Player_1","time_remaining":54}

### `STATE_RESUME`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `assigned_id` | str | Recipient player identifier | `"Player_1"` or `"Player_2"` |
| `opponent_nickname` | str | Opponent's nickname | 1–16 characters |
| `round` | int | Current match round | 1–20 |
| `tanks` | dict | Preserved tank positions & HP | `{id: {x, y, hp}}` |
| `craters` | list | Preserved list of terrain craters | `[{"x": int, "radius": int}, ...]` |
| `active_turn` | str | Active player whose turn resumes | `"Player_1"` or `"Player_2"` |
| `time_remaining` | int | Restored turn countdown timer in seconds | 1–60 |
| `move_used` | bool | Whether optional move was already used this turn | `true` or `false` |
| `scores` | dict | Current HP and round tallies | `{"Player_1_hp": int, "Player_2_hp": int, "rounds": int}` |
| `message` | str | Resumption status notification | Non-empty string |

- **Payload Example :** "payload":{"assigned_id":"Player_1","opponent_nickname":"Thunderbolt","round":3,"tanks":{"Player_1":{"x":13,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":75}},"craters":[{"x":70,"radius":2}],"active_turn":"Player_1","time_remaining":48,"move_used":true,"scores":{"Player_1_hp":100,"Player_2_hp":75,"rounds":3},"message":"Armor Crewman 'CobraKing' reconnected. Match resumed!"}

### `ERROR`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `error_code` | str | Error category | `"OUT_OF_TURN"` or `"INVALID_PARAMS"` or `"MOVE_ALREADY_USED"` or `"TIMEOUT"` or `"LOBBY_FULL"` or `"MALFORMED_FRAME"` |
| `detail` | str | Human-readable explanation of rejection | Non-empty string |
| `retry_allowed` | bool | Whether client may retry within remaining turn time | `true` or `false` |

- **Payload Example :** "payload":{"error_code":"OUT_OF_TURN","detail":"Player_2's turn.","retry_allowed":false}

### `DISCONNECT`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `reason` | str | Departure or forfeit cause | `"FORFEIT"` or `"USER_QUIT"` |

- **Payload Example :** "payload":{"reason":"FORFEIT"}

### `GAME_OVER`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `winner` | str | Match victor | `"Player_1"` or `"Player_2"` or `"DRAW"` |
| `reason` | str | Termination reason | `"HP_ELIMINATION"` or `"ROUND_LIMIT"` or `"FORFEIT"` or `"DISCONNECT"` |
| `final_scores` | dict | Final HP and completed rounds | `{"Player_1_hp": int, "Player_2_hp": int, "rounds": int}` |
| `message` | str | Match outcome summary message | Non-empty string |

- **Payload Example :** "payload":{"winner":"Player_1","reason":"HP_ELIMINATION","final_scores":{"Player_1_hp":100,"Player_2_hp":0,"rounds":5},"message":"Player_1 destroyed Player_2"}

### 2.4 Message Examples :
---
### Frame Example :
- **Client Action : `MOVE`**
```text
{"msg_type":"MOVE","player_id":"Player_1","timestamp":1727000005,"payload":{"direction":"RIGHT","distance":3}}\n
```

### Continuous Stream Example :
- **Server Actions : `STATE_UPDATE` and `GAME_OVER`**
```text
{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1727000010,"payload":{"round":5,"action":"FIRE","actor_id":"Player_1","tanks":{"Player_1":{"x":13,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":0}},"move_path":null,"trajectory":[[13,6],[25,15],[45,18],[70,6]],"impact":{"coord":[70,6],"result":"HIT"},"damage_dealt":25,"craters":[{"x":70,"radius":2}],"next_turn":"Player_2","time_remaining":30}}\n{"msg_type":"GAME_OVER","player_id":"SERVER","timestamp":1727000012,"payload":{"winner":"Player_1","reason":"HP_ELIMINATION","final_scores":{"Player_1_hp":100,"Player_2_hp":0,"rounds":5},"message":"Player_1 destroyed Player_2"}}\n
```

### JSON Schema Examples :

- **Client Action : `FIRE`**
```json
{
  "msg_type": "FIRE",
  "player_id": "Player_1",
  "timestamp": 1727000008,
  "payload": {
    "angle": 45,
    "power": 75
  }
}
```

- **Server Action : `STATE_UPDATE`**
```json
{
  "msg_type": "STATE_UPDATE",
  "player_id": "SERVER",
  "timestamp": 1727000010,
  "payload": {
    "round": 5,
    "action": "FIRE",
    "actor_id": "Player_1",
    "tanks": {
      "Player_1": {
        "x": 13,
        "y": 6,
        "hp": 100
      },
      "Player_2": {
        "x": 70,
        "y": 6,
        "hp": 0
      }
    },
    "move_path": null,
    "trajectory": [
      [13, 6],
      [25, 15],
      [45, 18],
      [70, 6]
    ],
    "impact": {
      "coord": [70, 6],
      "result": "HIT"
    },
    "damage_dealt": 25,
    "craters": [
      {
        "x": 70,
        "radius": 2
      }
    ],
    "next_turn": "Player_2",
    "time_remaining": 30
  }
}
```

---

## 3. Connection Termination, State Preservation & Reconnection Management

### 3.1 Application-Layer Disconnect :
- **Client Action :** Transmits a structured `DISCONNECT` frame (`reason: "FORFEIT"` or `"USER_QUIT"`) and calls `sock.close()` to initiate the TCP FIN 4-way handshake.
- **Server Action (`FORFEIT`) :** Receives `DISCONNECT` (`reason: "FORFEIT"`), transitions to `GAME_OVER`, broadcasts a `GAME_OVER` frame (`reason: "FORFEIT"`) declaring the surviving opponent the winner, unregisters both sockets from `selectors` (`sel.unregister(sock)`), closes the connections (`sock.close()`), and resets match state in `CLEANUP` back to `WAITING_FOR_PLAYERS`.
- **Server Action (`USER_QUIT`) :** During an active match, `DISCONNECT` (`reason: "USER_QUIT"`) triggers the 60-second state preservation grace period (Section 3.4) so the player can reconnect if desired.

### 3.2 Abrupt Drop & Exception Handling :
- All low-level socket `recv()` and `sendall()` operations are wrapped in `try/except` blocks catching:
  - `ConnectionResetError` (`TCP RST`) : A peer host forcibly closed the connection or crashed.
  - `BrokenPipeError` (`EPIPE`) : The application attempts to `sock.send()` or `sendall()` to a socket whose remote end is already closed.
  - `TimeoutError` / `OSError` : Socket timeout or underlying OS network error.
- When an exception is caught during an active match, the server does **not** crash or immediately delete the game room; instead, it transitions the player's session to `"TEMPORARILY_DISCONNECTED"` and preserves match state for a 60-second reconnection grace period (Section 3.4).

### 3.3 0-Byte EOF Detection :
- Non-blocking socket receive handlers evaluate `if not chunk:` (`b""`) on every `EVENT_READ` cycle.
- If 0-byte EOF is detected in `WAITING_FOR_PLAYERS` before `Player_2` joins, the server unregisters and closes `Player_1`'s socket, clears `clients`, and remains in `WAITING_FOR_PLAYERS`.
- If 0-byte EOF is detected during an active match (`PLAYER_TURN` or `EVALUATE_MOVE`), the server unregisters and closes only the dropped socket, marks the player `"TEMPORARILY_DISCONNECTED"`, and enters the 60-second reconnection grace period.

### 3.4 State Preservation, Reconnection Handshake & `STATE_RESUME` :
- **State Preservation (`RECONNECT_GRACE_SECONDS = 60`) :** When a client socket drops during an active match, the server marks `ClientSession.connection_status = "TEMPORARILY_DISCONNECTED"`, sets `disconnect_deadline = time.monotonic() + 60.0`, saves `paused_turn_remaining`, holds the complete game state (`board_matrix`, `tanks`, `craters`, `final_scores`, `active_turn`, `round`, `move_used`), and notifies the connected opponent with `STATE_UPDATE` (`action: "PLAYER_DISCONNECTED"`).
- **Re-connection Handshake :** When the player re-runs `client.py` within 60 seconds and transmits `CONNECT` using their player nickname/alias or ID (`"Player_1"` / `"Player_2"`), the server recognizes the returning player, attaches the new socket to their active `ClientSession`, clears `disconnect_deadline`, and restores `turn_deadline`.
- **Resumption (`STATE_RESUME`) :** Both clients receive a `STATE_RESUME` frame containing the current board state (`tanks`, `craters`), `scores`, `round`, `active_turn`, `time_remaining`, and `move_used`, allowing the match to proceed seamlessly without state loss.
- **Grace Period Expiration :** If the 60-second grace period expires without reconnection, the server sends a `GAME_OVER` frame to the surviving client with `"reason": "DISCONNECT"`, unregisters and closes all match sockets in `CLEANUP`, and resets the lobby for the next match.
