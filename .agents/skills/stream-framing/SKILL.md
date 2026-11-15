---
name: stream-framing
description: >-
  Use this skill when implementing, modifying, or debugging the non-blocking TCP
  stream accumulator, newline-delimited UTF-8 JSON framing parser, 0-byte EOF
  detection, or low-level socket send/receive resilience for Tankscii.
---

# Stream Framing & Message Extraction Engine

Follow these exact rules from `docs/ai_prompts.md`, `docs/protocol_blueprint.md`, and `docs/concurrency_design.md` whenever building or modifying TCP socket framing logic.

## 1. Transport & Framing Contract

- **Transport**: Non-blocking stream-oriented IPv4 TCP sockets (`socket.AF_INET`, `socket.SOCK_STREAM`, `sock.setblocking(False)`).
- **Framing Delimiter**: Strictly Newline-Delimited UTF-8 JSON (`\n`, ASCII `0x0A`).
- **Outbound Serialization**: Serialize payloads using compact JSON (`json.dumps(frame, separators=(",", ":")) + "\n"`), encoded as UTF-8 bytes (`encode("utf-8")`), and transmitted via `sock.sendall()`.
- **No 1:1 Chunk Assumption**: Never assume one `sock.recv(4096)` equals one JSON frame. A single read may return a partial frame (**fragmentation**) or multiple concatenated frames (**coalescing**).

## 2. Receiver Accumulator Pattern (`ClientSession.stream_buffer`)

1. **Per-Session Bytearray Buffer**:
   - Each `ClientSession` maintains an independent `stream_buffer: bytearray = bytearray()`.
2. **Non-Blocking Read & 0-Byte EOF Check**:
   - Read chunks via `chunk = session.sock.recv(4096)`.
   - Immediately check `if not chunk:` (`b""`). A 0-byte return indicates peer TCP `FIN` closure (EOF). Raise `ConnectionClosedError` or invoke the EOF teardown path immediately so the socket is unregistered from `selectors` and closed—preventing 100% CPU infinite spin loops.
3. **Delimiter Extraction Loop**:
   - Append incoming bytes: `session.stream_buffer.extend(chunk)`.
   - Loop while `b"\n" in session.stream_buffer`:
     - Split on the first newline: `raw_line, _, remainder = session.stream_buffer.partition(b"\n")`.
     - Replace buffer with trailing partial bytes: `session.stream_buffer = bytearray(remainder)`.
     - Decode `raw_line.decode("utf-8")` and parse with `json.loads()`.
     - If UTF-8 decoding or `json.loads()` fails, do not crash—emit a structured `ERROR` frame with `error_code: "MALFORMED_FRAME"`.

## 3. Exception Resilience

- Wrap all `sock.recv(4096)` and `sock.sendall()` calls in `try/except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)`.
- Never allow a socket exception to bubble up and terminate the `selectors` event loop.
