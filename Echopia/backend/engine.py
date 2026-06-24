"""Echopia engine: a thin wrapper around the NODES DBSArrayModel.

It owns the single source of truth for what the simulated patient's brain is
doing right now: the avatar's behavioral *state* (Rest / Movement / Sleep) and
the *stimulation* being applied. Each tick it advances NODES one chunk, pulls
the four "hotspot" display channels (one per lead), measures STN beta power
(the aDBS biomarker), resolves the applied stimulation amplitude for the
current mode, and drains the IPG battery a little.

The game front-end never talks to NODES directly -- it sends control messages
to this engine and receives a compact per-tick payload back. That keeps NODES
untouched and makes a future hardware sink (NODES_HIL -> NI-9263 -> IPG) just
another subscriber to the same state/stim stream.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

import numpy as np
from scipy.signal import welch

# Reuse the NODES simulator package without modifying it.
_NODES_DIR = Path(__file__).resolve().parents[2] / "NODES"
if str(_NODES_DIR) not in sys.path:
    sys.path.insert(0, str(_NODES_DIR))

from simulator import DBSArrayModel, StimulationCommand  # noqa: E402

# --- Channel selection -------------------------------------------------------
# One monopolar "hotspot" contact per lead drives the 4 plots in the game.
# Depth hotspot is contact index 3 (channel name *_depth_4); paddle hotspot is
# index 2 (*_paddle_3). NODES names channels as f"{side}_{lead}_{index+1}".
DEPTH_HOTSPOT_INDEX = 3
PADDLE_HOTSPOT_INDEX = 2

DISPLAY_CHANNELS = {
    "paddleL": f"left_paddle_{PADDLE_HOTSPOT_INDEX + 1}",
    "paddleR": f"right_paddle_{PADDLE_HOTSPOT_INDEX + 1}",
    "depthL": f"left_depth_{DEPTH_HOTSPOT_INDEX + 1}",
    "depthR": f"right_depth_{DEPTH_HOTSPOT_INDEX + 1}",
}

STIM_FREQUENCY_HZ = 130.0
BETA_BAND_HZ = (13.0, 30.0)

# Adaptive (state-lookup) amplitude schedule, mA.
ADAPTIVE_STATE_MA = {"Sleep": 1.0, "Rest": 2.0, "Movement": 3.0}

# Continuous-mode amplitude bounds the patient can dial on the phone.
AMP_MIN_MA = 1.0
AMP_MAX_MA = 3.0

# Beta power that maps to a "full" bar / max adaptive drive, normalized PER LEAD
# to that lead's own untreated (Rest, stim-off) baseline — the way real aDBS
# systems calibrate against a per-patient, per-hemisphere beta baseline. NODES'
# independent per-contact realizations give the two STN hotspots different raw
# beta power (left ~1.7x right at seed 7), so a single shared reference made the
# left bar read much fuller; per-lead refs make the two hemispheres comparable.
# (Calibrated with the engine's own 1 s/nperseg-256 estimator; see git history.)
# Refs set a bit above each lead's median baseline so the bar sits ~0.8 at rest
# and rarely saturates given the noisy 1 s estimate.
BETA_REF = {"depthL": 10.0, "depthR": 5.0}
BETA_REF_DEFAULT = 8.0
# One-pole smoothing of the displayed/controlled beta (reduces 1 s-window jitter).
BETA_SMOOTH = 0.25

# IPG battery model (cosmetic): a small idle drain plus an amplitude-dependent
# term, scaled so a demo session discharges slowly (tens of minutes), plus a
# charge rate used while the patient wears the IPG charger headband.
BATTERY_IDLE_DRAIN_PER_S = 2.0e-5
BATTERY_STIM_DRAIN_PER_MA_S = 6.0e-5
BATTERY_CHARGE_PER_S = 6.0e-3


@dataclass
class StimControl:
    mode: str = "off"  # "off" | "continuous" | "adaptive"
    adaptive_kind: str = "state"  # "state" | "closed_loop"
    amplitude_ma: float = 2.0  # user-set, used in continuous mode
    left_contact: int = DEPTH_HOTSPOT_INDEX
    right_contact: int = DEPTH_HOTSPOT_INDEX


@dataclass
class Control:
    state: str = "Rest"  # "Rest" | "Movement" | "Sleep"
    stim: StimControl = field(default_factory=StimControl)


class EchopiaEngine:
    def __init__(self, tick_hz: float = 20.0, seed: int = 7):
        self.model = DBSArrayModel(seed=seed)
        self.fs = self.model.fs
        self.tick_hz = tick_hz
        self.n_samples = max(1, round(self.fs / tick_hz))
        self.dt = self.n_samples / self.fs

        self.control = Control()
        self.battery = 0.85
        self.charging = False
        self.t = 0.0

        # ~1 s rolling buffers for the two depth channels (beta biomarker).
        self._beta_buflen = self.fs
        self._beta_buf: Dict[str, np.ndarray] = {
            "depthL": np.zeros(self._beta_buflen, dtype=np.float32),
            "depthR": np.zeros(self._beta_buflen, dtype=np.float32),
        }
        self._beta_norm = {"depthL": 0.0, "depthR": 0.0}

        # Slew-limited applied amplitudes (mA) so transitions look natural.
        self._applied = {"left": 0.0, "right": 0.0}

    # -- control input --------------------------------------------------------
    def apply_control(self, msg: dict) -> None:
        """Update state/stim from a (validated-ish) front-end message."""
        state = msg.get("state")
        if state in ("Rest", "Movement", "Sleep"):
            self.control.state = state

        if "charging" in msg:
            self.charging = bool(msg["charging"])

        stim = msg.get("stim") or {}
        c = self.control.stim
        if stim.get("mode") in ("off", "continuous", "adaptive"):
            c.mode = stim["mode"]
        if stim.get("adaptive_kind") in ("state", "closed_loop"):
            c.adaptive_kind = stim["adaptive_kind"]
        if "amplitude_ma" in stim:
            try:
                c.amplitude_ma = float(np.clip(float(stim["amplitude_ma"]), AMP_MIN_MA, AMP_MAX_MA))
            except (TypeError, ValueError):
                pass
        left = stim.get("left") or {}
        right = stim.get("right") or {}
        if "contact" in left:
            c.left_contact = int(left["contact"])
        if "contact" in right:
            c.right_contact = int(right["contact"])

    # -- amplitude resolution -------------------------------------------------
    def _target_amplitudes(self) -> tuple[float, float]:
        c = self.control.stim
        if c.mode == "off":
            return 0.0, 0.0
        if c.mode == "continuous":
            amp = float(np.clip(c.amplitude_ma, AMP_MIN_MA, AMP_MAX_MA))
            return amp, amp
        # adaptive
        if c.adaptive_kind == "closed_loop":
            # Simple, stable proportional biomarker controller: more beta -> more
            # drive, bounded to [AMP_MIN, AMP_MAX]. Per-side from each STN's beta.
            ampL = AMP_MIN_MA + (AMP_MAX_MA - AMP_MIN_MA) * float(np.clip(self._beta_norm["depthL"], 0.0, 1.0))
            ampR = AMP_MIN_MA + (AMP_MAX_MA - AMP_MIN_MA) * float(np.clip(self._beta_norm["depthR"], 0.0, 1.0))
            return ampL, ampR
        # adaptive state-lookup
        amp = ADAPTIVE_STATE_MA.get(self.control.state, 2.0)
        return amp, amp

    def _slew(self, current: float, target: float, rate_ma_per_s: float = 4.0) -> float:
        step = rate_ma_per_s * self.dt
        if target > current:
            return min(target, current + step)
        return max(target, current - step)

    # -- beta biomarker -------------------------------------------------------
    def _push_beta(self, key: str, chunk: np.ndarray) -> None:
        buf = self._beta_buf[key]
        n = chunk.size
        if n >= buf.size:
            buf[:] = chunk[-buf.size:]
        else:
            buf[:-n] = buf[n:]
            buf[-n:] = chunk
        nperseg = min(256, buf.size)
        freqs, psd = welch(buf, fs=self.fs, nperseg=nperseg)
        mask = (freqs >= BETA_BAND_HZ[0]) & (freqs <= BETA_BAND_HZ[1])
        power = float(np.trapezoid(psd[mask], freqs[mask])) if mask.any() else 0.0
        ref = BETA_REF.get(key, BETA_REF_DEFAULT)
        target = float(np.clip(power / ref, 0.0, 1.0))
        self._beta_norm[key] += BETA_SMOOTH * (target - self._beta_norm[key])

    # -- main tick ------------------------------------------------------------
    def tick(self) -> dict:
        tgtL, tgtR = self._target_amplitudes()
        self._applied["left"] = self._slew(self._applied["left"], tgtL)
        self._applied["right"] = self._slew(self._applied["right"], tgtR)

        stim_commands = {}
        if self._applied["left"] > 1e-3:
            stim_commands["left"] = StimulationCommand(
                "left", self.control.stim.left_contact, self._applied["left"], STIM_FREQUENCY_HZ
            )
        if self._applied["right"] > 1e-3:
            stim_commands["right"] = StimulationCommand(
                "right", self.control.stim.right_contact, self._applied["right"], STIM_FREQUENCY_HZ
            )

        out = self.model.simulate_chunk(
            stim_commands=stim_commands or None,
            n_samples=self.n_samples,
            state=self.control.state,
        )

        channels = {key: out[name] for key, name in DISPLAY_CHANNELS.items()}
        self._push_beta("depthL", channels["depthL"])
        self._push_beta("depthR", channels["depthR"])

        # Battery drain (and charge while wearing the IPG charger).
        amp_total = self._applied["left"] + self._applied["right"]
        drain = (BATTERY_IDLE_DRAIN_PER_S + BATTERY_STIM_DRAIN_PER_MA_S * amp_total) * self.dt
        charge = BATTERY_CHARGE_PER_S * self.dt if self.charging else 0.0
        self.battery = min(1.0, max(0.0, self.battery - drain + charge))

        self.t += self.dt

        return {
            "t": round(self.t, 4),
            "fs": self.fs,
            "n": self.n_samples,
            "state": self.control.state,
            "channels": {k: np.round(v, 3).tolist() for k, v in channels.items()},
            "beta": {k: round(v, 4) for k, v in self._beta_norm.items()},
            "stim_applied": {
                "left": round(self._applied["left"], 3),
                "right": round(self._applied["right"], 3),
                "mode": self.control.stim.mode,
                "adaptive_kind": self.control.stim.adaptive_kind,
            },
            "battery": round(self.battery, 4),
            "charging": self.charging,
        }


if __name__ == "__main__":
    # Self-test: print beta + applied amp across states and stim modes.
    eng = EchopiaEngine()

    def run(label, secs=2.0):
        ticks = int(secs * eng.tick_hz)
        last = None
        for _ in range(ticks):
            last = eng.tick()
        print(
            f"{label:30s} state={last['state']:8s} "
            f"betaL={last['beta']['depthL']:.3f} betaR={last['beta']['depthR']:.3f} "
            f"appliedL={last['stim_applied']['left']:.2f}mA appliedR={last['stim_applied']['right']:.2f}mA "
            f"batt={last['battery']*100:.1f}%"
        )

    eng.apply_control({"state": "Rest", "stim": {"mode": "off"}})
    run("Rest, stim OFF")
    eng.apply_control({"state": "Movement", "stim": {"mode": "off"}})
    run("Movement, stim OFF")
    eng.apply_control({"state": "Sleep", "stim": {"mode": "off"}})
    run("Sleep, stim OFF")
    eng.apply_control({"state": "Rest", "stim": {"mode": "continuous", "amplitude_ma": 3.0}})
    run("Rest, continuous 3mA")
    eng.apply_control({"state": "Rest", "stim": {"mode": "adaptive", "adaptive_kind": "state"}})
    run("Rest, adaptive(state)")
    eng.apply_control({"state": "Movement", "stim": {"mode": "adaptive", "adaptive_kind": "state"}})
    run("Movement, adaptive(state)")
    eng.apply_control({"state": "Rest", "stim": {"mode": "adaptive", "adaptive_kind": "closed_loop"}})
    run("Rest, adaptive(closed_loop)", secs=4.0)
