#!/usr/bin/env python3
"""Real-time amplitude and frequency estimation for the HIL input signal.

The mock-stimulation signal arriving on the NI-9222 analog input is a roughly
periodic waveform (a bench signal generator now; a real IPG later). These helpers
estimate its instantaneous **peak amplitude** (in volts) and **dominant frequency**
(in Hz) from a short rolling window of samples, cheaply enough to run ~10 times a
second on the GUI thread.

The algorithms mirror those used in DBS_HIL_GUI's analyzers (FFT dominant-frequency
detection plus a robust peak-to-peak amplitude), kept dependency-light here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class SignalMeasurement:
    """One estimate of the input waveform's character.

    ``valid`` is False until enough samples have accumulated to trust the estimate;
    callers should hold the previous good value (or zero stim) when invalid.
    """

    amplitude_v: float = 0.0      # peak amplitude (half of peak-to-peak), volts
    frequency_hz: float = 0.0     # dominant spectral frequency, Hz
    rms_v: float = 0.0            # RMS of the window (diagnostic)
    valid: bool = False


def measure_signal(
    samples: np.ndarray,
    fs: float,
    *,
    min_samples: int = 64,
    freq_lo_hz: float = 1.0,
    freq_hi_hz: float = 1000.0,
    detrend: bool = True,
) -> SignalMeasurement:
    """Estimate peak amplitude and dominant frequency of ``samples``.

    Parameters
    ----------
    samples : 1-D array of the most recent input samples (volts).
    fs : sample rate of ``samples`` in Hz.
    min_samples : below this many samples the estimate is marked invalid.
    freq_lo_hz / freq_hi_hz : band the dominant-frequency search is restricted to,
        so DC drift and out-of-band noise do not win the peak.
    detrend : remove the mean before estimating (recommended; ignores DC offset).
    """
    x = np.asarray(samples, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = x.size
    if n < min_samples or fs <= 0.0:
        return SignalMeasurement()

    if detrend:
        x = x - float(np.mean(x))

    # --- Amplitude ---
    # Robust peak-to-peak using 1st/99th percentiles to reject the odd glitch,
    # divided by two to report a peak (single-sided) amplitude.
    hi = float(np.percentile(x, 99.0))
    lo = float(np.percentile(x, 1.0))
    amplitude_v = max(0.0, 0.5 * (hi - lo))
    rms_v = float(np.sqrt(np.mean(np.square(x)))) if n else 0.0

    # --- Frequency ---
    # Windowed rFFT; pick the strongest bin inside the search band.
    window = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(x * window))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

    band = (freqs >= freq_lo_hz) & (freqs <= min(freq_hi_hz, fs / 2.0))
    frequency_hz = 0.0
    if np.any(band):
        band_spectrum = spectrum.copy()
        band_spectrum[~band] = 0.0
        peak_idx = int(np.argmax(band_spectrum))
        if band_spectrum[peak_idx] > 0.0:
            frequency_hz = _parabolic_peak_hz(spectrum, freqs, peak_idx)

    return SignalMeasurement(
        amplitude_v=amplitude_v,
        frequency_hz=frequency_hz,
        rms_v=rms_v,
        valid=True,
    )


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
