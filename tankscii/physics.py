"""Authoritative 2D battlefield physics, movement, trajectory, and damage engine."""

from __future__ import annotations

import math
from typing import Any

BOARD_WIDTH: int = 80
BOARD_HEIGHT: int = 24
BASE_TERRAIN_HEIGHT: int = 6
PLAYER_1_START_X: int = 10
PLAYER_2_START_X: int = 70
INITIAL_HP: int = 100
DIRECT_HIT_DAMAGE: int = 25
SHRAPNEL_HIT_DAMAGE: int = 10
SHRAPNEL_RADIUS: int = 2
CRATER_RADIUS: int = 2

GRAVITY_ACCELERATION: float = 60.0
CALIBRATED_RANGE_AT_75_POWER: float = 60.0
CALIBRATED_POWER_REFERENCE: float = 75.0


def create_initial_terrain(
    width: int = BOARD_WIDTH,
    base_height: int = BASE_TERRAIN_HEIGHT,
) -> list[int]:
    """Create the initial flat terrain elevation array of length ``width``.

    Args:
        width: Number of horizontal columns on the battlefield (default 80).
        base_height: Starting surface elevation for each column (default 6).

    Returns:
        A list of integers representing surface height ``y`` for each column ``x``.
    """
    return [base_height] * width


def create_initial_tanks(
    terrain_heights: list[int],
) -> dict[str, dict[str, int]]:
    """Create the initial tank state dictionary for Player_1 and Player_2.

    Args:
        terrain_heights: Per-column surface elevation list of length 80.

    Returns:
        A dictionary mapping player IDs to ``{"x": int, "y": int, "hp": int}``.
    """
    return {
        "Player_1": {
            "x": PLAYER_1_START_X,
            "y": terrain_heights[PLAYER_1_START_X],
            "hp": INITIAL_HP,
        },
        "Player_2": {
            "x": PLAYER_2_START_X,
            "y": terrain_heights[PLAYER_2_START_X],
            "hp": INITIAL_HP,
        },
    }


def stamp_tank_sprites(
    board_matrix: list[list[str]],
    tanks: dict[str, dict[str, int]],
) -> None:
    """Stamp 3-cell compact armored tank sprites onto `board_matrix` in place.

    Player_1 (facing right, 3 cells on row = y):
        [1>   (cols = x - 1, x, x + 1)

    Player_2 (facing left, 3 cells on row = y):
        <2]   (cols = x - 1, x, x + 1)

    Args:
        board_matrix: 24-row by 80-column character matrix modified in place.
        tanks: Dictionary mapping `"Player_1"` and `"Player_2"` to `{x, y, hp}`.
    """
    sprite_offsets: dict[str, list[tuple[int, int, str]]] = {
        "Player_1": [
            (-1, 0, "["),
            (0, 0, "1"),
            (1, 0, ">"),
        ],
        "Player_2": [
            (-1, 0, "<"),
            (0, 0, "2"),
            (1, 0, "]"),
        ],
    }

    for player_id, offsets in sprite_offsets.items():
        tank = tanks.get(player_id)
        if tank is None or int(tank.get("hp", 0)) <= 0:
            continue
        tank_x = int(tank["x"])
        tank_y = int(tank["y"])
        for dx, dy, char in offsets:
            col = tank_x + dx
            row = tank_y + dy
            if 0 <= col < BOARD_WIDTH and 0 <= row < BOARD_HEIGHT:
                board_matrix[row][col] = char


def build_board_matrix(
    terrain_heights: list[int],
    tanks: dict[str, dict[str, int]],
    craters: list[dict[str, int]] | None = None,
) -> list[list[str]]:
    """Build an 80x24 2D ASCII character matrix representing the battlefield.

    Row index ``y = 0`` corresponds to the bottom row of the battlefield and
    ``y = 23`` corresponds to the top sky row. Intact ground surface is
    rendered with ``=``, stepped crater surface floors with ``_``, and
    subterranean earth below the surface with ``:``.

    Args:
        terrain_heights: List of 80 integer terrain surface heights.
        tanks: Mapping of player IDs to tank ``x``, ``y``, and ``hp`` state.
        craters: Optional list of crater dictionaries ``{"x": int, "radius": int}``.

    Returns:
        A 24-row by 80-column 2D list of single-character ASCII strings.
    """
    board_matrix: list[list[str]] = [
        [" " for _ in range(BOARD_WIDTH)] for _ in range(BOARD_HEIGHT)
    ]

    crater_columns: set[int] = set()
    if craters:
        for crater in craters:
            center_x = int(crater.get("x", -1))
            radius = int(crater.get("radius", CRATER_RADIUS))
            for col in range(
                max(0, center_x - radius),
                min(BOARD_WIDTH - 1, center_x + radius) + 1,
            ):
                crater_columns.add(col)

    num_cols = min(BOARD_WIDTH, len(terrain_heights))
    for col in range(num_cols):
        surface_y = max(0, min(BOARD_HEIGHT - 1, terrain_heights[col]))
        for row in range(surface_y):
            board_matrix[row][col] = ":"
        if 0 <= surface_y < BOARD_HEIGHT:
            is_cratered = (
                col in crater_columns or surface_y < BASE_TERRAIN_HEIGHT
            )
            board_matrix[surface_y][col] = "_" if is_cratered else "="

    stamp_tank_sprites(board_matrix, tanks)
    return board_matrix


def apply_tank_move(
    tanks: dict[str, dict[str, int]],
    terrain_heights: list[int],
    player_id: str,
    direction: str,
    distance: int,
) -> list[list[int]]:
    """Move a player's tank across the terrain and record its coordinate trail.

    Clamps horizontal movement to ``0 <= x < BOARD_WIDTH`` and updates the
    tank's vertical coordinate ``y`` to rest on ``terrain_heights[new_x]``.

    Args:
        tanks: Authoritative tank state dictionary (mutated in-place).
        terrain_heights: Current 80-column terrain elevation list.
        player_id: Identifier of the moving player (``"Player_1"`` or ``"Player_2"``).
        direction: Movement direction (``"LEFT"`` or ``"RIGHT"``).
        distance: Number of columns to move (1 to 5).

    Returns:
        Ordered list of ``[x, y]`` coordinate pairs from start to end position.
    """
    tank = tanks[player_id]
    current_x = int(tank["x"])
    current_y = int(tank["y"])
    move_path: list[list[int]] = [[current_x, current_y]]

    step_delta = -1 if direction == "LEFT" else 1
    for _ in range(distance):
        next_x = max(0, min(BOARD_WIDTH - 1, current_x + step_delta))
        next_y = terrain_heights[next_x]
        current_x = next_x
        current_y = next_y
        if move_path[-1] != [current_x, current_y]:
            move_path.append([current_x, current_y])

    tank["x"] = current_x
    tank["y"] = current_y
    return move_path


def deform_terrain_with_crater(
    terrain_heights: list[int],
    craters: list[dict[str, int]],
    tanks: dict[str, dict[str, int]],
    impact_x: int,
    radius: int = CRATER_RADIUS,
) -> None:
    """Apply crater deformation around ``impact_x`` and settle tanks onto terrain.

    Appends ``{"x": impact_x, "radius": radius}`` to ``craters``, lowers
    ``terrain_heights[col]`` by 1 (clamped to ``>= 1``) within ``radius``
    columns of ``impact_x``, and updates each tank's ``y`` coordinate to match
    its underlying terrain elevation.

    Args:
        terrain_heights: Authoritative 80-column elevation list (mutated in-place).
        craters: Cumulative crater metadata list (mutated in-place).
        tanks: Authoritative tank state dictionary (mutated in-place).
        impact_x: Horizontal impact column index.
        radius: Horizontal crater radius (default 2).
    """
    craters.append({"x": int(impact_x), "radius": int(radius)})
    start_col = max(0, impact_x - radius)
    end_col = min(BOARD_WIDTH - 1, impact_x + radius)

    for col in range(start_col, end_col + 1):
        distance_from_center = abs(col - impact_x)
        depth_drop = 2 if distance_from_center == 0 else 1
        terrain_heights[col] = max(1, terrain_heights[col] - depth_drop)

    for tank in tanks.values():
        tank_x = int(tank["x"])
        if 0 <= tank_x < len(terrain_heights):
            tank["y"] = terrain_heights[tank_x]


def calculate_impact_damage(
    impact_x: int,
    impact_y: int,
    tanks: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Calculate ballistic impact damage for each tank based on horizontal distance.

    Damage rules:
    - Direct hit (``abs(tank_x - impact_x) == 0``): 25 HP damage.
    - Shrapnel hit (``1 <= abs(tank_x - impact_x) <= 2``): 10 HP damage.
    - Outside blast radius (``distance > 2``): 0 HP damage.

    Args:
        impact_x: Horizontal impact column index.
        impact_y: Vertical impact row index.
        tanks: Dictionary mapping player IDs to tank coordinates and HP.

    Returns:
        Dictionary mapping ``"Player_1"`` and ``"Player_2"`` to integer HP damage.
    """
    _ = impact_y
    damage_by_player: dict[str, int] = {"Player_1": 0, "Player_2": 0}

    for player_id, tank in tanks.items():
        horizontal_distance = abs(int(tank["x"]) - int(impact_x))
        if horizontal_distance == 0:
            damage_by_player[player_id] = DIRECT_HIT_DAMAGE
        elif horizontal_distance <= SHRAPNEL_RADIUS:
            damage_by_player[player_id] = SHRAPNEL_HIT_DAMAGE
        else:
            damage_by_player[player_id] = 0

    return damage_by_player


def simulate_ballistic_shot(
    shooter_id: str,
    angle_degrees: int,
    power_percent: int,
    tanks: dict[str, dict[str, int]],
    terrain_heights: list[int],
    craters: list[dict[str, int]],
) -> dict[str, Any]:
    """Simulate a ballistic projectile and apply resulting terrain and HP changes.

    Uses forward-relative firing angles (``0..180``) relative to the direction
    each tank faces: ``0-89 deg`` aims forward toward the opponent for BOTH
    ``Player_1`` (facing right ``+x``) and ``Player_2`` (facing left ``-x``),
    ``90 deg`` is straight up (``+y``), and ``91-180 deg`` aims backward.
    Calibrated such that a shot at ``angle_degrees=45`` and ``power_percent=75``
    travels 60 columns forward on level ground (e.g., between ``x=10`` and
    ``x=70`` in either direction).

    Args:
        shooter_id: Player ID firing the projectile (``"Player_1"`` or ``"Player_2"``).
        angle_degrees: Forward-relative launch angle in degrees (``0 <= angle_degrees <= 180``).
        power_percent: Launch power percentage (``1 <= power_percent <= 100``).
        tanks: Authoritative tank state dictionary (mutated in-place on hit).
        terrain_heights: Authoritative 80-column terrain heights (mutated in-place).
        craters: Cumulative crater list (mutated in-place on in-bounds impact).

    Returns:
        A dictionary containing:
        - ``"trajectory"``: ``list[list[int]]`` of deduplicated ``[x, y]`` points
        - ``"impact"``: ``{"coord": [impact_x, impact_y], "result": str}``
        - ``"damage_dealt"``: ``int`` (0, 10, or 25)
        - ``"damage_by_player"``: ``{"Player_1": int, "Player_2": int}``
    """
    shooter_tank = tanks[shooter_id]
    start_x = int(shooter_tank["x"])
    start_y = int(shooter_tank["y"])

    # Convert forward-relative angle (0..180) into world polar angle:
    # 0-89 deg aims forward toward the opponent for BOTH Player_1 (facing right)
    # and Player_2 (facing left), 90 deg is straight up, and 91-180 deg aims backward.
    if shooter_id == "Player_2":
        effective_angle = 180.0 - float(angle_degrees)
    else:
        effective_angle = float(angle_degrees)
    angle_radians = math.radians(effective_angle)
    effective_max_range = (
        float(power_percent) / CALIBRATED_POWER_REFERENCE
    ) * CALIBRATED_RANGE_AT_75_POWER
    initial_speed = math.sqrt(effective_max_range * GRAVITY_ACCELERATION)

    velocity_x = initial_speed * math.cos(angle_radians)
    velocity_y = initial_speed * math.sin(angle_radians)

    trajectory: list[list[int]] = [[start_x, start_y]]
    time_step = 0.01
    elapsed_time = 0.0
    max_simulation_steps = 5000

    out_of_bounds = False
    impact_x = start_x
    impact_y = start_y

    for _ in range(max_simulation_steps):
        elapsed_time += time_step
        pos_x = float(start_x) + velocity_x * elapsed_time
        pos_y = (
            float(start_y)
            + velocity_y * elapsed_time
            - 0.5 * GRAVITY_ACCELERATION * (elapsed_time**2)
        )
        current_vy = velocity_y - GRAVITY_ACCELERATION * elapsed_time

        rounded_x = int(round(pos_x))
        rounded_y = int(round(pos_y))

        if rounded_x < 0 or rounded_x >= BOARD_WIDTH:
            out_of_bounds = True
            impact_x = max(0, min(BOARD_WIDTH - 1, rounded_x))
            impact_y = max(0, min(BOARD_HEIGHT - 1, rounded_y))
            if trajectory[-1] != [impact_x, impact_y]:
                trajectory.append([impact_x, impact_y])
            break

        surface_height = terrain_heights[rounded_x]
        if pos_y <= float(surface_height) and current_vy <= 0.0:
            # Compute exact root when descending onto terrain at surface_height
            discriminant = (
                velocity_y**2
                - 2.0 * GRAVITY_ACCELERATION * (float(surface_height) - float(start_y))
            )
            if current_vy <= 0.0 and discriminant >= 0.0:
                exact_time = (
                    velocity_y + math.sqrt(discriminant)
                ) / GRAVITY_ACCELERATION
                exact_x = int(round(float(start_x) + velocity_x * exact_time))
            else:
                exact_x = rounded_x

            if exact_x < 0 or exact_x >= BOARD_WIDTH:
                out_of_bounds = True
                impact_x = max(0, min(BOARD_WIDTH - 1, exact_x))
                impact_y = max(0, min(BOARD_HEIGHT - 1, surface_height))
            else:
                impact_x = exact_x
                impact_y = terrain_heights[impact_x]

            if trajectory[-1] != [impact_x, impact_y]:
                trajectory.append([impact_x, impact_y])
            break

        clamped_y = max(0, min(BOARD_HEIGHT - 1, rounded_y))
        if trajectory[-1] != [rounded_x, clamped_y]:
            trajectory.append([rounded_x, clamped_y])

    if out_of_bounds:
        return {
            "trajectory": trajectory,
            "impact": {
                "coord": [impact_x, impact_y],
                "result": "MISS",
            },
            "damage_dealt": 0,
            "damage_by_player": {"Player_1": 0, "Player_2": 0},
        }

    damage_by_player = calculate_impact_damage(impact_x, impact_y, tanks)
    for player_id, damage in damage_by_player.items():
        if player_id in tanks and damage > 0:
            tanks[player_id]["hp"] = max(0, int(tanks[player_id]["hp"]) - damage)

    deform_terrain_with_crater(
        terrain_heights=terrain_heights,
        craters=craters,
        tanks=tanks,
        impact_x=impact_x,
        radius=CRATER_RADIUS,
    )

    opponent_id = "Player_2" if shooter_id == "Player_1" else "Player_1"
    opponent_damage = damage_by_player.get(opponent_id, 0)
    max_damage = max(damage_by_player.values(), default=0)
    damage_dealt = opponent_damage if opponent_damage > 0 else max_damage
    impact_result = "HIT" if max_damage > 0 else "GROUND"

    return {
        "trajectory": trajectory,
        "impact": {
            "coord": [impact_x, impact_y],
            "result": impact_result,
        },
        "damage_dealt": damage_dealt,
        "damage_by_player": damage_by_player,
    }
