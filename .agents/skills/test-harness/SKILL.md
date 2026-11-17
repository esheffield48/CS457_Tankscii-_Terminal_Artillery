---
name: test-harness
description: >-
  Use this skill when writing, running, or expanding the automated unit and
  integration test suite for Tankscii to verify CLI argument/port validation,
  DNS socket.gaierror handling, TCP stream fragmentation, coalescing, 0-byte
  EOF, RST/BrokenPipe recovery, schema fuzzing, and FSM lifecycle progression.
---

# Automated Test Suite & Protocol Verification Harness

Follow these requirements from `docs/ai_prompts.md` and Sprint 3 rubric criteria whenever creating or executing tests in `tests/`.

## 1. Core Testing Principles

- **No External Network Mocks**: Do not use third-party mocking libraries. Use `socket.socketpair()`, `unittest.mock.patch` (stdlib) for `socket.gethostbyname` / `time.monotonic`, or in-memory `ClientSession` byte buffers for fast, deterministic, CI-ready execution with `pytest` or `unittest`.
- **Non-Interactive**: All tests must run deterministically without human input or wall-clock `time.sleep(30)` delays.

## 2. Mandatory Test Categories

### Category 1: CLI Argument Parsing & DNS Resolution (`tests/test_cli_dns.py`)
1. **CLI Flags & Port Validation (`1024–65535`)**:
   - Verify `client.py` requires `-i` and `-p`, supports `-h`, and rejects ports `< 1024` or `> 65535` or non-integers with a clean usage error.
   - Verify `server.py` requires `-p`, supports `-h`, and rejects ports `< 1024` or `> 65535`.
2. **Graceful DNS Failure (`socket.gaierror`)**:
   - Simulate `socket.gethostbyname()` raising `socket.gaierror` in `client.py` and verify it prints the user-friendly DNS troubleshooting message (`[ERROR] Unable to resolve DNS hostname...`), does not print a raw Python traceback, and exits with status code `1`.

### Category 2: Stream Accumulator & Delimiter Tests (`tests/test_framing.py`)
1. **Byte-by-Byte Chunk Fragmentation**: Feed a valid UTF-8 JSON frame into `stream_buffer` (`bytearray`) 1 byte at a time and assert no message is emitted or fails until `b"\n"` arrives.
2. **Back-to-Back Message Coalescing**: Concatenate 3 distinct JSON frames (`b"{msg1}\n{msg2}\n{msg3}\n"`) into one read and assert all 3 objects are extracted in exact FIFO order.
3. **Unterminated Fragment Retention**: Feed partial frame bytes without `b"\n"`, assert 0 frames extracted and bytes remain in `stream_buffer`, then feed the remaining bytes ending in `b"\n"` and assert clean extraction.

### Category 3: Transport Exception, EOF & 60s Reconnection Resilience (`tests/test_framing.py` & `tests/test_server_fsm.py`)
1. **0-Byte EOF Detection & 60s Grace Period**: Simulate a peer closing its socket (`recv(4096)` returning `b""`) in both `WAITING_FOR_PLAYERS` (resets lobby) and `PLAYER_TURN` (marks player `TEMPORARILY_DISCONNECTED` for 60s, notifies survivor via `PLAYER_DISCONNECTED`, and awards `GAME_OVER (reason="DISCONNECT")` if 60s expires).
2. **Reconnection Handshake & `STATE_RESUME`**: Simulate a mid-match socket drop, reconnect within 60s using the player's nickname/alias or ID, and verify both clients receive `STATE_RESUME` and continue the match seamlessly.
3. **Peer RST & Mid-Broadcast BrokenPipe Recovery**: Simulate `ConnectionResetError` on `recv()` and `BrokenPipeError` during snapshot broadcast `sendall()`. Verify the server catches the exception, preserves match state for the 60s grace period, and transitions to `GAME_OVER (reason="DISCONNECT")` and `CLEANUP` when the grace period expires.

### Category 4: Schema, Turn & Numerical Boundary Fuzzing (`tests/test_protocol.py`)
1. **Out-of-Bounds Parameters**: Assert `angle = -10`, `angle = 220`, `power = 0`, `power = 150`, `distance = 9`, and target `x` outside `0..79` return `ERROR ("INVALID_PARAMS", retry_allowed=True)`.
2. **Duplicate Move in Same Turn**: Assert a second `MOVE` when `move_used == True` returns `ERROR ("MOVE_ALREADY_USED", retry_allowed=True)`.
3. **Out-of-Turn Rejection**: Assert commands from the waiting player return `ERROR ("OUT_OF_TURN", retry_allowed=False)` without changing `active_turn` or `turn_deadline`.
4. **Full Lobby Rejection**: Assert a 3rd client connecting while `2/2` slots are active and connected receives `ERROR ("LOBBY_FULL", retry_allowed=False)` and is unregistered/closed without disturbing the match.
5. **Malformed JSON**: Assert invalid JSON strings return `ERROR ("MALFORMED_FRAME")`.

### Category 5: FSM State Progression & Timer Verification (`tests/test_server_fsm.py`)
1. **Complete Match Lifecycle**: Verify `CONNECT` -> `LOBBY_WAIT` -> `GAME_START` -> `MOVE` (`STATE_UPDATE`) -> `FIRE` (`STATE_UPDATE` + `TURN_START`) -> `GAME_OVER` -> `CLEANUP` -> `WAITING_FOR_PLAYERS`.
2. **60-Second Turn Timeout & Boundary Race**: Advance monotonic clock past `turn_deadline` and verify `ERROR ("TIMEOUT", retry_allowed=False)` to the delinquent player plus `STATE_UPDATE (action="TIMEOUT", time_remaining=60)` switching `active_turn`; also verify a readable `FIRE` arriving on the same `select()` cycle is serviced before the clock check.
3. **20-Round Limit Stalemate**: Simulate 20 rounds with both tanks `HP > 0` and verify final `STATE_UPDATE` followed by `GAME_OVER (winner="DRAW", reason="ROUND_LIMIT")`.
4. **Mutual Destruction**: Simulate a shot reducing both tanks to `0 HP` on the same turn and verify `GAME_OVER (winner="DRAW", reason="HP_ELIMINATION")`.
