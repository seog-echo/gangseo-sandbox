#!/usr/bin/env python3
"""Real-time amplitude, frequency and pulse-width estimation for the HIL input.

The signal arriving on the NI-9222 analog input may be either:

* a roughly **continuous** waveform (a bench sine generator, or a real IPG seen
  through a low-pass), or
* a **pulsatile** DBS-style train: biphasic square pulses with a very short
  pulse width (tens of microseconds) at a low repetition rate (~100-200 Hz),
  i.e. a duty cycle of only a percent or two.

A percentile-based peak-to-peak amplitude (the previous approach) silently fails
on the pulsatile case: with <2% duty cycle the 99th percentile sits at baseline,
so the pulse height is thrown away as if it were a glitch. These helpers instead
estimate amplitude from the extreme samples (glitch-trimmed, not percentile-
clipped), and detect a pulse train directly from threshold crossings so they can
report **per-phase pulse width** and **repetition rate** as well. Sine/continuous
inputs fall back to FFT dominant-frequency detection.

To resolve a ~60 us pulse the input must be sampled fast enough that each phase
spans several samples (e.g. >=100 kHz). At the HIL default of 250 kHz a 60 us
phase is ~15 samples wide, enough for a stable width and a corroborated peak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(slots=True)
class SignalMeasurement:
    """One estimate of the input waveform's character.

    ``valid`` is False until enough samples have accumulated to trust the estimate;
    callers should hold the previous good value (or zero stim) when invalid.
    ``pulse_width_s`` is the per-phase width of a detected pulse train (0.0 when the
    input is continuous / not pulsatile); ``is_pulsatile`` records which path won.
    """

    amplitude_v: float = 0.0      # single-sided peak amplitude (baseline->peak), volts
    frequency_hz: float = 0.0     # repetition rate (pulsatile) or dominant tone, Hz
    pulse_width_s: float = 0.0    # per-phase pulse width, seconds (0 if continuous)
    rms_v: float = 0.0            # RMS of the window about baseline (diagnostic)
    is_pulsatile: bool = False    # True when a regular narrow-pulse train was found
    valid: bool = False


def measure_signal(
    samples: np.ndarray,
    fs: float,
    *,
    min_samples: int = 64,
    freq_lo_hz: float = 1.0,
    freq_hi_hz: float = 1000.0,
    pulse_threshold_frac: float = 0.5,
    max_duty_cycle: float = 0.2,
    min_pulses: int = 3,
) -> SignalMeasurement:
    """Estimate amplitude, frequency and (if pulsatile) pulse width of ``samples``.

    Parameters
    ----------
    samples : 1-D array of the most recent input samples (volts).
    fs : sample rate of ``samples`` in Hz.
    min_samples : below this many samples the estimate is marked invalid.
    freq_lo_hz / freq_hi_hz : band the FFT dominant-frequency fallback is restricted
        to (used only for non-pulsatile inputs).
    pulse_threshold_frac : fraction of the peak used as the on/off threshold for
        pulse-edge detection (0.5 = half-height, the usual pulse-width convention).
    max_duty_cycle : above this (active-time / period) the input is treated as
        continuous (e.g. a sine, whose half-height runs are ~1/3 of a period), not a
        narrow-pulse train, and the FFT path is used instead.
    min_pulses : need at least this many pulses in the window to trust a train.
    """
    x = np.asarray(samples, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < min_samples or fs <= 0.0:
        return SignalMeasurement()

    # Baseline via the median: robust to a sparse pulse train (most samples sit at
    # baseline, so the median IS the baseline) as well as to a DC offset on a sine.
    baseline = float(np.median(x))
    x0 = x - baseline
    rms_v = float(np.sqrt(np.mean(np.square(x0))))

    amplitude_v = _robust_peak_amplitude(x0)

    pulse = _detect_pulse_train(
        x0, fs, amplitude_v,
        threshold_frac=pulse_threshold_frac,
        max_duty_cycle=max_duty_cycle,
        min_pulses=min_pulses,
    )
    if pulse is not None:
        frequency_hz, pulse_width_s = pulse
        return SignalMeasurement(
            amplitude_v=amplitude_v,
            frequency_hz=frequency_hz,
            pulse_width_s=pulse_width_s,
            rms_v=rms_v,
            is_pulsatile=True,
            valid=True,
        )

    # Continuous / sine fallback: dominant spectral frequency in the search band.
    frequency_hz = _fft_dominant_hz(x0, fs, freq_lo_hz, freq_hi_hz)
    return SignalMeasurement(
        amplitude_v=amplitude_v,
        frequency_hz=frequency_hz,
        pulse_width_s=0.0,
        rms_v=rms_v,
        is_pulsatile=False,
        valid=True,
    )


def _robust_peak_amplitude(x0: np.ndarray) -> float:
    """Single-sided peak amplitude (baseline->peak) that survives a sparse, low-duty
    pulse train yet still rejects lone glitch samples.

    Uses the k-th most extreme sample on each side rather than a fixed percentile:
    a percentile high enough to land on a <2% duty-cycle pulse top is unknowable in
    advance, but trimming a small fixed count from each extreme drops one or two
    glitch samples while still landing on the pulse plateau (which, when adequately
    sampled, is many samples wide). Reports the larger of the two single-sided peaks
    so monophasic and biphasic pulses both read their true height.
    """
    n = x0.size
    if n == 0:
        return 0.0
    k = int(np.clip(round(0.0005 * n), 2, 64))
    k = min(k, n)
    hi = float(np.partition(x0, -k)[-k])     # value with k-1 samples above it
    lo = float(np.partition(x0, k - 1)[k - 1])  # value with k-1 samples below it
    return max(0.0, hi, -lo)


def _detect_pulse_train(
    x0: np.ndarray,
    fs: float,
    peak: float,
    *,
    threshold_frac: float,
    max_duty_cycle: float,
    min_pulses: int,
) -> Optional[tuple[float, float]]:
    """Detect a regular narrow-pulse train and return (rep_rate_hz, pulse_width_s).

    Threshold-crosses ``x0`` at +/- ``threshold_frac * peak``; each above-threshold
    run is one pulse phase. The polarity with the most regular onset spacing wins
    (so a biphasic train is keyed off whichever phase the generator leads with).
    Returns ``None`` when the signal is not a regular narrow-pulse train (too few
    pulses, irregular spacing, or a duty cycle too high to be "pulses" — e.g. a sine
    or square wave), so the caller falls back to FFT frequency.
    """
    if peak <= 1e-6 or fs <= 0.0:
        return None
    thr = threshold_frac * peak

    best: Optional[tuple[float, float, int]] = None  # (rep_hz, width_s, n_pulses)
    for sign in (1.0, -1.0):
        active = (sign * x0) > thr
        if int(np.count_nonzero(active)) < min_pulses:
            continue
        a = active.astype(np.int8)
        rises = np.flatnonzero(np.diff(a) == 1) + 1   # first sample of each run
        falls = np.flatnonzero(np.diff(a) == -1) + 1  # first sample after each run
        if rises.size < min_pulses:
            continue

        # Per-phase width: pair each rise with the next fall (drop a run still open
        # at the window edge so a truncated pulse cannot bias the width).
        widths = []
        for r in rises:
            f = falls[falls > r]
            if f.size:
                widths.append((int(f[0]) - int(r)) / fs)
        if len(widths) < min_pulses:
            continue
        width_s = float(np.median(widths))

        onsets = rises.astype(np.float64) / fs
        intervals = np.diff(onsets)
        if intervals.size < (min_pulses - 1):
            continue
        med_interval = float(np.median(intervals))
        if med_interval <= 0.0:
            continue
        # Regularity gate: reject jittery / spurious crossings (noise, transients).
        cv = float(np.std(intervals) / med_interval)
        if cv > 0.35:
            continue
        # Duty-cycle gate: real DBS pulses are narrow vs. their period. A sine's
        # half-height runs are ~1/3 of a period, a square wave ~1/2 — both exceed
        # this and are sent to the FFT path instead.
        if width_s / med_interval > max_duty_cycle:
            continue

        rep_hz = 1.0 / med_interval
        n_pulses = int(rises.size)
        if best is None or n_pulses > best[2]:
            best = (rep_hz, width_s, n_pulses)

    if best is None:
        return None
    return best[0], best[1]


def _fft_dominant_hz(x0: np.ndarray, fs: float, freq_lo_hz: float, freq_hi_hz: float) -> float:
    """Dominant spectral frequency of ``x0`` within the search band (Hz)."""
    n = x0.size
    window = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(x0 * window))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

    band = (freqs >= freq_lo_hz) & (freqs <= min(freq_hi_hz, fs / 2.0))
    if not np.any(band):
        return 0.0
    band_spectrum = spectrum.copy()
    band_spectrum[~band] = 0.0
    peak_idx = int(np.argmax(band_spectrum))
    if band_spectrum[peak_idx] <= 0.0:
        return 0.0
    return _parabolic_peak_hz(spectrum, freqs, peak_idx)


def _parabolic_peak_hz(spectrum: np.ndarray, freqs: np.ndarray, k: int) -> float:
    """Refine a discrete FFT peak to sub-bin resolution via parabolic interpolation."""
    if k <= 0 or k >= spectrum.size - 1:
        return float(freqs[k])
    a, b, c = spectrum[k - 1], spectrum[k], spectrum[k + 1]
    denom = (a - 2.0 * b + c)
    if denom == 0.0:
        return float(freqs[k])
    delta = 0.5 * (a - c) / denom            # in bins, range (-0.5, 0.5)
    df = float(freqs[1] - freqs[0]) if freqs.size > 1 else 0.0
    return max(0.0, float(freqs[k]) + delta * df)
