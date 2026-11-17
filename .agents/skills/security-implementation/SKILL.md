---
name: security-implementation
description: >-
  Use this skill when implementing or modifying security controls across
  server.py, client.py, tankscii/, or Cisco IOS Router R1 configurations to
  mitigate the 5 attack surfaces defined in docs/security_protocols.md.
---

# Security Implementation & Attack Surface Mitigations

Follow these exact rules from `docs/security_protocols.md` whenever implementing or updating security controls. Use **only the Python 3 standard library** (`ipaddress`, `hmac`, `hashlib`, `ssl`, `time`) and maintain full compatibility with both the **CML 3-subnet topology** and **localhost (`127.0.0.1`) testing**.

## 1. Attack Surface 1: Network Perimeter & Transport Layer (`server.py`, `client.py`, `framing.py`, `R1`)

1. **Trusted Subnet Allowlisting (`ipaddress`)**:
   - Validate `client_addr[0]` immediately upon `listen_sock.accept()` in `register_accepted_socket()` against `ALLOWED_SUBNETS`:
     - `127.0.0.0/8` (Localhost testing)
     - `192.168.10.0/24` (CML Subnet A — `es-Client1`)
     - `192.168.11.0/24` (CML Subnet B — `es-Client2`)
     - `192.168.20.0/24` (CML Subnet C — `es-Server`)
   - Log a `WARNING` and immediately close any socket originating outside `ALLOWED_SUBNETS` prior to selector registration.

2. **Opt-In Transport Layer Security (`-tls` / `--tls`)**:
   - Keep **cleartext TCP as the default** so Wireshark `.pcap` captures display human-readable UTF-8 JSON frames.
   - Support `-tls` / `--tls` in both `server.py` and `client.py` using `ssl.SSLContext`:
     - Server: `ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)` loading `cert.pem` / `key.pem` (SANs for `server.sheffield.edu`, `192.168.20.100`, `127.0.0.1`).
     - Client: `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` pinned to `cert.pem`.
     - Non-blocking `selectors` integration: catch `ssl.SSLWantReadError` and `ssl.SSLWantWriteError` alongside `BlockingIOError` in `read_available_frames()` and `send_frame_safely()`.
     - Support `SSLKEYLOGFILE` via `context.keylog_filename` when present in `os.environ`.

3. **Cisco IOS Router `R1` Extended ACL (`TANKSCII_SUBNET_ACL`)**:
   - Isolate Subnet A (`192.168.10.0/24`) and Subnet B (`192.168.11.0/24`) from direct peer-to-peer traffic and block privileged server ports (`< 1024`), while explicitly permitting **ICMP (`ping`)**, **DNS (`UDP/TCP 53`)**, **DHCP (`UDP 67/68`)**, and **Tankscii TCP ports (`1024–65535`)**.

## 2. Attack Surface 2: Non-Blocking TCP Stream Framing & Event Loop (`tankscii/framing.py`, `server.py`)

1. **`stream_buffer` 8 KB Ceiling (`MAX_BUFFER_BYTES = 8192`)**:
   - After appending each `recv(4096)` chunk to `session.stream_buffer`, check `if len(session.stream_buffer) > MAX_BUFFER_BYTES:`.
   - If exceeded before a `\n` (`0x0A`) delimiter arrives, send `ERROR ("MALFORMED_FRAME", retry_allowed=False)`, unregister the socket from `selectors`, and close the connection.

2. **Coalesced Command Flood Rate Limiting & Pre-Handshake Timeout**:
   - Cap frame extraction at `MAX_FRAMES_PER_READ = 8` frames per `EVENT_READ` cycle.
   - Disconnect clients exceeding `MAX_CONSECUTIVE_ERRORS = 15` invalid frames in a single turn.
   - Drop sockets in `unassigned_sessions` that fail to send a valid `CONNECT` within `HANDSHAKE_TIMEOUT_SECONDS = 15`.

## 3. Attack Surface 3: Session Management & 60-Second Reconnection Grace Window (`server.py`)

1. **Source-IP Reconnection Pinning (`session.addr[0]`)**:
   - During `_handle_reconnect_handshake()`, verify `reconnecting_session.addr[0] == disconnected_session.addr[0]`.
   - Reject cross-subnet reconnection hijack attempts (`192.168.11.2` attempting to claim `192.168.10.2`'s `TEMPORARILY_DISCONNECTED` tank) with `ERROR ("LOBBY_FULL", retry_allowed=False)` while preserving the disconnected player's grace timer.

2. **Socket-to-Identity Binding (`player_id` Anti-Spoofing)**:
   - Always resolve sender identity from the authenticated socket's `session.player_id` attribute and reject any frame where `frame["player_id"] != session.player_id`.

## 4. Attack Surface 4: Application Protocol Parser & Authoritative Physics (`tankscii/protocol.py`, `tankscii/physics.py`)

1. **Cleartext HMAC-SHA256 Authentication & 30s Replay Window**:
   - Read the HMAC key from `os.environ.get("TANKSCII_HMAC_SECRET")` (falling back to `DEFAULT_HMAC_SECRET` when unset so local/CML testing works with zero manual setup).
   - Compute and verify frame HMAC digests using `hmac.new(secret, canonical_bytes, hashlib.sha256).hexdigest()` and constant-time `hmac.compare_digest()`.
   - Enforce `abs(int(time.time()) - frame["timestamp"]) <= 30`.

2. **Strict JSON Type & Numerical Boundary Validation**:
   - Check `isinstance(value, int) and not isinstance(value, bool)` on all integer fields (`_is_strict_int`).
   - Reject `NaN`, `Infinity`, and `-Infinity` in `json.loads(..., parse_constant=...)` and trap `RecursionError` as `FrameDecodeError`.

3. **Server-Authoritative Physics Isolation**:
   - Never accept coordinates, trajectories, or damage values from client payloads; compute all movement, ballistic arcs, craters, and HP changes exclusively on the server.

## 5. Attack Surface 5: Terminal UI Renderer (`tankscii/protocol.py`, `tankscii/renderer.py`)

1. **Printable ASCII Nickname Sanitization**:
   - In `validate_connect_payload()`, verify every character `ch` in `nickname` satisfies `32 <= ord(ch) <= 126`, rejecting all ASCII control bytes (`0x00–0x1F`, `\x1b` ESC, `\r`, `\n`, `\b`, `0x7F` DEL) with `ERROR ("INVALID_PARAMS", retry_allowed=True)`.
