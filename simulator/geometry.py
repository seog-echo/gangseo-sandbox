from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict, Tuple

import numpy as np

PositionMap = Dict[int, Tuple[float, float]]


@dataclass(slots=True)
class LeadGeometry:
    positions_mm: PositionMap
    hotspot_indices: tuple[int, ...]
    hotspot_decay_mm: float
    baseline_floor: float


def build_depth_positions(n_contacts: int = 8, spacing_mm: float = 2.0) -> PositionMap:
    return {idx: (0.0, float(idx) * spacing_mm) for idx in range(n_contacts)}


def build_paddle_positions(row_spacing_mm: float = 8.0, col_spacing_mm: float = 10.0) -> PositionMap:
    return {
        0: (0.0, 0.0),
        1: (col_spacing_mm, 0.0),
        2: (2.0 * col_spacing_mm, 0.0),
        3: (3.0 * col_spacing_mm, 0.0),
        4: (0.0, row_spacing_mm),
        5: (col_spacing_mm, row_spacing_mm),
        6: (2.0 * col_spacing_mm, row_spacing_mm),
        7: (3.0 * col_spacing_mm, row_spacing_mm),
    }


def euclidean_distance(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    return float(sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2))


def distance_to_hotspots(positions_mm: PositionMap, contact_index: int, hotspot_indices: tuple[int, ...]) -> float:
    if not hotspot_indices:
        return 0.0
    contact_pos = positions_mm[contact_index]
    hotspot_distances = [euclidean_distance(contact_pos, positions_mm[idx]) for idx in hotspot_indices]
    return float(np.min(hotspot_distances))


def spatial_weight(distance_mm: float, decay_mm: float, floor: float) -> float:
    decay = max(float(decay_mm), 1e-12)
    floor = float(np.clip(floor, 0.0, 1.0))
    return float(floor + (1.0 - floor) * np.exp(-max(distance_mm, 0.0) / decay))


def channel_name(side: str, lead_kind: str, contact_index: int) -> str:
    return f"{side}_{lead_kind}_{contact_index + 1}"
