---
name: security-review
description: >-
  Use this skill when performing a security code review or auditing server.py,
  client.py, tankscii/, or CML router configurations against the 5 attack
  surfaces in docs/security_protocols.md.
---

# Security Review & Vulnerability Audit Checklist

Use this structured checklist from `docs/security_protocols.md` when reviewing code or network configurations for Tankscii.

## 1. Attack Surface 1 Audit: Network Perimeter & Transport Layer
- [ ] **Subnet Allowlisting**: Does `register_accepted_socket()` validate `client_addr[0]` via `ipaddress` against `127.0.0.0/8`, `192.168.10.0/24`, `192.168.11.0/24`, and `192.168.20.0/24` before registering with `selectors`?
- [ ] **Default Cleartext & Opt-In `-tls`**: Is cleartext TCP preserved as the default for Wireshark grading captures, and does `-tls` / `--tls` handle `ssl.SSLWantReadError` / `ssl.SSLWantWriteError` in non-blocking mode without blocking the event loop?
- [ ] **Router `R1` ACL Compliance**: Does the Cisco IOS ACL on `es-R1` isolate Subnet A (`192.168.10.0/24`) and Subnet B (`192.168.11.0/24`) while still permitting zero-loss ICMP `ping`, UDP/TCP `53` DNS, DHCP (`67/68`), and Game Server TCP ports (`1024–65535`)?

## 2. Attack Surface 2 Audit: TCP Stream Framing & Event Loop Availability
- [ ] **`stream_buffer` Memory Cap**: Does `tankscii/framing.py` enforce `len(session.stream_buffer) <= 8192` (`MAX_BUFFER_BYTES`) prior to `\n` extraction so an unterminated stream cannot exhaust memory?
- [ ] **0-Byte EOF & Exception Trapping**: Is every `recv()` and `sendall()` wrapped in `try/except (ConnectionResetError, BrokenPipeError, TimeoutError, ssl.SSLError, OSError)`, and does `if not chunk:` (`b""`) immediately unregister the socket?
- [ ] **Event-Loop Fairness & Timeouts**: Are coalesced frame bursts capped (`MAX_FRAMES_PER_READ = 8`) and idle unauthenticated sockets dropped after `HANDSHAKE_TIMEOUT_SECONDS = 15`?

## 3. Attack Surface 3 Audit: Session Management & 60s Reconnection Window
- [ ] **Source-IP Reconnection Pinning**: Does `_handle_reconnect_handshake()` verify `reconnecting_session.addr[0] == disconnected_session.addr[0]` so an opponent on another subnet cannot hijack a `TEMPORARILY_DISCONNECTED` tank during the 60-second grace period?
- [ ] **Socket-to-Identity Binding**: Does `server.py` authenticate turn ownership and actions strictly against `session.player_id` bound to the socket descriptor rather than trusting the client-supplied JSON `"player_id"` header?

## 4. Attack Surface 4 Audit: Protocol Parser & Authoritative Physics
- [ ] **HMAC & Replay Protection**: Are HMAC-SHA256 digests verified with constant-time `hmac.compare_digest()` and checked against the $\pm 30\text{s}$ replay window?
- [ ] **Strict JSON Types**: Do all integer checks reject booleans (`not isinstance(val, bool)`), and does JSON deserialization reject `NaN`, `Infinity`, and deep recursion (`RecursionError`)?
- [ ] **Server-Authoritative State**: Are all tank coordinates, ballistic trajectories, craters, and HP deductions (`25 HP` / `10 HP`) computed exclusively on the server?

## 5. Attack Surface 5 Audit: Terminal UI & Log Sanitization
- [ ] **ANSI Escape & Control-Byte Filtering**: Does `validate_connect_payload()` enforce printable ASCII (`32 <= ord(ch) <= 126`) on `nickname` so raw ANSI sequences (`\x1b[2J`, `\r`, `\b`) can never corrupt the opponent's `80x24` terminal display or forge server log lines?
