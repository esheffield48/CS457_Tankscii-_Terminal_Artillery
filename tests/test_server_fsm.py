"""Integration and FSM lifecycle tests for the authoritative Tankscii server."""

from __future__ import annotations

import logging
from typing import Any
import unittest

from server import TanksciiServer
from tankscii.framing import (
    append_and_extract_lines,
    decode_json_line,
    send_frame_safely,
)
from tankscii.protocol import (
    build_connect_message,
    build_disconnect_message,
    build_fire_message,
    build_move_message,
)
from tests import (
    InMemorySelector,
    InMemorySocket,
    create_in_memory_socketpair,
)


class FakeMonotonicClock:
    """Deterministic controllable monotonic clock for turn timer tests."""

    def __init__(self, initial_seconds: float = 1000.0) -> None:
        """Initialize fake clock at `initial_seconds`."""
        self.current_seconds: float = initial_seconds

    def __call__(self) -> float:
        """Return current simulated monotonic time in seconds."""
        return self.current_seconds

    def advance(self, delta_seconds: float) -> None:
        """Advance the simulated clock by `delta_seconds`."""
        self.current_seconds += delta_seconds


class TestServerFsmAndConcurrency(unittest.TestCase):
    """Verify the 8-state server FSM, turn enforcement, timeouts, and EOF handling."""

    def setUp(self) -> None:
        """Create a deterministic server instance with a controllable clock."""
        self.clock = FakeMonotonicClock(1000.0)
        self.selector = InMemorySelector()
        silent_logger = logging.getLogger("tankscii.server.test")
        silent_logger.handlers = [logging.NullHandler()]
        silent_logger.propagate = False
        self.server = TanksciiServer(
            port=8080,
            selector=self.selector,
            clock_fn=self.clock,
            first_turn_choice="Player_1",
            logger=silent_logger,
        )
        self.server.fsm_state = "WAITING_FOR_PLAYERS"
        self.open_sockets: list[InMemorySocket] = []

    def tearDown(self) -> None:
        """Close all test socketpair endpoints and shut down the server selector."""
        self.server.shutdown_server()
        for sock in self.open_sockets:
            try:
                sock.close()
            except OSError:
                pass

    def _create_registered_client_pair(
        self,
    ) -> tuple[InMemorySocket, InMemorySocket]:
        """Create a connected socketpair and register the server endpoint."""
        client_end, server_end = create_in_memory_socketpair()
        client_end.setblocking(False)
        self.open_sockets.extend([client_end, server_end])
        self.server.register_accepted_socket(server_end, ("127.0.0.1", 50000))  # type: ignore[arg-type]
        return client_end, server_end

    def _drain_frames_from_client_socket(
        self,
        client_sock: InMemorySocket,
    ) -> list[dict[str, Any]]:
        """Read and decode all complete JSON frames waiting on `client_sock`."""
        stream_buffer = bytearray()
        while True:
            try:
                chunk = client_sock.recv(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            stream_buffer.extend(chunk)

        raw_lines = append_and_extract_lines(stream_buffer, b"")
        return [decode_json_line(line) for line in raw_lines]

    def _start_two_player_match(
        self,
    ) -> tuple[InMemorySocket, InMemorySocket]:
        """Connect Player 1 and Player 2 and advance server to PLAYER_TURN."""
        p1_sock, _ = self._create_registered_client_pair()
        send_frame_safely(p1_sock, build_connect_message("Alpha", "1.0.0"))
        self.server.run_event_loop_once(0.0)

        p1_lobby_frames = self._drain_frames_from_client_socket(p1_sock)
        self.assertEqual(len(p1_lobby_frames), 1)
        self.assertEqual(p1_lobby_frames[0]["msg_type"], "LOBBY_WAIT")
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")

        p2_sock, _ = self._create_registered_client_pair()
        send_frame_safely(p2_sock, build_connect_message("Bravo", "1.0.0"))
        self.server.run_event_loop_once(0.0)

        p1_start_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_start_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p1_start_frames), 1)
        self.assertEqual(len(p2_start_frames), 1)
        self.assertEqual(p1_start_frames[0]["msg_type"], "GAME_START")
        self.assertEqual(p2_start_frames[0]["msg_type"], "GAME_START")
        self.assertEqual(self.server.fsm_state, "PLAYER_TURN")
        self.assertEqual(self.server.active_turn, "Player_1")

        return p1_sock, p2_sock

    def test_complete_match_lifecycle_move_fire_and_victory_cleanup(
        self,
    ) -> None:
        """Verify CONNECT -> LOBBY_WAIT -> GAME_START -> MOVE -> FIRE -> GAME_OVER -> CLEANUP."""
        p1_sock, p2_sock = self._start_two_player_match()

        # Round 1, Turn 1: Player_1 moves RIGHT 2 (x: 10 -> 12), then fires
        send_frame_safely(
            p1_sock, build_move_message("Player_1", "RIGHT", 2)
        )
        self.server.run_event_loop_once(0.0)
        p1_move_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_move_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(p1_move_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p1_move_frames[0]["payload"]["action"], "MOVE")
        self.assertEqual(p2_move_frames[0]["payload"]["action"], "MOVE")
        self.assertEqual(self.server.tanks["Player_1"]["x"], 12)
        self.assertTrue(self.server.move_used)

        # Move Player_1 back to x=10 on the next turn or set Player_2 at x=72 so 60-range hits directly:
        # First let Player_1 fire a warning shot, then Player_2 moves RIGHT 2 to x=72 (distance=60 from x=12).
        send_frame_safely(p1_sock, build_fire_message("Player_1", 45, 73))
        self.server.run_event_loop_once(0.0)
        self._drain_frames_from_client_socket(p1_sock)
        self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(self.server.active_turn, "Player_2")

        # Set Player_2 HP to 25 and position at x=70, Player_1 at x=10 with flat terrain height 6
        # to deterministically verify the final lethal direct hit (45 deg, 75 power -> 25 HP damage).
        self.server.terrain_heights = [6] * 80
        self.server.tanks["Player_1"] = {"x": 10, "y": 6, "hp": 100}
        self.server.tanks["Player_2"] = {"x": 70, "y": 6, "hp": 25}

        # Player_2 fires a harmless shot to hand turn back to Player_1
        send_frame_safely(p2_sock, build_fire_message("Player_2", 60, 20))
        self.server.run_event_loop_once(0.0)
        self._drain_frames_from_client_socket(p1_sock)
        self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(self.server.active_turn, "Player_1")
        self.assertEqual(self.server.round, 2)

        # Player_1 fires the lethal direct hit (angle=45, power=75 -> impact at x=70)
        send_frame_safely(p1_sock, build_fire_message("Player_1", 45, 75))
        self.server.run_event_loop_once(0.0)

        p1_end_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_end_frames = self._drain_frames_from_client_socket(p2_sock)

        self.assertEqual(len(p1_end_frames), 2)
        self.assertEqual(p1_end_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p1_end_frames[0]["payload"]["damage_dealt"], 25)
        self.assertEqual(p1_end_frames[1]["msg_type"], "GAME_OVER")
        self.assertEqual(p1_end_frames[1]["payload"]["winner"], "Player_1")
        self.assertEqual(
            p1_end_frames[1]["payload"]["reason"], "HP_ELIMINATION"
        )
        self.assertEqual(len(p2_end_frames), 2)
        self.assertEqual(p2_end_frames[1]["msg_type"], "GAME_OVER")

        # Verify server transitioned through CLEANUP back to WAITING_FOR_PLAYERS
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")
        self.assertEqual(len(self.server.clients), 0)

    def test_out_of_turn_command_rejected_without_mutating_state(self) -> None:
        """Verify Player_2 sending MOVE or FIRE during Player_1's turn gets OUT_OF_TURN."""
        p1_sock, p2_sock = self._start_two_player_match()
        initial_deadline = self.server.turn_deadline

        send_frame_safely(p2_sock, build_fire_message("Player_2", 135, 75))
        self.server.run_event_loop_once(0.0)

        p1_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_frames = self._drain_frames_from_client_socket(p2_sock)

        self.assertEqual(p1_frames, [])
        self.assertEqual(len(p2_frames), 1)
        self.assertEqual(p2_frames[0]["msg_type"], "ERROR")
        self.assertEqual(p2_frames[0]["payload"]["error_code"], "OUT_OF_TURN")
        self.assertFalse(p2_frames[0]["payload"]["retry_allowed"])
        self.assertEqual(self.server.active_turn, "Player_1")
        self.assertEqual(self.server.turn_deadline, initial_deadline)

    def test_duplicate_move_in_same_turn_rejected_with_move_already_used(
        self,
    ) -> None:
        """Verify a second MOVE in the same turn returns MOVE_ALREADY_USED."""
        p1_sock, p2_sock = self._start_two_player_match()

        send_frame_safely(p1_sock, build_move_message("Player_1", "RIGHT", 3))
        self.server.run_event_loop_once(0.0)
        self._drain_frames_from_client_socket(p1_sock)
        self._drain_frames_from_client_socket(p2_sock)

        # Attempt second MOVE in the same turn
        send_frame_safely(p1_sock, build_move_message("Player_1", "RIGHT", 2))
        self.server.run_event_loop_once(0.0)

        p1_frames = self._drain_frames_from_client_socket(p1_sock)
        self.assertEqual(len(p1_frames), 1)
        self.assertEqual(p1_frames[0]["msg_type"], "ERROR")
        self.assertEqual(
            p1_frames[0]["payload"]["error_code"], "MOVE_ALREADY_USED"
        )
        self.assertTrue(p1_frames[0]["payload"]["retry_allowed"])
        self.assertEqual(self.server.tanks["Player_1"]["x"], 13)

    def test_full_lobby_third_client_rejected_with_lobby_full(self) -> None:
        """Verify a 3rd client connecting during an active match receives LOBBY_FULL."""
        p1_sock, p2_sock = self._start_two_player_match()

        p3_client_sock, p3_server_sock = create_in_memory_socketpair()
        p3_client_sock.setblocking(False)
        self.open_sockets.extend([p3_client_sock, p3_server_sock])

        session_result = self.server.register_accepted_socket(
            p3_server_sock, ("127.0.0.1", 50003)  # type: ignore[arg-type]
        )
        self.assertIsNone(session_result)

        p3_frames = self._drain_frames_from_client_socket(p3_client_sock)
        self.assertEqual(len(p3_frames), 1)
        self.assertEqual(p3_frames[0]["msg_type"], "ERROR")
        self.assertEqual(p3_frames[0]["payload"]["error_code"], "LOBBY_FULL")
        self.assertFalse(p3_frames[0]["payload"]["retry_allowed"])

        # Active match remains undisturbed
        self.assertEqual(self.server.fsm_state, "PLAYER_TURN")
        self.assertEqual(len(self.server.clients), 2)
        _ = (p1_sock, p2_sock)

    def test_turn_timeout_and_boundary_race_ordering(self) -> None:
        """Verify 60s turn timeout handoff and that readable FIRE beats timeout race."""
        p1_sock, p2_sock = self._start_two_player_match()

        # 1. Advance clock past 60s deadline with no client input -> TIMEOUT
        self.clock.advance(60.5)
        self.server.run_event_loop_once(0.0)

        p1_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_frames = self._drain_frames_from_client_socket(p2_sock)

        self.assertEqual(len(p1_frames), 2)
        self.assertEqual(p1_frames[0]["msg_type"], "ERROR")
        self.assertEqual(p1_frames[0]["payload"]["error_code"], "TIMEOUT")
        self.assertEqual(p1_frames[1]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p1_frames[1]["payload"]["action"], "TIMEOUT")
        self.assertEqual(p1_frames[1]["payload"]["next_turn"], "Player_2")
        self.assertEqual(len(p2_frames), 1)
        self.assertEqual(self.server.active_turn, "Player_2")

        # 2. Boundary Race 1: Player_2 sends FIRE right as clock reaches turn_deadline.
        # Because run_event_loop_once services readable sockets BEFORE check_turn_timeout,
        # the FIRE action must succeed without emitting a TIMEOUT error.
        send_frame_safely(p2_sock, build_fire_message("Player_2", 45, 50))
        self.clock.advance(60.0)
        self.server.run_event_loop_once(0.0)

        p2_race_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_race_frames), 2)
        self.assertEqual(p2_race_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p2_race_frames[0]["payload"]["action"], "FIRE")
        self.assertEqual(p2_race_frames[0]["payload"]["actor_id"], "Player_2")
        self.assertIsNotNone(p2_race_frames[0]["payload"]["trajectory"])
        self.assertEqual(p2_race_frames[1]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p2_race_frames[1]["payload"]["action"], "TURN_START")
        self.assertIsNone(p2_race_frames[1]["payload"]["trajectory"])
        self.assertIsNone(p2_race_frames[1]["payload"]["impact"])
        self.assertEqual(self.server.active_turn, "Player_1")

    def test_twenty_round_limit_stalemate_ends_in_draw(self) -> None:
        """Verify completing 20 rounds with both tanks alive triggers DRAW (ROUND_LIMIT)."""
        p1_sock, p2_sock = self._start_two_player_match()

        # Fast-forward to Round 20 with 1 turn already completed (Player_2's final turn)
        self.server.round = 20
        self.server.turns_completed_in_round = 1
        self.server.active_turn = "Player_2"

        send_frame_safely(p2_sock, build_fire_message("Player_2", 80, 10))
        self.server.run_event_loop_once(0.0)

        p1_frames = self._drain_frames_from_client_socket(p1_sock)
        self.assertEqual(len(p1_frames), 2)
        self.assertEqual(p1_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p1_frames[1]["msg_type"], "GAME_OVER")
        self.assertEqual(p1_frames[1]["payload"]["winner"], "DRAW")
        self.assertEqual(p1_frames[1]["payload"]["reason"], "ROUND_LIMIT")
        self.assertEqual(
            p1_frames[1]["payload"]["final_scores"]["rounds"], 20
        )
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")

    def test_mutual_destruction_ends_in_draw_hp_elimination(self) -> None:
        """Verify both tanks reaching 0 HP on the same shot triggers DRAW (HP_ELIMINATION)."""
        p1_sock, p2_sock = self._start_two_player_match()

        # Position both tanks 1 column apart with 10 HP each and fire straight up (angle=90)
        self.server.tanks["Player_1"] = {"x": 40, "y": 6, "hp": 10}
        self.server.tanks["Player_2"] = {"x": 41, "y": 6, "hp": 10}

        send_frame_safely(p1_sock, build_fire_message("Player_1", 90, 20))
        self.server.run_event_loop_once(0.0)

        p1_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_frames = self._drain_frames_from_client_socket(p2_sock)

        self.assertEqual(len(p1_frames), 2)
        self.assertEqual(p1_frames[1]["msg_type"], "GAME_OVER")
        self.assertEqual(p1_frames[1]["payload"]["winner"], "DRAW")
        self.assertEqual(
            p1_frames[1]["payload"]["reason"], "HP_ELIMINATION"
        )
        self.assertEqual(
            p2_frames[1]["payload"]["final_scores"]["Player_1_hp"], 0
        )
        self.assertEqual(
            p2_frames[1]["payload"]["final_scores"]["Player_2_hp"], 0
        )
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")

    def test_zero_byte_eof_in_lobby_and_active_match(self) -> None:
        """Verify 0-byte EOF cleans up lobby in WAITING_FOR_PLAYERS and starts 60s grace period in match."""
        # Case A: Player 1 disconnects while waiting in lobby
        p1_lobby_sock, _ = self._create_registered_client_pair()
        send_frame_safely(
            p1_lobby_sock, build_connect_message("Solo", "1.0.0")
        )
        self.server.run_event_loop_once(0.0)
        self.assertIn("Player_1", self.server.clients)

        p1_lobby_sock.close()
        self.server.run_event_loop_once(0.0)
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")
        self.assertEqual(len(self.server.clients), 0)

        # Case B: Player 1 abruptly closes socket during active match -> enters 60s grace period, then Player 2 wins
        p1_sock, p2_sock = self._start_two_player_match()
        p1_sock.close()
        self.server.run_event_loop_once(0.0)

        self.assertEqual(
            self.server.clients["Player_1"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )
        self.assertEqual(self.server.fsm_state, "PLAYER_TURN")

        p2_disconnect_notice = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_disconnect_notice), 1)
        self.assertEqual(p2_disconnect_notice[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(
            p2_disconnect_notice[0]["payload"]["action"], "PLAYER_DISCONNECTED"
        )

        # Advance past the 60s grace period so the disconnect deadline expires
        self.clock.advance(60.5)
        self.server.run_event_loop_once(0.0)

        p2_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_frames), 1)
        self.assertEqual(p2_frames[0]["msg_type"], "GAME_OVER")
        self.assertEqual(p2_frames[0]["payload"]["winner"], "Player_2")
        self.assertEqual(p2_frames[0]["payload"]["reason"], "DISCONNECT")
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")
        self.assertEqual(len(self.server.clients), 0)

    def test_graceful_disconnect_forfeit_awards_victory_to_opponent(
        self,
    ) -> None:
        """Verify DISCONNECT (reason=FORFEIT) sends GAME_OVER (FORFEIT) to survivor."""
        p1_sock, p2_sock = self._start_two_player_match()

        send_frame_safely(
            p1_sock, build_disconnect_message("Player_1", "FORFEIT")
        )
        self.server.run_event_loop_once(0.0)

        p2_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_frames), 1)
        self.assertEqual(p2_frames[0]["msg_type"], "GAME_OVER")
        self.assertEqual(p2_frames[0]["payload"]["winner"], "Player_2")
        self.assertEqual(p2_frames[0]["payload"]["reason"], "FORFEIT")
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")
        self.assertEqual(len(self.server.clients), 0)

    def test_peer_rst_and_mid_broadcast_broken_pipe_recovery(self) -> None:
        """Verify ConnectionResetError on recv and BrokenPipeError on broadcast send."""
        # Case A: Peer TCP RST (ConnectionResetError) during recv()
        _, p2_sock = self._start_two_player_match()
        p1_server_sock = self.server.clients["Player_1"].sock
        assert isinstance(p1_server_sock, InMemorySocket)
        p1_server_sock.pending_recv_exception = ConnectionResetError(
            "Connection reset by peer"
        )

        self.server.run_event_loop_once(0.0)
        self.assertEqual(
            self.server.clients["Player_1"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )
        self.assertEqual(self.server.fsm_state, "PLAYER_TURN")

        p2_rst_notice = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_rst_notice), 1)
        self.assertEqual(p2_rst_notice[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(
            p2_rst_notice[0]["payload"]["action"], "PLAYER_DISCONNECTED"
        )

        self.clock.advance(60.5)
        self.server.run_event_loop_once(0.0)
        p2_rst_frames = self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(len(p2_rst_frames), 1)
        self.assertEqual(p2_rst_frames[0]["msg_type"], "GAME_OVER")
        self.assertEqual(p2_rst_frames[0]["payload"]["winner"], "Player_2")
        self.assertEqual(p2_rst_frames[0]["payload"]["reason"], "DISCONNECT")
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")

        # Case B: Mid-broadcast BrokenPipeError on Player 2's socket when Player 1 moves
        p1_sock_b, _ = self._start_two_player_match()
        p2_server_sock = self.server.clients["Player_2"].sock
        assert isinstance(p2_server_sock, InMemorySocket)
        p2_server_sock.pending_send_exception = BrokenPipeError("Broken pipe")

        send_frame_safely(
            p1_sock_b, build_move_message("Player_1", "RIGHT", 2)
        )
        self.server.run_event_loop_once(0.0)

        self.assertEqual(
            self.server.clients["Player_2"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )
        self.assertEqual(self.server.fsm_state, "PLAYER_TURN")

        p1_bpipe_notice = self._drain_frames_from_client_socket(p1_sock_b)
        self.assertEqual(len(p1_bpipe_notice), 2)
        self.assertEqual(p1_bpipe_notice[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p1_bpipe_notice[0]["payload"]["action"], "MOVE")
        self.assertEqual(p1_bpipe_notice[1]["msg_type"], "STATE_UPDATE")
        self.assertEqual(
            p1_bpipe_notice[1]["payload"]["action"], "PLAYER_DISCONNECTED"
        )

        self.clock.advance(60.5)
        self.server.run_event_loop_once(0.0)
        p1_bpipe_frames = self._drain_frames_from_client_socket(p1_sock_b)
        self.assertEqual(len(p1_bpipe_frames), 1)
        self.assertEqual(p1_bpipe_frames[0]["msg_type"], "GAME_OVER")
        self.assertEqual(p1_bpipe_frames[0]["payload"]["winner"], "Player_1")
        self.assertEqual(p1_bpipe_frames[0]["payload"]["reason"], "DISCONNECT")
        self.assertEqual(self.server.fsm_state, "WAITING_FOR_PLAYERS")

    def test_reconnection_handshake_and_state_resume_within_grace_period(
        self,
    ) -> None:
        """Verify a disconnected player reconnecting within 60s resumes match state via STATE_RESUME."""
        p1_sock, p2_sock = self._start_two_player_match()

        # Player_1 ("Alpha") moves RIGHT 3 (x=13, move_used=True) and fires (angle=45, power=73)
        send_frame_safely(p1_sock, build_move_message("Player_1", "RIGHT", 3))
        self.server.run_event_loop_once(0.0)
        self._drain_frames_from_client_socket(p1_sock)
        self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(self.server.tanks["Player_1"]["x"], 13)
        self.assertTrue(self.server.move_used)

        send_frame_safely(p1_sock, build_fire_message("Player_1", 45, 73))
        self.server.run_event_loop_once(0.0)
        self._drain_frames_from_client_socket(p1_sock)
        self._drain_frames_from_client_socket(p2_sock)
        self.assertEqual(self.server.active_turn, "Player_2")
        preserved_tanks = {
            pid: dict(state) for pid, state in self.server.tanks.items()
        }
        preserved_craters = list(self.server.craters)
        self.assertGreater(len(preserved_craters), 0)

        # Simulate socket drop on Player_2 ("Bravo")
        p2_sock.close()
        self.server.run_event_loop_once(0.0)

        self.assertEqual(
            self.server.clients["Player_2"].connection_status,
            "TEMPORARILY_DISCONNECTED",
        )
        p1_disc_frames = self._drain_frames_from_client_socket(p1_sock)
        self.assertEqual(len(p1_disc_frames), 1)
        self.assertEqual(p1_disc_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(
            p1_disc_frames[0]["payload"]["action"], "PLAYER_DISCONNECTED"
        )

        # Advance clock by 15.0s (within the 60s grace period) and reconnect as "Bravo"
        self.clock.advance(15.0)
        p2_reconn_sock, _ = self._create_registered_client_pair()
        send_frame_safely(
            p2_reconn_sock, build_connect_message("Bravo", "1.0.0")
        )
        self.server.run_event_loop_once(0.0)

        self.assertEqual(
            self.server.clients["Player_2"].connection_status, "CONNECTED"
        )
        p1_resume_frames = self._drain_frames_from_client_socket(p1_sock)
        p2_resume_frames = self._drain_frames_from_client_socket(
            p2_reconn_sock
        )
        self.assertEqual(len(p1_resume_frames), 1)
        self.assertEqual(len(p2_resume_frames), 1)
        self.assertEqual(p1_resume_frames[0]["msg_type"], "STATE_RESUME")
        self.assertEqual(p2_resume_frames[0]["msg_type"], "STATE_RESUME")

        p2_payload = p2_resume_frames[0]["payload"]
        self.assertEqual(p2_payload["assigned_id"], "Player_2")
        self.assertEqual(p2_payload["opponent_nickname"], "Alpha")
        self.assertEqual(p2_payload["round"], 1)
        self.assertEqual(p2_payload["active_turn"], "Player_2")
        self.assertEqual(p2_payload["tanks"], preserved_tanks)
        self.assertEqual(p2_payload["craters"], preserved_craters)

        # Verify reconnected Player_2 can immediately execute MOVE and FIRE commands
        send_frame_safely(
            p2_reconn_sock, build_move_message("Player_2", "LEFT", 2)
        )
        self.server.run_event_loop_once(0.0)
        p2_move_frames = self._drain_frames_from_client_socket(p2_reconn_sock)
        self.assertEqual(len(p2_move_frames), 1)
        self.assertEqual(p2_move_frames[0]["msg_type"], "STATE_UPDATE")
        self.assertEqual(p2_move_frames[0]["payload"]["action"], "MOVE")
        self.assertEqual(self.server.tanks["Player_2"]["x"], 68)

        send_frame_safely(
            p2_reconn_sock, build_fire_message("Player_2", 135, 60)
        )
        self.server.run_event_loop_once(0.0)
        self.assertEqual(self.server.active_turn, "Player_1")
        self.assertEqual(self.server.round, 2)


if __name__ == "__main__":
    unittest.main()
