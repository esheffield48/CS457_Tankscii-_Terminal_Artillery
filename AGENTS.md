# TANKSCII : Terminal Artillery — Architectural Rules & Engineering Constraints

You are developing **Tankscii : Terminal Artillery** in Python 3 for CS 457 (Computer Networks). Every file, function, socket handler, CLI entrypoint (`server.py` and `client.py`), and test in this repository must strictly obey these architectural specifications and non-negotiable constraints.

## 1. Authoritative Specification Sources of Truth

Always consult and obey these four specification documents in `docs/`:
1. [docs/protocol_blueprint.md](docs/protocol_blueprint.md): Transport framing, 9 canonical message schemas, and socket lifecycle management.
2. [docs/fsm_specification.md](docs/fsm_specification.md): Complete 8-state server Finite State Machine (`INIT` -> `WAITING_FOR_PLAYERS` -> `GAME_START` -> `PLAYER_TURN` -> `EVALUATE_MOVE` -> `CHECK_WIN_DRAW` -> `GAME_OVER` -> `CLEANUP`) and edge-case rules.
3. [docs/concurrency_design.md](docs/concurrency_design.md): Single-threaded non-blocking I/O multiplexing (`selectors.DefaultSelector`), 4-phase connection lifecycle, canonical state variables, and race condition isolation.
4. [docs/sequence_diagram.md](docs/sequence_diagram.md): End-to-end move validation, turn enforcement, synchronous `STATE_UPDATE` / `GAME_OVER` broadcast flows, and score synchronization.

---

## 2. Core System Scope & Authoritative Model

- **Target Domain & Topology**: 2 client nodes (`Player_1` and `Player_2`, `MAX_PLAYERS = 2`) communicating over IPv4 TCP stream sockets (`socket.AF_INET`, `socket.SOCK_STREAM`) with an authoritative game server (`server.sheffield.edu`).
- **Required Root Entrypoints**: `server.py` and `client.py` at the repository root, formatted cleanly to **PEP 8**, modular, with clear explanatory comments.
- **Single Source of Truth**: The server owns all match state:
  - `fsm_state`: `"INIT"`, `"WAITING_FOR_PLAYERS"`, `"GAME_START"`, `"PLAYER_TURN"`, `"EVALUATE_MOVE"`, `"CHECK_WIN_DRAW"`, `"GAME_OVER"`, `"CLEANUP"`
  - `clients`: `{player_id: ClientSession}` (max 2 players)
  - `board_matrix`: `80 x 24` 2D ASCII grid
  - `terrain_heights`: `list[int]` of length 80 (`0 <= y < 24`)
  - `craters`: `[{"x": int, "radius": int}, ...]`
  - `tanks`: `{"Player_1": {"x": int, "y": int, "hp": int}, "Player_2": {"x": int, "y": int, "hp": int}}` (`0 <= hp <= 100`, initial `hp = 100`, `Player_1` at `x=10`, `Player_2` at `x=70`)
  - `active_turn`: `"Player_1"` or `"Player_2"`
  - `turn_deadline`: `time.monotonic() + 30.0`
  - `move_used`: `bool` (at most 1 optional `MOVE` of `1–5` units per turn)
  - `round`: `int` (`1–20`)
  - `final_scores`: `{"Player_1_hp": int, "Player_2_hp": int, "rounds": int}`
- **Damage & Win/Draw Rules**:
  - **Direct Hit**: `25 HP` damage.
  - **Shrapnel Hit** (within 2 units of impact): `10 HP` damage.
  - **Victory**: Opponent HP reaches `0` while player HP `> 0` (`"HP_ELIMINATION"`), opponent sends `DISCONNECT` (`"FORFEIT"`), or opponent abruptly disconnects/EOF/RST (`"DISCONNECT"`).
  - **Draw**: Both tanks reach `0 HP` in the same turn (`"HP_ELIMINATION"`) OR both tanks remain alive when the 20-round limit expires (`"ROUND_LIMIT"`).

---

## 3. Sprint 3 Mandatory CLI, DNS & UI Requirements

1. **CLI Argument Parsing & Port Validation (`1024–65535`)**:
   - **`client.py`**: Must implement `-h` / `--help` (usage guidance), `-i <server_hostname_or_ip>` (required), and `-p <port>` (required). Example: `python3 client.py -i server.sheffield.edu -p 8080`.
   - **`server.py`**: Must implement `-h` / `--help` (usage guidance) and `-p <port>` (required), and **must bind its listening socket to `0.0.0.0`**. Example: `python3 server.py -p 8080`.
   - **Port Validation**: Both `client.py` and `server.py` must validate that `-p <port>` is an integer in the range **`1024–65535`**, displaying helpful usage syntax and exiting cleanly with a non-zero exit code when arguments are missing or out of bounds.

2. **Graceful DNS Resolution & `socket.gaierror` Handling (`client.py`)**:
   - Never hardcode IP addresses in `client.py`. Resolve the `-i` hostname via `socket.gethostbyname(args.hostname)`.
   - Explicitly catch `socket.gaierror`. Never allow `client.py` to crash with an unhandled exception or print a raw Python traceback on DNS failure.
   - On `socket.gaierror`, print a user-friendly error message advising the user to check their Cisco router DNS configuration (`'ip dns server'` / `'ip host'`) or domain spelling, and exit cleanly with `sys.exit(1)`.

3. **Client Terminal UI (`client.py`)**:
   - Render a clean, human-readable `80x24` ASCII game board (terrain, tanks, craters, projectile trajectory), live HP/round score summaries, and clear turn status (`"Your Turn"`, `"Waiting for Player 2..."`).
   - Use the exact same newline-delimited (`\n`) `bytearray` stream accumulator and JSON serialization on the client side as on the server side.

---

## 4. Non-Negotiable Transport & Concurrency Constraints

1. **Single-Threaded Non-Blocking Multiplexing (`selectors.DefaultSelector`)**:
   - Do **not** spawn OS threads per client. Multiplex the listening socket (`SO_REUSEADDR`, `bind(('0.0.0.0', port))`, `setblocking(False)`, `data=None`) and connected client sockets (`setblocking(False)`, `data=ClientSession`) on a single deterministic event loop thread.
   - Use dynamic selector timeouts: `timeout = None` in `WAITING_FOR_PLAYERS`; `timeout = max(0.0, turn_deadline - time.monotonic())` in `PLAYER_TURN`.
   - Always service readable socket events returned by `sel.select(timeout)` **before** checking `time.monotonic() >= turn_deadline`.

2. **Newline-Delimited UTF-8 JSON Framing (`\n`, `0x0A`)**:
   - Never assume a 1:1 relationship between `sock.recv(4096)` and application messages.
   - Every outbound frame must be serialized to compact UTF-8 JSON terminated by exactly one `\n`.
   - Both `server.py` and `client.py` must accumulate incoming `recv(4096)` bytes into a persistent `stream_buffer = bytearray()`, extracting complete frames in a `while b"\n" in stream_buffer:` loop and retaining trailing partial bytes.

3. **Mandatory 0-Byte EOF & Exception Trapping**:
   - Always check `if not chunk:` (`b""`) immediately after `recv(4096)` to detect TCP `FIN` closure and unregister the socket—preventing 100% CPU spin loops.
   - Wrap all low-level `recv()` and `sendall()` calls in `try/except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)` blocks. Socket errors must never crash the server event loop.
   - Broadcasts must iterate over a static snapshot `list(clients.items())`, recording failed client IDs in `disconnected_ids` to clean up *after* the broadcast loop completes.

4. **Strict Protocol Validation & Error Semantics**:
   - Every frame must contain header fields `"msg_type"` (`str`), `"player_id"` (`"Player_1" | "Player_2" | "SERVER"`), `"timestamp"` (`int`), and `"payload"` (`dict`).
   - Reject invalid or out-of-turn inputs with structured `ERROR` frames (`"OUT_OF_TURN"`, `"INVALID_PARAMS"`, `"MOVE_ALREADY_USED"`, `"TIMEOUT"`, `"LOBBY_FULL"`, `"MALFORMED_FRAME"`) with accurate `retry_allowed` booleans rather than raising unhandled exceptions.

---

## 5. General Coding Style, Modularity & Quality Standards

1. **Strict Modularity & Separation of Concerns**:
   - Do **not** write monolithic scripts that mix socket transport, protocol validation, physics calculations, and ASCII rendering in a single file or giant function.
   - Keep `server.py` and `client.py` as clean, focused top-level entrypoints (CLI argument parsing, event/socket loop orchestration, and high-level state dispatch) that import shared, reusable modules from a dedicated package (`tankscii/`):
     - `tankscii/framing.py`: Low-level TCP `bytearray` stream accumulator, `\n` delimiter extraction, 0-byte EOF detection, and safe socket send/receive wrappers.
     - `tankscii/protocol.py`: Message envelope builders, JSON serialization/deserialization, and strict schema/boundary validators for all 9 canonical message types.
     - `tankscii/physics.py`: `80x24` terrain generation, tank movement validation, ballistic trajectory simulation, crater deformation, and `25 HP` / `10 HP` damage calculation.
     - `tankscii/renderer.py`: Pure `80x24` ASCII battlefield, trajectory arc, scoreboard HUD, and turn status formatting for the terminal UI.
   - Each function and class must have a single, clear responsibility and remain small enough to unit test in isolation.

2. **PEP 8 Compliance & Naming Conventions**:
   - Strictly follow **PEP 8** formatting: 4-space indentation, clean imports grouped in standard order (stdlib, third-party [none], local), and readable line lengths (`<= 88–100` characters).
   - Use `snake_case` for functions, methods, and variables; `PascalCase` for classes (`ClientSession`, `TanksciiServer`); and `UPPER_SNAKE_CASE` for module-level constants (`MAX_PLAYERS = 2`, `RECV_BUFFER_SIZE = 4096`, `TURN_DURATION = 30.0`, `MIN_PORT = 1024`, `MAX_PORT = 65535`).
   - **Descriptive Variable & Verb-First Function Naming**:
     - Variables must have clear, descriptive noun phrases that convey their exact role and units (e.g., `terrain_heights`, `turn_deadline`, `disconnected_ids`, `remaining_seconds`—avoid cryptic single-letter or ambiguous names like `d`, `tmp`, or `val`).
     - Functions and methods must be named with a leading action verb (`extract_complete_frames`, `validate_fire_payload`, `simulate_ballistic_trajectory`, `broadcast_state_update`, `resolve_server_hostname`) and perform a single, specific task.

3. **Type Hints & Self-Documenting Interfaces**:
   - Include Python 3 type annotations (`from __future__ import annotations`, `dict[str, Any]`, `list[ int ]`, `bytearray`, `selectors.BaseSelector`) on all function and method signatures.
   - Prefer `dataclasses.dataclass` (e.g., `ClientSession`) for structured state containers over untyped tuples.

4. **Docstrings & Explanatory Networking Comments**:
   - Every module, class, and public function must have a concise docstring explaining its purpose, inputs, and return/error behavior.
   - Include clear inline explanatory comments at critical networking and state-machine boundaries (e.g., why `0-byte EOF` unregisters from `selectors`, how `b"\n"` partitioning resolves TCP fragmentation and coalescing, why readable events are serviced before `time.monotonic() >= turn_deadline`, and how snapshot iteration prevents dictionary mutation hazards during broadcasts).

5. **Python Standard Library Only (Zero External Dependencies)**:
   - Use **only** the Python 3 standard library (`socket`, `selectors`, `json`, `argparse`, `time`, `math`, `random`, `sys`, `dataclasses`, `typing`, `unittest`) for runtime code so `server.py` and `client.py` execute out-of-the-box on any CSU lab machine or Cisco Modeling Labs (CML) Linux node without `pip install`.
