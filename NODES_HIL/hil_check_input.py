#!/usr/bin/env python3
"""Standalone NI-9222 input sanity check (read-only; drives nothing).

Reads the analog input the HIL loop uses for the "mock stimulation" and prints
what it measures, so you can confirm the signal generator -> NI-9222 chain works
before blaming the simulator. Run with the generator ON:

    python hil_check_input.py            # auto-detect NI-9222, channel ai0
    python hil_check_input.py --channel 1 --seconds 2

Interpreting the output:
    * amplitude_pk ~= half of peak-to-peak. The HIL mapping is 1.0 mA per volt
      PEAK (4 V pk -> 4 mA). So if you want a few mA of stim, you need a few volts
      PEAK here -> several volts peak-to-peak on the generator.
    * If amplitude_pk is ~0 while the generator is on, the input is not reaching
      ai0: check the channel number, differential wiring (signal->aiX+,
      ground->aiX-), and that the generator output is enabled.
    * Remember the Hi-Z gotcha: a generator set to a 50 ohm load but driving the
      9222's high-impedance input outputs ~2x its displayed amplitude.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from signal_metrics import measure_signal

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
except Exception:
    nidaqmx = None


def find_ai_device() -> str:
    system = nidaqmx.system.System.local()
    for device in system.devices:
        if "9222" in getattr(device, "product_type", ""):
            return getattr(device, "name", "")
    # Fall back to the first device that has AI channels.
    for device in system.devices:
        if list(getattr(device, "ai_physical_chans", [])):
            return getattr(device, "name", "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only NI-9222 input check for the HIL loop.")
    ap.add_argument("--channel", type=int, default=0, help="AI channel index (default 0)")
    ap.add_argument("--rate", type=float, default=250000.0,
                    help="Sample rate Hz (default 250000; raise toward the 9222's 500000 max "
                         "for finer pulse-width resolution)")
    ap.add_argument("--seconds", type=float, default=1.0, help="Capture duration (default 1.0)")
    ap.add_argument("--device", type=str, default="", help="Force device name (e.g. cDAQ1Mod3)")
    args = ap.parse_args()

    if nidaqmx is None:
        print("nidaqmx not installed - cannot read hardware. Install requirements into the venv.")
        return 2

    device = args.device or find_ai_device()
    if not device:
        print("No NI-9222 (or AI-capable device) found. Check NI-MAX and cabling.")
        return 2

    phys = f"{device}/ai{args.channel}"
    n = int(args.rate * args.seconds)
    print(f"Reading {phys} at {args.rate:.0f} Hz for {args.seconds:.1f} s (differential)...")

    task = nidaqmx.Task()
    try:
        kwargs = dict(min_val=-10.0, max_val=10.0)
        term = getattr(TerminalConfiguration, "DIFFERENTIAL", None)
        if term is not None:
            kwargs["terminal_config"] = term
        task.ai_channels.add_ai_voltage_chan(phys, **kwargs)
        task.timing.cfg_samp_clk_timing(rate=args.rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=n)
        data = task.read(number_of_samples_per_channel=n, timeout=args.seconds + 5.0)
    except Exception as exc:
        print(f"Read failed: {exc}")
        return 1
    finally:
        try:
            task.close()
        except Exception:
            pass

    x = np.asarray(data, dtype=np.float64).ravel()
    meas = measure_signal(x, args.rate)
    vpp = float(np.max(x) - np.min(x)) if x.size else 0.0
    print("-" * 56)
    print(f"  samples         : {x.size}")
    print(f"  min / max       : {np.min(x):+.4f} / {np.max(x):+.4f} V")
    print(f"  peak-to-peak    : {vpp:.4f} V")
    print(f"  amplitude_pk    : {meas.amplitude_v:.4f} V   (-> {meas.amplitude_v:.3f} mA stim at 1 mA/V)")
    if meas.is_pulsatile:
        print(f"  signal type     : pulsatile (narrow-pulse train)")
        print(f"  repetition rate : {meas.frequency_hz:.2f} Hz")
        print(f"  pulse width     : {meas.pulse_width_s * 1e6:.1f} us (per phase, at half-height)")
    else:
        print(f"  signal type     : continuous")
        print(f"  dominant freq   : {meas.frequency_hz:.2f} Hz")
    print(f"  RMS             : {meas.rms_v:.4f} V")
    print("-" * 56)
    if meas.is_pulsatile and meas.pulse_width_s * args.rate < 5.0:
        print("  NOTE: pulse spans <5 samples at this rate; raise --rate for a reliable")
        print("        width/peak (e.g. --rate 500000 -> 2 us/sample).")
    if meas.amplitude_v < 0.02:
        print("  WARNING: amplitude is below the stim deadband (0.02 V).")
        print("  NODES would receive ~0 mA -> no beta suppression. Check wiring/channel/output.")
    else:
        print("  Input looks live. This amplitude WILL drive stim in the HIL loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
