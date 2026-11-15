"""Tankscii : Terminal Artillery shared package constants and modules."""

from __future__ import annotations

DEFAULT_CLIENT_VERSION: str = "1.0.0"
DEFAULT_P1_NICKNAME: str = "CobraKing"
DEFAULT_P2_NICKNAME: str = "Thunderbolt"
MIN_PORT: int = 1024
MAX_PORT: int = 65535
RECV_BUFFER_SIZE: int = 4096
MAX_PLAYERS: int = 2
TURN_DURATION_SECONDS: int = 60
MAX_ROUNDS: int = 20

__all__ = [
    "DEFAULT_CLIENT_VERSION",
    "DEFAULT_P1_NICKNAME",
    "DEFAULT_P2_NICKNAME",
    "MIN_PORT",
    "MAX_PORT",
    "RECV_BUFFER_SIZE",
    "MAX_PLAYERS",
    "TURN_DURATION_SECONDS",
    "MAX_ROUNDS",
]
