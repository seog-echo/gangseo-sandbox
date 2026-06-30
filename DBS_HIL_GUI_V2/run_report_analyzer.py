from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Dict, List, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover - optional dependency
    np = None

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover - optional dependency
    plt = None


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def load_csv_rows(path: Path) -> Tuple[List[str], List[List[str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def load_signal_series(path: Path, skip_initial_s: float = 0.0) -> Dict[str, Any]:
    header, rows = load_csv_rows(path)
    result: Dict[str, Any] = {
        "file": str(path),
        "exists": path.exists(),
        "row_count": 0,
        "raw_row_count": len(rows),
        "header": header,
        "duration_s": 0.0,
        "analyzed_duration_s": 0.0,
        "sample_rate_hz_est": None,
        "analysis_start_time_s": None,
        "analysis_skip_initial_s": skip_initial_s,
        "time_values": [],
        "channel_samples": {},
    }
    if not header or not rows:
        return result

    raw_time_values: List[float] = []
    raw_channel_samples: Dict[str, List[float]] = {name: [] for name in header[1:]}

    for row in rows:
        if not row:
            continue
        t = parse_float(row[0] if len(row) > 0 else None)
        if t is None:
            continue
        raw_time_values.append(t)
        for idx, channel_name in enumerate(header[1:], start=1):
            value = parse_float(row[idx] if idx < len(row) else None)
            raw_channel_samples[channel_name].append(value if value is not None else float("nan"))

    if not raw_time_values:
        return result

    t0 = min(raw_time_values)
    analysis_start = t0 + max(0.0, skip_initial_s)
    keep_idx = [i for i, t in enumerate(raw_time_values) if t >= analysis_start]

    time_values = [raw_time_values[i] for i in keep_idx]
    channel_samples = {
        name: [raw_channel_samples[name][i] for i in keep_idx]
        for name in header[1:]
    }

    result["time_values"] = time_values
    result["channel_samples"] = channel_samples
    result["row_count"] = len(time_values)
    result["analysis_start_time_s"] = analysis_start

    result["duration_s"] = max(raw_time_values) - min(raw_time_values)
    if time_values:
        result["analyzed_duration_s"] = max(time_values) - min(time_values)
    # Estimate from the analyzed window to avoid startup-transient timing skew.
    result["sample_rate_hz_est"] = estimate_sample_rate(time_values if len(time_values) >= 3 else raw_time_values)

    return result


def summarize_numeric_series(samples: List[float]) -> Dict[str, Any]:
    clean = [x for x in samples if isinstance(x, (int, float)) and math.isfinite(x)]
    if not clean:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "rms": None,
            "p2p": None,
        }

    count = len(clean)
    avg = mean(clean)
    variance = sum((x - avg) ** 2 for x in clean) / count
    rms = math.sqrt(sum(x * x for x in clean) / count)
    return {
        "count": count,
        "min": min(clean),
        "max": max(clean),
        "mean": avg,
        "std": math.sqrt(variance),
        "rms": rms,
        "p2p": max(clean) - min(clean),
    }


def summarize_signal_data(signal_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": signal_data.get("file"),
        "exists": signal_data.get("exists", False),
        "row_count": signal_data.get("row_count", 0),
        "raw_row_count": signal_data.get("raw_row_count", 0),
        "header": signal_data.get("header", []),
        "duration_s": signal_data.get("duration_s", 0.0),
        "analyzed_duration_s": signal_data.get("analyzed_duration_s", 0.0),
        "sample_rate_hz_est": signal_data.get("sample_rate_hz_est"),
        "analysis_start_time_s": signal_data.get("analysis_start_time_s"),
        "analysis_skip_initial_s": signal_data.get("analysis_skip_initial_s", 0.0),
        "channels": {
            channel_name: summarize_numeric_series(samples)
            for channel_name, samples in signal_data.get("channel_samples", {}).items()
        },
    }


def summarize_battery_csv(path: Path) -> Dict[str, Any]:
    header, rows = load_csv_rows(path)
    result: Dict[str, Any] = {
        "file": str(path),
        "exists": path.exists(),
        "row_count": len(rows),
        "header": header,
        "pre_test": {},
        "post_test": {},
        "delta": {},
    }
    if not header or not rows:
        return result

    index = {name: idx for idx, name in enumerate(header)}

    def get_row_phase(phase_name: str) -> List[str] | None:
        for row in rows:
            if len(row) > index.get("Phase", -1) and row[index["Phase"]] == phase_name:
                return row
        return None

    pre_row = get_row_phase("pre-test")
    post_row = get_row_phase("post-test")

    def extract(row: List[str] | None) -> Dict[str, Any]:
        if row is None:
            return {}
        battery = parse_float(row[index["Battery_mV"]]) if "Battery_mV" in index and len(row) > index["Battery_mV"] else None
        temp = parse_float(row[index["Temperature_C"]]) if "Temperature_C" in index and len(row) > index["Temperature_C"] else None
        charge = parse_float(row[index["ChargeLevel"]]) if "ChargeLevel" in index and len(row) > index["ChargeLevel"] else None
        return {
            "battery_mV": battery,
            "temperature_C": temp,
            "charge_level": charge,
        }

    pre = extract(pre_row)
    post = extract(post_row)
    result["pre_test"] = pre
    result["post_test"] = post

    if pre and post:
        result["delta"] = {
            "battery_mV": (post.get("battery_mV") or 0.0) - (pre.get("battery_mV") or 0.0),
            "temperature_C": (post.get("temperature_C") or 0.0) - (pre.get("temperature_C") or 0.0),
            "charge_level": (post.get("charge_level") or 0.0) - (pre.get("charge_level") or 0.0),
        }
    return result


def load_metadata(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_ipg_uv_scale(metadata: Dict[str, Any], cli_gain_uv_per_count: float) -> Tuple[float, str]:
    ipg = metadata.get("ipg", {}) if isinstance(metadata, dict) else {}
    rec = ipg.get("recording", {}) if isinstance(ipg.get("recording", {}), dict) else {}

    if bool(rec.get("samples_converted_to_uV", False)):
        return 1.0, "metadata:samples_already_uV"

    adc_lsb_uv = parse_float(rec.get("adc_lsb_uV"))
    if adc_lsb_uv is not None and adc_lsb_uv > 0:
        return float(adc_lsb_uv), "metadata:adc_lsb_uV"

    return float(cli_gain_uv_per_count), "cli:gain_uv_per_count"


def estimate_sample_rate(time_values: List[float]) -> float | None:
    if len(time_values) < 3:
        return None
    finite_times = [float(t) for t in time_values if math.isfinite(t)]
    if len(finite_times) < 3:
        return None

    duration = finite_times[-1] - finite_times[0]
    if duration <= 0:
        return None

    # Wall-clock IPG timestamps can be bursty (short intra-chunk steps + chunk gaps).
    # Use global effective rate so FFT scaling reflects full-run timing.
    effective_rate = (len(finite_times) - 1) / duration
    if effective_rate <= 0:
        return None
    return float(effective_rate)


def dominant_frequency(samples: List[float], sample_rate_hz: float | None, fmin_hz: float = 1.0) -> Dict[str, Any]:
    if np is None or not sample_rate_hz or len(samples) < 64:
        return {"frequency_hz": None, "magnitude": None}

    arr = np.asarray(samples, dtype=float)
    arr = arr[np.isfinite(arr)]
    arr = arr - np.mean(arr)
    n = arr.size
    if n < 64:
        return {"frequency_hz": None, "magnitude": None}

    window = np.hanning(n)
    spec = np.abs(np.fft.rfft(arr * window))
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    mask = freqs >= fmin_hz
    if not np.any(mask):
        return {"frequency_hz": None, "magnitude": None}
    masked_spec = spec[mask]
    masked_freqs = freqs[mask]
    idx = int(np.argmax(masked_spec))
    return {
        "frequency_hz": float(masked_freqs[idx]),
        "magnitude": float(masked_spec[idx]),
    }


def _psd(samples: List[float], sample_rate_hz: float | None) -> Tuple[Any, Any]:
    if np is None or not sample_rate_hz or len(samples) < 128:
        return [], []
    arr = np.asarray(samples, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 128:
        return [], []

    arr = arr - np.mean(arr)
    n = arr.size
    nperseg = min(4096, max(256, 2 ** int(math.floor(math.log2(n // 4 if n >= 1024 else n)))))
    step = max(1, nperseg // 2)
    if n < nperseg:
        nperseg = n
        step = n

    window = np.hanning(nperseg)
    scale = sample_rate_hz * max(np.sum(window * window), 1.0)
    freqs = np.fft.rfftfreq(nperseg, d=1.0 / sample_rate_hz)
    acc = np.zeros(freqs.shape, dtype=float)
    count = 0
    start = 0
    while start + nperseg <= n:
        seg = arr[start : start + nperseg]
        seg = seg - np.mean(seg)
        xw = seg * window
        acc += (np.abs(np.fft.rfft(xw)) ** 2) / scale
        count += 1
        start += step
    if count == 0:
        return [], []
    pxx = acc / float(count)
    return freqs, pxx


def _spectral_channel_summary(
    samples: List[float],
    sample_rate_hz: float | None,
    expected_hz: List[float],
    fmin: float,
    fmax: float,
    tol_ratio: float,
) -> Dict[str, Any]:
    freqs, pxx = _psd(samples, sample_rate_hz)
    peaks = _top_psd_peaks(freqs, pxx, fmin=fmin, fmax=fmax, top_n=6)
    if not peaks:
        return {
            "dominant_frequency_hz": None,
            "nearest_expected_hz": None,
            "delta_hz": None,
            "status": "no-peak",
            "unexpected_peak_hz": None,
        }

    dom = peaks[0]
    dom_f = parse_float(dom.get("frequency_hz"))
    nearest = None
    delta = None
    matched = False
    if expected_hz and dom_f is not None:
        nearest = min(expected_hz, key=lambda f: abs(f - dom_f))
        delta = abs(dom_f - nearest)
        matched = delta <= max(1.0, tol_ratio * nearest)

    unexpected = None
    for p in peaks:
        f = parse_float(p.get("frequency_hz"))
        if f is None:
            continue
        if not expected_hz:
            unexpected = f
            break
        if not any(abs(f - ef) <= max(1.0, tol_ratio * ef) for ef in expected_hz):
            unexpected = f
            break

    return {
        "dominant_frequency_hz": dom_f,
        "nearest_expected_hz": nearest,
        "delta_hz": delta,
        "status": "match" if matched else "off-target",
        "unexpected_peak_hz": unexpected,
    }


def _top_psd_peaks(freqs: Any, pxx: Any, fmin: float, fmax: float, top_n: int = 5) -> List[Dict[str, float]]:
    if np is None or len(freqs) == 0 or len(pxx) == 0:
        return []
    mask = (freqs >= fmin) & (freqs <= fmax)
    if not np.any(mask):
        return []
    f = freqs[mask]
    p = pxx[mask]
    if f.size < 3:
        return []

    peak_idx = np.where((p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:]))[0] + 1
    if peak_idx.size == 0:
        peak_idx = np.argsort(p)[-top_n:]
    ranked = sorted(peak_idx.tolist(), key=lambda i: float(p[i]), reverse=True)[:top_n]
    return [{"frequency_hz": float(f[i]), "psd": float(p[i])} for i in ranked]


def dominant_frequency_near(
    samples: List[float],
    sample_rate_hz: float | None,
    target_hz: float | None,
    span_hz: float = 20.0,
) -> Dict[str, Any]:
    if np is None or not sample_rate_hz or not target_hz or len(samples) < 64:
        return {"frequency_hz": None, "magnitude": None}

    arr = np.asarray(samples, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 64:
        return {"frequency_hz": None, "magnitude": None}
    arr = arr - np.mean(arr)
    n = arr.size

    window = np.hanning(n)
    spec = np.abs(np.fft.rfft(arr * window))
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    lo = max(0.1, target_hz - span_hz)
    hi = target_hz + span_hz
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return {"frequency_hz": None, "magnitude": None}

    masked_spec = spec[mask]
    masked_freqs = freqs[mask]
    idx = int(np.argmax(masked_spec))
    return {"frequency_hz": float(masked_freqs[idx]), "magnitude": float(masked_spec[idx])}


def estimate_sine_amplitude(
    time_values: List[float],
    samples: List[float],
    frequency_hz: float | None,
    active_epochs: List[Tuple[float, float]] | None = None,
) -> Dict[str, Any]:
    if np is None or not frequency_hz or len(time_values) < 32 or len(samples) < 32:
        return {"amplitude_v": None, "rms_v": None}

    n = min(len(time_values), len(samples))
    t = np.asarray(time_values[:n], dtype=float)
    x = np.asarray(samples[:n], dtype=float)
    valid = np.isfinite(t) & np.isfinite(x)
    t = t[valid]
    x = x[valid]
    if t.size < 32:
        return {"amplitude_v": None, "rms_v": None}

    if active_epochs:
        mask = _mask_from_epochs(t, active_epochs)
        t = t[mask]
        x = x[mask]
        if t.size < 32:
            return {"amplitude_v": None, "rms_v": None}

    x = x - np.mean(x)
    w = 2.0 * math.pi * frequency_hz
    sinv = np.sin(w * t)
    cosv = np.cos(w * t)
    a = 2.0 * float(np.mean(x * sinv))
    b = 2.0 * float(np.mean(x * cosv))
    amp = math.sqrt(a * a + b * b)
    return {
        "amplitude_v": amp,
        "rms_v": amp / math.sqrt(2.0),
    }


def _moving_average(arr: Any, window: int) -> Any:
    if window <= 1:
        return arr.copy()
    kernel = np.ones(window, dtype=float) / float(window)
    return np.convolve(arr, kernel, mode="same")


def _extract_epochs(mask: Any, time_arr: Any, min_points: int) -> List[Tuple[float, float]]:
    epochs: List[Tuple[float, float]] = []
    i = 0
    n = len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        start = i
        while i < n and mask[i]:
            i += 1
        end = i - 1
        if (end - start + 1) >= min_points:
            epochs.append((float(time_arr[start]), float(time_arr[end])))
    return epochs


def detect_active_epochs(
    time_values: List[float],
    samples: List[float],
    sample_rate_hz: float | None,
) -> Dict[str, Any]:
    if np is None or not sample_rate_hz or len(time_values) < 64 or len(samples) < 64:
        return {"epochs": [], "threshold": None}

    n = min(len(time_values), len(samples))
    time_arr = np.asarray(time_values[:n], dtype=float)
    sig = np.asarray(samples[:n], dtype=float)
    valid = np.isfinite(time_arr) & np.isfinite(sig)
    time_arr = time_arr[valid]
    sig = sig[valid]
    if sig.size < 64:
        return {"epochs": [], "threshold": None}
    baseline_win = max(5, int(sample_rate_hz * 0.2))
    envelope_win = max(5, int(sample_rate_hz * 0.02))

    detrended = sig - _moving_average(sig, baseline_win)
    envelope = _moving_average(np.abs(detrended), envelope_win)
    med = float(np.median(envelope))
    mad = float(np.median(np.abs(envelope - med)))
    threshold = med + 4.0 * max(mad, 1e-9)
    mask = envelope > threshold
    epochs = _extract_epochs(mask, time_arr, min_points=max(5, int(sample_rate_hz * 0.05)))
    if not epochs and time_arr.size > 1:
        epochs = [(float(time_arr[0]), float(time_arr[-1]))]
    return {"epochs": epochs, "threshold": threshold}


def _mask_from_epochs(time_arr: Any, epochs: List[Tuple[float, float]]) -> Any:
    if not epochs:
        return np.ones_like(time_arr, dtype=bool)
    mask = np.zeros_like(time_arr, dtype=bool)
    for start, end in epochs:
        mask |= (time_arr >= start) & (time_arr <= end)
    return mask


def analyze_ipg_pulses(
    time_values: List[float],
    samples: List[float],
    sample_rate_hz: float | None,
    gain_uv_per_count: float,
    expected_freqs_hz: List[float],
) -> Dict[str, Any]:
    if np is None or not sample_rate_hz or len(time_values) < 128 or len(samples) < 128:
        return {}

    n = min(len(time_values), len(samples))
    time_arr = np.asarray(time_values[:n], dtype=float)
    sig = np.asarray(samples[:n], dtype=float)
    valid = np.isfinite(time_arr) & np.isfinite(sig)
    time_arr = time_arr[valid]
    sig = sig[valid]
    if sig.size < 128:
        return {}

    epoch_result = detect_active_epochs(time_arr.tolist(), sig.tolist(), sample_rate_hz)
    epochs = epoch_result.get("epochs", [])

    baseline_win = max(5, int(sample_rate_hz * 0.05))
    detrended = sig - _moving_average(sig, baseline_win)
    abs_sig = np.abs(detrended)

    active_mask = _mask_from_epochs(time_arr, epochs)
    active_abs = abs_sig[active_mask] if np.any(active_mask) else abs_sig
    noise_floor = float(np.median(active_abs)) if active_abs.size else float(np.median(abs_sig))
    p95 = float(np.percentile(active_abs, 95)) if active_abs.size else float(np.percentile(abs_sig, 95))
    peak_threshold = noise_floor + 0.35 * max(p95 - noise_floor, 1e-9)

    local_peaks = np.where(
        (abs_sig[1:-1] > abs_sig[:-2])
        & (abs_sig[1:-1] >= abs_sig[2:])
        & (abs_sig[1:-1] > peak_threshold)
        & active_mask[1:-1]
    )[0] + 1

    pos_peaks = np.where(
        (detrended[1:-1] > detrended[:-2])
        & (detrended[1:-1] >= detrended[2:])
        & (detrended[1:-1] > peak_threshold)
        & active_mask[1:-1]
    )[0] + 1
    neg_peaks = np.where(
        (detrended[1:-1] < detrended[:-2])
        & (detrended[1:-1] <= detrended[2:])
        & (detrended[1:-1] < -peak_threshold)
        & active_mask[1:-1]
    )[0] + 1

    best_expected_hz = None
    best_mag = -1.0
    for fexp in expected_freqs_hz:
        cand = dominant_frequency_near(sig.tolist(), sample_rate_hz, fexp, span_hz=20.0)
        mag = parse_float(cand.get("magnitude")) or 0.0
        if mag > best_mag:
            best_mag = mag
            best_expected_hz = fexp
    if not best_expected_hz:
        best_expected_hz = max(expected_freqs_hz) if expected_freqs_hz else 130.0

    refractory = max(1, int(sample_rate_hz / max(best_expected_hz * 1.2, 1.0)))
    period_s = 1.0 / max(best_expected_hz, 1.0)
    pair_gap_s = max(1.0 / max(sample_rate_hz, 1.0), min(0.006, 0.45 * period_s))
    selected: List[int] = []
    last_idx = -10 * refractory
    for idx in local_peaks.tolist():
        if idx - last_idx >= refractory:
            selected.append(idx)
            last_idx = idx

    # Biphasic pair matcher: one pulse event is a near-in-time opposite-polarity pair.
    signed_candidates: List[Tuple[int, int]] = []
    signed_candidates.extend((int(i), 1) for i in pos_peaks.tolist())
    signed_candidates.extend((int(i), -1) for i in neg_peaks.tolist())
    signed_candidates.sort(key=lambda x: x[0])

    paired_events: List[int] = []
    pair_count = 0
    i = 0
    while i < len(signed_candidates) - 1:
        idx0, pol0 = signed_candidates[i]
        idx1, pol1 = signed_candidates[i + 1]
        dt = float(time_arr[idx1] - time_arr[idx0])
        if pol0 != pol1 and 0.0 < dt <= pair_gap_s:
            pair_count += 1
            paired_events.append(idx0)
            i += 2
        else:
            i += 1

    if paired_events:
        # Merge pair events with generic events, then enforce refractory spacing.
        merged = sorted(set(selected + paired_events))
        selected = []
        last_idx = -10 * refractory
        for idx in merged:
            if idx - last_idx >= refractory:
                selected.append(idx)
                last_idx = idx

    if not selected:
        return {
            "active_epochs": epochs,
            "pulse_count": 0,
            "paired_event_count": 0,
            "pair_ratio_percent": 0.0,
            "peak_threshold_counts": peak_threshold,
            "observed_frequency_hz": None,
            "interval_ms_mean": None,
            "interval_ms_std": None,
            "amplitude_uv_mean": None,
            "amplitude_uv_std": None,
            "apparent_width_ms_mean": None,
            "apparent_width_ms_std": None,
            "offset_counts_mean": float(np.mean(sig)),
            "offset_counts_std": float(np.std(sig)),
            "clip_ratio_percent": float(np.mean(np.abs(sig) >= 8190.0) * 100.0),
        }

    peak_idx = np.asarray(selected, dtype=int)
    peak_times = time_arr[peak_idx]
    peak_amp_counts = abs_sig[peak_idx]

    intervals = np.diff(peak_times)
    interval_mean_s = float(np.mean(intervals)) if intervals.size else None
    interval_std_s = float(np.std(intervals)) if intervals.size else None

    # Apparent width from full-width at half-peak in detrended recording.
    widths_s: List[float] = []
    max_width_pts = max(2, int(sample_rate_hz * 0.01))
    for idx in peak_idx.tolist():
        h = abs_sig[idx] * 0.5
        left = idx
        left_limit = max(0, idx - max_width_pts)
        while left > left_limit and abs_sig[left] >= h:
            left -= 1
        right = idx
        right_limit = min(len(abs_sig) - 1, idx + max_width_pts)
        while right < right_limit and abs_sig[right] >= h:
            right += 1
        widths_s.append(float(time_arr[right] - time_arr[left]))

    observed_freq_hz = (1.0 / interval_mean_s) if interval_mean_s and interval_mean_s > 0 else None
    active_duration_s = sum(max(0.0, end - start) for start, end in epochs)
    expected_pulse_count = int(active_duration_s * best_expected_hz) if active_duration_s > 0 else None
    coverage_pct = None
    if expected_pulse_count and expected_pulse_count > 0:
        coverage_pct = 100.0 * len(peak_idx) / expected_pulse_count
    pair_ratio_percent = 100.0 * pair_count / max(len(peak_idx), 1)

    return {
        "active_epochs": epochs,
        "pulse_count": int(len(peak_idx)),
        "paired_event_count": int(pair_count),
        "pair_ratio_percent": pair_ratio_percent,
        "expected_frequency_hz": best_expected_hz,
        "expected_pulse_count_active": expected_pulse_count,
        "detection_coverage_percent": coverage_pct,
        "peak_threshold_counts": peak_threshold,
        "observed_frequency_hz": observed_freq_hz,
        "interval_ms_mean": (interval_mean_s * 1000.0) if interval_mean_s is not None else None,
        "interval_ms_std": (interval_std_s * 1000.0) if interval_std_s is not None else None,
        "amplitude_uv_mean": float(np.mean(peak_amp_counts) * gain_uv_per_count),
        "amplitude_uv_std": float(np.std(peak_amp_counts) * gain_uv_per_count),
        "apparent_width_ms_mean": float(np.mean(widths_s) * 1000.0) if widths_s else None,
        "apparent_width_ms_std": float(np.std(widths_s) * 1000.0) if widths_s else None,
        "offset_counts_mean": float(np.mean(sig)),
        "offset_counts_std": float(np.std(sig)),
        "clip_ratio_percent": float(np.mean(np.abs(sig) >= 8190.0) * 100.0),
    }


def analyze_ni_cycles(
    time_values: List[float],
    samples: List[float],
    active_epochs: List[Tuple[float, float]] | None = None,
) -> Dict[str, Any]:
    if len(time_values) < 32 or len(samples) < 32:
        return {}

    if np is None:
        return {}

    n = min(len(time_values), len(samples))
    time_arr = np.asarray(time_values[:n], dtype=float)
    sig = np.asarray(samples[:n], dtype=float)
    valid = np.isfinite(time_arr) & np.isfinite(sig)
    time_arr = time_arr[valid]
    sig = sig[valid]
    if sig.size < 32:
        return {}

    if active_epochs:
        mask = _mask_from_epochs(time_arr, active_epochs)
    else:
        mask = np.ones_like(time_arr, dtype=bool)

    t = time_arr[mask]
    x = sig[mask]
    if t.size < 32:
        return {}

    x = x - np.mean(x)

    # Rising zero-crossings for robust cycle timing in sine-like signals.
    crossing_times: List[float] = []
    for i in range(x.size - 1):
        x0 = x[i]
        x1 = x[i + 1]
        if x0 <= 0.0 < x1 and (x1 - x0) != 0.0:
            alpha = -x0 / (x1 - x0)
            crossing_times.append(float(t[i] + alpha * (t[i + 1] - t[i])))

    if len(crossing_times) < 3:
        return {}

    periods = [b - a for a, b in zip(crossing_times[:-1], crossing_times[1:]) if b > a]
    if not periods:
        return {}

    freq_est = 1.0 / mean(periods)

    # Cycle-by-cycle apparent amplitude (peak-to-peak/2 between consecutive crossings).
    cycle_amps: List[float] = []
    for t0, t1 in zip(crossing_times[:-1], crossing_times[1:]):
        idx = np.where((t >= t0) & (t < t1))[0]
        if idx.size < 2:
            continue
        segment = x[idx]
        amp = 0.5 * (float(np.max(segment)) - float(np.min(segment)))
        cycle_amps.append(amp)

    return {
        "cycle_count": len(periods),
        "frequency_hz_mean": float(freq_est),
        "frequency_hz_std": float(np.std(periods) / (mean(periods) ** 2)) if len(periods) > 1 else 0.0,
        "amplitude_v_mean": float(mean(cycle_amps)) if cycle_amps else None,
        "amplitude_v_std": float(np.std(cycle_amps)) if len(cycle_amps) > 1 else 0.0,
        "rms_v": float(math.sqrt(float(np.mean(x * x)))),
    }


def build_config_context(metadata: Dict[str, Any]) -> Dict[str, Any]:
    ipg = metadata.get("ipg", {})
    ni = metadata.get("ni", {})
    ipg_recording_meta = ipg.get("recording", {}) if isinstance(ipg.get("recording", {}), dict) else {}

    stim_cfg = ipg.get("stimulation", {}).get("channel_settings", {})
    rec_cfg = ipg.get("recording", {}).get("channel_settings", {})

    ni_shunt_ohm = 1000.0
    ipg_divider_ratio = 1000.0

    stim_enabled = []
    for ch, cfg in stim_cfg.items():
        if cfg.get("enabled"):
            amp_ma = parse_float(cfg.get("amplitude_ma"))
            biphasic = bool(cfg.get("biphasic", False))
            peak_v = (amp_ma / 1000.0) * ni_shunt_ohm if amp_ma is not None else None
            p2p_v = (2.0 * peak_v) if (peak_v is not None and biphasic) else peak_v
            divider_peak_v = (peak_v / ipg_divider_ratio) if peak_v is not None else None
            stim_enabled.append(
                {
                    "channel": ch,
                    **cfg,
                    "expected_voltage_peak_v_across_shunt": peak_v,
                    "expected_voltage_p2p_v_across_shunt": p2p_v,
                    "expected_divider_peak_v": divider_peak_v,
                }
            )

    rec_enabled = []
    for ch, cfg in rec_cfg.items():
        if cfg.get("enabled"):
            rec_enabled.append({"channel": ch, **cfg})

    ao_cfg = ni.get("ao", {}).get("waveforms", {})
    ao_active = []
    for ch in ni.get("ao", {}).get("active_channels", []):
        ao_active.append({"channel": str(ch), **ao_cfg.get(str(ch), {})})

    return {
        "run_mode": metadata.get("execution", {}).get("run_mode", "n/a"),
        "configured_duration_s": metadata.get("app", {}).get("default_test_duration_s", "n/a"),
        "ni_ai_sample_rate_hz": ni.get("ai", {}).get("sample_rate_hz"),
        "ni_ao_sample_rate_hz": ni.get("ao", {}).get("sample_rate_hz"),
        "ipg_nominal_sample_rate_hz": ipg_recording_meta.get("nominal_sample_rate_hz"),
        "ipg_measured_effective_sample_rate_hz": ipg_recording_meta.get("measured_effective_sample_rate_hz"),
        "ipg_measured_sample_count": ipg_recording_meta.get("measured_sample_count"),
        "ipg_measured_time_span_s": ipg_recording_meta.get("measured_time_span_s"),
        "ipg_measured_effective_rate_method": ipg_recording_meta.get("measured_effective_rate_method"),
        "ni_shunt_ohm": ni_shunt_ohm,
        "ipg_divider_ratio": ipg_divider_ratio,
        "ipg_recording_enabled": rec_enabled,
        "ipg_stimulation_enabled": stim_enabled,
        "ni_ao_active": ao_active,
        "ni_ai_active": ni.get("ai", {}).get("active_channels", []),
        # User-provided bench wiring map.
        "routing": {
            "ni_ai_from_ipg_stim": {
                "Channel_0": {"ipg_stim_channel": "1", "lead": "Lead 1"},
                "Channel_1": {"ipg_stim_channel": "2", "lead": "Lead 2"},
            },
            "ipg_recording_from_ni_ao": {
                "Channel_1": {"ni_ao_channel": "0", "lead": "Lead 1"},
                "Channel_2": {"ni_ao_channel": "0", "lead": "Lead 1"},
                "Channel_9": {"ni_ao_channel": "0", "lead": "Lead 1"},
                "Channel_3": {"ni_ao_channel": "1", "lead": "Lead 2"},
                "Channel_4": {"ni_ao_channel": "1", "lead": "Lead 2"},
            },
        },
    }


def analyze_performance(
    metadata: Dict[str, Any],
    ni_signal_data: Dict[str, Any],
    ipg_signal_data: Dict[str, Any],
    ni_summary: Dict[str, Any],
    ipg_summary: Dict[str, Any],
    gain_uv_per_count: float,
    gain_source: str = "cli:gain_uv_per_count",
) -> Dict[str, Any]:
    config_context = build_config_context(metadata)

    ni_sr = ni_summary.get("sample_rate_hz_est")
    ipg_sr = ipg_summary.get("sample_rate_hz_est")

    ni_dom_freq: Dict[str, Any] = {}
    ni_cycle_metrics: Dict[str, Any] = {}
    ipg_dom_freq: Dict[str, Any] = {}
    ipg_pulse_metrics: Dict[str, Any] = {}

    for ch_name, samples in ni_signal_data.get("channel_samples", {}).items():
        ni_dom_freq[ch_name] = dominant_frequency(samples, ni_sr)
        ni_cycle_metrics[ch_name] = analyze_ni_cycles(ni_signal_data.get("time_values", []), samples)

    expected_stim_freqs = [
        parse_float(ch.get("frequency_hz"))
        for ch in config_context.get("ipg_stimulation_enabled", [])
        if parse_float(ch.get("frequency_hz")) is not None
    ]

    for ch_name, samples in ipg_signal_data.get("channel_samples", {}).items():
        ipg_dom_freq[ch_name] = dominant_frequency(samples, ipg_sr)
        ipg_pulse_metrics[ch_name] = analyze_ipg_pulses(
            time_values=ipg_signal_data.get("time_values", []),
            samples=samples,
            sample_rate_hz=ipg_sr,
            gain_uv_per_count=gain_uv_per_count,
            expected_freqs_hz=[f for f in expected_stim_freqs if f],
        )

    # Cross-path expectations with explicit per-channel routing.
    # NI records IPG stimulation; IPG records NI output.
    stim_by_channel = {
        str(ch.get("channel")): ch
        for ch in config_context.get("ipg_stimulation_enabled", [])
    }
    ao_waveforms = metadata.get("ni", {}).get("ao", {}).get("waveforms", {})
    ao_active = {str(x) for x in metadata.get("ni", {}).get("ao", {}).get("active_channels", [])}

    ni_expected_by_channel: Dict[str, Dict[str, Any]] = {}
    for ni_ch, route in config_context.get("routing", {}).get("ni_ai_from_ipg_stim", {}).items():
        stim_ch = str(route.get("ipg_stim_channel"))
        stim = stim_by_channel.get(stim_ch, {})
        ni_expected_by_channel[ni_ch] = {
            "expected_frequency_hz": parse_float(stim.get("frequency_hz")),
            "source": f"IPG stim ch{stim_ch}",
            "source_active": bool(stim),
        }

    ipg_expected_by_channel: Dict[str, Dict[str, Any]] = {}
    for ipg_ch, route in config_context.get("routing", {}).get("ipg_recording_from_ni_ao", {}).items():
        ao_ch = str(route.get("ni_ao_channel"))
        wf = ao_waveforms.get(ao_ch, {})
        ipg_expected_by_channel[ipg_ch] = {
            "expected_frequency_hz": parse_float(wf.get("frequency_hz")),
            "source": f"NI AO{ao_ch}",
            "source_active": ao_ch in ao_active,
        }

    ni_vs_config: Dict[str, Any] = {}
    for ch_name, samples in ni_signal_data.get("channel_samples", {}).items():
        routed = ni_expected_by_channel.get(ch_name, {})
        expected_list = [parse_float(routed.get("expected_frequency_hz"))] if parse_float(routed.get("expected_frequency_hz")) else []
        summary = _spectral_channel_summary(
            samples=samples,
            sample_rate_hz=ni_sr,
            expected_hz=expected_list,
            fmin=1.0,
            fmax=min((ni_sr or 0.0) / 2.0, 1000.0) if ni_sr else 1000.0,
            tol_ratio=0.08,
        )
        ef = parse_float(summary.get("nearest_expected_hz"))
        mf = parse_float(summary.get("dominant_frequency_hz"))
        freq_pct = None
        if ef and mf:
            freq_pct = 100.0 * (mf - ef) / ef
        ni_vs_config[ch_name] = {
            "expected_frequency_hz": ef,
            "measured_frequency_hz": mf,
            "frequency_error_percent": freq_pct,
            "status": summary.get("status"),
            "source": routed.get("source"),
            "source_active": routed.get("source_active"),
        }

    ipg_vs_config: Dict[str, Any] = {}
    for ch_name, samples in ipg_signal_data.get("channel_samples", {}).items():
        routed = ipg_expected_by_channel.get(ch_name, {})
        expected_list = [parse_float(routed.get("expected_frequency_hz"))] if parse_float(routed.get("expected_frequency_hz")) else []
        summary = _spectral_channel_summary(
            samples=samples,
            sample_rate_hz=ipg_sr,
            expected_hz=expected_list,
            fmin=1.0,
            fmax=400.0,
            tol_ratio=0.1,
        )
        ef = parse_float(summary.get("nearest_expected_hz"))
        mf = parse_float(summary.get("dominant_frequency_hz"))
        freq_pct = None
        if ef and mf:
            freq_pct = 100.0 * (mf - ef) / ef
        ipg_vs_config[ch_name] = {
            "expected_frequency_hz": ef,
            "measured_frequency_hz": mf,
            "frequency_error_percent": freq_pct,
            "status": summary.get("status"),
            "source": routed.get("source"),
            "source_active": routed.get("source_active"),
        }

    spectral: Dict[str, Any] = {"ni": {}, "ipg": {}}
    for ch_name, samples in ni_signal_data.get("channel_samples", {}).items():
        routed = ni_expected_by_channel.get(ch_name, {})
        expected_list = [parse_float(routed.get("expected_frequency_hz"))] if parse_float(routed.get("expected_frequency_hz")) else []
        spectral["ni"][ch_name] = _spectral_channel_summary(
            samples=samples,
            sample_rate_hz=ni_sr,
            expected_hz=expected_list,
            fmin=1.0,
            fmax=min((ni_sr or 0.0) / 2.0, 1000.0) if ni_sr else 1000.0,
            tol_ratio=0.08,
        )
        spectral["ni"][ch_name]["source"] = routed.get("source")
        spectral["ni"][ch_name]["source_active"] = routed.get("source_active")

    for ch_name, samples in ipg_signal_data.get("channel_samples", {}).items():
        routed = ipg_expected_by_channel.get(ch_name, {})
        expected_list = [parse_float(routed.get("expected_frequency_hz"))] if parse_float(routed.get("expected_frequency_hz")) else []
        spectral["ipg"][ch_name] = _spectral_channel_summary(
            samples=samples,
            sample_rate_hz=ipg_sr,
            expected_hz=expected_list,
            fmin=1.0,
            fmax=400.0,
            tol_ratio=0.1,
        )
        spectral["ipg"][ch_name]["source"] = routed.get("source")
        spectral["ipg"][ch_name]["source_active"] = routed.get("source_active")

    return {
        "config_context": config_context,
        "ni_dominant_frequency": ni_dom_freq,
        "ni_cycle_metrics": ni_cycle_metrics,
        "ipg_dominant_frequency": ipg_dom_freq,
        "ipg_pulse_metrics": ipg_pulse_metrics,
        "spectral_analysis": spectral,
        "ni_vs_config": ni_vs_config,
        "ipg_vs_config": ipg_vs_config,
        "gain_uv_per_count": gain_uv_per_count,
        "gain_source": gain_source,
    }


def _decimate(time_values: List[float], samples: List[float], max_points: int = 4000) -> Tuple[List[float], List[float]]:
    n = min(len(time_values), len(samples))
    if n <= max_points or n == 0:
        return time_values[:n], samples[:n]
    stride = max(1, n // max_points)
    return time_values[:n:stride], samples[:n:stride]


def _smooth_series(arr: Any, window: int = 7) -> Any:
    if np is None:
        return arr
    x = np.asarray(arr, dtype=float)
    if x.size < window or window <= 1:
        return x
    kernel = np.ones(window, dtype=float) / float(window)
    return np.convolve(x, kernel, mode="same")


def generate_visualizations(
    report_dir: Path,
    ni_signal_data: Dict[str, Any],
    ipg_signal_data: Dict[str, Any],
    battery_summary: Dict[str, Any],
    performance: Dict[str, Any],
) -> Dict[str, str]:
    if np is None or plt is None:
        return {}

    out: Dict[str, str] = {}

    ni_time = ni_signal_data.get("time_values", [])
    ni_channels = ni_signal_data.get("channel_samples", {})
    if ni_time and ni_channels:
        fig, ax = plt.subplots(figsize=(10, 4))
        t0 = ni_time[0]
        window_end = t0 + 1.0
        for name, samples in ni_channels.items():
            pairs = [(t, v) for t, v in zip(ni_time, samples) if t <= window_end]
            if not pairs:
                continue
            t = [p[0] - t0 for p in pairs]
            y = [p[1] for p in pairs]
            t, y = _decimate(t, y, max_points=3000)
            ax.plot(t, y, lw=1.0, label=name)
        ax.set_title("NI Capture (IPG Stim Across 1k, First 1s After Startup-Trim)")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Voltage (V)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fname = "ni_waveforms.png"
        fig.savefig(report_dir / fname, dpi=140)
        plt.close(fig)
        out["ni_waveforms"] = fname

        fig, ax = plt.subplots(figsize=(10, 4))
        sr = estimate_sample_rate(ni_time)
        if sr:
            for name, samples in ni_channels.items():
                freqs, pxx = _psd(samples, sr)
                if freqs.size == 0:
                    continue
                mask = (freqs >= 0.0) & (freqs <= min(sr / 2.0, 200.0))
                if not np.any(mask):
                    continue
                y = 10.0 * np.log10(np.maximum(pxx[mask], 1e-20))
                y = _smooth_series(y, window=9)
                ax.plot(freqs[mask], y, lw=1.1, label=name)
            for ch in performance.get("config_context", {}).get("ni_ao_active", []):
                freq = parse_float(ch.get("frequency_hz"))
                if freq:
                    ax.axvline(freq, color="red", alpha=0.2, lw=1.0)
        ax.set_title("NI PSD (All Channels, 0-200 Hz)")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("PSD (dB/Hz)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fname = "ni_psd.png"
        fig.savefig(report_dir / fname, dpi=140)
        plt.close(fig)
        out["ni_psd"] = fname

    ipg_time = ipg_signal_data.get("time_values", [])
    ipg_channels = ipg_signal_data.get("channel_samples", {})
    gain = performance.get("gain_uv_per_count", 25.15)
    if ipg_time and ipg_channels:
        fig, ax = plt.subplots(figsize=(10, 4))
        t0 = ipg_time[0]
        window_end = t0 + 2.0
        for name, samples in ipg_channels.items():
            pairs = [(t, v) for t, v in zip(ipg_time, samples) if t <= window_end]
            if not pairs:
                continue
            t = [p[0] - t0 for p in pairs]
            y = [p[1] * gain for p in pairs]
            t, y = _decimate(t, y, max_points=3000)
            ax.plot(t, y, lw=0.9, label=name)
        ax.set_title("IPG Recording (First 2s After Startup-Trim, Scaled to uV)")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Amplitude (uV)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fname = "ipg_waveforms_uv.png"
        fig.savefig(report_dir / fname, dpi=140)
        plt.close(fig)
        out["ipg_waveforms"] = fname

        fig, ax = plt.subplots(figsize=(10, 4))
        sr = estimate_sample_rate(ipg_time)
        if sr:
            for name, samples in ipg_channels.items():
                freqs, pxx = _psd(samples, sr)
                if freqs.size == 0:
                    continue
                mask = (freqs >= 0.0) & (freqs <= min(sr / 2.0, 200.0))
                if not np.any(mask):
                    continue
                y = 10.0 * np.log10(np.maximum(pxx[mask], 1e-20))
                y = _smooth_series(y, window=9)
                ax.plot(freqs[mask], y, lw=1.1, label=name)
            for ch in performance.get("config_context", {}).get("ipg_stimulation_enabled", []):
                freq = parse_float(ch.get("frequency_hz"))
                if freq:
                    ax.axvline(freq, color="red", alpha=0.2, lw=1.0)
        ax.set_title("IPG PSD (All Channels, 0-200 Hz)")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("PSD (dB/Hz)")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fname = "ipg_psd.png"
        fig.savefig(report_dir / fname, dpi=140)
        plt.close(fig)
        out["ipg_psd"] = fname

    pre = battery_summary.get("pre_test", {})
    post = battery_summary.get("post_test", {})
    if pre and post:
        fig, axes = plt.subplots(1, 2, figsize=(8, 3.6))
        axes[0].bar(["Pre", "Post"], [pre.get("battery_mV", 0.0), post.get("battery_mV", 0.0)], color=["#2563eb", "#059669"])
        axes[0].set_title("Battery (mV)")
        axes[1].bar(["Pre", "Post"], [pre.get("temperature_C", 0.0), post.get("temperature_C", 0.0)], color=["#f59e0b", "#ef4444"])
        axes[1].set_title("Temperature (C)")
        for ax in axes:
            ax.grid(True, axis="y", alpha=0.25)
        fig.tight_layout()
        fname = "battery_temp.png"
        fig.savefig(report_dir / fname, dpi=140)
        plt.close(fig)
        out["battery_temp"] = fname

    return out


def render_markdown_report(
    report_path: Path,
    metadata: Dict[str, Any],
    ni_summary: Dict[str, Any],
    ipg_summary: Dict[str, Any],
    battery_summary: Dict[str, Any],
    performance: Dict[str, Any],
    html_path: Path,
    visual_assets: Dict[str, str],
) -> None:
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = []
    lines.append("# Session Analysis Report")
    lines.append("")
    lines.append(f"- Generated: {created_at}")
    lines.append(f"- Metadata file: {metadata.get('_source_path', 'n/a')}")
    lines.append(f"- HTML report: {html_path}")
    lines.append("")

    cfg = performance.get("config_context", {})
    lines.append("## Run Summary")
    lines.append("")
    lines.append(f"- Configured duration (s): {cfg.get('configured_duration_s', 'n/a')}")
    lines.append(f"- NI rows analyzed: {ni_summary.get('row_count', 0)}")
    lines.append(f"- IPG rows analyzed: {ipg_summary.get('row_count', 0)}")
    lines.append(f"- NI full duration (s): {fmt(ni_summary.get('duration_s'), 2)}")
    lines.append(f"- IPG full duration (s): {fmt(ipg_summary.get('duration_s'), 2)}")
    lines.append(f"- NI analyzed duration (s): {fmt(ni_summary.get('analyzed_duration_s'), 2)}")
    lines.append(f"- IPG analyzed duration (s): {fmt(ipg_summary.get('analyzed_duration_s'), 2)}")
    lines.append(f"- NI sample rate est (Hz): {fmt(ni_summary.get('sample_rate_hz_est'), 2)}")
    lines.append(f"- IPG sample rate est (Hz): {fmt(ipg_summary.get('sample_rate_hz_est'), 2)}")
    ipg_nominal_sr = parse_float(cfg.get("ipg_nominal_sample_rate_hz"))
    ipg_measured_sr = parse_float(cfg.get("ipg_measured_effective_sample_rate_hz"))
    ipg_measured_n = cfg.get("ipg_measured_sample_count")
    ipg_measured_span = parse_float(cfg.get("ipg_measured_time_span_s"))
    if ipg_measured_sr is not None:
        lines.append(
            f"- IPG measured effective sample rate (Hz): {fmt(ipg_measured_sr, 2)} "
            f"(samples={ipg_measured_n}, span={fmt(ipg_measured_span, 3)} s)"
        )
    if ipg_nominal_sr is not None and ipg_measured_sr is not None and ipg_nominal_sr > 0:
        ipg_dev_pct = 100.0 * (ipg_measured_sr - ipg_nominal_sr) / ipg_nominal_sr
        lines.append(f"- IPG measured-vs-nominal deviation (%): {fmt(ipg_dev_pct, 2)}")
    _ipg_nominal_md = ipg_nominal_sr if ipg_nominal_sr is not None and ipg_nominal_sr > 0 else 1024.0
    _ipg_sr_est_md = ipg_summary.get("sample_rate_hz_est")
    if _ipg_sr_est_md is not None and abs(_ipg_sr_est_md - _ipg_nominal_md) / _ipg_nominal_md > 0.05:
        _ipg_sr_pct_md = 100.0 * (_ipg_sr_est_md - _ipg_nominal_md) / _ipg_nominal_md
        _ipg_sr_dir_md = "below" if _ipg_sr_pct_md < 0 else "above"
        lines.append(
            f"- \u26a0\ufe0f Effective IPG delivery rate ({_ipg_sr_est_md:.0f} Hz) is "
            f"{abs(_ipg_sr_pct_md):.1f}% {_ipg_sr_dir_md} nominal {_ipg_nominal_md:.0f} Hz \u2014 "
            "CSV timestamps are wall-clock based; frequency analysis is automatically corrected."
        )
    lines.append(f"- Startup-trim ignored at beginning (s): {fmt(ni_summary.get('analysis_skip_initial_s'), 2)}")
    lines.append(f"- NI raw rows / analyzed rows: {ni_summary.get('raw_row_count', 0)} / {ni_summary.get('row_count', 0)}")
    lines.append(f"- IPG raw rows / analyzed rows: {ipg_summary.get('raw_row_count', 0)} / {ipg_summary.get('row_count', 0)}")
    lines.append(f"- Run mode: {cfg.get('run_mode', 'n/a')}")
    lines.append(f"- IPG gain used (uV/count): {fmt(performance.get('gain_uv_per_count'), 2)}")
    lines.append(f"- IPG gain source: {performance.get('gain_source', 'n/a')}")
    lines.append("")
    lines.append("Hardware assumptions: IPG recording is voltage divider output (1/1000). NI reading is across 1k ohm resistor.")
    lines.append("")

    lines.append("## Config Context")
    lines.append("")
    lines.append("### Enabled IPG Stimulation Channels")
    for ch in cfg.get("ipg_stimulation_enabled", []):
        lines.append(
            f"- Ch{ch.get('channel')}: {ch.get('lead')} E{ch.get('anode')}->E{ch.get('cathode')}, "
            f"{fmt(parse_float(ch.get('amplitude_ma')), 2)} mA, {fmt(parse_float(ch.get('pulse_width_ms')) * 1000.0 if parse_float(ch.get('pulse_width_ms')) else None, 1)} us, "
            f"{fmt(parse_float(ch.get('frequency_hz')), 2)} Hz, burst on/off {fmt(parse_float(ch.get('burst_on_s')), 2)}/{fmt(parse_float(ch.get('burst_off_s')), 2)} s, "
            f"expected shunt Vpeak/Vpp {fmt(ch.get('expected_voltage_peak_v_across_shunt'), 3)}/{fmt(ch.get('expected_voltage_p2p_v_across_shunt'), 3)} V, "
            f"divider output peak {fmt((parse_float(ch.get('expected_divider_peak_v')) or 0.0) * 1000.0 if ch.get('expected_divider_peak_v') is not None else None, 3)} mV"
        )
    lines.append("")

    lines.append("### Enabled IPG Recording Channels")
    for ch in cfg.get("ipg_recording_enabled", []):
        neg = ch.get("negative_elec")
        neg_str = f"E{neg}" if isinstance(neg, (int, float)) else str(neg)
        lines.append(f"- Ch{ch.get('channel')}: {ch.get('lead')} +E{ch.get('positive_elec')} / -{neg_str}")
    lines.append("")

    lines.append("## Cross-Path Frequency Check")
    lines.append("")
    lines.append("### NI (expected from IPG stim frequencies)")
    for ch_name, comp in performance.get("ni_vs_config", {}).items():
        lines.append(f"### {ch_name}")
        lines.append(f"- Source: {comp.get('source', 'n/a')} (active: {comp.get('source_active', 'n/a')})")
        lines.append(f"- Expected freq (Hz): {fmt(comp.get('expected_frequency_hz'), 2)}")
        lines.append(f"- Measured freq (Hz): {fmt(comp.get('measured_frequency_hz'), 2)}")
        lines.append(f"- Freq error (%): {fmt(comp.get('frequency_error_percent'), 2)}")
        lines.append(f"- Status: {comp.get('status', 'n/a')}")
        lines.append("")

    lines.append("### IPG (expected from NI AO frequencies)")
    for ch_name, comp in performance.get("ipg_vs_config", {}).items():
        lines.append(f"### {ch_name}")
        lines.append(f"- Source: {comp.get('source', 'n/a')} (active: {comp.get('source_active', 'n/a')})")
        lines.append(f"- Expected freq (Hz): {fmt(comp.get('expected_frequency_hz'), 2)}")
        lines.append(f"- Measured freq (Hz): {fmt(comp.get('measured_frequency_hz'), 2)}")
        lines.append(f"- Freq error (%): {fmt(comp.get('frequency_error_percent'), 2)}")
        lines.append(f"- Status: {comp.get('status', 'n/a')}")
        lines.append("")

    lines.append("## Spectral Analysis And Noise Candidates")
    lines.append("")
    lines.append("### NI Channels (concise)")
    for ch_name, spec in performance.get("spectral_analysis", {}).get("ni", {}).items():
        lines.append(
            f"- {ch_name} [{spec.get('source', 'n/a')} active={spec.get('source_active', 'n/a')}]: dominant {fmt(spec.get('dominant_frequency_hz'), 2)} Hz, "
            f"expected {fmt(spec.get('nearest_expected_hz'), 2)} Hz, "
            f"delta {fmt(spec.get('delta_hz'), 2)} Hz, status {spec.get('status', 'n/a')}, "
            f"unexpected peak {fmt(spec.get('unexpected_peak_hz'), 2)} Hz"
        )
    lines.append("")

    lines.append("### IPG Channels (concise)")
    for ch_name, spec in performance.get("spectral_analysis", {}).get("ipg", {}).items():
        lines.append(
            f"- {ch_name} [{spec.get('source', 'n/a')} active={spec.get('source_active', 'n/a')}]: dominant {fmt(spec.get('dominant_frequency_hz'), 2)} Hz, "
            f"expected {fmt(spec.get('nearest_expected_hz'), 2)} Hz, "
            f"delta {fmt(spec.get('delta_hz'), 2)} Hz, status {spec.get('status', 'n/a')}, "
            f"unexpected peak {fmt(spec.get('unexpected_peak_hz'), 2)} Hz"
        )
    lines.append("")

    lines.append("## IPG Pulse Analysis")
    lines.append("")
    lines.append("Note: IPG pulse width/amplitude are apparent values from recording channels and can differ from programmed output due to offsets, tissue path, and front-end response.")
    lines.append("")
    for ch_name, metrics in performance.get("ipg_pulse_metrics", {}).items():
        lines.append(f"### {ch_name}")
        lines.append(f"- Pulse count: {metrics.get('pulse_count', 0)}")
        lines.append(f"- Biphasic paired events: {metrics.get('paired_event_count', 0)}")
        lines.append(f"- Pair ratio (% of detected pulses): {fmt(metrics.get('pair_ratio_percent'), 2)}")
        lines.append(f"- Observed pulse frequency (Hz): {fmt(metrics.get('observed_frequency_hz'), 2)}")
        lines.append(f"- Inter-pulse interval mean (ms): {fmt(metrics.get('interval_ms_mean'), 3)}")
        lines.append(f"- Inter-pulse interval std (ms): {fmt(metrics.get('interval_ms_std'), 3)}")
        lines.append(f"- Apparent pulse amplitude mean (uV): {fmt(metrics.get('amplitude_uv_mean'), 1)}")
        lines.append(f"- Apparent pulse amplitude std (uV): {fmt(metrics.get('amplitude_uv_std'), 1)}")
        lines.append(f"- Apparent pulse width mean (ms): {fmt(metrics.get('apparent_width_ms_mean'), 4)}")
        lines.append(f"- Apparent pulse width std (ms): {fmt(metrics.get('apparent_width_ms_std'), 4)}")
        lines.append(f"- Clip ratio (% near +/-8192): {fmt(metrics.get('clip_ratio_percent'), 3)}")
        lines.append(f"- Expected pulse frequency used (Hz): {fmt(metrics.get('expected_frequency_hz'), 2)}")
        lines.append(f"- Expected pulse count in active epochs: {fmt(metrics.get('expected_pulse_count_active'), 0)}")
        lines.append(f"- Detection coverage (%): {fmt(metrics.get('detection_coverage_percent'), 2)}")
        epochs = metrics.get("active_epochs", [])
        lines.append(f"- Detected active epochs: {len(epochs)}")
        lines.append("")

    lines.append("## Battery And Temperature")
    lines.append("")
    pre = battery_summary.get("pre_test", {})
    post = battery_summary.get("post_test", {})
    delta = battery_summary.get("delta", {})
    lines.append(f"- Pre-test battery (mV): {fmt(pre.get('battery_mV'), 1)}")
    lines.append(f"- Post-test battery (mV): {fmt(post.get('battery_mV'), 1)}")
    lines.append(f"- Delta battery (mV): {fmt(delta.get('battery_mV'), 1)}")
    lines.append(f"- Pre-test temperature (C): {fmt(pre.get('temperature_C'), 2)}")
    lines.append(f"- Post-test temperature (C): {fmt(post.get('temperature_C'), 2)}")
    lines.append(f"- Delta temperature (C): {fmt(delta.get('temperature_C'), 2)}")
    lines.append("")

    if visual_assets:
        lines.append("## Visual Assets")
        lines.append("")
        for key, val in visual_assets.items():
            lines.append(f"- {key}: {val}")
        lines.append("")

    lines.append("## Inputs")
    lines.append("")
    lines.append(f"- NI CSV: {ni_summary.get('file', 'n/a')}")
    lines.append(f"- IPG CSV: {ipg_summary.get('file', 'n/a')}")
    lines.append(f"- Battery CSV: {battery_summary.get('file', 'n/a')}")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")


def _html_escape(text: Any) -> str:
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_html_report(
    html_path: Path,
    metadata: Dict[str, Any],
    ni_summary: Dict[str, Any],
    ipg_summary: Dict[str, Any],
    battery_summary: Dict[str, Any],
    performance: Dict[str, Any],
    visual_assets: Dict[str, str],
) -> None:
    cfg = performance.get("config_context", {})
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def row(label: str, value: Any) -> str:
        return f"<tr><th>{_html_escape(label)}</th><td>{_html_escape(value)}</td></tr>"

    stim_rows = []
    for ch in cfg.get("ipg_stimulation_enabled", []):
        shunt_v = f"{fmt(ch.get('expected_voltage_peak_v_across_shunt'), 3)}/{fmt(ch.get('expected_voltage_p2p_v_across_shunt'), 3)}"
        stim_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch.get('channel'))}</td>"
            f"<td>{_html_escape(ch.get('lead'))}</td>"
            f"<td>{_html_escape(ch.get('anode'))}</td>"
            f"<td>{_html_escape(ch.get('cathode'))}</td>"
            f"<td>{_html_escape(fmt(parse_float(ch.get('amplitude_ma')), 2))}</td>"
            f"<td>{_html_escape(fmt(parse_float(ch.get('pulse_width_ms')) * 1000.0 if parse_float(ch.get('pulse_width_ms')) else None, 1))}</td>"
            f"<td>{_html_escape(fmt(parse_float(ch.get('frequency_hz')), 2))}</td>"
            f"<td>{_html_escape(fmt(parse_float(ch.get('burst_on_s')), 2))}/{_html_escape(fmt(parse_float(ch.get('burst_off_s')), 2))}</td>"
            f"<td>{_html_escape(shunt_v)}</td>"
            "</tr>"
        )

    rec_rows = []
    for ch in cfg.get("ipg_recording_enabled", []):
        neg = ch.get("negative_elec")
        neg_str = f"E{neg}" if isinstance(neg, (int, float)) else str(neg)
        rec_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch.get('channel'))}</td>"
            f"<td>{_html_escape(ch.get('lead'))}</td>"
            f"<td>{_html_escape(ch.get('positive_elec'))}</td>"
            f"<td>{_html_escape(neg_str)}</td>"
            "</tr>"
        )

    ni_comp_rows = []
    for ch_name, comp in performance.get("ni_vs_config", {}).items():
        ni_comp_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch_name)}</td>"
            f"<td>{_html_escape(comp.get('source', 'n/a'))}</td>"
            f"<td>{_html_escape(comp.get('source_active', 'n/a'))}</td>"
            f"<td>{_html_escape(fmt(comp.get('expected_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(comp.get('measured_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(comp.get('frequency_error_percent'), 2))}</td>"
            f"<td>{_html_escape(comp.get('status', 'n/a'))}</td>"
            "</tr>"
        )

    ipg_comp_rows = []
    for ch_name, comp in performance.get("ipg_vs_config", {}).items():
        ipg_comp_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch_name)}</td>"
            f"<td>{_html_escape(comp.get('source', 'n/a'))}</td>"
            f"<td>{_html_escape(comp.get('source_active', 'n/a'))}</td>"
            f"<td>{_html_escape(fmt(comp.get('expected_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(comp.get('measured_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(comp.get('frequency_error_percent'), 2))}</td>"
            f"<td>{_html_escape(comp.get('status', 'n/a'))}</td>"
            "</tr>"
        )

    ipg_pulse_rows = []
    for ch_name, m in performance.get("ipg_pulse_metrics", {}).items():
        ipg_pulse_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch_name)}</td>"
            f"<td>{_html_escape(m.get('pulse_count', 0))}</td>"
            f"<td>{_html_escape(m.get('paired_event_count', 0))}</td>"
            f"<td>{_html_escape(fmt(m.get('pair_ratio_percent'), 2))}</td>"
            f"<td>{_html_escape(fmt(m.get('expected_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(m.get('observed_frequency_hz'), 2))}</td>"
            f"<td>{_html_escape(fmt(m.get('amplitude_uv_mean'), 1))}</td>"
            f"<td>{_html_escape(fmt(m.get('amplitude_uv_std'), 1))}</td>"
            f"<td>{_html_escape(fmt(m.get('apparent_width_ms_mean'), 4))}</td>"
            f"<td>{_html_escape(fmt(m.get('apparent_width_ms_std'), 4))}</td>"
            f"<td>{_html_escape(fmt(m.get('clip_ratio_percent'), 3))}</td>"
            f"<td>{_html_escape(fmt(m.get('detection_coverage_percent'), 2))}</td>"
            "</tr>"
        )

    ni_spectral_rows = []
    for ch_name, spec in performance.get("spectral_analysis", {}).get("ni", {}).items():
        top = fmt(spec.get("dominant_frequency_hz"), 2)
        exp = fmt(spec.get("nearest_expected_hz"), 2)
        delta = fmt(spec.get("delta_hz"), 2)
        unexpected = fmt(spec.get("unexpected_peak_hz"), 2)
        ni_spectral_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch_name)}</td>"
            f"<td>{_html_escape(spec.get('source', 'n/a'))}</td>"
            f"<td>{_html_escape(spec.get('source_active', 'n/a'))}</td>"
            f"<td>{_html_escape(top)}</td>"
            f"<td>{_html_escape(exp)}</td>"
            f"<td>{_html_escape(delta)}</td>"
            f"<td>{_html_escape(spec.get('status', 'n/a'))}</td>"
            f"<td>{_html_escape(unexpected)}</td>"
            "</tr>"
        )

    ipg_spectral_rows = []
    for ch_name, spec in performance.get("spectral_analysis", {}).get("ipg", {}).items():
        top = fmt(spec.get("dominant_frequency_hz"), 2)
        exp = fmt(spec.get("nearest_expected_hz"), 2)
        delta = fmt(spec.get("delta_hz"), 2)
        unexpected = fmt(spec.get("unexpected_peak_hz"), 2)
        ipg_spectral_rows.append(
            "<tr>"
            f"<td>{_html_escape(ch_name)}</td>"
            f"<td>{_html_escape(spec.get('source', 'n/a'))}</td>"
            f"<td>{_html_escape(spec.get('source_active', 'n/a'))}</td>"
            f"<td>{_html_escape(top)}</td>"
            f"<td>{_html_escape(exp)}</td>"
            f"<td>{_html_escape(delta)}</td>"
            f"<td>{_html_escape(spec.get('status', 'n/a'))}</td>"
            f"<td>{_html_escape(unexpected)}</td>"
            "</tr>"
        )

    image_blocks = []
    for key, filename in visual_assets.items():
        image_blocks.append(
            "<figure class='card'>"
            f"<figcaption>{_html_escape(key.replace('_', ' ').title())}</figcaption>"
            f"<img src='{_html_escape(filename)}' loading='lazy' />"
            "</figure>"
        )

    ipg_nominal_sr = parse_float(cfg.get("ipg_nominal_sample_rate_hz"))
    _ipg_nominal_html = ipg_nominal_sr if ipg_nominal_sr is not None and ipg_nominal_sr > 0 else 1024.0
    _ipg_sr_est_html = ipg_summary.get("sample_rate_hz_est")
    _ipg_rate_warn_row = ""
    if _ipg_sr_est_html is not None and abs(_ipg_sr_est_html - _ipg_nominal_html) / _ipg_nominal_html > 0.05:
        _ipg_sr_pct_html = 100.0 * (_ipg_sr_est_html - _ipg_nominal_html) / _ipg_nominal_html
        _ipg_sr_dir_html = "below" if _ipg_sr_pct_html < 0 else "above"
        _ipg_rate_warn_row = (
            "<tr>"
            "<th style='color:#854d0e;'>&#x26A0;&#xFE0F; IPG rate note</th>"
            f"<td style='color:#854d0e;'>Effective delivery rate ({_ipg_sr_est_html:.0f}\u202fHz) is "
            f"{abs(_ipg_sr_pct_html):.1f}% {_ipg_sr_dir_html} nominal {_ipg_nominal_html:.0f}\u202fHz \u2014 "
            "wall-clock timestamps in use; frequency analysis is auto-corrected.</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>DBS Session Analysis Report</title>
  <style>
    :root {{
      --bg: #f7f7f5;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --accent: #0f766e;
      --border: #e5e7eb;
    }}
    body {{ margin: 0; font-family: "Segoe UI", Tahoma, sans-serif; color: var(--text); background: linear-gradient(180deg, #eef7f4 0%, var(--bg) 40%); }}
    main {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    h1, h2, h3 {{ margin: 0 0 10px 0; }}
    h1 {{ font-size: 1.7rem; color: #0b4f4a; }}
    .sub {{ color: var(--muted); margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
    .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.03); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{ border-bottom: 1px solid var(--border); text-align: left; padding: 7px 8px; }}
    th {{ color: #0f172a; background: #f8fafc; }}
    td {{ color: #111827; }}
    .kv th {{ width: 48%; }}
    .note {{ color: #374151; background: #ecfeff; border: 1px solid #a5f3fc; border-radius: 8px; padding: 10px; font-size: 0.92rem; }}
    figure {{ margin: 0; }}
    figcaption {{ margin-bottom: 8px; font-weight: 600; color: #0b4f4a; }}
    img {{ width: 100%; border: 1px solid var(--border); border-radius: 8px; background: #fff; }}
    details {{ margin-top: 10px; }}
    summary {{ cursor: pointer; color: #0f766e; font-weight: 600; }}
  </style>
</head>
<body>
  <main>
    <h1>DBS Session Analysis Report</h1>
    <p class="sub">Generated {created_at} | Metadata: {_html_escape(metadata.get('_source_path', 'n/a'))}</p>

    <section class="grid">
      <article class="card">
        <h2>Run Summary</h2>
        <table class="kv">
          {row("Configured duration (s)", cfg.get("configured_duration_s", "n/a"))}
          {row("Run mode", cfg.get("run_mode", "n/a"))}
          {row("NI rows analyzed", ni_summary.get("row_count", 0))}
          {row("IPG rows analyzed", ipg_summary.get("row_count", 0))}
          {row("NI full duration (s)", fmt(ni_summary.get("duration_s"), 2))}
          {row("IPG full duration (s)", fmt(ipg_summary.get("duration_s"), 2))}
          {row("NI analyzed duration (s)", fmt(ni_summary.get("analyzed_duration_s"), 2))}
          {row("IPG analyzed duration (s)", fmt(ipg_summary.get("analyzed_duration_s"), 2))}
          {row("NI sample rate est (Hz)", fmt(ni_summary.get("sample_rate_hz_est"), 2))}
          {row("IPG sample rate est (Hz)", fmt(ipg_summary.get("sample_rate_hz_est"), 2))}
          {row("IPG gain source", performance.get("gain_source", "n/a"))}
          {_ipg_rate_warn_row}
                    {row("Startup-trim (s)", fmt(ni_summary.get("analysis_skip_initial_s"), 2))}
                    {row("NI raw/analyzed rows", f"{ni_summary.get('raw_row_count', 0)} / {ni_summary.get('row_count', 0)}")}
                    {row("IPG raw/analyzed rows", f"{ipg_summary.get('raw_row_count', 0)} / {ipg_summary.get('row_count', 0)}")}
          {row("Gain (uV/count)", fmt(performance.get("gain_uv_per_count"), 2))}
        </table>
                <p class="note" style="margin-top:10px;">Hardware assumptions: IPG recording is divider output (1/1000). NI reading is across 1k ohm resistor. For biphasic square pulse amplitude I, expected shunt Vpp is 2 * I * R.</p>
      </article>

      <article class="card">
        <h2>Battery And Temperature</h2>
        <table class="kv">
          {row("Pre battery (mV)", fmt(battery_summary.get("pre_test", {}).get("battery_mV"), 1))}
          {row("Post battery (mV)", fmt(battery_summary.get("post_test", {}).get("battery_mV"), 1))}
          {row("Delta battery (mV)", fmt(battery_summary.get("delta", {}).get("battery_mV"), 1))}
          {row("Pre temp (C)", fmt(battery_summary.get("pre_test", {}).get("temperature_C"), 2))}
          {row("Post temp (C)", fmt(battery_summary.get("post_test", {}).get("temperature_C"), 2))}
          {row("Delta temp (C)", fmt(battery_summary.get("delta", {}).get("temperature_C"), 2))}
        </table>
      </article>
    </section>

    <section class="grid" style="margin-top:14px;">
      <article class="card">
        <h2>IPG Stim Config</h2>
        <table>
                    <tr><th>Ch</th><th>Lead</th><th>Anode</th><th>Cathode</th><th>Amp (mA)</th><th>PW (us)</th><th>Freq (Hz)</th><th>Burst on/off (s)</th><th>Shunt Vpeak/Vpp (V)</th></tr>
          {''.join(stim_rows) if stim_rows else '<tr><td colspan="9">No enabled stimulation channels</td></tr>'}
        </table>
      </article>

      <article class="card">
        <h2>IPG Recording Config</h2>
        <table>
          <tr><th>Ch</th><th>Lead</th><th>Input +</th><th>Input -</th></tr>
          {''.join(rec_rows) if rec_rows else '<tr><td colspan="4">No enabled recording channels</td></tr>'}
        </table>
      </article>
    </section>

    <section class="card" style="margin-top:14px;">
            <h2>NI (Expected From IPG Stim)</h2>
      <table>
                <tr><th>Channel</th><th>Source</th><th>Source Active</th><th>Exp Freq (Hz)</th><th>Meas Freq (Hz)</th><th>Freq Err (%)</th><th>Status</th></tr>
                {''.join(ni_comp_rows) if ni_comp_rows else '<tr><td colspan="7">No comparable NI channels found</td></tr>'}
            </table>
        </section>

        <section class="card" style="margin-top:14px;">
            <h2>IPG (Expected From NI AO)</h2>
            <table>
                <tr><th>Channel</th><th>Source</th><th>Source Active</th><th>Exp Freq (Hz)</th><th>Meas Freq (Hz)</th><th>Freq Err (%)</th><th>Status</th></tr>
                {''.join(ipg_comp_rows) if ipg_comp_rows else '<tr><td colspan="7">No comparable IPG channels found</td></tr>'}
      </table>
    </section>

    <section class="card" style="margin-top:14px;">
      <h2>IPG Pulse Analysis</h2>
      <p class="note">Apparent pulse amplitude/width are estimated from recording channels after offset-robust detrending. Values may differ from programmed stim output.</p>
      <table>
                <tr><th>Channel</th><th>Pulse Count</th><th>Paired Events</th><th>Pair Ratio (%)</th><th>Exp Freq (Hz)</th><th>Obs Freq (Hz)</th><th>Amp Mean (uV)</th><th>Amp Std (uV)</th><th>Width Mean (ms)</th><th>Width Std (ms)</th><th>Clip Ratio (%)</th><th>Coverage (%)</th></tr>
                {''.join(ipg_pulse_rows) if ipg_pulse_rows else '<tr><td colspan="12">No pulse metrics available</td></tr>'}
      </table>
    </section>

        <section class="grid" style="margin-top:14px;">
            <article class="card">
                <h2>NI Spectral Summary</h2>
                <table>
                    <tr><th>Channel</th><th>Source</th><th>Source Active</th><th>Dominant (Hz)</th><th>Expected (Hz)</th><th>Delta (Hz)</th><th>Status</th><th>Unexpected Peak (Hz)</th></tr>
                    {''.join(ni_spectral_rows) if ni_spectral_rows else '<tr><td colspan="8">No NI spectral results</td></tr>'}
                </table>
            </article>
            <article class="card">
                <h2>IPG Spectral Summary</h2>
                <table>
                    <tr><th>Channel</th><th>Source</th><th>Source Active</th><th>Dominant (Hz)</th><th>Expected (Hz)</th><th>Delta (Hz)</th><th>Status</th><th>Unexpected Peak (Hz)</th></tr>
                    {''.join(ipg_spectral_rows) if ipg_spectral_rows else '<tr><td colspan="8">No IPG spectral results</td></tr>'}
                </table>
            </article>
        </section>

    <section class="grid" style="margin-top:14px;">
      {''.join(image_blocks) if image_blocks else '<article class="card"><h2>Visualizations</h2><p>Matplotlib/Numpy not available, no images generated.</p></article>'}
    </section>

    <section class="card" style="margin-top:14px;">
      <details>
        <summary>Input Files</summary>
        <table class="kv">
          {row("NI CSV", ni_summary.get("file", "n/a"))}
          {row("IPG CSV", ipg_summary.get("file", "n/a"))}
          {row("Battery CSV", battery_summary.get("file", "n/a"))}
        </table>
      </details>
    </section>
  </main>
</body>
</html>
"""

    html_path.write_text(html, encoding="utf-8")


def generate_comprehensive_report(
    ni_csv: Path,
    ipg_csv: Path,
    battery_csv: Path,
    metadata_json: Path,
    output_root: Path,
    stem: str,
    gain_uv_per_count: float,
    skip_initial_s: float,
) -> Dict[str, str]:
    report_dir = ensure_directory(output_root / "reports" / stem)
    report_path = report_dir / "analysis_report.md"
    report_html_path = report_dir / "analysis_report.html"
    summary_json_path = report_dir / "analysis_summary.json"

    metadata = load_metadata(metadata_json)
    metadata["_source_path"] = str(metadata_json)

    ni_signal_data = load_signal_series(ni_csv, skip_initial_s=skip_initial_s)
    ipg_signal_data = load_signal_series(ipg_csv, skip_initial_s=skip_initial_s)
    ni_summary = summarize_signal_data(ni_signal_data)
    ipg_summary = summarize_signal_data(ipg_signal_data)
    battery_summary = summarize_battery_csv(battery_csv)

    resolved_gain_uv_per_count, gain_source = resolve_ipg_uv_scale(metadata, gain_uv_per_count)

    performance = analyze_performance(
        metadata=metadata,
        ni_signal_data=ni_signal_data,
        ipg_signal_data=ipg_signal_data,
        ni_summary=ni_summary,
        ipg_summary=ipg_summary,
        gain_uv_per_count=resolved_gain_uv_per_count,
        gain_source=gain_source,
    )

    visual_assets = generate_visualizations(
        report_dir=report_dir,
        ni_signal_data=ni_signal_data,
        ipg_signal_data=ipg_signal_data,
        battery_summary=battery_summary,
        performance=performance,
    )

    summary_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "ni_csv": str(ni_csv),
            "ipg_csv": str(ipg_csv),
            "battery_csv": str(battery_csv),
            "metadata_json": str(metadata_json),
        },
        "ni": ni_summary,
        "ipg": ipg_summary,
        "battery": battery_summary,
        "performance": performance,
        "visual_assets": visual_assets,
    }

    render_markdown_report(
        report_path=report_path,
        metadata=metadata,
        ni_summary=ni_summary,
        ipg_summary=ipg_summary,
        battery_summary=battery_summary,
        performance=performance,
        html_path=report_html_path,
        visual_assets=visual_assets,
    )

    render_html_report(
        html_path=report_html_path,
        metadata=metadata,
        ni_summary=ni_summary,
        ipg_summary=ipg_summary,
        battery_summary=battery_summary,
        performance=performance,
        visual_assets=visual_assets,
    )

    with summary_json_path.open("w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, indent=2)

    return {
        "report_dir": str(report_dir),
        "report_path": str(report_path),
        "report_html_path": str(report_html_path),
        "summary_json_path": str(summary_json_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate post-run analysis report from saved CSV/JSON artifacts.")
    parser.add_argument("--ni-csv", required=True)
    parser.add_argument("--ipg-csv", required=True)
    parser.add_argument("--battery-csv", required=True)
    parser.add_argument("--metadata-json", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--stem", required=True)
    parser.add_argument("--gain-uv-per-count", type=float, default=25.15)
    parser.add_argument("--skip-initial-s", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_comprehensive_report(
        ni_csv=Path(args.ni_csv),
        ipg_csv=Path(args.ipg_csv),
        battery_csv=Path(args.battery_csv),
        metadata_json=Path(args.metadata_json),
        output_root=Path(args.output_root),
        stem=str(args.stem),
        gain_uv_per_count=float(args.gain_uv_per_count),
        skip_initial_s=float(args.skip_initial_s),
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
