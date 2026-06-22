from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt

from .config import LeadGeometry, LeadProfile
from .geometry import distance_to_hotspots, spatial_weight


@dataclass(slots=True)
class ContactBaseline:
    """Per-contact baseline as an aperiodic 1/f floor plus additive oscillatory
    peaks, so state scalars and stim suppression act on the *peaks only* and the
    1/f floor is never scaled.

    ``background`` is the full broadband 1/f (continuous across all bands) — the
    aperiodic floor. ``beta_peak`` and ``gamma_activity`` are oscillatory
    components added *on top* of that floor: scaling them down shrinks the peak
    toward the floor (a smaller hill) rather than carving a hole below the 1/f.
    Shared oscillators are mixed into the peaks (not the floor) for coherence.
    ``slow_indep`` is the unit-variance Sleep slow wave (scaled by ``slow_amp``).
    ``gradient`` lets a fraction of a shared component survive bipolar derivation.
    """

    background: np.ndarray        # full broadband 1/f (aperiodic floor, all bands)
    beta_peak: np.ndarray         # additive beta peak (oscillatory beta above 1/f)
    gamma_activity: np.ndarray    # additive gamma component (oscillatory gamma above 1/f)
    slow_indep: np.ndarray        # unit-variance delta/theta slow wave (Sleep)
    slow_amp: float               # slow-wave amplitude = gain * RMS * spatial weight
    beta_amp: float               # RMS of beta_peak (to scale the shared beta)
    gamma_amp: float              # RMS of gamma_activity (to scale the shared gamma)
    gradient: float               # 0.6..1.0 across-contact weight for shared injection


def _color_from_phases(
    freqs: np.ndarray,
    phases: np.ndarray,
    alpha: float,
    n_samples: int,
) -> np.ndarray:
    """Synthesize unit-variance 1/f^alpha noise from a fixed phase realization."""
    scale = np.ones_like(freqs)
    nz = freqs > 0
    scale[nz] = 1.0 / np.power(freqs[nz], alpha / 2.0)

    spectrum = scale * np.exp(1j * phases)
    signal = np.fft.irfft(spectrum, n=n_samples)
    signal = signal - np.mean(signal)
    signal = signal / (np.std(signal) + 1e-12)
    return signal


def _colored_noise(alpha: float, n_samples: int, fs: int, rng: np.random.Generator) -> np.ndarray:
    freqs = np.fft.rfftfreq(n_samples, d=1 / fs)
    phases = rng.uniform(0, 2 * np.pi, len(freqs))
    return _color_from_phases(freqs, phases, alpha, n_samples)


def _slow_wave_from_phases(
    freqs: np.ndarray,
    phases: np.ndarray,
    alpha: float,
    n_samples: int,
    knee_hz: float = 0.5,
    band_hz: float = 4.0,
    taper_hz: float = 7.0,
) -> np.ndarray:
    """Band-limited delta/theta slow-wave term (unit variance).

    Power is confined to ``[knee_hz, taper_hz]`` with a cosine roll-off above
    ``band_hz``. The low-frequency knee prevents the infra-slow blow-up that a
    global 1/f re-coloring produces, so the added power stays in the visible,
    physiologically meaningful slow-wave band rather than sub-Hz drift.
    """
    scale = np.zeros_like(freqs)
    nz = freqs > 0
    scale[nz] = 1.0 / np.power(freqs[nz], alpha / 2.0)

    window = np.zeros_like(freqs)
    passband = (freqs >= knee_hz) & (freqs <= band_hz)
    window[passband] = 1.0
    rolloff = (freqs > band_hz) & (freqs < taper_hz)
    window[rolloff] = 0.5 * (1.0 + np.cos(np.pi * (freqs[rolloff] - band_hz) / (taper_hz - band_hz)))

    spectrum = scale * window * np.exp(1j * phases)
    signal = np.fft.irfft(spectrum, n=n_samples)
    signal = signal - np.mean(signal)
    signal = signal / (np.std(signal) + 1e-12)
    return signal


def _band_limited_noise(
    low_hz: float,
    high_hz: float,
    n_samples: int,
    fs: int,
    rng: np.random.Generator,
) -> np.ndarray:
    nyq = fs / 2.0
    low = max(0.5, low_hz) / nyq
    high = min(nyq - 1.0, high_hz) / nyq
    if not (0.0 < low < high < 1.0):
        return np.zeros(n_samples, dtype=np.float64)

    white = rng.normal(0.0, 1.0, n_samples)
    sos = butter(4, [low, high], btype="band", output="sos")
    band = sosfiltfilt(sos, white)
    band = band - np.mean(band)
    band = band / (np.std(band) + 1e-12)
    return band


def _spectral_bump(center_hz: float, sigma_hz: float, n_samples: int, fs: int, rng: np.random.Generator) -> np.ndarray:
    """Unit-variance noise with a Gaussian spectral envelope (a smooth bump).

    Used for finely-tuned gamma: power is concentrated around ``center_hz`` and
    tapers smoothly to the 1/f floor at the edges, so scaling it raises/lowers a
    natural hump rather than shifting a flat band-limited shelf as a block.
    """
    freqs = np.fft.rfftfreq(n_samples, d=1 / fs)
    env = np.exp(-0.5 * ((freqs - center_hz) / (sigma_hz + 1e-12)) ** 2)
    phases = rng.uniform(0, 2 * np.pi, len(freqs))
    signal = np.fft.irfft(env * np.exp(1j * phases), n=n_samples)
    signal = signal - np.mean(signal)
    signal = signal / (np.std(signal) + 1e-12)
    return signal


def _bandpass(signal: np.ndarray, low_hz: float, high_hz: float, fs: int) -> np.ndarray:
    nyq = fs / 2.0
    low = max(0.5, low_hz) / nyq
    high = min(nyq - 1.0, high_hz) / nyq
    if not (0.0 < low < high < 1.0):
        return np.zeros_like(signal)
    sos = butter(4, [low, high], btype="band", output="sos")
    return sosfiltfilt(sos, signal)


# ---------------------------------------------------------------------------
# Shared oscillators (one realization injected into multiple channels to create
# inter-channel coherence). All are unit-variance; the model scales them to each
# target channel's band amplitude before mixing.
# ---------------------------------------------------------------------------

def generate_shared_slow_wave(n_samples: int, fs: int, seed: int, alpha: float = 1.4) -> np.ndarray:
    """Global delta/theta slow wave shared across all leads (Sleep coherence)."""
    rng = np.random.default_rng(seed)
    freqs = np.fft.rfftfreq(n_samples, d=1 / fs)
    phases = rng.uniform(0, 2 * np.pi, len(freqs))
    return _slow_wave_from_phases(freqs, phases, alpha, n_samples).astype(np.float32)


def generate_shared_gamma(
    n_samples: int, fs: int, seed: int, center_hz: float = 75.0, sigma_hz: float = 9.0
) -> np.ndarray:
    """Per-hemisphere finely-tuned gamma oscillator shared by STN+M1 (Movement
    coherence). Same spectral bump as the contacts' gamma so coherence is clean."""
    rng = np.random.default_rng(seed)
    return _spectral_bump(center_hz, sigma_hz, n_samples, fs, rng).astype(np.float32)


def generate_shared_beta(
    n_samples: int,
    fs: int,
    seed: int,
    beta_hz: float = 21.5,
    low_hz: float = 13.0,
    high_hz: float = 30.0,
) -> np.ndarray:
    """Per-hemisphere beta oscillator shared by STN+M1 (Rest coherence).

    Includes a carrier so the shared signal overlaps the contacts' beta carrier
    and the coherence is clean across the whole beta band.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples, dtype=np.float64) / fs
    broad = _band_limited_noise(low_hz, high_hz, n_samples, fs, rng)
    carrier = np.sin(2 * np.pi * beta_hz * t + rng.uniform(0, 2 * np.pi))
    sig = 0.7 * carrier + 0.6 * broad
    sig = sig - np.mean(sig)
    sig = sig / (np.std(sig) + 1e-12)
    return sig.astype(np.float32)


def generate_contact_baseline(
    profile: LeadProfile,
    geometry: LeadGeometry,
    contact_index: int,
    fs: int,
    duration_s: float,
    seed: int,
    sleep_slow_wave_gain: float = 0.42,
    gamma_center_hz: float = 75.0,
    gamma_sigma_hz: float = 9.0,
) -> ContactBaseline:
    """Synthesize a contact's baseline as a continuous 1/f floor plus additive
    oscillatory peaks (see ``ContactBaseline``).

    The full 1/f ``background`` is the aperiodic floor; ``beta_peak`` and
    ``gamma_activity`` are added on top and are the only parts the model scales
    (with state scalars / stim suppression), so reducing a band lowers its peak
    toward the floor rather than carving a hole below the 1/f.
    """
    n_samples = int(fs * duration_s)
    rng = np.random.default_rng(seed)

    colored_rest = _colored_noise(profile.alpha, n_samples, fs, rng)
    t = np.arange(n_samples, dtype=np.float64) / fs

    distance_mm = distance_to_hotspots(geometry.positions_mm, contact_index, geometry.hotspot_indices)
    hotspot_weight = spatial_weight(distance_mm, geometry.hotspot_decay_mm, geometry.baseline_floor)
    base_weight = 0.88 + 0.12 * hotspot_weight
    beta_weight = (
        0.30 + 0.50 * hotspot_weight + 0.20 * np.power(hotspot_weight, 2.0)
    ) * (0.97 + 0.06 * rng.random())
    gamma_weight = 0.96 + 0.04 * rng.random()

    # Additive beta peak (carrier + narrowband) that sits ABOVE the 1/f floor.
    beta_band = _band_limited_noise(profile.beta_low_hz, profile.beta_high_hz, n_samples, fs, rng)
    beta_half_bw = max(1.8, 0.6 * profile.beta_sigma_hz)
    beta_low = max(profile.beta_low_hz, profile.beta_hz - beta_half_bw)
    beta_high = min(profile.beta_high_hz, profile.beta_hz + beta_half_bw)
    beta_broad = _band_limited_noise(beta_low, beta_high, n_samples, fs, rng)
    beta_carrier = np.sin(2 * np.pi * profile.beta_hz * t + rng.uniform(0, 2 * np.pi))
    modulation = np.clip(0.8 + 0.25 * beta_band, 0.3, 1.4)
    beta_peak = profile.beta_uv * beta_weight * modulation * (0.7 * beta_carrier + 0.6 * beta_broad)

    # Additive finely-tuned gamma BUMP (smooth Gaussian-shaped peak) above the
    # 1/f floor — scaling it raises/lowers a hump that blends into the floor at
    # the edges, rather than shifting a flat shelf as a block.
    gamma_bump = _spectral_bump(gamma_center_hz, gamma_sigma_hz, n_samples, fs, rng)
    gamma_activity = profile.gamma_activity_uv * gamma_weight * gamma_bump

    rms_scale = profile.baseline_rms_uv * base_weight
    # Full, continuous 1/f floor — never scaled by state or stim.
    background = colored_rest * rms_scale

    # Independent slow wave (unit variance; scaled by slow_amp at runtime).
    sw_rng = np.random.default_rng(seed + 777)
    freqs = np.fft.rfftfreq(n_samples, d=1 / fs)
    sw_phases = sw_rng.uniform(0, 2 * np.pi, len(freqs))
    slow_indep = _slow_wave_from_phases(freqs, sw_phases, profile.alpha, n_samples)
    slow_amp = sleep_slow_wave_gain * rms_scale

    gradient = 0.6 + 0.4 * float(hotspot_weight)

    return ContactBaseline(
        background=background.astype(np.float32),
        beta_peak=beta_peak.astype(np.float32),
        gamma_activity=gamma_activity.astype(np.float32),
        slow_indep=slow_indep.astype(np.float32),
        slow_amp=float(slow_amp),
        beta_amp=float(np.std(beta_peak)),
        gamma_amp=float(np.std(gamma_activity)),
        gradient=float(gradient),
    )
