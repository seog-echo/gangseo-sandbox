#!/usr/bin/env python3
"""Tiny reader for NODES Parquet recordings.

Usage:
    python load_recording.py [path.parquet]

With no argument it loads the most recent file in ./recordings/.
Needs only numpy + pyarrow (both in requirements.txt); pandas is optional.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pyarrow.parquet as pq


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        found = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "recordings", "*.parquet")))
        if not found:
            sys.exit("No recordings found — pass a .parquet path explicitly.")
        path = found[-1]

    pf = pq.ParquetFile(path)
    meta = {k.decode(): v.decode() for k, v in (pf.schema_arrow.metadata or {}).items()}
    fs = float(meta.get("fs_hz", "nan"))
    channels = meta.get("channels", "").split(",")

    table = pf.read()  # whole file -> Arrow Table
    n = table.num_rows
    print(f"file      : {path}")
    print(f"samples   : {n}  ({n / fs:.2f} s @ {fs:.0f} Hz)")
    print(f"units     : {meta.get('units')}   start: {meta.get('start_time')}")
    print(f"channels  : {len(channels)}  e.g. {channels[:3]} ...")

    # Time vector and the 32-channel matrix (samples x channels), float32 µV.
    t = table.column("time").to_numpy()
    data = np.column_stack([table.column(ch).to_numpy() for ch in channels])
    print(f"time      : {t[0]:.3f} .. {t[-1]:.3f} s")
    print(f"data      : shape {data.shape}, dtype {data.dtype}")

    # Per-sample context (stim + behavioral state travel with every sample).
    states = table.column("state").to_pylist()
    print(f"states    : {sorted(set(states))}")
    print(
        "L stim end: on={} {:.2f} mA @ {:.0f} Hz".format(
            table.column("stim_left_on")[-1].as_py(),
            table.column("stim_left_ma")[-1].as_py(),
            table.column("stim_left_hz")[-1].as_py(),
        )
    )

    ch = channels[0]
    x = data[:, 0]
    print(f"{ch}: mean={x.mean():.2f} µV  rms={x.std():.2f} µV")

    # Optional one-liner if you have pandas:  df = pq.read_table(path).to_pandas()


if __name__ == "__main__":
    main()
