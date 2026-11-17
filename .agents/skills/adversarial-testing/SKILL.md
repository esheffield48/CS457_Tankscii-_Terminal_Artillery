---
name: adversarial-testing
description: >-
  Use this skill when writing, running, or expanding adversarial, red-team, and
  fuzzing security tests (e.g. tests/test_security.py) to verify mitigations
  across all 5 attack surfaces in docs/security_protocols.md.
---

# Adversarial Security Testing & Protocol Fuzzing Harness

Follow these requirements from `docs/security_protocols.md` when creating or running adversarial security tests in `tests/`.

## 1. Core Adversarial Testing Principles

- **Deterministic In-Memory Execution**: Use `InMemorySocket` and `InMemorySelector` from `tests/__init__.py` alongside controllable monotonic/wall-clock functions so all security tests execute deterministically in milliseconds without external third-party libraries.
- **Zero Server Crash Tolerance**: No malformed byte stream, forged header, or socket error may raise an unhandled exception in `server.run_event_loop_once()` or corrupt an active match.

## 2. Mandatory Adversarial Test Cases (`tests/test_security.py`)

### Attack Surface 1: Network Perimeter & Transport
1. **Foreign Subnet Intrusion Rejection**:
   - Simulate `register_accepted_socket()` with a foreign peer IP (e.g., `("10.99.99.5", 52000)` or `("172.16.0.9", 52000)`).
   - Assert the socket is immediately closed and is **not** added to `unassigned_sessions` or registered in `selector`.
2. **Opt-In TLS Flag Parsing & Handshake Resilience**:
   - Verify `-tls` / `--tls` CLI flag parsing in `server.py` and `client.py`, and verify non-blocking `ssl.SSLWantReadError` / `ssl.SSLWantWriteError` handling does not drop healthy sessions.

### Attack Surface 2: TCP Stream Framing & Event Loop Availability
3. **Unbounded `stream_buffer` Memory DoS (`> 8 KB` Without `\n`)**:
   - Feed `9000` bytes without a `\n` (`0x0A`) delimiter into a connected client socket.
   - Assert the server responds with `ERROR ("MALFORMED_FRAME", retry_allowed=False)`, unregisters the socket, and closes the connection cleanly.
4. **Coalesced Command Flood & Error Threshold**:
   - Pipeline a single TCP read containing `20` consecutive invalid/out-of-turn JSON frames (`b"{frame}\n" * 20`).
   - Assert per-read frame rate limiting (`MAX_FRAMES_PER_READ = 8`) and consecutive error threshold protection keep the event loop responsive.
5. **Slowloris Pre-Handshake Timeout (`15s`)**:
   - Accept a socket from an allowed subnet (`192.168.10.2`), send no `CONNECT` frame, advance the clock by `16.0s`, and assert the idle socket is automatically unregistered and closed.

### Attack Surface 3: Session Management & 60s Reconnection Window
6. **Cross-Subnet Reconnection Hijack Attempt**:
   - Start a match between `Player_1` (`192.168.10.2`, `"CobraKing"`) and `Player_2` (`192.168.11.2`, `"Thunderbolt"`).
   - Drop `Player_1`'s socket so `Player_1` enters `TEMPORARILY_DISCONNECTED` (60s grace window).
   - Connect a new socket from Subnet B (`192.168.11.2`) sending `CONNECT {"nickname": "CobraKing"}`.
   - Assert the server rejects the hijack attempt with `ERROR ("LOBBY_FULL")`, closes the attacker's socket, and keeps `Player_1`'s slot reserved so the real `192.168.10.2` client can still reconnect and receive `STATE_RESUME`.
7. **Cross-Player Header `player_id` Spoofing**:
   - During `Player_1`'s turn, transmit a `FIRE` or `MOVE` frame over `Player_2`'s socket with `"player_id": "Player_1"` in the JSON header.
   - Assert the server rejects the spoofed frame (`ERROR`) without mutating `tanks`, `move_used`, or `active_turn`.

### Attack Surface 4: Protocol Parser & Physics Engine
8. **HMAC Tampering & Replay Window Rejection**:
   - Mutate `"angle"` or `"power"` in a signed frame without updating `"hmac"`, or submit a frame with a stale `timestamp` (`now - 120`), and assert rejection with `ERROR ("MALFORMED_FRAME")`.
9. **JSON Type Confusion & Parser Fuzzing (`bool`, `NaN`, `Infinity`, Deep Nesting)**:
   - Feed `{"direction": "RIGHT", "distance": true}`, `{"angle": NaN, "power": 50}`, `{"angle": Infinity, "power": 50}`, and deeply nested JSON (`"[" * 1500 + "]" * 1500`) and assert every case returns a structured `ERROR` frame without raising an unhandled exception.

### Attack Surface 5: Terminal UI Renderer
10. **ANSI Escape & Control-Character Nickname Injection**:
    - Send `CONNECT` payloads with `"nickname": "Hack\u001b[2J"`, `"nickname": "Spoof\r100HP"`, and `"nickname": "Bell\u0007"`.
    - Assert `validate_connect_payload()` rejects all control/escape characters with `ERROR ("INVALID_PARAMS", retry_allowed=True)`.
