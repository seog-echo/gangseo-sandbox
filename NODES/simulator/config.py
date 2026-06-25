from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

from .geometry import LeadGeometry, build_depth_positions, build_paddle_positions


@dataclass(slots=True)
class LeadProfile:
    name: str
    alpha: float
    beta_hz: float
    beta_low_hz: float
    beta_high_hz: float
    beta_uv: float
    beta_sigma_hz: float
    gamma_activity_uv: float
    baseline_rms_uv: float
    amax_uv: float
    a50_ma: float
    hill_n: float
    tau_on_s: float
    tau_off_s: float
    stim_decay_mm: float
    artifact_scale_uv_per_ma: float
    artifact_2f_ratio: float
    beta_suppression_start_ma: float
    beta_suppression_end_ma: float
    beta_suppression_strength: float
    freq_center_hz: float = 75.0
    freq_sigma_hz: float = 22.0
    # Sleep-state delta/theta slow-wave amplitude, as a multiple of this lead's
    # broadband RMS. Cortex (paddle/M1) is set higher than depth (STN) so the
    # cortical slow waves dominate, matching NREM physiology.
    sleep_slow_wave_gain: float = 0.42
    # Within-lead spatial correlation length (mm) for the broadband 1/f floor.
    # Two contacts separated by ``d`` share floor correlation ~ exp(-d/lambda),
    # so neighbors look alike in the time domain and divergence grows with
    # separation (volume conduction). Larger -> stronger monopolar similarity
    # (and stronger common-mode cancellation in bipolar derivations). This only
    # shapes the cross-contact correlation; each contact's PSD is unchanged.
    correlation_length_mm: float = 10.0


@dataclass(slots=True)
class SimulatorConfig:
    fs: int = 1024
    # Length of one rendered stream block. The model double-buffers blocks and
    # crossfades at the seam, re-randomizing every block, so the output never
    # repeats regardless of recording duration (no more fixed-loop replay).
    # Kept at 600 s so the 1/f floor's infra-slow normalization (and hence every
    # contact's band power) matches the pre-streaming model exactly; the floor's
    # variance below ~1/T Hz scales as sqrt(T), so a shorter block would shift
    # visible-band power. Each block is an independent realization regardless.
    block_duration_s: float = 600.0
    crossfade_s: float = 1.0
    # Retained for compatibility; no longer the stream period (see block_*).
    baseline_duration_s: int = 600
    noise_uv: float = 0.8
    jitter_ou_tau_s: float = 0.35
    jitter_ou_sigma_hz: float = 0.9
    # Time constant for smoothing behavioral-state scalar transitions (one-pole).
    state_transition_tau_s: float = 0.5
    lead_geometries: Dict[tuple[str, str], LeadGeometry] = field(default_factory=dict)
    lead_profiles: Dict[tuple[str, str], LeadProfile] = field(default_factory=dict)


@dataclass(slots=True)
class StateModifiers:
    """Scalar multipliers applied per behavioral state and lead kind.

    Baseline feature scalars (``beta_scalar``, ``gamma_scalar``, ``sleep_blend``)
    reshape the synthesized local power; stimulation scalars (``a50_scalar``,
    ``suppression_scalar``) reshape the tick-by-tick entrainment/suppression
    response. All default to the Rest identity (no change).
    """

    beta_scalar: float = 1.0          # multiplies the isolated beta component
    gamma_scalar: float = 1.0         # multiplies the gamma band (broadband gamma + floor)
    sleep_blend: float = 0.0          # 0 -> no slow waves, 1 -> full delta/theta slow-wave term
    a50_scalar: float = 1.0           # multiplies Hill midpoint (entrainment resistance)
    suppression_scalar: float = 1.0   # multiplies max beta-suppression strength


# Behavioral states the GUI can request. Rest is the identity (everything 1.0),
# so Rest-state output is bit-for-bit equivalent to the pre-upgrade model.
BEHAVIORAL_STATES = ("Rest", "Movement", "Sleep")

STATE_MODIFIERS: Dict[tuple[str, str], StateModifiers] = {
    # --- Rest: identity ---
    ("Rest", "depth"): StateModifiers(),
    ("Rest", "paddle"): StateModifiers(),

    # Note: beta_scalar / gamma_scalar are amplitude multipliers, so band POWER
    # scales ~ scalar**2. Values below target physiologically reasonable power
    # ratios vs Rest (see comments), verified by band-power measurement.
    #
    # --- Movement ---
    # STN (depth): endogenous beta desynchronization (~60% power drop) + modest
    # broadband/gamma rise (line length/variance); cortex harder to entrain
    # (higher a50) and stimulation must not push already-low beta below a
    # physiological floor (suppression < 1).
    ("Movement", "depth"): StateModifiers(
        beta_scalar=0.5,        # beta power ~0.35-0.4x (mid-range ERD)
        gamma_scalar=1.5,       # modest STN gamma rise (stays below beta)
        a50_scalar=1.5,
        suppression_scalar=0.6,
    ),
    # M1 (paddle): sensorimotor beta desynchronization (classic movement ERD) +
    # movement high-gamma synchronization (~2-2.5x power); harder to entrain.
    ("Movement", "paddle"): StateModifiers(
        beta_scalar=0.5,        # beta power ~0.35-0.4x (cortical ERD)
        gamma_scalar=1.95,      # finely-tuned gamma emerges with movement, capped <= beta
        a50_scalar=1.5,
    ),

    # --- Sleep ---
    # NREM/slow-wave sleep reduces beta in both STN and sensorimotor cortex as
    # the spectrum shifts to delta/slow oscillations.
    # STN (depth): slow-wave dominance (sleep_blend) + strongly reduced beta
    # (~0.16x power) — deeper than the movement ERD, as NREM nearly abolishes beta.
    ("Sleep", "depth"): StateModifiers(
        beta_scalar=0.4,
        sleep_blend=1.0,
    ),
    # M1 (paddle): cortical slow-wave dominance, strongly reduced beta (~0.2x),
    # high-gamma reduction (~0.5x), and strong rejection of high-frequency
    # continuous entrainment.
    ("Sleep", "paddle"): StateModifiers(
        beta_scalar=0.45,       # beta power ~0.2x (NREM cortical beta reduction)
        gamma_scalar=0.7,       # gamma power ~0.5x
        sleep_blend=1.0,
        a50_scalar=2.5,
    ),
}


def get_state_modifiers(state: str, lead_kind: str) -> StateModifiers:
    """Look up state modifiers, falling back to the Rest identity."""
    return STATE_MODIFIERS.get((state, lead_kind)) or StateModifiers()


@dataclass(slots=True)
class StateCoherence:
    """Per-state inter-channel coherence coefficients.

    Each field is a per-channel *loading* (shared-amplitude fraction) onto a
    shared oscillator. The magnitude-squared coherence between two channels is
    ~ (product of their loadings on the common oscillator)**2 * a carrier boost.
    Oscillators / pairings:
      - hemisphere beta (shared by STN+M1 on a side): ``beta_depth`` is the STN
        loading, ``beta_paddle`` the M1 loading -> intra-hem STN-M1 beta coherence.
      - bilateral beta (shared by left+right STN): ``beta_bilateral`` -> the
        (weaker) inter-hemisphere STN-STN beta coupling seen in PD.
      - hemisphere gamma (STN+M1 on a side): ``gamma`` -> movement STN-M1 gamma.
      - global delta (all four leads): ``delta`` -> sleep slow-wave coherence
        (only audible while ``sleep_blend`` > 0).
    Depth budget: ``beta_depth + beta_bilateral`` must stay below ~1.
    """

    beta_depth: float = 0.0
    beta_paddle: float = 0.0
    beta_bilateral: float = 0.0
    gamma: float = 0.0
    delta: float = 0.0


# Coherence loadings per behavioral state (tuned against measured MSC).
# Rest: strong intra-hem STN-M1 beta (~0.5) + modest bilateral STN-STN beta
#   (~0.25, weaker as the two STNs run semi-independent beta oscillators).
# Movement: gamma coupling appears; both beta couplings break down.
# Sleep: strong global delta coupling; beta couplings low.
STATE_COHERENCE: Dict[str, StateCoherence] = {
    "Rest": StateCoherence(beta_depth=0.62, beta_paddle=0.97, beta_bilateral=0.36, gamma=0.25, delta=0.0),
    "Movement": StateCoherence(beta_depth=0.20, beta_paddle=0.50, beta_bilateral=0.18, gamma=0.95, delta=0.0),
    "Sleep": StateCoherence(beta_depth=0.18, beta_paddle=0.50, beta_bilateral=0.18, gamma=0.25, delta=0.90),
}

# Bands the shared oscillators occupy.
COHERENCE_BANDS = {
    "beta": (13.0, 30.0),
    "gamma": (50.0, 120.0),
    "delta": (0.5, 7.0),
}


def get_state_coherence(state: str) -> StateCoherence:
    """Look up coherence coefficients, falling back to zero (incoherent)."""
    return STATE_COHERENCE.get(state) or StateCoherence()


def default_config() -> SimulatorConfig:
    depth_positions = build_depth_positions(n_contacts=8, spacing_mm=2.0)
    paddle_positions = build_paddle_positions(row_spacing_mm=8.0, col_spacing_mm=10.0)

    lead_geometries = {
        ("left", "depth"): LeadGeometry(depth_positions, (3,), 2.4, 0.08),
        ("right", "depth"): LeadGeometry(depth_positions, (3,), 2.4, 0.08),
        ("left", "paddle"): LeadGeometry(paddle_positions, (2, 6), 4.4, 0.08),
        ("right", "paddle"): LeadGeometry(paddle_positions, (2, 6), 4.4, 0.08),
    }

    lead_profiles = {
        ("left", "depth"): LeadProfile(
            name="Left Depth LFP (STN)",
            alpha=1.5,
            beta_hz=21.5,
            beta_low_hz=13.0,
            beta_high_hz=30.0,
            beta_uv=3.8,
            beta_sigma_hz=3.9,
            gamma_activity_uv=0.50,
            baseline_rms_uv=22.0,
            amax_uv=0.8,
            a50_ma=2.2,
            hill_n=3.6,
            tau_on_s=1.0,
            tau_off_s=0.7,
            stim_decay_mm=2.2,
            artifact_scale_uv_per_ma=8.0,
            artifact_2f_ratio=0.20,
            beta_suppression_start_ma=0.2,
            beta_suppression_end_ma=1.8,
            beta_suppression_strength=0.72,
            sleep_slow_wave_gain=0.30,
            correlation_length_mm=18.0,
        ),
        ("right", "depth"): LeadProfile(
            name="Right Depth LFP (STN)",
            alpha=1.5,
            beta_hz=21.5,
            beta_low_hz=13.0,
            beta_high_hz=30.0,
            beta_uv=3.8,
            beta_sigma_hz=3.9,
            gamma_activity_uv=0.50,
            baseline_rms_uv=22.4,
            amax_uv=0.85,
            a50_ma=2.25,
            hill_n=3.6,
            tau_on_s=1.0,
            tau_off_s=0.7,
            stim_decay_mm=2.2,
            artifact_scale_uv_per_ma=8.2,
            artifact_2f_ratio=0.20,
            beta_suppression_start_ma=0.2,
            beta_suppression_end_ma=1.8,
            beta_suppression_strength=0.75,
            sleep_slow_wave_gain=0.30,
            correlation_length_mm=18.0,
        ),
        ("left", "paddle"): LeadProfile(
            name="Left Cortex ECoG (Paddle)",
            alpha=1.3,
            beta_hz=21.5,
            beta_low_hz=13.0,
            beta_high_hz=30.0,
            beta_uv=14.4,
            beta_sigma_hz=4.0,
            gamma_activity_uv=2.48,
            baseline_rms_uv=56.0,
            amax_uv=8.0,
            a50_ma=2.0,
            hill_n=4.0,
            tau_on_s=0.7,
            tau_off_s=0.5,
            stim_decay_mm=8.0,
            artifact_scale_uv_per_ma=48.0,
            artifact_2f_ratio=0.15,
            beta_suppression_start_ma=0.0,
            beta_suppression_end_ma=1.8,
            beta_suppression_strength=0.60,
            sleep_slow_wave_gain=0.60,
            correlation_length_mm=28.0,
        ),
        ("right", "paddle"): LeadProfile(
            name="Right Cortex ECoG (Paddle)",
            alpha=1.3,
            beta_hz=21.5,
            beta_low_hz=13.0,
            beta_high_hz=30.0,
            beta_uv=14.4,
            beta_sigma_hz=4.0,
            gamma_activity_uv=2.48,
            baseline_rms_uv=57.2,
            amax_uv=8.4,
            a50_ma=2.0,
            hill_n=4.0,
            tau_on_s=0.7,
            tau_off_s=0.5,
            stim_decay_mm=8.0,
            artifact_scale_uv_per_ma=48.0,
            artifact_2f_ratio=0.15,
            beta_suppression_start_ma=0.0,
            beta_suppression_end_ma=1.8,
            beta_suppression_strength=0.62,
            sleep_slow_wave_gain=0.60,
            correlation_length_mm=28.0,
        ),
    }

    return SimulatorConfig(
        lead_geometries=lead_geometries,
        lead_profiles=lead_profiles,
    )
