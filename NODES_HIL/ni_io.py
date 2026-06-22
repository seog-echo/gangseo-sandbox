#!/usr/bin/env python3
"""NI-DAQmx I/O layer for the NODES hardware-in-the-loop simulator.

Two independent streams run while a HIL session is active:

* **Analog OUT (NI-9263)** — one selected NODES channel is streamed, sample-for-
  sample at the simulator's native rate, scaled from microvolts to volts. The GUI
  tick pushes each freshly generated chunk via :meth:`NiHilIO.push_ao`; a writer
  thread feeds the DAQ buffer with regeneration disabled so the analog signal is a
  true continuous stream (not a looping waveform).

* **Analog IN (NI-9222)** — the external "mock stimulation" is sampled continuously
  on a reader thread into a rolling buffer. The GUI reads the latest window with
  :meth:`NiHilIO.get_ai_window` to extract amplitude/frequency, and the raw chunks
  are emitted via :data:`NiHilIO.ai_chunk_ready` for live plotting.

When the ``nidaqmx`` package or the physical modules are unavailable the class runs
in **simulation mode**: the AO writer simply drains its queue and the AI reader
synthesises a sine so the entire closed loop can be developed and demonstrated
without hardware.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from PySide6 import QtCore

try:  # nidaqmx is optional; absence simply forces simulation mode.
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, RegenerationMode, TerminalConfiguration
except Exception:  # pragma: no cover - depends on host
    nidaqmx = None
    AcquisitionType = None
    RegenerationMode = None
    TerminalConfiguration = None


AI_PRODUCT_HINT = "9222"
AO_PRODUCT_HINT = "9263"


@dataclass(slots=True)
class AiConfig:
    device_name: str = ""
    channel: int = 0
    sample_rate_hz: float = 20000.0
    voltage_min: float = -10.0
    voltage_max: float = 10.0
    window_seconds: float = 0.3            # rolling window kept for measurement
    chunk_samples: int = 400               # samples per read (~20 ms at 20 kHz)


@dataclass(slots=True)
class AoConfig:
    device_name: str = ""
    channel: int = 0
    sample_rate_hz: float = 1024.0         # NODES native fs
    voltage_min: float = -10.0
    voltage_max: float = 10.0
    buffer_seconds: float = 0.5            # onboard/host buffer cushion
    # Real-data backlog accumulated before streaming starts. The simulator feeds
    # AO in ~100 ms GUI-thread bursts; this cushion absorbs that jitter (and the
    # periodic heavy-redraw stalls) so the writer rarely underruns.
    target_latency_s: float = 0.3


@dataclass(slots=True)
class DeviceStatus:
    driver_available: bool = False
    ai_present: bool = False
    ao_present: bool = False
    ai_device_name: str = ""
    ao_device_name: str = ""
    details: List[str] = field(default_factory=list)


class NiHilIO(QtCore.QObject):
    """Owns the AI reader and AO writer threads for one HIL session."""

    ai_chunk_ready = QtCore.Signal(object)   # np.ndarray of new AI samples (volts)
    status_text = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._ai_cfg = AiConfig()
        self._ao_cfg = AoConfig()
        self.simulation_mode = nidaqmx is None

        self._stop = threading.Event()
        self._ai_thread: Optional[threading.Thread] = None
        self._ao_thread: Optional[threading.Thread] = None
        self._ai_task = None
        self._ao_task = None

        # Rolling AI window (volts), guarded by a lock; written by reader thread,
        # read by the GUI tick.
        self._ai_lock = threading.Lock()
        self._ai_window = np.zeros(0, dtype=np.float64)
        self._ai_window_max = 1

        # AO sample queue (volts). Bounded so a stalled consumer cannot grow it
        # without limit; the writer paces on the DAQ clock.
        self._ao_lock = threading.Lock()
        self._ao_pending = np.zeros(0, dtype=np.float64)
        self._ao_underruns = 0           # count of held-last-value pads (diagnostic)
        self._ao_underrun_warned = False
        self._external_ao = False        # caller paces AO via write_ao_block()

        # Synthetic input parameters used only in simulation mode (live-settable).
        self.sim_amplitude_v = 1.5
        self.sim_frequency_hz = 60.0
        self._sim_phase = 0.0

    # ------------------------------------------------------------------ devices
    @staticmethod
    def check_devices() -> DeviceStatus:
        status = DeviceStatus(driver_available=nidaqmx is not None)
        if nidaqmx is None:
            status.details.append("nidaqmx package not installed - running in simulation mode")
            return status
        try:
            system = nidaqmx.system.System.local()
            devices = list(system.devices)
            if not devices:
                status.details.append("No NI-DAQmx devices found (check NI-MAX and cabling)")
            for device in devices:
                product = getattr(device, "product_type", "")
                name = getattr(device, "name", "")
                status.details.append(f"{name}: {product}")
                if AI_PRODUCT_HINT in product:
                    status.ai_present = True
                    status.ai_device_name = name
                if AO_PRODUCT_HINT in product:
                    status.ao_present = True
                    status.ao_device_name = name
        except Exception as exc:  # pragma: no cover - hardware dependent
            status.details.append(f"DAQmx scan error: {exc}")
        return status

    # -------------------------------------------------------------------- start
    def start(self, ai_cfg: AiConfig, ao_cfg: AoConfig, *, force_simulation: bool = False,
              external_ao: bool = False) -> None:
        """Start the AI reader and AO output.

        ``external_ao=True`` means the caller drives AO itself by calling
        :meth:`write_ao_block` from its own (generation) thread, which blocks on
        the DAQ buffer and thereby paces the caller to the hardware clock. In that
        mode the internal queue-based AO writer thread is NOT started. This is the
        glitch-free path: the generator stays in lockstep with the 1024 Hz output
        and is decoupled from GUI redraw jitter.
        """
        self._ai_cfg = ai_cfg
        self._ao_cfg = ao_cfg
        self._external_ao = external_ao
        self._ai_window_max = max(1, int(ai_cfg.sample_rate_hz * ai_cfg.window_seconds))
        with self._ai_lock:
            self._ai_window = np.zeros(0, dtype=np.float64)
        with self._ao_lock:
            self._ao_pending = np.zeros(0, dtype=np.float64)

        self.simulation_mode = force_simulation or nidaqmx is None
        self._stop.clear()

        if not self.simulation_mode:
            try:
                self._configure_ai_task()
                self._configure_ao_task()
            except Exception as exc:
                # Fall back to simulation rather than aborting the whole session.
                self._close_tasks()
                self.simulation_mode = True
                self.error.emit(f"NI hardware init failed ({exc}); falling back to simulation mode")

        mode = "SIMULATION" if self.simulation_mode else "HARDWARE"
        ao_mode = "ext-paced" if external_ao else "queued"
        self.status_text.emit(
            f"HIL I/O started [{mode}] - AI ch{ai_cfg.channel}@{ai_cfg.sample_rate_hz:.0f}Hz, "
            f"AO ch{ao_cfg.channel}@{ao_cfg.sample_rate_hz:.0f}Hz ({ao_mode})"
        )

        self._ai_thread = threading.Thread(target=self._ai_loop, daemon=True)
        self._ai_thread.start()
        if not external_ao:
            self._ao_thread = threading.Thread(target=self._ao_loop, daemon=True)
            self._ao_thread.start()

    def write_ao_block(self, samples_v: np.ndarray) -> bool:
        """Write one block of already-scaled (volts) samples to the AO, blocking
        until the DAQ buffer has room. Returns False on stop/error.

        On hardware this blocking write paces the calling thread to the 1024 Hz
        output clock (and lets it run ahead to keep the buffer full). In simulation
        it sleeps for the block's duration so the caller is still paced to ~real
        time. Use only with ``external_ao=True``."""
        data = np.asarray(samples_v, dtype=np.float64).ravel()
        if self.simulation_mode or self._ao_task is None:
            if data.size:
                time.sleep(data.size / max(1.0, self._ao_cfg.sample_rate_hz))
            return not self._stop.is_set()
        return self._write_ao(data)

    def stop(self) -> None:
        self._stop.set()
        for task in (self._ai_task, self._ao_task):
            try:
                if task is not None:
                    task.stop()
            except Exception:
                pass
        # Threads are daemons and observe the stop event; give them a moment.
        for thread in (self._ai_thread, self._ao_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        self._close_tasks()
        self.status_text.emit("HIL I/O stopped")

    # --------------------------------------------------------------- AO feeding
    def push_ao(self, chunk_v: np.ndarray) -> None:
        """Queue already-scaled (volts) samples for analog output."""
        data = np.asarray(chunk_v, dtype=np.float64).ravel()
        if data.size == 0:
            return
        with self._ao_lock:
            # Cap backlog at ~2 buffers so a paused/slow consumer cannot accumulate
            # unbounded latency; drop oldest if it ever grows that large.
            max_pending = max(1, int(self._ao_cfg.sample_rate_hz * self._ao_cfg.buffer_seconds * 4))
            merged = np.concatenate([self._ao_pending, data])
            if merged.size > max_pending:
                merged = merged[-max_pending:]
            self._ao_pending = merged

    # ----------------------------------------------------------- AI measurement
    def get_ai_window(self) -> tuple[np.ndarray, float]:
        """Return a copy of the current rolling AI window and its sample rate."""
        with self._ai_lock:
            return self._ai_window.copy(), float(self._ai_cfg.sample_rate_hz)

    # ============================================================ internals: AI
    def _configure_ai_task(self) -> None:
        cfg = self._ai_cfg
        if not cfg.device_name:
            raise RuntimeError("AI device name is not set")
        terminal = getattr(TerminalConfiguration, "DIFFERENTIAL", None) if TerminalConfiguration else None
        task = nidaqmx.Task()
        kwargs = dict(min_val=cfg.voltage_min, max_val=cfg.voltage_max)
        if terminal is not None:
            kwargs["terminal_config"] = terminal
        task.ai_channels.add_ai_voltage_chan(f"{cfg.device_name}/ai{cfg.channel}", **kwargs)
        buf = max(cfg.chunk_samples * 10, int(cfg.sample_rate_hz * 2))
        task.timing.cfg_samp_clk_timing(
            rate=cfg.sample_rate_hz,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=buf,
        )
        task.start()
        self._ai_task = task

    def _ai_loop(self) -> None:
        cfg = self._ai_cfg
        if self.simulation_mode:
            self._ai_loop_simulated()
            return
        while not self._stop.is_set():
            try:
                data = self._ai_task.read(number_of_samples_per_channel=cfg.chunk_samples, timeout=1.0)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.error.emit(f"NI AI read failed: {exc}")
                break
            chunk = np.asarray(data, dtype=np.float64).ravel()
            self._ingest_ai(chunk)

    def _ai_loop_simulated(self) -> None:
        cfg = self._ai_cfg
        dt_chunk = cfg.chunk_samples / cfg.sample_rate_hz
        while not self._stop.is_set():
            n = cfg.chunk_samples
            t = self._sim_phase + np.arange(n, dtype=np.float64) / cfg.sample_rate_hz
            chunk = self.sim_amplitude_v * np.sin(2.0 * np.pi * self.sim_frequency_hz * t)
            chunk += 0.01 * np.random.standard_normal(n)  # a little measurement noise
            self._sim_phase = t[-1] + 1.0 / cfg.sample_rate_hz
            self._ingest_ai(chunk)
            time.sleep(dt_chunk)

    def _ingest_ai(self, chunk: np.ndarray) -> None:
        with self._ai_lock:
            merged = np.concatenate([self._ai_window, chunk])
            if merged.size > self._ai_window_max:
                merged = merged[-self._ai_window_max:]
            self._ai_window = merged
        self.ai_chunk_ready.emit(chunk)

    # ============================================================ internals: AO
    def _configure_ao_task(self) -> None:
        cfg = self._ao_cfg
        if not cfg.device_name:
            raise RuntimeError("AO device name is not set")
        task = nidaqmx.Task()
        task.ao_channels.add_ao_voltage_chan(
            f"{cfg.device_name}/ao{cfg.channel}",
            min_val=cfg.voltage_min,
            max_val=cfg.voltage_max,
        )
        buf = max(64, int(cfg.sample_rate_hz * cfg.buffer_seconds))
        task.timing.cfg_samp_clk_timing(
            rate=cfg.sample_rate_hz,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=buf,
        )
        # True streaming: never replay stale buffer contents on underrun.
        if RegenerationMode is not None:
            try:
                task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
            except Exception:
                pass
        # Pre-fill most of the buffer with silence to build a timing cushion.
        prefill = np.zeros(int(buf * 0.8), dtype=np.float64)
        task.write(prefill, auto_start=False)
        task.start()
        self._ao_task = task

    def _ao_loop(self) -> None:
        cfg = self._ao_cfg
        # Write in modest blocks; task.write blocks until buffer space frees up,
        # which paces this loop to the AO sample clock.
        block = max(32, int(cfg.sample_rate_hz * 0.05))  # ~50 ms blocks
        target_latency = max(block, int(cfg.sample_rate_hz * cfg.target_latency_s))
        last_value = 0.0
        primed = False

        while not self._stop.is_set():
            if self.simulation_mode or self._ao_task is None:
                # No hardware: drain the queue at ~real time and discard.
                out, last_value = self._take_ao(block, last_value)
                time.sleep(block / cfg.sample_rate_hz)
                continue

            if not primed:
                # Let a real-data cushion build before streaming, feeding silence
                # to keep the DAQ buffer alive in the meantime. This one-time
                # priming is what prevents steady-state underruns (the 0 V gaps).
                with self._ao_lock:
                    have = self._ao_pending.size
                if have >= target_latency:
                    primed = True
                else:
                    if not self._write_ao(np.zeros(block, dtype=np.float64)):
                        break
                    continue

            out, last_value = self._take_ao(block, last_value)
            if not self._write_ao(out):
                break

    def _write_ao(self, samples: np.ndarray) -> bool:
        try:
            self._ao_task.write(samples, auto_start=False, timeout=2.0)
            return True
        except Exception as exc:
            if self._stop.is_set():
                return False
            self.error.emit(f"NI AO write failed: {exc}")
            return False

    def _take_ao(self, block: int, last_value: float) -> tuple[np.ndarray, float]:
        """Pull ``block`` samples from the pending AO queue. On underrun, HOLD the
        last value (a brief DC hold) rather than dropping to 0 V, and report it.
        Returns the block and the updated last value."""
        with self._ao_lock:
            if self._ao_pending.size >= block:
                out = self._ao_pending[:block]
                self._ao_pending = self._ao_pending[block:]
                return out, float(out[-1])
            out = self._ao_pending
            self._ao_pending = np.zeros(0, dtype=np.float64)

        if out.size:
            last_value = float(out[-1])
        if out.size < block:
            # Underrun: hold the last sample so the trace shows a short flat
            # segment instead of a jump to zero.
            self._ao_underruns += 1
            if not self._ao_underrun_warned:
                self._ao_underrun_warned = True
                self.status_text.emit(
                    "NI AO underrun: producer briefly fell behind; holding last value. "
                    "Increase AoConfig.target_latency_s if this is frequent."
                )
            pad = np.full(block - out.size, last_value, dtype=np.float64)
            out = np.concatenate([out, pad])
        return out, last_value

    def _close_tasks(self) -> None:
        for attr in ("_ai_task", "_ao_task"):
            task = getattr(self, attr)
            if task is None:
                continue
            try:
                task.stop()
            except Exception:
                pass
            try:
                task.close()
            except Exception:
                pass
            setattr(self, attr, None)
