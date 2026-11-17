---
name: client-cli-dns-ui
description: >-
  Use this skill when implementing or modifying the Tankscii client application
  (`client.py`), CLI argument parsing (`-h`, `-i`, `-p` and port range
  1024-65535 validation for both client and server), graceful DNS hostname
  resolution (`socket.gethostbyname` / `socket.gaierror`), or the 80x24 ASCII
  terminal UI renderer.
---

# Client CLI, DNS Resolution & Terminal UI

Follow these exact Sprint 3 and protocol specifications when building or modifying `client.py`, CLI argument parsers, or the ASCII terminal UI.

## 1. CLI Argument Parsing & Port Validation (`1024–65535`)

Both `client.py` and `server.py` must use `argparse` and validate port numbers before creating sockets:

### `client.py` CLI Contract
- **Usage**:
  - `python3 client.py -h`
  - `python3 client.py -i server.sheffield.edu -p 8080`
- **Required Flags**:
  - `-h` / `--help`: Displays help instructions and usage syntax.
  - `-i <hostname_or_ip>`: Server DNS hostname or IP address (`required=True`).
  - `-p <port>`: Server port (`required=True`, `type=int`, validated `1024 <= port <= 65535`).

### `server.py` CLI Contract
- **Usage**:
  - `python3 server.py -h`
  - `python3 server.py -p 8080`
- **Required Flags**:
  - `-h` / `--help`: Displays help instructions and usage syntax.
  - `-p <port>`: Listening port (`required=True`, `type=int`, validated `1024 <= port <= 65535`).
- **Bind Address**: Must bind to `("0.0.0.0", port)`.

### Port Validation Rule
If `<port>` is not an integer or is outside `1024..65535`, print a clear usage/error message via `parser.error(...)` (or `print` + `sys.exit(1)` / `sys.exit(2)`) without raising an unhandled Python traceback.

## 2. Graceful DNS Resolution (`socket.gaierror`)

In `client.py`, never hardcode IP addresses. Resolve the `-i` hostname explicitly using `socket.gethostbyname()` and catch `socket.gaierror`:

```python
try:
    server_ip = socket.gethostbyname(args.hostname)
except socket.gaierror:
    print(f"[ERROR] Unable to resolve DNS hostname '{args.hostname}'.")
    print(
        "Please check your Cisco router DNS configuration "
        "('ip dns server' / 'ip host') or verify domain spelling."
    )
    sys.exit(1)
```

Also catch `(ConnectionRefusedError, TimeoutError, OSError)` during `sock.connect((server_ip, args.port))` so `client.py` always exits cleanly with a user-friendly message and exit code `1` if the server is unreachable.

## 3. Client Transport & Terminal UI (`80x24` ASCII Board)

1. **Stream Framing**:
   - `client.py` must use `socket.socket(socket.AF_INET, socket.SOCK_STREAM)` and the exact same `bytearray` newline-delimited (`\n`) stream accumulator as `server.py` (never assume one `recv()` equals one message).
2. **Handshake & Reconnection Flow**:
   - On connect, prompt for (or accept) a nickname (`1–16` chars, defaults `"CobraKing"` / `"Thunderbolt"`) and send `CONNECT` (`{"nickname": ..., "client_version": "1.0.0"}`).
   - On `LOBBY_WAIT`: Display `"Waiting for Player 2..."` (`payload["message"]`).
   - On `GAME_START`: Record `assigned_id`, `opponent_nickname`, initial `tanks`, `first_turn`, and render the initial `80x24` ASCII battlefield.
   - On `STATE_RESUME`: Restore `assigned_id`, `opponent_nickname`, `round`, `tanks`, `craters`, `active_turn`, `time_remaining`, and `move_used`, render the `[STATE_RESUME]` banner and `80x24` battlefield, and resume turn prompts.
3. **Turn & Action UI**:
   - Clearly display centered scoreboard HUD (`Gunner awaiting orders (60s remaining)` vs. `STANDBY — Enemy crew <Opponent> is aiming (60s remaining)`), current round (`Round XX/20`), and live HP bars (`<Nickname> [1> [##########] 100HP | <Opponent> <2] [##########] 100HP`).
   - When active (`next_turn == assigned_id`), allow the player to:
     - `MOVE <LEFT|RIGHT> <1-5>` (optional, once per turn)
     - `FIRE <angle 0-180> <power 1-100>` (forward-relative: `0–89` forward toward enemy)
     - `QUIT` / `FORFEIT` (sends `DISCONNECT` with `reason: "FORFEIT"` or `"USER_QUIT"` and closes socket cleanly)
   - On `STATE_UPDATE`: Animate `FIRE` projectile trajectories in place (`animate_fire_trajectory_in_place`), redraw clean `TURN_START` boards (`redraw_hud_and_board_in_place`), and display `PLAYER_DISCONNECTED` status when the server holds match state for 60s.
   - On `ERROR`: Display `[ERROR] (<error_code>): <detail>` and re-prompt if `retry_allowed` is `True`.
   - On `GAME_OVER`: Render the final match summary banner (`winner`, `reason`, `final_scores`) and close the socket cleanly.
