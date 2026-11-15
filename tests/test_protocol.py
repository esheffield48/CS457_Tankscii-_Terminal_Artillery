"""Unit tests for the 9 canonical Tankscii message schemas and boundary validators."""

from __future__ import annotations

import unittest

from tankscii.protocol import (
    build_connect_message,
    build_disconnect_message,
    build_error_message,
    build_fire_message,
    build_game_over_message,
    build_game_start_message,
    build_lobby_wait_message,
    build_move_message,
    build_state_resume_message,
    build_state_update_message,
    validate_connect_payload,
    validate_disconnect_payload,
    validate_fire_payload,
    validate_frame_envelope,
    validate_move_payload,
)


class TestProtocolSchemasAndValidation(unittest.TestCase):
    """Verify all 9 message builders and strict boundary/type validators."""

    def test_all_nine_canonical_message_builders_produce_valid_envelopes(
        self,
    ) -> None:
        """Verify every canonical message builder passes validate_frame_envelope."""
        sample_tanks = {
            "Player_1": {"x": 10, "y": 6, "hp": 100},
            "Player_2": {"x": 70, "y": 6, "hp": 100},
        }
        messages = [
            build_connect_message("Alpha", "1.0.0", "Player_1"),
            build_lobby_wait_message("Player_1", "Waiting for opponent..."),
            build_game_start_message(
                "Player_1", "Bravo", sample_tanks, "Player_1", 30
            ),
            build_move_message("Player_1", "RIGHT", 3),
            build_fire_message("Player_1", 45, 75),
            build_state_update_message(
                round_num=1,
                action="FIRE",
                actor_id="Player_1",
                tanks=sample_tanks,
                move_path=None,
                trajectory=[[10, 6], [40, 21], [70, 6]],
                impact={"coord": [70, 6], "result": "HIT"},
                damage_dealt=25,
                craters=[{"x": 70, "radius": 2}],
                next_turn="Player_2",
                time_remaining=30,
            ),
            build_error_message("OUT_OF_TURN", "Player_1's turn.", False),
            build_disconnect_message("Player_1", "FORFEIT"),
            build_game_over_message(
                winner="Player_1",
                reason="HP_ELIMINATION",
                final_scores={
                    "Player_1_hp": 75,
                    "Player_2_hp": 0,
                    "rounds": 4,
                },
                message="Player 1 wins!",
            ),
        ]

        self.assertEqual(len(messages), 9)
        for msg in messages:
            with self.subTest(msg_type=msg["msg_type"]):
                is_valid, detail = validate_frame_envelope(msg)
                self.assertTrue(is_valid, detail)

    def test_state_resume_message_builder_produces_valid_envelope(self) -> None:
        """Verify build_state_resume_message passes validate_frame_envelope and populates fields."""
        sample_tanks = {
            "Player_1": {"x": 13, "y": 6, "hp": 100},
            "Player_2": {"x": 70, "y": 6, "hp": 75},
        }
        resume_msg = build_state_resume_message(
            assigned_id="Player_2",
            opponent_nickname="Alpha",
            round_num=2,
            tanks=sample_tanks,
            craters=[{"x": 70, "radius": 2}],
            active_turn="Player_2",
            time_remaining=45,
            move_used=False,
            scores={"Player_1_hp": 100, "Player_2_hp": 75, "rounds": 2},
            message="Session resumed.",
        )
        is_valid, detail = validate_frame_envelope(resume_msg)
        self.assertTrue(is_valid, detail)
        self.assertEqual(resume_msg["msg_type"], "STATE_RESUME")
        self.assertEqual(resume_msg["payload"]["assigned_id"], "Player_2")
        self.assertEqual(resume_msg["payload"]["opponent_nickname"], "Alpha")
        self.assertEqual(resume_msg["payload"]["round"], 2)
        self.assertEqual(resume_msg["payload"]["active_turn"], "Player_2")
        self.assertFalse(resume_msg["payload"]["move_used"])

    def test_validate_fire_payload_boundary_fuzzing(self) -> None:
        """Verify FIRE angle (0..180) and power (1..100) reject out-of-bounds values."""
        valid_ok, code, _, _ = validate_fire_payload({"angle": 45, "power": 75})
        self.assertTrue(valid_ok)
        self.assertEqual(code, "")

        invalid_payloads = [
            {"angle": -10, "power": 50},
            {"angle": 220, "power": 50},
            {"angle": 45, "power": 0},
            {"angle": 45, "power": 150},
            {"angle": True, "power": 50},
            {"angle": 45, "power": False},
            {"angle": "45", "power": 50},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                is_valid, error_code, _, retry_allowed = validate_fire_payload(
                    payload
                )
                self.assertFalse(is_valid)
                self.assertEqual(error_code, "INVALID_PARAMS")
                self.assertTrue(retry_allowed)

    def test_validate_move_payload_boundary_and_duplicate_move_fuzzing(
        self,
    ) -> None:
        """Verify MOVE distance (1..5), direction, board edges, and move_used flag."""
        valid_ok, _, _, _ = validate_move_payload(
            {"direction": "RIGHT", "distance": 5},
            current_x=10,
            move_used=False,
        )
        self.assertTrue(valid_ok)

        # Duplicate move in same turn -> MOVE_ALREADY_USED
        dup_ok, dup_code, _, dup_retry = validate_move_payload(
            {"direction": "RIGHT", "distance": 2},
            current_x=13,
            move_used=True,
        )
        self.assertFalse(dup_ok)
        self.assertEqual(dup_code, "MOVE_ALREADY_USED")
        self.assertTrue(dup_retry)

        # Out-of-bounds distance, invalid direction, and board edge violations
        invalid_cases = [
            ({"direction": "RIGHT", "distance": 0}, 10),
            ({"direction": "RIGHT", "distance": 9}, 10),
            ({"direction": "UP", "distance": 3}, 10),
            ({"direction": "LEFT", "distance": 5}, 2),
            ({"direction": "RIGHT", "distance": 5}, 78),
            ({"direction": "LEFT", "distance": True}, 10),
        ]
        for payload, current_x in invalid_cases:
            with self.subTest(payload=payload, current_x=current_x):
                is_valid, error_code, _, retry_allowed = validate_move_payload(
                    payload=payload,
                    current_x=current_x,
                    move_used=False,
                )
                self.assertFalse(is_valid)
                self.assertEqual(error_code, "INVALID_PARAMS")
                self.assertTrue(retry_allowed)

    def test_validate_connect_and_disconnect_payloads(self) -> None:
        """Verify CONNECT nickname/SemVer rules and DISCONNECT reason rules."""
        ok, _, _, _ = validate_connect_payload(
            {"nickname": "Alpha", "client_version": "1.0.0"}
        )
        self.assertTrue(ok)

        invalid_connects = [
            {"nickname": "", "client_version": "1.0.0"},
            {"nickname": "   ", "client_version": "1.0.0"},
            {"nickname": "A" * 17, "client_version": "1.0.0"},
            {"nickname": "Alpha", "client_version": "invalid_version"},
        ]
        for bad_payload in invalid_connects:
            with self.subTest(payload=bad_payload):
                is_valid, code, _, retry = validate_connect_payload(bad_payload)
                self.assertFalse(is_valid)
                self.assertEqual(code, "INVALID_PARAMS")
                self.assertFalse(retry)

        disc_ok, _, _, _ = validate_disconnect_payload({"reason": "FORFEIT"})
        self.assertTrue(disc_ok)

        disc_bad, disc_code, _, _ = validate_disconnect_payload(
            {"reason": "RAGE_QUIT"}
        )
        self.assertFalse(disc_bad)
        self.assertEqual(disc_code, "INVALID_PARAMS")


if __name__ == "__main__":
    unittest.main()
