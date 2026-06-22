from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Dict

import numpy as np

from .geometry import LeadGeometry, euclidean_distance


@dataclass(slots=True)
class BipolarSelection:
    side: str
    lead_kind: str
    contact_a: int
    contact_b: int
    normalize: bool = True


def convert_bipolar(
    monopolar_signals: Dict[str, np.ndarray],
    selection: BipolarSelection,
    geometry: LeadGeometry,
) -> tuple[np.ndarray, dict[str, float]]:
    if selection.contact_a == selection.contact_b:
        raise ValueError("Bipolar contacts must be different")

    name_a = f"{selection.side}_{selection.lead_kind}_{selection.contact_a + 1}"
    name_b = f"{selection.side}_{selection.lead_kind}_{selection.contact_b + 1}"
    if name_a not in monopolar_signals or name_b not in monopolar_signals:
        raise KeyError(f"Missing monopolar channels for {name_a} or {name_b}")

    signal_a = monopolar_signals[name_a]
    signal_b = monopolar_signals[name_b]
    bipolar = signal_a - signal_b
    if selection.normalize:
        bipolar = bipolar / sqrt(2.0)

    dist_mm = euclidean_distance(
        geometry.positions_mm[selection.contact_a],
        geometry.positions_mm[selection.contact_b],
    )
    metadata = {
        "distance_mm": float(dist_mm),
        "normalize": selection.normalize,
    }
    return bipolar.astype(np.float32), metadata
