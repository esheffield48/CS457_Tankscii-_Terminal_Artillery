"""Unit tests for CLI argument parsing, port range validation, and DNS resolution."""

from __future__ import annotations

import contextlib
import io
import socket
import unittest
from unittest.mock import patch

from client import (
    parse_client_arguments,
    parse_turn_command_input,
    resolve_server_hostname,
)
from server import parse_server_arguments


class TestCliAndDnsResolution(unittest.TestCase):
    """Verify Sprint 3 CLI flags, port bounds (1024-65535), and DNS handling."""

    def test_parse_client_arguments_accepts_valid_flags_and_ports(self) -> None:
        """Verify client CLI accepts -i and -p within 1024..65535."""
        args_min = parse_client_arguments(
            ["-i", "server.sheffield.edu", "-p", "1024"]
        )
        self.assertEqual(args_min.hostname, "server.sheffield.edu")
        self.assertEqual(args_min.port, 1024)

        args_mid = parse_client_arguments(
            ["-i", "192.168.10.50", "-p", "8080", "-n", "Alpha"]
        )
        self.assertEqual(args_mid.hostname, "192.168.10.50")
        self.assertEqual(args_mid.port, 8080)
        self.assertEqual(args_mid.nickname, "Alpha")

        args_max = parse_client_arguments(
            ["-i", "server.sheffield.edu", "-p", "65535"]
        )
        self.assertEqual(args_max.port, 65535)

    def test_parse_client_arguments_rejects_missing_or_out_of_bounds_ports(
        self,
    ) -> None:
        """Verify client CLI rejects missing flags and ports outside 1024..65535."""
        invalid_arg_vectors: list[list[str]] = [
            [],
            ["-i", "server.sheffield.edu"],
            ["-p", "8080"],
            ["-i", "server.sheffield.edu", "-p", "80"],
            ["-i", "server.sheffield.edu", "-p", "1023"],
            ["-i", "server.sheffield.edu", "-p", "65536"],
            ["-i", "server.sheffield.edu", "-p", "not_a_port"],
        ]
        for argv in invalid_arg_vectors:
            with self.subTest(argv=argv):
                stderr_buffer = io.StringIO()
                with contextlib.redirect_stderr(stderr_buffer):
                    with self.assertRaises(SystemExit) as ctx:
                        parse_client_arguments(argv)
                self.assertNotEqual(ctx.exception.code, 0)

    def test_parse_server_arguments_accepts_valid_ports(self) -> None:
        """Verify server CLI accepts -p within 1024..65535."""
        args_min = parse_server_arguments(["-p", "1024"])
        self.assertEqual(args_min.port, 1024)

        args_std = parse_server_arguments(["-p", "8080"])
        self.assertEqual(args_std.port, 8080)

        args_max = parse_server_arguments(["-p", "65535"])
        self.assertEqual(args_max.port, 65535)

    def test_parse_server_arguments_rejects_missing_or_out_of_bounds_ports(
        self,
    ) -> None:
        """Verify server CLI rejects missing -p and ports outside 1024..65535."""
        invalid_arg_vectors: list[list[str]] = [
            [],
            ["-p", "80"],
            ["-p", "1023"],
            ["-p", "65536"],
            ["-p", "invalid"],
        ]
        for argv in invalid_arg_vectors:
            with self.subTest(argv=argv):
                stderr_buffer = io.StringIO()
                with contextlib.redirect_stderr(stderr_buffer):
                    with self.assertRaises(SystemExit) as ctx:
                        parse_server_arguments(argv)
                self.assertNotEqual(ctx.exception.code, 0)

    def test_resolve_server_hostname_handles_gaierror_gracefully(self) -> None:
        """Verify DNS failure catches socket.gaierror, prints guidance, and exits 1."""
        stdout_buffer = io.StringIO()
        with patch(
            "client.socket.gethostbyname",
            side_effect=socket.gaierror(8, "nodename nor servname provided"),
        ):
            with contextlib.redirect_stdout(stdout_buffer):
                with self.assertRaises(SystemExit) as ctx:
                    resolve_server_hostname("unknown.sheffield.edu")

        self.assertEqual(ctx.exception.code, 1)
        output = stdout_buffer.getvalue()
        self.assertIn("[ERROR] Unable to resolve DNS hostname", output)
        self.assertIn("Cisco router DNS configuration", output)
        self.assertNotIn("Traceback (most recent call last)", output)

    def test_resolve_server_hostname_returns_resolved_ip_on_success(
        self,
    ) -> None:
        """Verify resolve_server_hostname returns the IPv4 address from DNS."""
        with patch(
            "client.socket.gethostbyname", return_value="10.0.10.25"
        ) as mock_dns:
            resolved_ip = resolve_server_hostname("server.sheffield.edu")
        self.assertEqual(resolved_ip, "10.0.10.25")
        mock_dns.assert_called_once_with("server.sheffield.edu")

    def test_parse_turn_command_input_constructs_valid_protocol_frames(
        self,
    ) -> None:
        """Verify MOVE, FIRE, FORFEIT, QUIT, and invalid CLI turn inputs."""
        move_frame, err, is_disc = parse_turn_command_input(
            "move right 4", "Player_1"
        )
        self.assertIsNone(err)
        self.assertFalse(is_disc)
        self.assertIsNotNone(move_frame)
        assert move_frame is not None
        self.assertEqual(move_frame["msg_type"], "MOVE")
        self.assertEqual(
            move_frame["payload"], {"direction": "RIGHT", "distance": 4}
        )

        fire_frame, err, is_disc = parse_turn_command_input(
            "FIRE 45 75", "Player_1"
        )
        self.assertIsNone(err)
        self.assertFalse(is_disc)
        self.assertIsNotNone(fire_frame)
        assert fire_frame is not None
        self.assertEqual(fire_frame["msg_type"], "FIRE")
        self.assertEqual(fire_frame["payload"], {"angle": 45, "power": 75})

        forfeit_frame, err, is_disc = parse_turn_command_input(
            "FORFEIT", "Player_2"
        )
        self.assertIsNone(err)
        self.assertTrue(is_disc)
        assert forfeit_frame is not None
        self.assertEqual(forfeit_frame["msg_type"], "DISCONNECT")
        self.assertEqual(forfeit_frame["payload"], {"reason": "FORFEIT"})

        quit_frame, err, is_disc = parse_turn_command_input("QUIT", "Player_2")
        self.assertIsNone(err)
        self.assertTrue(is_disc)
        assert quit_frame is not None
        self.assertEqual(quit_frame["payload"], {"reason": "USER_QUIT"})

        invalid_frame, err, is_disc = parse_turn_command_input(
            "JUMP 10", "Player_1"
        )
        self.assertIsNone(invalid_frame)
        self.assertIsNotNone(err)
        self.assertFalse(is_disc)


if __name__ == "__main__":
    unittest.main()
