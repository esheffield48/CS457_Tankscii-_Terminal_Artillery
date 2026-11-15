# CS457_Tankscii-_Terminal_Artillery

**Tankscii : Terminal Artillery** is a Python 3 CLI turn-based tactical artillery game operating over IPv4 TCP sockets across a Cisco Modeling Labs (CML) multi-subnet network with a custom newline-delimited JSON application-layer protocol.

---

## Requirements

- **Python 3.10+** (uses only the Python 3 standard library — zero external dependencies or `pip install` required).

---

## Quick Start (Running Tankscii)

You will need **three terminal windows**: one for the authoritative server and one for each of the two players.

### 1. Start the Game Server (`server.py`)
In Terminal 1, launch the server on a valid port (`1024–65535`). The server binds to `0.0.0.0`:

```bash
python server.py -p 8080
```
*(On Linux/CML nodes, use `python3 server.py -p 8080`)*

- View server CLI help:
  ```bash
  python server.py -h
  ```
- Stop the server at any time with `Ctrl + C`.

### 2. Connect Player 1 (`client.py`)
In Terminal 2, connect to the server using its hostname (e.g., `server.sheffield.edu` on CML) or IP address (`127.0.0.1` for local testing):

```bash
python client.py -i 127.0.0.1 -p 8080
```
- You will be prompted to enter a nickname (`1–16` characters, or press `Enter` for the default `CobraKing`), or pass `-n <nickname>` directly on the command line.

### 3. Connect Player 2 (`client.py`)
In Terminal 3, connect the second player:

```bash
python client.py -i 127.0.0.1 -p 8080
```
- Enter a nickname (or press `Enter` for the default `Thunderbolt`). Once both players join, the tactical briefing and `80x24` ASCII battlefield launch automatically.

---

## In-Game Commands

On your turn (`Gunner awaiting orders (60s remaining)`), enter commands at the prompt:

| Command | Syntax | Description |
| :--- | :--- | :--- |
| **`MOVE`** | `MOVE <LEFT\|RIGHT> <1-5>` | Optional repositioning (at most **1 move per turn** before firing). Example: `MOVE RIGHT 3` |
| **`FIRE`** | `FIRE <0-180> <1-100>` | Launch projectile and end turn (`0–89` aims forward toward enemy, `90` straight up, `91–180` backward; power `1–100`). Example: `FIRE 45 75` |
| **`FORFEIT`** | `FORFEIT` | Surrender the match immediately. |
| **`QUIT`** | `QUIT` | Disconnect from the client. |
| **`HELP`** | `HELP` | Display the in-game command syntax summary. |

### Reconnection Support (60-Second Grace Period)
If a client disconnects unexpectedly (`Ctrl + C`, terminal closure, or network drop) during an active match, the server holds the match state for **60 seconds** (`TEMPORARILY_DISCONNECTED`). Re-run `client.py` and enter the **same nickname** (or player ID) within 60 seconds to automatically re-attach to your tank and resume the match via `STATE_RESUME`.

---

## Running the Automated Test Suite

Run all 28 unit and integration tests (CLI/DNS validation, stream framing, protocol schema fuzzing, 8-state FSM, and 60s disconnect/reconnection recovery):

```bash
python -m unittest discover -s tests -v
```

