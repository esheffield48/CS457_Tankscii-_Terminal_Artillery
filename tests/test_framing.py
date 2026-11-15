"""Unit tests for TCP bytearray stream framing, fragmentation, coalescing, and EOF."""

from __future__ import annotations

import unittest

from tankscii.framing import (
    ConnectionClosedError,
    FrameDecodeError,
    append_and_extract_lines,
    decode_json_line,
    receive_and_extract_raw_lines,
    serialize_frame,
)
from tankscii.protocol import (
    build_connect_message,
    build_fire_message,
    build_move_message,
)
from tests import create_in_memory_socketpair


class TestStreamFraming(unittest.TestCase):
    """Verify newline-delimited UTF-8 JSON framing over TCP stream buffers."""

    def test_byte_by_byte_chunk_fragmentation(self) -> None:
        """Feed a serialized JSON frame 1 byte at a time until trailing newline."""
        original_msg = build_fire_message(
            player_id="Player_1", angle=45, power=75, timestamp=1710000001
        )
        wire_bytes = serialize_frame(original_msg)
        stream_buffer = bytearray()

        # Feed every byte except the final '\n' (0x0A)
        for single_byte in wire_bytes[:-1]:
            extracted = append_and_extract_lines(
                stream_buffer, bytes([single_byte])
            )
            self.assertEqual(extracted, [])

        self.assertEqual(len(stream_buffer), len(wire_bytes) - 1)

        # Feed the final '\n' byte and verify complete extraction
        final_extracted = append_and_extract_lines(
            stream_buffer, wire_bytes[-1:]
        )
        self.assertEqual(len(final_extracted), 1)
        self.assertEqual(len(stream_buffer), 0)

        decoded_msg = decode_json_line(final_extracted[0])
        self.assertEqual(decoded_msg, original_msg)

    def test_back_to_back_message_coalescing(self) -> None:
        """Concatenate 3 distinct JSON frames in a single read and verify FIFO order."""
        msg_1 = build_connect_message("Alpha", timestamp=1710000001)
        msg_2 = build_move_message("Player_1", "RIGHT", 3, timestamp=1710000002)
        msg_3 = build_fire_message("Player_1", 45, 75, timestamp=1710000003)

        coalesced_chunk = (
            serialize_frame(msg_1)
            + serialize_frame(msg_2)
            + serialize_frame(msg_3)
        )
        stream_buffer = bytearray()
        raw_lines = append_and_extract_lines(stream_buffer, coalesced_chunk)

        self.assertEqual(len(raw_lines), 3)
        self.assertEqual(len(stream_buffer), 0)
        decoded_messages = [decode_json_line(line) for line in raw_lines]
        self.assertEqual(decoded_messages, [msg_1, msg_2, msg_3])

    def test_unterminated_fragment_retention_across_reads(self) -> None:
        """Verify partial bytes without newline remain in stream_buffer until completed."""
        original_msg = build_move_message(
            "Player_2", "LEFT", 5, timestamp=1710000010
        )
        wire_bytes = serialize_frame(original_msg)
        split_index = len(wire_bytes) // 2

        part_1 = wire_bytes[:split_index]
        part_2 = wire_bytes[split_index:]

        stream_buffer = bytearray()
        extracted_first = append_and_extract_lines(stream_buffer, part_1)
        self.assertEqual(extracted_first, [])
        self.assertEqual(bytes(stream_buffer), part_1)

        extracted_second = append_and_extract_lines(stream_buffer, part_2)
        self.assertEqual(len(extracted_second), 1)
        self.assertEqual(len(stream_buffer), 0)
        self.assertEqual(decode_json_line(extracted_second[0]), original_msg)

    def test_zero_byte_eof_detection_raises_connection_closed_error(
        self,
    ) -> None:
        """Verify 0-byte read on a closed peer socket raises ConnectionClosedError."""
        local_sock, remote_sock = create_in_memory_socketpair()
        try:
            local_sock.setblocking(False)
            remote_sock.close()

            with self.assertRaises(ConnectionClosedError):
                receive_and_extract_raw_lines(local_sock, bytearray())
        finally:
            local_sock.close()

    def test_decode_json_line_rejects_malformed_or_non_dict_json(self) -> None:
        """Verify decode_json_line raises FrameDecodeError on invalid payloads."""
        invalid_payloads: list[bytes] = [
            b"",
            b"   ",
            b"{not_valid_json",
            b"[1, 2, 3]",
            b'"just_a_string"',
            b"\xff\xfe\xfd",
        ]
        for raw_payload in invalid_payloads:
            with self.subTest(raw_payload=raw_payload):
                with self.assertRaises(FrameDecodeError):
                    decode_json_line(raw_payload)


if __name__ == "__main__":
    unittest.main()
