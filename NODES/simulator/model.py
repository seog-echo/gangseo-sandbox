from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

import numpy as np

from .baseline import (
    ContactBaseline,
    generate_contact_baseline,
    generate_shared_beta,
    generate_shared_gamma,
    generate_shared_slow_wave,
)
from .config import LeadProfile, SimulatorConfig, default_config, get_state_coherence, get_state_modifiers
from .geometry import channel_name, distance_to_hotspots, spatial_weight


# Behavioral-state scalar fields smoothed tick-by-tick toward their target.
_STATE_FIELDS = ("beta_scalar", "gamma_scalar", "sleep_blend", "a50_scalar", "suppression_scalar")
# Coherence loading fields smoothed tick-by-tick.
_COHERENCE_FIELDS = ("beta_depth", "beta_paddle", "beta_bilateral", "gamma", "delta")


@dataclass(slots=True)
class ContactState:
    baseline_cursor: int = 0
    env_uv: float = 0.0
    jitter_hz: float = 0.0
    phase_half: float = 0.0
    phase_artifact: float = 0.0
    phase_artifact_2f: float = 0.0


@dataclass(slots=True)
class StimulationCommand:
    side: str
    contact_index: int | None
    amplitude_ma: float
    frequency_hz: float = 130.0


class DBSArrayModel:
    def __init__(self, config: SimulatorConfig | None = None, seed: int = 7):
        self.config = config or default_config()
        self.fs = self.config.fs
        self._rng = np.random.default_rng(seed)

        # Per-contact decomposed baselines (background + band components).
        self._contacts: Dict[str, ContactBaseline] = {}
        self._states: Dict[str, ContactState] = {}
        self._specs: Dict[str, tuple[str, str, int]] = {}

        for side in ("left", "right"):
            for lead_kind in ("depth", "paddle"):
                geometry = self.config.lead_geometries[(side, lead_kind)]
                profile = self.config.lead_profiles[(side, lead_kind)]
                for contact_index in range(len(geometry.positions_mm)):
                    name = channel_name(side, lead_kind, contact_index)
                    self._contacts[name] = generate_contact_baseline(
                        profile=profile,
                        geometry=geometry,
                        contact_index=contact_index,
                        fs=self.fs,
                        duration_s=self.config.baseline_duration_s,
                        seed=seed + 100 * (len(self._specs) + 1),
                        sleep_slow_wave_gain=profile.sleep_slow_wave_gain,
                    )
                    self._states[name] = ContactState()
                    self._specs[name] = (side, lead_kind, contact_index)

        # Shared oscillators that create inter-channel coherence: one global
        # delta (all leads), and per-hemisphere beta and gamma (STN+M1 each side).
        n = int(self.fs * self.config.baseline_duration_s)
        self._shared_delta = generate_shared_slow_wave(n, self.fs, seed + 9001)
        self._shared_beta = {
            "left": generate_shared_beta(n, self.fs, seed + 9101),
            "right": generate_shared_beta(n, self.fs, seed + 9102),
        }
        # One bilateral STN beta oscillator shared by left+right depth leads.
        self._shared_beta_bilat = generate_shared_beta(n, self.fs, seed + 9300)
        self._shared_gamma = {
            "left": generate_shared_gamma(n, self.fs, seed + 9201),
            "right": generate_shared_gamma(n, self.fs, seed + 9202),
        }
        self._shared_cursor = 0
        self._shared_size = self._shared_delta.size

        # Smoothed (one-pole) state scalars per lead kind; initialized to Rest.
        self._state_current: Dict[str, Dict[str, float]] = {
            lead_kind: {field: getattr(get_state_modifiers("Rest", lead_kind), field) for field in _STATE_FIELDS}
            for lead_kind in ("depth", "paddle")
        }
        # Smoothed coherence coefficients (model-level), initialized to Rest.
        rest_coh = get_state_coherence("Rest")
        self._coherence_current: Dict[str, float] = {f: getattr(rest_coh, f) for f in _COHERENCE_FIELDS}

    def reset(self) -> None:
        for key in self._states:
            self._states[key] = ContactState()
        self._shared_cursor = 0
        for lead_kind in ("depth", "paddle"):
            rest = get_state_modifiers("Rest", lead_kind)
            self._state_current[lead_kind] = {field: getattr(rest, field) for field in _STATE_FIELDS}
        rest_coh = get_state_coherence("Rest")
        self._coherence_current = {f: getattr(rest_coh, f) for f in _COHERENCE_FIELDS}

    def _update_state_scalars(self, state: str, dt_s: float) -> None:
        alpha = 1.0 - np.exp(-dt_s / (self.config.state_transition_tau_s + 1e-12))
        for lead_kind in ("depth", "paddle"):
            target = get_state_modifiers(state, lead_kind)
            current = self._state_current[lead_kind]
            for field in _STATE_FIELDS:
                current[field] += alpha * (getattr(target, field) - current[field])

    def _update_coherence(self, state: str, dt_s: float) -> None:
        alpha = 1.0 - np.exp(-dt_s / (self.config.state_transition_tau_s + 1e-12))
        target = get_state_coherence(state)
        for field in _COHERENCE_FIELDS:
            self._coherence_current[field] += alpha * (getattr(target, field) - self._coherence_current[field])

    @staticmethod
    def _hill(amplitude_ma: float, a50: float, n: float) -> float:
        a = max(0.0, float(amplitude_ma))
        return float((a**n) / (a**n + a50**n + 1e-12))

    @staticmethod
    def _freq_gain(half_freq_hz: float, center_hz: float, sigma_hz: float) -> float:
        return float(np.exp(-0.5 * ((half_freq_hz - center_hz) / (sigma_hz + 1e-12)) ** 2))

    def _update_env(self, env_uv: float, target_uv: float, dt_s: float, profile: LeadProfile) -> float:
        tau = profile.tau_on_s if target_uv >= env_uv else profile.tau_off_s
        alpha = 1.0 - np.exp(-dt_s / (tau + 1e-12))
        return env_uv + alpha * (target_uv - env_uv)

    def _update_jitter(self, jitter_hz: float, dt_s: float) -> float:
        tau = self.config.jitter_ou_tau_s
        sigma = self.config.jitter_ou_sigma_hz
        dt = max(float(dt_s), 1e-12)
        decay = np.exp(-dt / (tau + 1e-12))
        var_dt = (sigma**2) * (tau / 2.0) * (1.0 - np.exp(-2.0 * dt / (tau + 1e-12)))
        noise = self._rng.normal(0.0, 1.0)
        return (jitter_hz * decay) + np.sqrt(max(var_dt, 0.0)) * noise

    @staticmethod
    def _circular(arr: np.ndarray, cursor: int, n_samples: int) -> np.ndarray:
        """Read n_samples from a circular buffer starting at cursor (no mutation)."""
        size = arr.size
        end = cursor + n_samples
        if end <= size:
            return arr[cursor:end]
        split = size - cursor
        return np.concatenate((arr[cursor:], arr[: n_samples - split]))

    def _pull_contact(self, channel: str, n_samples: int):
        """Pull aligned chunks of a contact's components and advance its cursor."""
        cb = self._contacts[channel]
        state = self._states[channel]
        cur = state.baseline_cursor
        chunks = (
            self._circular(cb.background, cur, n_samples),
            self._circular(cb.beta_peak, cur, n_samples),
            self._circular(cb.gamma_activity, cur, n_samples),
            self._circular(cb.slow_indep, cur, n_samples),
        )
        state.baseline_cursor = (cur + n_samples) % cb.background.size
        return chunks

    def _stim_spatial_scale(
        self,
        side: str,
        lead_kind: str,
        contact_index: int,
        stim_command: StimulationCommand | None,
    ) -> float:
        if stim_command is None or stim_command.contact_index is None or stim_command.amplitude_ma <= 0.0:
            return 0.0

        if lead_kind == "depth":
            if side != stim_command.side:
                return 0.0
            distance_mm = abs(contact_index - stim_command.contact_index) * 2.0
            return spatial_weight(distance_mm, self.config.lead_profiles[(side, "depth")].stim_decay_mm, 0.08)

        if side != stim_command.side:
            return 0.0

        depth_geometry = self.config.lead_geometries[(side, "depth")]
        depth_profile = self.config.lead_profiles[(side, "depth")]
        paddle_geometry = self.config.lead_geometries[(side, "paddle")]
        paddle_profile = self.config.lead_profiles[(side, "paddle")]

        depth_drive_distance = distance_to_hotspots(
            depth_geometry.positions_mm,
            stim_command.contact_index,
            depth_geometry.hotspot_indices,
        )
        paddle_local_distance = distance_to_hotspots(
            paddle_geometry.positions_mm,
            contact_index,
            paddle_geometry.hotspot_indices,
        )
        depth_drive = spatial_weight(depth_drive_distance, depth_profile.stim_decay_mm, 0.12)
        paddle_local = spatial_weight(paddle_local_distance, paddle_profile.stim_decay_mm, 0.08)
        return depth_drive * paddle_local

    @staticmethod
    def _mix(indep_chunk, shared_chunk, shared_amp, r, gradient):
        """Energy-preserving mix of an independent band component with a shared
        oscillator. Shared power fraction at this contact is ``r * gradient**2``
        (so MSC between two hotspot contacts approaches ``r**2``); total band
        variance is preserved regardless of r."""
        sf = max(0.0, min(1.0, r * gradient * gradient))  # shared power fraction
        shared_coeff = np.sqrt(sf)
        indep_coeff = np.sqrt(max(0.0, 1.0 - sf))
        return indep_coeff * indep_chunk + shared_coeff * shared_chunk * shared_amp

    @staticmethod
    def _mix3(indep_chunk, shared_a, shared_b, shared_amp, r_a, r_b, gradient):
        """Energy-preserving mix of an independent component with TWO shared
        oscillators (e.g. STN beta = hemisphere STN-M1 + bilateral STN-STN).
        Shared fractions are capped so the independent fraction stays >= 0."""
        g2 = gradient * gradient
        sa = max(0.0, r_a * g2)
        sb = max(0.0, r_b * g2)
        total = sa + sb
        if total > 0.98:
            scale = 0.98 / total
            sa *= scale
            sb *= scale
        indep_coeff = np.sqrt(max(0.0, 1.0 - sa - sb))
        return (
            indep_coeff * indep_chunk
            + np.sqrt(sa) * shared_a * shared_amp
            + np.sqrt(sb) * shared_b * shared_amp
        )

    def simulate_chunk(
        self,
        stim_commands: Mapping[str, StimulationCommand] | None = None,
        n_samples: int = 500,
        include_subharmonics: bool = False,
        state: str = "Rest",
    ) -> Dict[str, np.ndarray]:
        outputs: Dict[str, np.ndarray] = {}
        stim_commands = dict(stim_commands or {})
        dt = n_samples / self.fs

        self._update_state_scalars(state, dt)
        self._update_coherence(state, dt)

        # Shared oscillator chunks (one realization for the whole array this tick).
        cur = self._shared_cursor
        shared_delta = self._circular(self._shared_delta, cur, n_samples)
        shared_beta = {s: self._circular(self._shared_beta[s], cur, n_samples) for s in ("left", "right")}
        shared_beta_bilat = self._circular(self._shared_beta_bilat, cur, n_samples)
        shared_gamma = {s: self._circular(self._shared_gamma[s], cur, n_samples) for s in ("left", "right")}
        self._shared_cursor = (cur + n_samples) % self._shared_size

        r_beta_depth = self._coherence_current["beta_depth"]
        r_beta_paddle = self._coherence_current["beta_paddle"]
        r_beta_bilat = self._coherence_current["beta_bilateral"]
        r_gamma = self._coherence_current["gamma"]
        r_delta = self._coherence_current["delta"]

        t = np.arange(n_samples, dtype=np.float64) / self.fs

        for name, (side, lead_kind, contact_index) in self._specs.items():
            profile = self.config.lead_profiles[(side, lead_kind)]
            scalars = self._state_current[lead_kind]
            cb = self._contacts[name]
            stim_command = stim_commands.get(side)

            background, beta_peak, gamma_activity, slow_indep = self._pull_contact(name, n_samples)

            stim_scale = self._stim_spatial_scale(side, lead_kind, contact_index, stim_command)
            amplitude_ma = 0.0 if stim_command is None else max(0.0, stim_command.amplitude_ma)
            frequency_hz = 130.0 if stim_command is None else max(0.01, float(stim_command.frequency_hz))

            beta_factor = 1.0
            if amplitude_ma >= profile.beta_suppression_start_ma and stim_scale > 0.0:
                suppression = (amplitude_ma - profile.beta_suppression_start_ma) / (
                    profile.beta_suppression_end_ma - profile.beta_suppression_start_ma
                )
                suppression = min(1.0, max(0.0, suppression))
                beta_factor = 1.0 - profile.beta_suppression_strength * scalars["suppression_scalar"] * suppression * stim_scale

            g = cb.gradient

            # Additive beta peak: depth (STN) mixes the hemisphere STN-M1
            # oscillator AND the bilateral STN-STN oscillator; paddle (M1) mixes
            # only the hemisphere oscillator. Stim suppression + state scalar act
            # on this peak only — the 1/f floor in `background` is never scaled,
            # so a suppressed band shrinks toward the floor, never below it.
            if lead_kind == "depth":
                beta_peak_mixed = self._mix3(
                    beta_peak, shared_beta[side], shared_beta_bilat, cb.beta_amp, r_beta_depth, r_beta_bilat, g
                )
            else:
                beta_peak_mixed = self._mix(beta_peak, shared_beta[side], cb.beta_amp, r_beta_paddle, g)
            beta_term = beta_peak_mixed * beta_factor * scalars["beta_scalar"]

            # Additive gamma activity, scaled on top of the 1/f floor.
            gamma_band = self._mix(gamma_activity, shared_gamma[side], cb.gamma_amp, r_gamma, g)
            gamma_term = scalars["gamma_scalar"] * gamma_band

            # Additive Sleep slow wave, gated by the sleep blend.
            blend = scalars["sleep_blend"]
            slow_term = blend * self._mix(slow_indep, shared_delta, cb.slow_amp, r_delta, g)

            signal = background + beta_term + gamma_term + slow_term

            half_freq = frequency_hz / 2.0
            gain = self._freq_gain(half_freq, profile.freq_center_hz, profile.freq_sigma_hz)
            a50_effective = profile.a50_ma * scalars["a50_scalar"]
            target_env = profile.amax_uv * self._hill(amplitude_ma, a50_effective, profile.hill_n) * gain
            cstate = self._states[name]
            cstate.env_uv = self._update_env(cstate.env_uv, target_env * stim_scale, dt, profile)
            cstate.jitter_hz = self._update_jitter(cstate.jitter_hz, dt)

            half_freq_actual = max(0.01, half_freq + cstate.jitter_hz)
            half = cstate.env_uv * np.sin(2 * np.pi * half_freq_actual * t + cstate.phase_half)

            if include_subharmonics:
                third = 0.25 * cstate.env_uv * np.sin(2 * np.pi * max(0.01, frequency_hz / 3.0) * t)
                quarter = 0.15 * cstate.env_uv * np.sin(2 * np.pi * max(0.01, frequency_hz / 4.0) * t)
            else:
                third = 0.0
                quarter = 0.0

            artifact_amp = profile.artifact_scale_uv_per_ma * amplitude_ma * stim_scale
            artifact = artifact_amp * np.sin(2 * np.pi * frequency_hz * t + cstate.phase_artifact)
            artifact_2f = (
                artifact_amp
                * profile.artifact_2f_ratio
                * np.sin(2 * np.pi * 2.0 * frequency_hz * t + cstate.phase_artifact_2f)
            )

            noise = self._rng.normal(0.0, self.config.noise_uv, size=n_samples)
            signal = signal + half + third + quarter + artifact + artifact_2f + noise
            outputs[name] = signal.astype(np.float32)

            cstate.phase_half = (cstate.phase_half + 2 * np.pi * half_freq_actual * dt) % (2 * np.pi)
            cstate.phase_artifact = (cstate.phase_artifact + 2 * np.pi * frequency_hz * dt) % (2 * np.pi)
            cstate.phase_artifact_2f = (cstate.phase_artifact_2f + 2 * np.pi * 2 * frequency_hz * dt) % (2 * np.pi)

        return outputs
