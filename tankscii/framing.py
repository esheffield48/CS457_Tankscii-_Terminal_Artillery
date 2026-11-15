"""Low-level TCP stream framing, delimiter extraction, and socket I/O helpers.

This module implements the newline-delimited UTF-8 JSON framing layer (`\\n`,
0x0A) over non-blocking TCP stream sockets (`socket.AF_INET`, `socket.SOCK_STREAM`).
Because TCP is a byte-stream protocol with no inherent message boundaries, a
single `recv(4096)` call may yield a partial JSON object (fragmentation) or
multiple concatenated JSON objects (coalescing). Each client session therefore
accumulates incoming bytes in a persistent `bytearray` and extracts complete
frames only when a `\\n` delimiter is encountered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import socket
from typing import Any

from tankscii import RECV_BUFFER_SIZE


class ConnectionClosedError(Exception):
    """Raised when a peer socket returns 0 bytes (EOF / TCP FIN) or drops."""


class FrameDecodeError(Exception):
    """Raised when a raw newline-delimited frame fails UTF-8 or JSON decoding."""


@dataclass
class ClientSession:
    """Per-connection state container attached to `selectors` registration data.

    Attributes:
        sock: Non-blocking IPv4 TCP client socket.
        addr: Remote `(ip_address, port)` tuple of the connected client.
        stream_buffer: Persistent accumulator for incomplete incoming TCP bytes.
        player_id: Assigned canonical role (`"Player_1"` or `"Player_2"`), or None
            before `CONNECT` handshake completes.
        nickname: Validated player display name (1-16 characters).
    """

    sock: socket.socket
    addr: tuple[str, int]
    stream_buffer: bytearray = field(default_factory=bytearray)
    player_id: str | None = None
    nickname: str = ""


def serialize_frame(message: dict[str, Any]) -> bytes:
    """Serialize a message dictionary to compact UTF-8 JSON terminated by `\\n`.

    Args:
        message: Application-layer envelope dictionary.

    Returns:
        UTF-8 encoded bytes ending with a single newline (`0x0A`) delimiter.
    """
    compact_json = json.dumps(message, separators=(",", ":"))
    return (compact_json + "\n").encode("utf-8")


def append_and_extract_lines(
    stream_buffer: bytearray, incoming_chunk: bytes
) -> list[bytes]:
    """Append incoming TCP bytes and extract all complete `\\n`-delimited lines.

    Partitioning on `b"\\n"` in a loop handles both TCP stream fragmentation
    (retaining trailing partial bytes in `stream_buffer` until the next read)
    and TCP packet coalescing (extracting multiple back-to-back frames from a
    single `recv()` chunk).

    Args:
        stream_buffer: Mutable per-session `bytearray` accumulator.
        incoming_chunk: Newly read raw bytes from the TCP socket.

    Returns:
        A list of complete raw byte lines (excluding the trailing `\\n`).
    """
    if incoming_chunk:
        stream_buffer.extend(incoming_chunk)

    complete_lines: list[bytes] = []
    # Partition on the first newline delimiter until no full frame remains.
    while b"\n" in stream_buffer:
        raw_line, _, remainder = stream_buffer.partition(b"\n")
        stream_buffer[:] = remainder
        complete_lines.append(bytes(raw_line))

    return complete_lines


def decode_json_line(raw_line: bytes) -> dict[str, Any]:
    """Decode a single raw byte line as UTF-8 and parse it into a JSON object.

    Args:
        raw_line: A single newline-stripped frame of bytes.

    Returns:
        Parsed dictionary representing the JSON message envelope.

    Raises:
        FrameDecodeError: If the line is empty, contains invalid UTF-8 bytes,
            has invalid JSON syntax, or does not decode to a JSON object (`dict`).
    """
    if not raw_line or not raw_line.strip():
        raise FrameDecodeError("Empty frame received.")

    try:
        decoded_text = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameDecodeError(f"Invalid UTF-8 encoding in frame: {exc}") from exc

    try:
        parsed_object = json.loads(decoded_text)
    except json.JSONDecodeError as exc:
        raise FrameDecodeError(f"Malformed JSON syntax in frame: {exc}") from exc

    if not isinstance(parsed_object, dict):
        raise FrameDecodeError("Frame root must be a JSON object (dict).")

    return parsed_object


def receive_and_extract_raw_lines(
    sock: socket.socket,
    stream_buffer: bytearray,
    buffer_size: int = RECV_BUFFER_SIZE,
) -> list[bytes]:
    """Read available bytes from a non-blocking TCP socket and extract frames.

    Immediately checks for a 0-byte return (`b""`) from `sock.recv()` to detect
    peer TCP `FIN` closure (EOF). Callers must unregister the socket from
    `selectors` upon catching `ConnectionClosedError` to prevent 100% CPU
    busy-wait spin loops on a readable closed socket.

    Args:
        sock: Non-blocking TCP socket to read from.
        stream_buffer: Mutable per-session `bytearray` accumulator.
        buffer_size: Maximum bytes to read in a single `recv()` call.

    Returns:
        List of complete `\\n`-delimited raw byte frames extracted so far.

    Raises:
        ConnectionClosedError: On 0-byte EOF or fatal socket read errors
            (`ConnectionResetError`, `BrokenPipeError`, `TimeoutError`, `OSError`).
    """
    try:
        incoming_chunk = sock.recv(buffer_size)
    except BlockingIOError:
        # Non-blocking socket has no bytes ready right now; try again next tick.
        return []
    except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError) as exc:
        raise ConnectionClosedError(f"Socket read error: {exc}") from exc

    # A 0-byte read on a readable socket indicates peer TCP FIN (EOF).
    if not incoming_chunk:
        raise ConnectionClosedError("Peer closed connection (0-byte EOF).")

    return append_and_extract_lines(stream_buffer, incoming_chunk)


def send_frame_safely(sock: socket.socket, message: dict[str, Any]) -> bool:
    """Serialize and transmit a JSON frame over TCP without raising socket errors.

    Wraps `sock.sendall()` in a `try/except` block catching `ConnectionResetError`,
    `BrokenPipeError`, `TimeoutError`, and `OSError` so that an abrupt client drop
    never crashes the server or client event loop.

    Args:
        sock: Target TCP socket.
        message: Complete message envelope dictionary to serialize and send.

    Returns:
        `True` if the entire frame was written to the socket send buffer,
        `False` if a socket exception occurred.
    """
    encoded_frame = serialize_frame(message)
    try:
        sock.sendall(encoded_frame)
        return True
    except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
        return False
