from __future__ import annotations

import queue
import threading
from typing import List, Sequence, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


# One queued item: (time[n] float64, data[n,32] float32, stim tuple, state str)
#   stim = (left_on, left_contact, left_ma, left_hz, right_on, right_contact, right_ma, right_hz)
Item = Tuple[np.ndarray, np.ndarray, Tuple, str]


class ParquetRecorder:
    """Streams recorded chunks to a Parquet file from a background thread.

    The GUI thread only calls :meth:`submit` (a thread-safe queue put), so it
    never touches disk. This worker thread builds Arrow tables, compresses, and
    writes row groups incrementally — constant memory, any duration, no GUI
    stalls. Call :meth:`stop` to finalize; poll :meth:`is_running` for completion
    and read :attr:`saved_path` / :attr:`error` afterward.
    """

    ROWGROUP_ROWS = 8192  # ~8 s at 1024 Hz per row group

    def __init__(self, path: str, channel_names: Sequence[str], fs: int, start_iso: str):
        self.path = path
        self.channel_names = list(channel_names)
        self.fs = int(fs)
        self.saved_path: str | None = None
        self.error: str | None = None
        self.rows_written = 0
        self._q: "queue.Queue[Item | None]" = queue.Queue()
        self._schema = self._build_schema(start_iso)
        self._thread = threading.Thread(target=self._run, name="ParquetRecorder", daemon=True)

    # ---- producer API (called from the GUI thread) ----
    def start(self) -> None:
        self._thread.start()

    def submit(self, time_arr: np.ndarray, data_mat: np.ndarray, stim: Tuple, state: str) -> None:
        self._q.put((time_arr, data_mat, stim, state))

    def stop(self) -> None:
        self._q.put(None)

    def is_running(self) -> bool:
        return self._thread.is_alive()

    # ---- internals (worker thread) ----
    def _build_schema(self, start_iso: str) -> pa.Schema:
        fields = [("time", pa.float64())]
        fields += [(name, pa.float32()) for name in self.channel_names]
        fields += [
            ("stim_left_on", pa.bool_()),
            ("stim_left_contact", pa.int8()),
            ("stim_left_ma", pa.float32()),
            ("stim_left_hz", pa.float32()),
            ("stim_right_on", pa.bool_()),
            ("stim_right_contact", pa.int8()),
            ("stim_right_ma", pa.float32()),
            ("stim_right_hz", pa.float32()),
            ("state", pa.string()),
        ]
        metadata = {
            b"generator": b"NODES Unified DBS Simulator",
            b"fs_hz": str(self.fs).encode(),
            b"units": b"microvolts",
            b"channels": ",".join(self.channel_names).encode(),
            b"start_time": start_iso.encode(),
        }
        return pa.schema(fields, metadata=metadata)

    def _run(self) -> None:
        writer = None
        buf: List[Item] = []
        rows = 0
        try:
            writer = pq.ParquetWriter(self.path, self._schema, compression="zstd")
            while True:
                item = self._q.get()
                if item is None:
                    break
                buf.append(item)
                rows += len(item[0])
                if rows >= self.ROWGROUP_ROWS:
                    self._flush(writer, buf)
                    buf, rows = [], 0
            if buf:
                self._flush(writer, buf)
        except Exception as exc:  # noqa: BLE001 - surface any write error to the GUI
            self.error = str(exc)
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception as exc:  # noqa: BLE001
                    self.error = self.error or str(exc)
            self.saved_path = self.path if self.error is None else None

    def _flush(self, writer: "pq.ParquetWriter", buf: List[Item]) -> None:
        times = np.concatenate([it[0] for it in buf])
        data = np.concatenate([it[1] for it in buf], axis=0)  # (N, 32)
        lengths = [len(it[0]) for it in buf]

        def expand(stim_index: int) -> np.ndarray:
            return np.concatenate([np.full(n, it[2][stim_index]) for n, it in zip(lengths, buf)])

        states = np.concatenate([np.full(n, it[3], dtype=object) for n, it in zip(lengths, buf)])

        cols = {"time": pa.array(times, pa.float64())}
        for i, name in enumerate(self.channel_names):
            cols[name] = pa.array(data[:, i], pa.float32())
        cols["stim_left_on"] = pa.array(expand(0).astype(bool), pa.bool_())
        cols["stim_left_contact"] = pa.array(expand(1).astype(np.int8), pa.int8())
        cols["stim_left_ma"] = pa.array(expand(2), pa.float32())
        cols["stim_left_hz"] = pa.array(expand(3), pa.float32())
        cols["stim_right_on"] = pa.array(expand(4).astype(bool), pa.bool_())
        cols["stim_right_contact"] = pa.array(expand(5).astype(np.int8), pa.int8())
        cols["stim_right_ma"] = pa.array(expand(6), pa.float32())
        cols["stim_right_hz"] = pa.array(expand(7), pa.float32())
        cols["state"] = pa.array(states, pa.string())

        writer.write_table(pa.table(cols, schema=self._schema))
        self.rows_written += len(times)
