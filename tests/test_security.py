"""Adversarial security and protocol fuzzing tests across all 5 attack surfaces."""

from __future__ import annotations

import logging
import ssl
from typing import Any
import unittest

from client import parse_client_arguments
from server import TanksciiServer, is_trusted_client_ip, parse_server_arguments
from tankscii import HANDSHAKE_TIMEOUT_SECONDS, MAX_BUFFER_BYTES
from tankscii.framing import (
    BufferOverflowError,
    FrameDecodeError,
    append_and_extract_lines,
    decode_json_line,
    receive_and_extract_raw_lines,
    send_frame_safely,
)
from tankscii.protocol import (
    build_connect_message,
    build_fire_message,
    build_move_message,
    validate_connect_payload,
    validate_frame_envelope,
)
from tests import (
    InMemorySelector,
    InMemorySocket,
    create_in_memory_socketpair,
)


class FakeMonotonicClock:
    """Deterministic controllable monotonic clock for security timeout tests."""

    def __init__(self, initial_seconds: float = 1000.0) -> None:
        self.current_seconds: float = initial_seconds

    def __call__(self) -> float:
        return self.current_seconds

    def advance(self, delta_seconds: float) -> None:
        self.current_seconds += delta_seconds


def _drain_frames(sock: InMemorySocket) -> list[dict[str, Any]]:
    """Read and decode all complete newline-delimited frames from an InMemorySocket."""
    buf = bytearray()
    frames: list[dict[str, Any]] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        for raw_line in append_and_extract_lines(buf, chunk):
            frames.append(decode_json_line(raw_line))
    return frames


class TestSecurityMitigations(unittest.TestCase):
    """Adversarial test suite verifying all 5 Attack Surfaces in docs/security_protocols.md."""

    def setUp(self) -> None:
        self.clock = FakeMonotonicClock()
        self.selector = InMemorySelector()
        logger = logging.getLogger("tankscii.test.security")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.server = TanksciiServer(
            port=8080,
            host="0.0.0.0",
            selector=self.selector,
            clock_fn=self.clock,
            first_turn_choice="Player_1",
            logger=logger,
        )
        self.server.fsm_state = "WAITING_FOR_PLAYERS"

    def _connect_player(
        self,
        nickname: str,
        player_id: str,
        ip_address: str = "127.0.0.1",
    ) -> tuple[InMemorySocket, InMemorySocket]:
        client_sock, server_sock = create_in_memory_socketpair()
        self.server.register_accepted_socket(
            server_sock,  # type: ignore[arg-type]
            (ip_address, 50000),
        )
        send_frame_safely(
            client_sock,  # type: ignore[arg-type]
            build_connect_message(nickname, "1.0.0", player_id),
        )
        self.server.run_event_loop_once(0.0)
        return client_sock, server_sock

    # ------------------------------------------------------------------
    # Attack Surface 1: Network Perimeter & Transport
    # ------------------------------------------------------------------
    def test_foreign_subnet_connection_rejected_before_registration(self) -> None:
        """Reject foreign subnet IPs (10.99.99.5) while permitting localhost & CML."""
        self.assertTrue(is_trusted_client_ip("127.0.0.1"))
        self.assertTrue(is_trusted_client_ip("192.168.10.2"))
        self.assertTrue(is_trusted_client_ip("192.168.11.2"))
        self.assertTrue(is_trusted_client_ip("192.168.20.100"))
        self.assertFalse(is_trusted_client_ip("10.99.99.5"))
        self.assertFalse(is_trusted_client_ip("not-an-ip"))

        _, foreign_server_sock = create_in_memory_socketpair()
        session = self.server.register_accepted_socket(
            foreign_server_sock,  # type: ignore[arg-type]
            ("10.99.99.5", 52000),
        )
        self.assertIsNone(session)
        self.assertTrue(foreign_server_sock.closed)
        self.assertEqual(len(self.server.unassigned_sessions), 0)

    def test_opt_in_tls_cli_flags_and_want_read_resilience(self) -> None:
        """Verify -tls/--tls CLI flags default to False and handle SSLWantReadError."""
        default_server = parse_server_arguments(["-p", "8080"])
        self.assertFalse(default_server.tls)
        tls_server = parse_server_arguments(["-p", "8080", "-tls"])
        self.assertTrue(tls_server.tls)

        default_client = parse_client_arguments(
            ["-i", "server.sheffield.edu", "-p", "8080"]
        )
        self.assertFalse(default_client.tls)
        tls_client = parse_client_arguments(
            ["-i", "server.sheffield.edu", "-p", "8080", "--tls"]
        )
        self.assertTrue(tls_client.tls)

        class WantReadSocket:
            def recv(self, _size: int) -> bytes:
                raise ssl.SSLWantReadError("TLS record incomplete")

        buf = bytearray()
        self.assertEqual(
            receive_and_extract_raw_lines(WantReadSocket(), buf),  # type: ignore[arg-type]
            [],
        )

    # ------------------------------------------------------------------
    # Attack Surface 2: TCP Stream Framing & Event Loop Availability
    # ------------------------------------------------------------------
    def test_stream_buffer_overflow_above_8kb_disconnects_client(self) -> None:
        """Disconnect client exceeding MAX_BUFFER_BYTES without a newline."""
        client_sock, _ = self._connect_player("CobraKing", "Player_1")
        _ = _drain_frames(client_sock)

        oversized_payload = b"A" * (MAX_BUFFER_BYTES + 100)
        client_sock.sendall(oversized_payload)
        for _ in range(3):
            if "Player_1" not in self.server.clients:
                break
            self.server.run_event_loop_once(0.0)

        frames = _drain_frames(client_sock)
        self.assertTrue(
            any(
                f["msg_type"] == "ERROR"
                and f["payload"]["error_code"] == "MALFORMED_FRAME"
                for f in frames
            )
        )
        self.assertNotIn("Player_1", self.server.clients)

    def test_coalesced_command_flood_rate_limit_and_error_threshold(self) -> None:
        """Disconnect client after MAX_CONSECUTIVE_ERRORS invalid frames."""
        client_sock, _ = self._connect_player("CobraKing", "Player_1")
        _ = _drain_frames(client_sock)

        flood = b"{bad_json}\n" * 20
        client_sock.sendall(flood)
        self.server.run_event_loop_once(0.0)
        # First read caps at MAX_FRAMES_PER_READ (8); send second burst to exceed 15.
        if "Player_1" in self.server.clients:
            client_sock.sendall(flood)
            self.server.run_event_loop_once(0.0)

        self.assertNotIn("Player_1", self.server.clients)

    def test_slowloris_pre_handshake_timeout_closes_idle_socket(self) -> None:
        """Close unauthenticated socket that sends no CONNECT within 15s."""
        _, server_sock = create_in_memory_socketpair()
        session = self.server.register_accepted_socket(
            server_sock,  # type: ignore[arg-type]
            ("192.168.10.2", 51000),
        )
        self.assertIsNotNone(session)
        self.assertEqual(len(self.server.unassigned_sessions), 1)

        self.clock.advance(HANDSHAKE_TIMEOUT_SECONDS + 1.0)
        self.server.run_event_loop_once(0.0)

        self.assertEqual(len(self.server.unassigned_sessions), 0)
        self.assertTrue(server_sock.closed)

    # ------------------------------------------------------------------
    # Attack Surface 3: Session Management & 60s Reconnection Window
    # ------------------------------------------------------------------
    def test_cross_subnet_reconnection_hijack_rejected_by_ip_pinning(self) -> None:
        """Reject Subnet B hijack of Subnet A's TEMPORARILY_DISCONNECTED slot."""
        p1_sock, _ = self._connect_player("CobraKing", "Player_1", "192.168.10.2")
        p2_sock, _ = self._connect_player("Thunderbolt", "Player_2", "192.168.11.2")
        _ = _drain_frames(p1_sock)
        _ = _drain_frames(p2_sock)

        # Simulate Player_1 dropping unexpectedly.
        p1_sock.close()
        self.server.run_event_loop_once(0.0)
        self.assertEqual(
            self.server.clients["Player_1"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )

        # Attacker from Subnet B (192.168.11.2) tries to claim CobraKing.
        attacker_sock, attacker_srv_sock = create_in_memory_socketpair()
        self.server.register_accepted_socket(
            attacker_srv_sock,  # type: ignore[arg-type]
            ("192.168.11.2", 53000),
        )
        send_frame_safely(
            attacker_sock,  # type: ignore[arg-type]
            build_connect_message("CobraKing", "1.0.0", "Player_1"),
        )
        self.server.run_event_loop_once(0.0)

        attacker_frames = _drain_frames(attacker_sock)
        self.assertEqual(attacker_frames[0]["msg_type"], "ERROR")
        self.assertEqual(attacker_frames[0]["payload"]["error_code"], "LOBBY_FULL")
        self.assertEqual(
            self.server.clients["Player_1"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )

        # Genuine Player_1 from Subnet A (192.168.10.2) reconnects cleanly.
        reconn_sock, _ = self._connect_player("CobraKing", "Player_1", "192.168.10.2")
        reconn_frames = _drain_frames(reconn_sock)
        self.assertTrue(any(f["msg_type"] == "STATE_RESUME" for f in reconn_frames))

    def test_cross_player_header_player_id_spoofing_rejected(self) -> None:
        """Reject Player_2 sending FIRE with 'player_id': 'Player_1'."""
        p1_sock, _ = self._connect_player("CobraKing", "Player_1", "192.168.10.2")
        p2_sock, _ = self._connect_player("Thunderbolt", "Player_2", "192.168.11.2")
        _ = _drain_frames(p1_sock)
        _ = _drain_frames(p2_sock)

        spoofed_fire = build_fire_message("Player_1", angle=45, power=75)
        send_frame_safely(p2_sock, spoofed_fire)  # type: ignore[arg-type]
        self.server.run_event_loop_once(0.0)

        p2_frames = _drain_frames(p2_sock)
        self.assertEqual(p2_frames[0]["msg_type"], "ERROR")
        self.assertEqual(p2_frames[0]["payload"]["error_code"], "OUT_OF_TURN")
        self.assertEqual(self.server.active_turn, "Player_1")

    # ------------------------------------------------------------------
    # Attack Surface 4: Protocol Parser & Physics Engine
    # ------------------------------------------------------------------
    def test_hmac_tampering_and_replay_window_rejection(self) -> None:
        """Reject tampered HMAC digests and timestamps outside +/-30s window."""
        frame = build_fire_message("Player_1", angle=45, power=75, timestamp=1000)
        is_ok, _ = validate_frame_envelope(
            frame, require_hmac=True, current_epoch=1010
        )
        self.assertTrue(is_ok)

        tampered = dict(frame)
        tampered["payload"] = {"angle": 45, "power": 99}
        is_ok, _ = validate_frame_envelope(tampered, require_hmac=True)
        self.assertFalse(is_ok)

        is_ok, _ = validate_frame_envelope(
            frame, require_hmac=True, current_epoch=1120
        )
        self.assertFalse(is_ok)

    def test_json_type_confusion_nan_infinity_and_deep_recursion_fuzzing(self) -> None:
        """Reject bool-as-int, NaN, Infinity, and deeply nested JSON."""
        bool_move = build_move_message("Player_1", direction="RIGHT", distance=True)  # type: ignore[arg-type]
        p1_sock, _ = self._connect_player("CobraKing", "Player_1")
        p2_sock, _ = self._connect_player("Thunderbolt", "Player_2")
        _ = _drain_frames(p1_sock)
        _ = _drain_frames(p2_sock)

        send_frame_safely(p1_sock, bool_move)  # type: ignore[arg-type]
        self.server.run_event_loop_once(0.0)
        p1_frames = _drain_frames(p1_sock)
        self.assertEqual(p1_frames[0]["msg_type"], "ERROR")
        self.assertEqual(p1_frames[0]["payload"]["error_code"], "INVALID_PARAMS")

        with self.assertRaises(FrameDecodeError):
            decode_json_line(b'{"angle": NaN, "power": 50}')
        with self.assertRaises(FrameDecodeError):
            decode_json_line(b'{"angle": Infinity, "power": 50}')
        with self.assertRaises(FrameDecodeError):
            decode_json_line(b"[" * 1500 + b"]" * 1500)
        with self.assertRaises(BufferOverflowError):
            append_and_extract_lines(bytearray(), b"X" * (MAX_BUFFER_BYTES + 1))

    # ------------------------------------------------------------------
    # Attack Surface 5: Terminal UI Renderer
    # ------------------------------------------------------------------
    def test_ansi_escape_and_control_character_nickname_sanitization(self) -> None:
        """Reject nicknames containing ANSI escape sequences or control bytes."""
        for bad_name in ("Hack\x1b[2J", "Spoof\r100HP", "Bell\x07", "Line\nBreak"):
            valid, error_code, _, _ = validate_connect_payload(
                {"nickname": bad_name, "client_version": "1.0.0"}
            )
            self.assertFalse(valid, msg=f"Expected rejection for {bad_name!r}")
            self.assertEqual(error_code, "INVALID_PARAMS")


if __name__ == "__main__":
    unittest.main()
