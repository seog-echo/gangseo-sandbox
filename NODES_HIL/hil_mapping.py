#!/usr/bin/env python3
"""Map a measured input signal onto NODES stimulation parameters.

This is the heart of the closed loop: the amplitude and frequency extracted from
the NI-9222 input (see :mod:`signal_metrics`) are converted into a
``StimulationCommand`` that drives the NODES model, which in turn reshapes every
channel's neural signal.

Defaults (chosen with the user):
    * Amplitude: 4 V peak  -> 4.0 mA  (i.e. 1.0 mA per volt), clamped to [0, 4] mA.
    * Frequency: direct pass-through, clamped to NODES' [10, 200] Hz stim range.
    * Target: LEFT side only, depth contact index 3 (contact 4, the STN hotspot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from simulator.model import StimulationCommand

# NODES stimulation limits (mirror the ranges the GUI exposes).
STIM_AMP_MIN_MA = 0.0
STIM_AMP_MAX_MA = 4.0
STIM_FREQ_MIN_HZ = 10.0
STIM_FREQ_MAX_HZ = 200.0


@dataclass(slots=True)
class HilMapping:
    """Calibration for input-signal -> stim-parameter conversion.

    All fields are live-editable from the GUI so the loop can be retuned without
    restarting a run.
    """

    # Target stimulation site.
    side: str = "left"                 # "left" or "right"
    contact_index: int = 3             # 0-based; 3 == contact 4 (depth hotspot)

    # Amplitude: stim_ma = amp_gain_ma_per_v * measured_amplitude_v  (then clamped).
    amp_gain_ma_per_v: float = 1.0     # 4 V pk -> 4 mA
    amp_deadband_v: float = 0.02       # ignore tiny noise below this input amplitude

    # Frequency: stim_hz = freq_gain * measured_hz + freq_offset_hz (then clamped).
    freq_gain: float = 1.0             # 1.0 == direct pass-through
    freq_offset_hz: float = 0.0

    # When the input is below the deadband, hold this frequency (stim is off anyway,
    # but a sane value avoids a 0 Hz command being sent to the model).
    idle_frequency_hz: float = 130.0


@dataclass(slots=True)
class StimDrive:
    """The resolved stim state for one tick, ready to display and command."""

    enabled: bool
    side: str
    contact_index: int
    amplitude_ma: float
    frequency_hz: float


def resolve_drive(amplitude_v: float, frequency_hz: float, mapping: HilMapping) -> StimDrive:
    """Convert a single measurement into a clamped stim drive."""
    amp_ma = mapping.amp_gain_ma_per_v * max(0.0, float(amplitude_v))
    amp_ma = _clamp(amp_ma, STIM_AMP_MIN_MA, STIM_AMP_MAX_MA)

    freq = mapping.freq_gain * float(frequency_hz) + mapping.freq_offset_hz
    freq = _clamp(freq, STIM_FREQ_MIN_HZ, STIM_FREQ_MAX_HZ)

    below_deadband = float(amplitude_v) < mapping.amp_deadband_v
    enabled = (amp_ma > STIM_AMP_MIN_MA) and not below_deadband
    if not enabled:
        amp_ma = 0.0
        freq = mapping.idle_frequency_hz

    return StimDrive(
        enabled=enabled,
        side=mapping.side,
        contact_index=int(mapping.contact_index),
        amplitude_ma=amp_ma,
        frequency_hz=freq,
    )


def drive_to_commands(drive: StimDrive) -> Dict[str, StimulationCommand]:
    """Build the ``stim_commands`` dict expected by ``DBSArrayModel.simulate_chunk``."""
    if not drive.enabled:
        return {}
    return {
        drive.side: StimulationCommand(
            side=drive.side,
            contact_index=drive.contact_index,
            amplitude_ma=drive.amplitude_ma,
            frequency_hz=drive.frequency_hz,
        )
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
