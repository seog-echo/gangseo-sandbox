from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
except Exception:
    np = None

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def read_ipg_csv(path: Path) -> Tuple[List[str], List[float], Dict[str, List[float]]]:
    header: List[str] = []
    time_values: List[float] = []
    channels: Dict[str, List[float]] = {}
    if not path.exists():
        return header, time_values, channels

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return [], [], {}

        for ch in header[1:]:
            channels[ch] = []

        for row in reader:
            if not row:
                continue
            try:
                t = float(row[0])
            except Exception:
                continue
            time_values.append(t)
            for i, ch in enumerate(header[1:], start=1):
                val = float("nan")
                if i < len(row):
                    text = str(row[i]).strip()
                    if text:
                        try:
                            val = float(text)
                        except Exception:
                            val = float("nan")
                channels[ch].append(val)

    return header, time_values, channels


def estimate_fs(time_values: List[float]) -> Optional[float]:
    if len(time_values) < 3:
        return None
    dts = np.diff(np.asarray(time_values, dtype=float)) if np is not None else [
        time_values[i + 1] - time_values[i] for i in range(len(time_values) - 1)
    ]
    if np is not None:
        dt = float(np.median(dts))
    else:
        sorted_dts = sorted(dts)
        dt = float(sorted_dts[len(sorted_dts) // 2])
    if dt <= 0:
        return None
    return 1.0 / dt


def trim_indices(time_values: List[float], trim_s: float) -> Tuple[int, int]:
    if not time_values:
        return 0, 0
    t0 = float(time_values[0])
    t1 = float(time_values[-1])
    start_t = t0 + max(0.0, trim_s)
    end_t = t1 - max(0.0, trim_s)
    if end_t <= start_t:
        return 0, len(time_values)
    start_idx = 0
    while start_idx < len(time_values) and time_values[start_idx] < start_t:
        start_idx += 1
    end_idx = len(time_values)
    while end_idx > start_idx and time_values[end_idx - 1] > end_t:
        end_idx -= 1
    return start_idx, end_idx


def sine_fit_metrics(
    t: List[float],
    y: List[float],
    f_hz: float,
) -> Dict[str, Optional[float]]:
    if np is None or len(t) < 32 or len(y) < 32 or not math.isfinite(f_hz) or f_hz <= 0:
        return {"amp": None, "rms": None, "noise_rms": None, "snr_db": None}

    ta = np.asarray(t, dtype=float)
    ya = np.asarray(y, dtype=float)
    valid = np.isfinite(ta) & np.isfinite(ya)
    ta = ta[valid]
    ya = ya[valid]
    if ta.size < 32:
        return {"amp": None, "rms": None, "noise_rms": None, "snr_db": None}

    ya = ya - float(np.mean(ya))
    w = 2.0 * math.pi * f_hz
    s = np.sin(w * ta)
    c = np.cos(w * ta)
    a = 2.0 * float(np.mean(ya * s))
    b = 2.0 * float(np.mean(ya * c))
    amp = math.sqrt(a * a + b * b)
    fit = a * s + b * c
    resid = ya - fit
    sig_rms = amp / math.sqrt(2.0)
    noise_rms = float(np.sqrt(np.mean(resid * resid)))
    snr_db = None
    if noise_rms > 0 and sig_rms > 0:
        snr_db = 20.0 * math.log10(sig_rms / noise_rms)
    return {"amp": amp, "rms": sig_rms, "noise_rms": noise_rms, "snr_db": snr_db}


def peak_to_peak_amplitude(y: List[float]) -> Dict[str, Optional[float]]:
    if np is None or len(y) < 4:
        return {"p2p": None, "amp": None}

    ya = np.asarray(y, dtype=float)
    ya = ya[np.isfinite(ya)]
    if ya.size < 4:
        return {"p2p": None, "amp": None}

    p2p = float(np.max(ya) - np.min(ya))
    return {"p2p": p2p, "amp": (p2p / 2.0)}


def estimate_peak_frequency_hz(t: List[float], y: List[float]) -> Optional[float]:
    if np is None or len(t) < 32 or len(y) < 32:
        return None

    fs = estimate_fs(t)
    if fs is None or fs <= 0:
        return None

    ta = np.asarray(t, dtype=float)
    ya = np.asarray(y, dtype=float)
    valid = np.isfinite(ta) & np.isfinite(ya)
    ya = ya[valid]
    if ya.size < 32:
        return None

    # Remove DC and apply a window before FFT peak search.
    ya = ya - float(np.mean(ya))
    window = np.hanning(ya.size)
    yw = ya * window
    spec = np.fft.rfft(yw)
    freqs = np.fft.rfftfreq(yw.size, d=1.0 / fs)
    if freqs.size < 2:
        return None

    mags = np.abs(spec)
    mags[0] = 0.0  # Ignore DC peak.
    peak_idx = int(np.argmax(mags))
    peak_hz = float(freqs[peak_idx])
    if not math.isfinite(peak_hz) or peak_hz <= 0:
        return None
    return peak_hz


def compute_cutoff_and_slope(points: List[Tuple[float, float]]) -> Dict[str, Optional[float]]:
    if len(points) < 3:
        return {"cutoff_hz": None, "slope_db_per_oct": None}

    pts = sorted(points, key=lambda x: x[0])
    low_gain = mean([g for _, g in pts[: min(3, len(pts))]])
    high_gain = mean([g for _, g in pts[-min(3, len(pts)) :]])

    is_hpf = high_gain > low_gain
    if is_hpf:
        target = high_gain - 3.0
        crossing = lambda g0, g1: (g0 <= target and g1 >= target) or (g0 >= target and g1 <= target)
    else:
        target = low_gain - 3.0
        crossing = lambda g0, g1: (g0 >= target and g1 <= target) or (g0 <= target and g1 >= target)

    cutoff = None
    for i in range(1, len(pts)):
        f0, g0 = pts[i - 1]
        f1, g1 = pts[i]
        if crossing(g0, g1):
            if abs(g1 - g0) < 1e-9:
                cutoff = f1
            else:
                ratio = (target - g0) / (g1 - g0)
                cutoff = f0 + ratio * (f1 - f0)
            break

    slope = None
    hi = pts[-min(3, len(pts)) :]
    if len(hi) >= 2 and np is not None:
        xs = np.asarray([math.log(max(f, 1e-9), 2.0) for f, _ in hi], dtype=float)
        ys = np.asarray([g for _, g in hi], dtype=float)
        A = np.vstack([xs, np.ones_like(xs)]).T
        m, _ = np.linalg.lstsq(A, ys, rcond=None)[0]
        slope = float(m)

    return {"cutoff_hz": cutoff, "slope_db_per_oct": slope}


def _interp_crossing(f0: float, g0: float, f1: float, g1: float, target_db: float) -> Optional[float]:
    if not (math.isfinite(f0) and math.isfinite(f1) and math.isfinite(g0) and math.isfinite(g1) and math.isfinite(target_db)):
        return None
    if f0 <= 0 or f1 <= 0:
        return None
    if abs(g1 - g0) < 1e-12:
        return float(f1)
    ratio = (target_db - g0) / (g1 - g0)
    ratio = max(0.0, min(1.0, ratio))
    # Interpolate in log-frequency domain for filter response estimates.
    lf0 = math.log10(f0)
    lf1 = math.log10(f1)
    lf = lf0 + ratio * (lf1 - lf0)
    return float(10.0**lf)


def _estimate_slope_db_per_oct(points: List[Tuple[float, float]]) -> Optional[float]:
    if len(points) < 2 or np is None:
        return None
    xs = np.asarray([math.log(max(f, 1e-9), 2.0) for f, _ in points], dtype=float)
    ys = np.asarray([g for _, g in points], dtype=float)
    A = np.vstack([xs, np.ones_like(xs)]).T
    m, _ = np.linalg.lstsq(A, ys, rcond=None)[0]
    return float(m)


def _confidence_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.6:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


def _score_corner_confidence(
    pts: List[Tuple[float, float]],
    target_gain_db: float,
    cutoff_hz: Optional[float],
    bracket: Optional[Tuple[float, float, float, float]],
) -> Dict[str, Any]:
    if cutoff_hz is None or bracket is None or cutoff_hz <= 0:
        return {
            "score": 0.0,
            "score_pct": 0.0,
            "label": "none",
            "support_points": 0,
            "bracket_octaves": None,
            "local_slope_db_per_oct": None,
        }

    f0, g0, f1, g1 = bracket
    if f0 <= 0 or f1 <= 0:
        return {
            "score": 0.0,
            "score_pct": 0.0,
            "label": "none",
            "support_points": 0,
            "bracket_octaves": None,
            "local_slope_db_per_oct": None,
        }

    bracket_octaves = abs(math.log(max(f1, 1e-9), 2.0) - math.log(max(f0, 1e-9), 2.0))
    if bracket_octaves <= 0.25:
        bracket_score = 1.0
    elif bracket_octaves <= 0.5:
        bracket_score = 0.8
    elif bracket_octaves <= 1.0:
        bracket_score = 0.6
    else:
        bracket_score = 0.3

    local_slope = abs((g1 - g0) / max(bracket_octaves, 1e-9))
    if local_slope >= 12.0:
        slope_score = 1.0
    elif local_slope >= 6.0:
        slope_score = 0.8
    elif local_slope >= 3.0:
        slope_score = 0.6
    else:
        slope_score = 0.35

    # Count nearby points around cutoff and close to transition level.
    nearby = [
        (f, g)
        for f, g in pts
        if f > 0
        and abs(math.log(max(f, 1e-9), 2.0) - math.log(max(cutoff_hz, 1e-9), 2.0)) <= 1.0
        and abs(g - target_gain_db) <= 4.0
    ]
    support_points = len(nearby)
    if support_points >= 4:
        support_score = 1.0
    elif support_points == 3:
        support_score = 0.8
    elif support_points == 2:
        support_score = 0.6
    elif support_points == 1:
        support_score = 0.4
    else:
        support_score = 0.2

    score = 0.4 * slope_score + 0.35 * bracket_score + 0.25 * support_score
    score = max(0.0, min(1.0, score))
    return {
        "score": score,
        "score_pct": score * 100.0,
        "label": _confidence_label(score),
        "support_points": support_points,
        "bracket_octaves": bracket_octaves,
        "local_slope_db_per_oct": local_slope,
    }


def estimate_response_characteristics(points: List[Tuple[float, float]]) -> Dict[str, Optional[float] | str]:
    if len(points) < 4:
        return {
            "response_type": "unknown",
            "reference_gain_db": None,
            "target_gain_db": None,
            "peak_gain_db": None,
            "peak_frequency_hz": None,
            "hpf_cutoff_hz": None,
            "lpf_cutoff_hz": None,
            "legacy_cutoff_hz": None,
            "slope_low_db_per_oct": None,
            "slope_high_db_per_oct": None,
            "hpf_confidence_score": 0.0,
            "hpf_confidence_label": "none",
            "lpf_confidence_score": 0.0,
            "lpf_confidence_label": "none",
        }

    pts = sorted([(float(f), float(g)) for f, g in points if f is not None and g is not None and f > 0], key=lambda x: x[0])
    if len(pts) < 4:
        return {
            "response_type": "unknown",
            "reference_gain_db": None,
            "target_gain_db": None,
            "peak_gain_db": None,
            "peak_frequency_hz": None,
            "hpf_cutoff_hz": None,
            "lpf_cutoff_hz": None,
            "legacy_cutoff_hz": None,
            "slope_low_db_per_oct": None,
            "slope_high_db_per_oct": None,
            "hpf_confidence_score": 0.0,
            "hpf_confidence_label": "none",
            "lpf_confidence_score": 0.0,
            "lpf_confidence_label": "none",
        }

    peak_idx = max(range(len(pts)), key=lambda i: pts[i][1])
    peak_freq_hz, peak_gain_db = pts[peak_idx]

    # Estimate passband reference from the neighborhood around the peak response
    # first, then gracefully fall back if the sweep has sparse/irregular points.
    gains_all = [g for _, g in pts]
    gains_within_1db = [g for g in gains_all if g >= (peak_gain_db - 1.0)]
    gains_within_2db = [g for g in gains_all if g >= (peak_gain_db - 2.0)]
    gains_within_3db = [g for g in gains_all if g >= (peak_gain_db - 3.0)]

    if len(gains_within_1db) >= 3:
        reference_gain_db = float(mean(gains_within_1db))
    elif len(gains_within_2db) >= 3:
        reference_gain_db = float(mean(gains_within_2db))
    elif len(gains_within_3db) >= 3:
        reference_gain_db = float(mean(gains_within_3db))
    else:
        gains_sorted = sorted(gains_all, reverse=True)
        top_n = max(3, min(len(gains_sorted), int(math.ceil(len(gains_sorted) * 0.25))))
        reference_gain_db = float(mean(gains_sorted[:top_n]))

    target_gain_db = reference_gain_db - 3.0

    # Left side crossing => HPF corner (rising into passband)
    hpf_cutoff_hz: Optional[float] = None
    hpf_bracket: Optional[Tuple[float, float, float, float]] = None
    for i in range(1, peak_idx + 1):
        f0, g0 = pts[i - 1]
        f1, g1 = pts[i]
        if (g0 <= target_gain_db <= g1) or (g1 <= target_gain_db <= g0):
            hpf_cutoff_hz = _interp_crossing(f0, g0, f1, g1, target_gain_db)
            hpf_bracket = (f0, g0, f1, g1)

    # Right side crossing => LPF corner (falling out of passband)
    lpf_cutoff_hz: Optional[float] = None
    lpf_bracket: Optional[Tuple[float, float, float, float]] = None
    for i in range(peak_idx + 1, len(pts)):
        f0, g0 = pts[i - 1]
        f1, g1 = pts[i]
        if (g0 >= target_gain_db >= g1) or (g1 >= target_gain_db >= g0):
            lpf_cutoff_hz = _interp_crossing(f0, g0, f1, g1, target_gain_db)
            lpf_bracket = (f0, g0, f1, g1)
            break

    low_slice = pts[: min(5, len(pts))]
    high_slice = pts[-min(5, len(pts)) :]
    slope_low = _estimate_slope_db_per_oct(low_slice)
    slope_high = _estimate_slope_db_per_oct(high_slice)

    response_type = "bandpass"
    if hpf_cutoff_hz is not None and lpf_cutoff_hz is None:
        response_type = "hpf"
    elif hpf_cutoff_hz is None and lpf_cutoff_hz is not None:
        response_type = "lpf"
    elif hpf_cutoff_hz is None and lpf_cutoff_hz is None:
        response_type = "flat_or_undetermined"

    legacy_cutoff_hz = hpf_cutoff_hz if hpf_cutoff_hz is not None else lpf_cutoff_hz
    hpf_conf = _score_corner_confidence(pts, target_gain_db, hpf_cutoff_hz, hpf_bracket)
    lpf_conf = _score_corner_confidence(pts, target_gain_db, lpf_cutoff_hz, lpf_bracket)
    return {
        "response_type": response_type,
        "reference_gain_db": reference_gain_db,
        "target_gain_db": target_gain_db,
        "peak_gain_db": peak_gain_db,
        "peak_frequency_hz": peak_freq_hz,
        "hpf_cutoff_hz": hpf_cutoff_hz,
        "lpf_cutoff_hz": lpf_cutoff_hz,
        "legacy_cutoff_hz": legacy_cutoff_hz,
        "slope_low_db_per_oct": slope_low,
        "slope_high_db_per_oct": slope_high,
        "hpf_confidence_score": hpf_conf["score_pct"],
        "hpf_confidence_label": hpf_conf["label"],
        "lpf_confidence_score": lpf_conf["score_pct"],
        "lpf_confidence_label": lpf_conf["label"],
    }


def render_block_plot(
    out_path: Path,
    time_values: List[float],
    channels: Dict[str, List[float]],
    trim_start: int,
    trim_end: int,
    title: str,
) -> None:
    if plt is None or not time_values:
        return
    fig, axes = plt.subplots(len(channels), 1, figsize=(10, max(4, 2.2 * len(channels))), sharex=True)
    if len(channels) == 1:
        axes = [axes]

    t = np.asarray(time_values, dtype=float)
    for ax, (ch, values) in zip(axes, channels.items()):
        y = np.asarray(values, dtype=float)
        ax.plot(t, y, linewidth=0.8, alpha=0.6, label=f"{ch} raw")
        if trim_end > trim_start:
            ax.plot(t[trim_start:trim_end], y[trim_start:trim_end], linewidth=1.2, label=f"{ch} analyzed")
        n = len(t)
        if n > 10:
            mid0 = int(n * 0.45)
            mid1 = min(n, mid0 + max(1, int(n * 0.1)))
            late0 = int(n * 0.8)
            late1 = min(n, late0 + max(1, int(n * 0.1)))
            ax.axvspan(float(t[mid0]), float(t[mid1 - 1]), color="#a7f3d0", alpha=0.15)
            ax.axvspan(float(t[late0]), float(t[late1 - 1]), color="#bfdbfe", alpha=0.15)
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper right", fontsize=8)

    axes[0].set_title(title)
    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def render_gain_plot(
    out_path: Path,
    channel_points: Dict[str, List[Tuple[float, float]]],
    response_summary: Dict[str, Dict[str, Any]],
    title: str,
) -> None:
    if plt is None:
        return
    fig = plt.figure(figsize=(8, 5))
    plotted = False
    for ch, pts in channel_points.items():
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda x: x[0])
        f = [x for x, _ in pts_sorted]
        g = [y for _, y in pts_sorted]
        plt.semilogx(f, g, marker="o", linewidth=1.2, label=ch)

        stats = response_summary.get(ch, {}) if isinstance(response_summary, dict) else {}
        hpf_cut = stats.get("hpf_cutoff_hz")
        lpf_cut = stats.get("lpf_cutoff_hz")
        target = stats.get("target_gain_db")
        if hpf_cut is not None and target is not None:
            plt.axvline(float(hpf_cut), linestyle="--", linewidth=0.9, alpha=0.35)
            plt.plot([float(hpf_cut)], [float(target)], marker="x", markersize=7)
        if lpf_cut is not None and target is not None:
            plt.axvline(float(lpf_cut), linestyle="--", linewidth=0.9, alpha=0.35)
            plt.plot([float(lpf_cut)], [float(target)], marker="x", markersize=7)
        plotted = True
    plt.grid(True, which="both", alpha=0.3)
    plt.xlabel("Frequency (Hz)")
    plt.ylabel("Gain (dB)")
    plt.title(title)
    if plotted:
        plt.legend()
    plt.tight_layout()
    plt.savefig(out_path)
    plt.close(fig)


def render_linearity_plot(out_path: Path, channel_points: Dict[str, List[Tuple[float, float]]], title: str) -> None:
    if plt is None:
        return
    fig = plt.figure(figsize=(8, 5))
    plotted = False
    for ch, pts in channel_points.items():
        if not pts:
            continue
        pts_sorted = sorted(pts, key=lambda x: x[0])
        x = [p[0] for p in pts_sorted]
        y = [p[1] for p in pts_sorted]
        plt.plot(x, y, marker="o", linewidth=1.2, label=ch)
        plotted = True
    plt.grid(True, alpha=0.3)
    plt.xlabel("Effective Input Amplitude (uV)")
    plt.ylabel("Output Amplitude (uV)")
    plt.title(title)
    if plotted:
        plt.legend()
    plt.tight_layout()
    plt.savefig(out_path)
    plt.close(fig)


def _fmt(value: Optional[float], digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def metadata_has_uv_samples(metadata: Dict[str, Any]) -> bool:
    if not isinstance(metadata, dict):
        return False
    rec = metadata.get("ipg", {}).get("recording", {})
    if not isinstance(rec, dict):
        return False
    unit = str(rec.get("samples_unit", "")).strip().lower()
    converted = bool(rec.get("samples_converted_to_uV", False))
    return converted or unit in {"uv", "microvolt", "microvolts"}


def recover_block_drive_values(auto_block: Dict[str, Any], metadata: Dict[str, Any], auto_cfg: Dict[str, Any]) -> Tuple[float, float]:
    """Recover block drive values from AO map when representative metadata values are stale/zero."""
    phase = str(auto_block.get("phase", "")).strip().lower()
    ao_values_raw = auto_block.get("ao_values", {})
    phase_index = int(_safe_float(auto_block.get("phase_index", 0), 0.0))

    # Fallback: recover AO values from embedded config snapshot in run metadata.
    if (not isinstance(ao_values_raw, dict) or not ao_values_raw) and phase_index > 0:
        exec_cfg = metadata.get("execution", {}) if isinstance(metadata, dict) else {}
        auto_cfg_snapshot = exec_cfg.get("auto_ipg_testing", {}) if isinstance(exec_cfg, dict) else {}
        if isinstance(auto_cfg_snapshot, dict):
            if phase == "frequency":
                blocks = auto_cfg_snapshot.get("frequency_sweep", {}).get("blocks", [])
            elif phase == "amplitude":
                blocks = auto_cfg_snapshot.get("amplitude_sweep", {}).get("blocks", [])
            else:
                blocks = []
            if isinstance(blocks, list) and 1 <= phase_index <= len(blocks):
                candidate = blocks[phase_index - 1]
                if isinstance(candidate, dict):
                    ao_values_raw = candidate.get("ao_values", {})
    ao_values: Dict[str, float] = {}
    if isinstance(ao_values_raw, dict):
        ao_values = {str(k): _safe_float(v, 0.0) for k, v in ao_values_raw.items()}

    mapping_cfg = auto_cfg.get("mapping", {}) if isinstance(auto_cfg, dict) else {}
    mapped_ao_keys: List[str] = []
    if isinstance(mapping_cfg, dict):
        for ao_key, ipg_targets in mapping_cfg.items():
            if isinstance(ipg_targets, list) and len(ipg_targets) > 0:
                mapped_ao_keys.append(str(ao_key))

    # Prefer mapped AO channels to recover the effective sweep drive.
    mapped_drive_values = [abs(ao_values.get(key, 0.0)) for key in mapped_ao_keys if abs(ao_values.get(key, 0.0)) > 0.0]
    any_drive_values = [abs(v) for v in ao_values.values() if abs(v) > 0.0]
    recovered_drive = mapped_drive_values[0] if mapped_drive_values else (any_drive_values[0] if any_drive_values else 0.0)

    freq_hz = _safe_float(auto_block.get("frequency_hz", 0.0), 0.0)
    ao_amp_v = _safe_float(auto_block.get("amplitude_v", 0.0), 0.0)

    if phase == "frequency":
        if freq_hz <= 0.0 and recovered_drive > 0.0:
            freq_hz = recovered_drive
        if ao_amp_v <= 0.0:
            ao_amp_v = _safe_float(auto_cfg.get("frequency_sweep", {}).get("fixed_amplitude_v", 0.0), 0.0)
    elif phase == "amplitude":
        if ao_amp_v <= 0.0 and recovered_drive > 0.0:
            ao_amp_v = recovered_drive
        if freq_hz <= 0.0:
            freq_hz = _safe_float(auto_cfg.get("amplitude_sweep", {}).get("fixed_frequency_hz", 0.0), 0.0)

    return freq_hz, ao_amp_v


def build_ni_ai_reference_lookup(auto_cfg: Dict[str, Any]) -> Dict[int, int]:
    reference_cfg = auto_cfg.get("ni_ai_reference", {}) if isinstance(auto_cfg, dict) else {}
    if not isinstance(reference_cfg, dict) or not bool(reference_cfg.get("enabled", False)):
        return {}

    lookup: Dict[int, int] = {}
    for entry in reference_cfg.get("mapping", []):
        if not isinstance(entry, dict):
            continue
        try:
            ni_ai_channel = int(entry.get("ni_ai_channel", 0))
        except Exception:
            continue
        if ni_ai_channel <= 0:
            continue
        for value in entry.get("ipg_rec_channels", []):
            try:
                ipg_channel = int(value)
            except Exception:
                continue
            if ipg_channel > 0 and ipg_channel not in lookup:
                lookup[ipg_channel] = ni_ai_channel
    return lookup


def write_html_report(path: Path, payload: Dict[str, Any]) -> None:
    assumptions = payload["assumptions"]
    input_reference_mode = str(payload.get("input_reference_mode", "ao_divider"))
    freq_response = payload.get("frequency_response", {})
    blocks = payload.get("blocks", [])
    generated = payload.get("generated_files", [])
    gain_plot_name = str(payload.get("gain_plot_name", "gain_vs_frequency.png"))
    inline_raw_plots = bool(payload.get("inline_raw_plots", False))

    channel_rows = []
    for ch, stats in freq_response.items():
        channel_rows.append(
            "<tr>"
            f"<td>{ch}</td>"
            f"<td>{str(stats.get('response_type', 'unknown'))}</td>"
            f"<td>{_fmt(stats.get('reference_gain_db'))}</td>"
            f"<td>{_fmt(stats.get('target_gain_db'))}</td>"
            f"<td>{_fmt(stats.get('hpf_cutoff_hz'))}</td>"
            f"<td>{_fmt(stats.get('hpf_confidence_score'), 1)} ({str(stats.get('hpf_confidence_label', 'none'))})</td>"
            f"<td>{_fmt(stats.get('lpf_cutoff_hz'))}</td>"
            f"<td>{_fmt(stats.get('lpf_confidence_score'), 1)} ({str(stats.get('lpf_confidence_label', 'none'))})</td>"
            f"<td>{_fmt(stats.get('peak_frequency_hz'))}</td>"
            f"<td>{_fmt(stats.get('peak_gain_db'))}</td>"
            "</tr>"
        )

    block_rows = []
    block_detail_sections: List[str] = []
    for block in blocks:
        block_rows.append(
            "<tr>"
            f"<td>{block['block_index']}</td>"
            f"<td>{block['phase']}</td>"
            f"<td>{_fmt(float(block.get('frequency_hz', 0.0)))}</td>"
            f"<td>{_fmt(float(block.get('ao_amplitude_v', 0.0)))}</td>"
            f"<td>{block.get('partial', False)}</td>"
            f"<td>{block.get('quality', {}).get('status', 'n/a')}</td>"
            f"<td>{_fmt(block.get('fs_est_hz'))}</td>"
            "</tr>"
        )

        channel_detail_rows: List[str] = []
        for ch_name, metrics in block.get("channels", {}).items():
            channel_detail_rows.append(
                "<tr>"
                f"<td>{ch_name}</td>"
                f"<td>{str(metrics.get('input_source', 'ao_divider'))}</td>"
                f"<td>{_fmt(metrics.get('reference_channel'))}</td>"
                f"<td>{_fmt(metrics.get('input_uv_effective'))}</td>"
                f"<td>{_fmt(metrics.get('input_p2p_uv'))}</td>"
                f"<td>{_fmt(metrics.get('reference_peak_frequency_hz'))}</td>"
                f"<td>{_fmt(metrics.get('peak_frequency_hz'))}</td>"
                f"<td>{_fmt(metrics.get('output_amp_uv'))}</td>"
                f"<td>{_fmt(metrics.get('output_p2p_uv'))}</td>"
                f"<td>{_fmt(metrics.get('gain_db'))}</td>"
                f"<td>{_fmt(metrics.get('snr_db'))}</td>"
                f"<td>{_fmt(metrics.get('noise_rms'))}</td>"
                "</tr>"
            )

        block_detail_sections.append(
            "<h3>Block {idx} Detail</h3>"
            "<table>"
            "<thead><tr><th>Channel</th><th>Input Source</th><th>Ref Ch</th><th>Input Amp (uV, peak)</th><th>Input P-P (uV)</th><th>Ref Peak Freq (Hz)</th><th>Peak Frequency (Hz)</th><th>Output Amp (uV, peak)</th><th>Output P-P (uV)</th><th>Gain (dB)</th><th>SNR (dB)</th><th>Noise RMS (uV)</th></tr></thead>"
            "<tbody>{rows}</tbody>"
            "</table>".format(idx=block.get("block_index"), rows="".join(channel_detail_rows))
        )

    image_tags = []
    raw_plot_links: List[str] = []
    for file_path in generated:
        p = Path(file_path)
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
            if p.name == gain_plot_name:
                image_tags.insert(0, f"<figure><img src=\"{p.name}\" style=\"max-width:100%;height:auto;\"><figcaption>{p.name} (includes estimated -3 dB corner markers)</figcaption></figure>")
            elif p.name.startswith("block_"):
                raw_plot_links.append(f"<li><a href=\"{p.name}\">{p.name}</a></li>")
                if inline_raw_plots:
                    image_tags.append(f"<figure><img src=\"{p.name}\" style=\"max-width:100%;height:auto;\"><figcaption>{p.name}</figcaption></figure>")
            else:
                image_tags.append(f"<figure><img src=\"{p.name}\" style=\"max-width:100%;height:auto;\"><figcaption>{p.name}</figcaption></figure>")

    html = f"""
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <title>Auto IPG Test Report</title>
    <style>
        body {{ font-family: Segoe UI, Arial, sans-serif; margin: 20px; color: #1f2937; }}
        h1, h2, h3 {{ color: #0f172a; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
        th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; font-size: 13px; }}
        th {{ background: #f3f4f6; }}
        .muted {{ color: #6b7280; }}
        figure {{ margin: 18px 0; }}
        figcaption {{ font-size: 12px; color: #4b5563; margin-top: 6px; }}
    </style>
</head>
<body>
    <h1>Auto IPG Test Report</h1>

    <h2>Primary Result: Bandpass Gain vs Frequency</h2>
    <p>
        Gain baseline is estimated from the passband (top-gain points), then -3 dB targets are used to estimate both
        HPF and LPF corner frequencies when crossings exist.
    </p>
    <p class="muted">
        Input reference mode: {'NI AI reference' if input_reference_mode == 'ni_ai' else 'AO divider fallback'}.
    </p>
    <p class="muted">
        Confidence score (0-100) indicates robustness of each corner estimate based on local transition slope,
        crossing bracket width, and nearby support points around the -3 dB target.
    </p>
    {''.join(image_tags[:1])}

    <h2>Bandpass Corner Summary</h2>
    <table>
        <thead>
            <tr>
                <th>Channel</th>
                <th>Response Type</th>
                <th>Passband Ref Gain (dB)</th>
                <th>-3 dB Target (dB)</th>
                <th>Estimated HPF -3 dB (Hz)</th>
                <th>HPF Confidence</th>
                <th>Estimated LPF -3 dB (Hz)</th>
                <th>LPF Confidence</th>
                <th>Peak Frequency (Hz)</th>
                <th>Peak Gain (dB)</th>
            </tr>
        </thead>
        <tbody>{''.join(channel_rows)}</tbody>
    </table>

    <h2>Assumptions</h2>
    <ul>
        <li>ADC conversion: {assumptions['ipg_adc_lsb_uV']} uV/count</li>
        <li>AO voltage divider ratio: 1/{int(assumptions['voltage_divider_ratio'])}</li>
        <li>Edge trim for analysis: {assumptions['trim_edge_seconds']} s at beginning and end</li>
    </ul>

    <h2>Session Summary</h2>
    <ul>
        <li>Session directory: {payload['session_dir']}</li>
        <li>Total blocks analyzed: {payload['total_blocks']}</li>
        <li>Completed blocks: {payload['completed_blocks']}</li>
        <li>Stopped early: {payload['stopped']}</li>
    </ul>

    <h2>Block Details</h2>
    <table>
        <thead><tr><th>Block</th><th>Mode</th><th>Frequency (Hz)</th><th>AO Amplitude (V)</th><th>Partial</th><th>Quality</th><th>fs est (Hz)</th></tr></thead>
        <tbody>{''.join(block_rows)}</tbody>
    </table>
    {''.join(block_detail_sections)}

    <h2>Additional Figures</h2>
    {''.join(image_tags[1:])}

    <h2>Raw Block Time-Series Plots</h2>
    <p class="muted">
        Raw per-block plots are generated and saved in the report folder.
        They are listed below instead of being fully embedded to keep this report concise.
    </p>
    <ul>{''.join(raw_plot_links)}</ul>

    <h2>Generated Files</h2>
    <ul>{''.join(f'<li>{Path(p).name}</li>' for p in generated)}</ul>
    <p class=\"muted\">Generated automatically by auto_test_session_analyzer.py</p>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze auto IPG test session output")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--summary-json", required=True)
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    summary_path = Path(args.summary_json)
    report_dir = session_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    session_summary = load_json(summary_path)
    auto_cfg = load_json(report_dir / "auto_test_config.json")
    analysis_cfg = auto_cfg.get("analysis") if isinstance(auto_cfg, dict) else None
    if isinstance(analysis_cfg, dict):
        # Remove deprecated gain-based assumption and keep ADC-based conversion settings.
        analysis_cfg.pop("ipg_gain_uS", None)
        analysis_cfg.setdefault("ipg_adc_lsb_uV", 0.6)
    if isinstance(auto_cfg, dict):
        (report_dir / "auto_test_config.json").write_text(
            json.dumps(auto_cfg, indent=2),
            encoding="utf-8",
        )
    assumptions = {
        "voltage_divider_ratio": float(auto_cfg.get("analysis", {}).get("voltage_divider_ratio", 1000.0)),
        "trim_edge_seconds": float(auto_cfg.get("analysis", {}).get("trim_edge_seconds", 2.0)),
        "ipg_adc_lsb_uV": float(auto_cfg.get("analysis", {}).get("ipg_adc_lsb_uV", 0.6)),
    }
    ni_ai_reference_map = build_ni_ai_reference_lookup(auto_cfg)
    ni_ai_reference_enabled = bool(auto_cfg.get("ni_ai_reference", {}).get("enabled", False))
    
    if ni_ai_reference_enabled:
        print(f"[Analyzer] NI AI reference enabled with {len(ni_ai_reference_map)} IPG->NI AI channel mappings: {ni_ai_reference_map}")
    else:
        print("[Analyzer] NI AI reference disabled - using AO divider fallback for input reference")

    blocks_out: List[Dict[str, Any]] = []
    channel_freq_points: Dict[str, List[Tuple[float, float]]] = {}
    channel_amp_points: Dict[str, List[Tuple[float, float]]] = {}
    generated_files: List[str] = []

    artifacts = session_summary.get("artifacts", []) if isinstance(session_summary, dict) else []
    for idx, artifact in enumerate(artifacts, start=1):
        ipg_path = Path(str(artifact.get("ipg_path", "")))
        metadata_path = Path(str(artifact.get("metadata_path", "")))
        metadata = load_json(metadata_path)
        auto_block = metadata.get("execution", {}).get("auto_block", {})
        freq_hz, ao_amp_v = recover_block_drive_values(auto_block, metadata, auto_cfg)
        partial = bool(auto_block.get("partial", False))
        phase = str(auto_block.get("phase", ""))
        samples_already_uv = metadata_has_uv_samples(metadata)

        header, time_values, channels = read_ipg_csv(ipg_path)
        if not samples_already_uv:
            scale_uv = assumptions["ipg_adc_lsb_uV"]
            for ch_name, samples in channels.items():
                channels[ch_name] = [
                    (float(sample) * scale_uv) if (isinstance(sample, float) and math.isfinite(sample)) else sample
                    for sample in samples
                ]

        ni_time_values: List[float] = []
        ni_channels: Dict[str, List[float]] = {}
        if ni_ai_reference_enabled:
            ni_path = Path(str(artifact.get("ni_path", "")))
            if not ni_path.exists():
                print(f"[Analyzer] WARNING: NI AI reference enabled but CSV not found: {ni_path}")
            else:
                _, ni_time_values, ni_channels = read_ipg_csv(ni_path)
                if ni_time_values and ni_channels:
                    print(f"[Analyzer] Run {idx}: Loaded NI AI reference from {ni_path.name} with {len(ni_channels)} channels and {len(ni_time_values)} samples")
                    for ch_name, samples in ni_channels.items():
                        ni_channels[ch_name] = [
                            (float(sample) * 1_000_000.0) if (isinstance(sample, float) and math.isfinite(sample)) else sample
                            for sample in samples
                        ]
                else:
                    print(f"[Analyzer] WARNING: NI AI CSV found but no channels/time data: time_vals={len(ni_time_values)}, channels={len(ni_channels)}")
        start_idx, end_idx = trim_indices(time_values, assumptions["trim_edge_seconds"])
        fs = estimate_fs(time_values)
        ni_start_idx, ni_end_idx = trim_indices(ni_time_values, assumptions["trim_edge_seconds"]) if ni_time_values else (0, 0)

        quality = {"status": "ok", "nan_fraction": {}, "has_empty_channel": False}
        block_channels: Dict[str, Any] = {}
        for ch_name, samples in channels.items():
            total = len(samples)
            nan_count = sum(1 for x in samples if not (isinstance(x, float) and math.isfinite(x)))
            frac = (nan_count / total) if total else 1.0
            quality["nan_fraction"][ch_name] = frac
            if total == 0 or all(not (isinstance(x, float) and math.isfinite(x)) for x in samples):
                quality["has_empty_channel"] = True

            seg_t = time_values[start_idx:end_idx]
            seg_y = samples[start_idx:end_idx]
            peak_hz = estimate_peak_frequency_hz(seg_t, seg_y)
            p2p_metrics = peak_to_peak_amplitude(seg_y)
            output_p2p_uv = p2p_metrics["p2p"]
            output_amp_uv = p2p_metrics["amp"]
            reference_channel = None
            input_source = "ao_divider"
            reference_peak_hz = None
            reference_fit = {"amp": None, "rms": None, "noise_rms": None, "snr_db": None}
            input_uv = None
            input_p2p_uv = None
            if ni_ai_reference_enabled:
                try:
                    ipg_channel_number = int(ch_name.split("_")[-1])
                except Exception:
                    ipg_channel_number = None
                if ipg_channel_number is not None:
                    reference_channel = ni_ai_reference_map.get(ipg_channel_number)
                if reference_channel is not None:
                    reference_trace = ni_channels.get(f"Channel_{reference_channel - 1}")
                    if reference_trace and ni_time_values:
                        reference_t = ni_time_values[ni_start_idx:ni_end_idx]
                        reference_y = reference_trace[ni_start_idx:ni_end_idx]
                        input_source = "ni_ai"
                        reference_peak_hz = estimate_peak_frequency_hz(reference_t, reference_y)
                        reference_fit_freq = (
                            reference_peak_hz if (reference_peak_hz is not None and reference_peak_hz > 0) else freq_hz
                        )
                        reference_fit = sine_fit_metrics(reference_t, reference_y, reference_fit_freq)
                        reference_p2p = peak_to_peak_amplitude(reference_y)
                        input_uv = reference_p2p["amp"]
                        input_p2p_uv = reference_p2p["p2p"]
                        input_source = "ni_ai"
            if input_uv is None:
                input_v = ao_amp_v / assumptions["voltage_divider_ratio"] if assumptions["voltage_divider_ratio"] > 0 else None
                input_uv = (input_v * 1e6) if input_v is not None else None
                input_p2p_uv = (input_uv * 2.0) if input_uv is not None else None

            fit_freq = reference_peak_hz if (reference_peak_hz is not None and reference_peak_hz > 0) else (peak_hz if (peak_hz is not None and peak_hz > 0) else freq_hz)
            fit = sine_fit_metrics(seg_t, seg_y, fit_freq)
            gain_db = None
            if output_amp_uv is not None and input_uv is not None and output_amp_uv > 0 and input_uv > 0:
                gain_db = 20.0 * math.log10(output_amp_uv / input_uv)

            block_channels[ch_name] = {
                "input_source": input_source,
                # Report reference channel in zero-based indexing to match NI CSV columns
                # and the main GUI NI AI channel display.
                "reference_channel": (reference_channel - 1) if reference_channel is not None else None,
                "input_uv_effective": input_uv,
                "input_p2p_uv": input_p2p_uv,
                "reference_peak_frequency_hz": reference_peak_hz,
                "reference_output_amp_uv": reference_fit["amp"],
                "reference_output_p2p_uv": None if input_p2p_uv is None else input_p2p_uv,
                "reference_noise_rms": reference_fit["noise_rms"],
                "reference_snr_db": reference_fit["snr_db"],
                "output_amp_uv": output_amp_uv,
                "output_p2p_uv": output_p2p_uv,
                "output_amp_sinefit_uv": fit["amp"],
                "peak_frequency_hz": peak_hz,
                "gain_db": gain_db,
                "snr_db": fit["snr_db"],
                "noise_rms": fit["noise_rms"],
            }

            if gain_db is not None:
                channel_freq_points.setdefault(ch_name, []).append((freq_hz, gain_db))
            if phase == "amplitude" and input_uv is not None and output_amp_uv is not None:
                channel_amp_points.setdefault(ch_name, []).append((input_uv, output_amp_uv))

        if quality["has_empty_channel"] or any(v > 0.1 for v in quality["nan_fraction"].values()):
            quality["status"] = "warning"

        block_plot = report_dir / f"block_{idx:03d}_timeseries.png"
        render_block_plot(
            block_plot,
            time_values,
            channels,
            start_idx,
            end_idx,
            title=f"Block {idx}: {phase}  f={freq_hz} Hz  AO={ao_amp_v} V",
        )
        if block_plot.exists():
            generated_files.append(str(block_plot))

        blocks_out.append(
            {
                "block_index": idx,
                "phase": phase,
                "frequency_hz": freq_hz,
                "ao_amplitude_v": ao_amp_v,
                "partial": partial,
                "fs_est_hz": fs,
                "row_count": len(time_values),
                "trimmed_row_count": max(0, end_idx - start_idx),
                "quality": quality,
                "channels": block_channels,
                "ipg_csv": str(ipg_path),
                "metadata_json": str(metadata_path),
            }
        )

    freq_response: Dict[str, Any] = {}
    for ch_name, points in channel_freq_points.items():
        stats = estimate_response_characteristics(points)
        # Preserve legacy fields for downstream compatibility.
        stats["cutoff_hz"] = stats.get("legacy_cutoff_hz")
        stats["slope_db_per_oct"] = stats.get("slope_high_db_per_oct")
        freq_response[ch_name] = stats

    gain_plot = report_dir / "gain_vs_frequency.png"
    render_gain_plot(gain_plot, channel_freq_points, freq_response, "Bandpass Gain vs Frequency (-3 dB corners)")
    if gain_plot.exists():
        generated_files.append(str(gain_plot))

    linearity_plot = report_dir / "output_vs_input_amplitude.png"
    render_linearity_plot(linearity_plot, channel_amp_points, "Auto Test Output vs Input (Amplitude Sweep)")
    if linearity_plot.exists():
        generated_files.append(str(linearity_plot))

    payload: Dict[str, Any] = {
        "session_dir": str(session_dir),
        "stopped": bool(session_summary.get("stopped", False)),
        "total_blocks": int(session_summary.get("total_blocks", len(blocks_out))),
        "completed_blocks": int(session_summary.get("completed_blocks", len(blocks_out))),
        "assumptions": assumptions,
        "input_reference_mode": "ni_ai" if ni_ai_reference_enabled else "ao_divider",
        "frequency_response": freq_response,
        "blocks": blocks_out,
        "generated_files": generated_files,
        "gain_plot_name": gain_plot.name,
        "inline_raw_plots": False,
    }

    summary_out = report_dir / "auto_test_analysis_summary.json"
    save_json(summary_out, payload)
    generated_files.append(str(summary_out))

    report_html = report_dir / "auto_test_report.html"
    write_html_report(report_html, payload)
    generated_files.append(str(report_html))

    save_json(summary_out, payload)

    print(
        json.dumps(
            {
                "report_path": str(report_html),
                "summary_path": str(summary_out),
                "report_dir": str(report_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
