"""Shared deterministic in-memory TCP socketpair and selector for unit testing.

Provides an in-memory implementation of connected stream sockets (`InMemorySocket`)
and a `selectors.BaseSelector`-compatible selector (`InMemorySelector`) that
faithfully model POSIX TCP stream semantics:
- Arbitrary chunk fragmentation and coalescing in `recv()` / `sendall()`.
- `BlockingIOError` when a non-blocking socket has no unread bytes and its peer is open.
- `EVENT_READ` wakeup and `b""` (0-byte EOF) return when the remote peer calls `close()`.
- Injectable `ConnectionResetError` (`TCP RST`) on `recv()` and `BrokenPipeError`
  (`EPIPE`) on `sendall()` for deterministic transport fault testing.
"""

from __future__ import annotations

from collections.abc import Mapping
import selectors
from typing import Any


class InMemorySocket:
    """In-memory stream socket endpoint modeling non-blocking IPv4 TCP behavior."""

    def __init__(self, fd_id: int) -> None:
        """Initialize an unconnected in-memory socket endpoint."""
        self._fd_id: int = fd_id
        self.peer: InMemorySocket | None = None
        self.recv_buffer: bytearray = bytearray()
        self.is_blocking: bool = True
        self.closed: bool = False
        self.peer_closed: bool = False
        self.pending_recv_exception: Exception | None = None
        self.pending_send_exception: Exception | None = None

    def fileno(self) -> int:
        """Return a pseudo file descriptor integer for selector keys."""
        return self._fd_id

    def setblocking(self, flag: bool) -> None:
        """Configure blocking or non-blocking mode on this endpoint."""
        self.is_blocking = bool(flag)

    def recv(self, bufsize: int) -> bytes:
        """Read up to `bufsize` bytes from the stream or return b'' on peer EOF."""
        if self.closed:
            raise OSError("Socket is closed.")

        if self.pending_recv_exception is not None:
            exc = self.pending_recv_exception
            self.pending_recv_exception = None
            raise exc

        if self.recv_buffer:
            chunk = bytes(self.recv_buffer[:bufsize])
            del self.recv_buffer[:bufsize]
            return chunk

        # When no buffered bytes remain and the peer has closed its write half,
        # POSIX TCP sockets return b"" (0-byte EOF).
        if self.peer_closed:
            return b""

        if not self.is_blocking:
            raise BlockingIOError("Resource temporarily unavailable.")

        return b""

    def sendall(self, data: bytes) -> None:
        """Append all `data` bytes to the connected peer's receive buffer."""
        if self.closed:
            raise OSError("Socket is closed.")

        if self.pending_send_exception is not None:
            exc = self.pending_send_exception
            self.pending_send_exception = None
            raise exc

        if self.peer is None or self.peer.closed or self.peer_closed:
            raise BrokenPipeError("Broken pipe: peer socket is closed.")

        self.peer.recv_buffer.extend(data)

    def close(self) -> None:
        """Close this socket endpoint and signal TCP FIN (EOF) to the peer."""
        if not self.closed:
            self.closed = True
            if self.peer is not None:
                self.peer.peer_closed = True

    def is_readable(self) -> bool:
        """Return True if `EVENT_READ` should fire (unread data, EOF, or pending error)."""
        if self.closed:
            return False
        return (
            bool(self.recv_buffer)
            or self.peer_closed
            or self.pending_recv_exception is not None
        )


class InMemorySelector(selectors.BaseSelector):
    """Deterministic `selectors.BaseSelector` for `InMemorySocket` endpoints."""

    def __init__(self) -> None:
        """Initialize an empty selector registry."""
        super().__init__()
        self._keys: dict[Any, selectors.SelectorKey] = {}

    def register(
        self,
        fileobj: Any,
        events: int,
        data: Any = None,
    ) -> selectors.SelectorKey:
        """Register a socket object for `EVENT_READ` / `EVENT_WRITE` monitoring."""
        if fileobj in self._keys:
            raise KeyError(f"{fileobj!r} is already registered.")
        fd = fileobj.fileno() if hasattr(fileobj, "fileno") else id(fileobj)
        key = selectors.SelectorKey(
            fileobj=fileobj,
            fd=fd,
            events=events,
            data=data,
        )
        self._keys[fileobj] = key
        return key

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        """Unregister a previously registered socket object."""
        if fileobj not in self._keys:
            raise KeyError(f"{fileobj!r} is not registered.")
        return self._keys.pop(fileobj)

    def select(
        self,
        timeout: float | None = None,
    ) -> list[tuple[selectors.SelectorKey, int]]:
        """Return all registered socket keys that are currently readable."""
        _ = timeout
        ready: list[tuple[selectors.SelectorKey, int]] = []
        for fileobj, key in list(self._keys.items()):
            if (
                key.events & selectors.EVENT_READ
                and isinstance(fileobj, InMemorySocket)
                and fileobj.is_readable()
            ):
                ready.append((key, selectors.EVENT_READ))
        return ready

    def get_map(self) -> Mapping[Any, selectors.SelectorKey]:
        """Return the mapping of registered file objects to selector keys."""
        return self._keys

    def close(self) -> None:
        """Clear all registered keys."""
        self._keys.clear()


_NEXT_FD_COUNTER: int = 1000


def create_in_memory_socketpair() -> tuple[InMemorySocket, InMemorySocket]:
    """Create a connected pair of `InMemorySocket` endpoints."""
    global _NEXT_FD_COUNTER
    sock_a = InMemorySocket(_NEXT_FD_COUNTER)
    sock_b = InMemorySocket(_NEXT_FD_COUNTER + 1)
    _NEXT_FD_COUNTER += 2
    sock_a.peer = sock_b
    sock_b.peer = sock_a
    return sock_a, sock_b
