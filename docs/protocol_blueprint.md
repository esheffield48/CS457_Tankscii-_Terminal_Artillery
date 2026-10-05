# TANKSCII : Application-Layer Protocol Blueprint

**Author :** Ethan Sheffield  
**Date :** 2026-10-04
**Course :** CS 457 - Computer Networks  
**Target Server Domain :** `server.sheffield.edu`

## 1 Transport Layer & Packet Framing Mechanism

### 1.1 Transport Protocol : 
---
- **TCP**

### 1.2 Framing Mechanism :
---
- **Newline-Delimited JSON :** `\n` (`0x0A`)
- **Framing Rule :** Every JSON object is UTF-8 encoded and terminated by a newline character `\n` (`0x0A`).
- **Receiver Extraction Logic :**
  A receiver accumulates incoming bytes into a stream buffer and When a `\n` is encountered, the complete line is extracted and trailing data remains in the buffer. Once a message has been extracted from the stream buffer, it is deserialzed into a JSON object. 

## 2 Application Message Schema :

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
| `LOBBY_WAIT` | Server -> Client | Server notifies Client 1 that is is waiting for Player 2 to connect. |
| `GAME_START` | Server -> Clients | Server notifies both clients that the game has started, assisgns player roles, and spawns the map and tanks. |
| `MOVE` | Client -> Server | Active player submits move direction and distance value(optional). |
| `FIRE` | Client -> Server | Active player submits firing angle and power. |
| `STATE_UPDATE` | Server -> Clients | Server broadcasts updated map state, players health, and active player turn. |
| `ERROR` | Server -> Client | Server notifies client of out-of-turn move, invalid input values, or malformed messages. | 
| `DISCONNECT` | Client -> Server | Client notifies server of intentional departure/quit. |
| `GAME_OVER` | Server -> Clients | Server broadcasts final game outcome (Winner / Draw / Forfeit), number of rounds completed, and final health values. |

### 2.3 Message Type Payload Details :
---
### `CONNECT`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `nickname` | str | Player's nickname | 1–16 characters |
| `client_version` | str | Version string | SemVer format (e.g., `"1.0.0"`) |

- **Payload Example :** "payload":{"nickname":"SideQwest","client_version":"1.0.0"}

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
| `turn_duration` | int | Turn countdown limit in seconds | 30 |

- **Payload Example :** "payload":{"assigned_id":"Player_1","opponent_nickname":"LionTurtle","tanks":{"Player_1":{"x":10,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":100}},"first_turn":"Player_1","turn_duration":30}

### `MOVE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `direction` | str | Movement direction | `"LEFT"` or `"RIGHT"` |
| `distance` | int | Distance to travel | 1–5 |

- **Payload Example :** "payload":{"direction":"RIGHT","distance":3}

### `FIRE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `angle` | int | Launch angle in degrees | 0 <= angle <= 180 |
| `power` | int | Launch power percentage | 1 <= power <= 100 |

- **Payload Example :** "payload":{"angle":45,"power":75}

### `STATE_UPDATE`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `round` | int | Current match round | 1–20 |
| `action` | str | Triggering action | `"MOVE"` or `"FIRE"` or `"TIMEOUT"` |
| `actor_id` | str | Active player performing action | `"Player_1"` or `"Player_2"` |
| `tanks` | dict | Tank positions & HP | `{id: {x, y, hp}}` |
| `move_path` | list | Coordinates traversed during movement | `[[x, y], ...]` (`null` on `"FIRE"`) |
| `trajectory` | list | Path of the projectile | `[[x, y], ...]` (`null` on `"MOVE"`) |
| `impact` | dict | Impact point & result | `{"coord": [x, y], "result": "HIT" or "MISS" or "GROUND"}` (`null` on `"MOVE"`) |
| `damage_dealt` | int | HP damage inflicted on opponent | >= 0 (`0` on `"MOVE"`) |
| `craters` | list | List of terrain craters | `[{"x": int, "radius": int}, ...]` (empty `[]` on `"MOVE"`) |
| `next_turn` | str | Active player for next turn | `"Player_1"` or `"Player_2"` |
| `time_remaining` | int | Turn countdown timer in seconds | 1–30 |

- **Payload Example :** "payload":{"round":1,"action":"MOVE","actor_id":"Player_1","tanks":{"Player_1":{"x":13,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":100}},"move_path":[[10,6],[11,6],[12,6],[13,6]],"trajectory":null,"impact":null,"damage_dealt":0,"craters":[],"next_turn":"Player_1","time_remaining":24}

### `ERROR`

| JSON Key | Type | Description | Constraints |
| :--- | :--- | :--- | :--- |
| `error_code` | str | Error category | `"OUT_OF_TURN"` or `"INVALID_PARAMS"` or `"MOVE_ALREADY_USED"` or `"TIMEOUT"` or `"MALFORMED_FRAME"` |
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
{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1727000010,"payload":{"round":5,"action":"FIRE","actor_id":"Player_1","tanks":{"Player_1":{"x":13,"y":6,"hp":100},"Player_2":{"x":70,"y":6,"hp":0}},"move_path":null,"trajectory":[[13,6],[25,15],[45,18],[70,6]],"impact":{"coord":[70,6],"result":"HIT"},"damage_dealt":35,"craters":[{"x":70,"radius":2}],"next_turn":"Player_2","time_remaining":30}}\n{"msg_type":"GAME_OVER","player_id":"SERVER","timestamp":1727000012,"payload":{"winner":"Player_1","reason":"HP_ELIMINATION","final_scores":{"Player_1_hp":100,"Player_2_hp":0,"rounds":5},"message":"Player_1 destroyed Player_2"}}\n
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
    "damage_dealt": 35,
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

## 3 Connection Termination & Socket Lifecycle Management

### 3.1 Application-Layer Disconnect :
- **Client Action :** Transmits a structured `DISCONNECT` frame and calls `sock.close()` to initiate the TCP FIN 4-way handshake.
- **Server Action:** Receives `DISCONNECT`, sends a `GAME_OVER` packet declaring the opponent the winner by forfeit, releases socket resources, and closes the connection.

### 3.2 Abrupt Drop & Exception Handling:
- All low-level socket operations are wrapped in `try/except` blocks catching:
  - `ConnectionResetError` (`TCP RST`) : A peer host forcibly closed the connection or crashed.
  - `BrokenPipeError` (`EPIPE`) : The application attempts to `sock.send()` or `sendall()` to a socket whose remote end is already closed.
  - `TimeoutError` : (If a socket timeout is configured and the peer stops responding).
- When an exception is caught, the server logs the abrupt disconnection and error, sends a `GAME_OVER` frame to the remaining client with `"reason":"DISCONNECT"`, releases socket resources, and closes the connection.

### 3.3 0-Byte EOF Detection:
- Socket receive loops evaluate `if not data: break` on every receive cycle.
- After a user cleanly exits (e.g. types `/quit`, presses `Ctl+C`, or closes the terminal), calling `sock.close()`, 0 bytes (`b""`) is detected, the server logs the clean disconnection, sends a `GAME_OVER` frame to the remaining client with `"reason":"DISCONNECT"`, releases socket resources, and closes the connection.
