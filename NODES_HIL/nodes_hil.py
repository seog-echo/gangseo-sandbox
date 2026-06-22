#!/usr/bin/env python3
"""NODES Hardware-in-the-Loop simulator.

Runs the full NODES Unified DBS simulator (unchanged) inside a partially closed
loop with NI-DAQ hardware:

    selected NODES channel ---(scaled uV->V)---> NI-9263 AO ---> oscilloscope / IPG
    external mock stimulation ---> NI-9222 AI ---> amplitude + frequency
        ---> NODES stimulation parameters (LEFT side) ---> reshapes all channels

While a HIL session is active the simulator's manual Stimulation controls are
disabled (stim is driven by the measured input), but the Behavioral State controls
remain fully live. A separate Loop Monitor window shows the output and input
traces with live amplitude/frequency/stim readouts.

Real-time architecture: during a HIL run the simulation does NOT run on the GUI
timer. A dedicated generation thread owns the model and loops "simulate one block
-> write it to NI-9263 (blocking) -> stash all channels for display". The blocking
AO write paces the generator to the hardware clock, so the analog output stays
glitch-free regardless of GUI redraw load. The GUI timer becomes a pure display
consumer that drains the stashed chunks and redraws at its own (jittery) pace.

Run:  python nodes_hil.py
"""

from __future__ import annotations

import sys
import threading

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from hil_mapping import HilMapping, StimDrive, drive_to_commands, resolve_drive
from hil_monitor import HilMonitorWindow
from ni_io import AiConfig, AoConfig, NiHilIO
from nodes import UnifiedDBSWindow
from signal_metrics import SignalMeasurement, measure_signal

# Default microvolt -> volt scaling multiplier for the AO output (applied to the
# signal already converted to volts). 10000 maps ~50 uV to ~0.5 V on the scope.
DEFAULT_AO_GAIN = 10000.0

# Default AO oversampling factor. The NODES channel (1024 Hz) is interpolated to
# 1024 x N Hz before output so the DAC staircase images sit well above the IPG's
# band. Important because the IPG has no anti-aliasing filter and samples ~1024 Hz.
DEFAULT_AO_OVERSAMPLE = 8


class NodesHilWindow(UnifiedDBSWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NODES HIL - Unified DBS Simulator (Hardware-in-the-Loop)")

        # HIL state.
        self._hil_running = False
        self._io = NiHilIO()
        self._io.status_text.connect(self._on_io_status)
        self._io.error.connect(self._on_io_error)
        self.mapping = HilMapping()              # left side, contact 4, 1 mA/V, pass-through
        self._hil_drive = StimDrive(False, self.mapping.side, self.mapping.contact_index, 0.0,
                                    self.mapping.idle_frequency_hz)
        self._last_meas = SignalMeasurement()
        self._ao_channel = f"{self.mapping.side}_depth_4"   # a sensible default to scope
        self._ao_gain = DEFAULT_AO_GAIN
        self._ao_oversample = DEFAULT_AO_OVERSAMPLE
        self._ao_prev_sample: float | None = None            # interp continuity across blocks

        # Generation thread + GUI display hand-off.
        self._gen_thread: threading.Thread | None = None
        self._gen_stop = threading.Event()
        self._gen_block = max(32, round(self.fs * 0.05))     # ~50 ms generation blocks
        self._disp_lock = threading.Lock()
        self._disp_queue: list[dict] = []                    # chunks awaiting GUI display
        self._disp_cap = 40                                  # ~2 s backlog cap (drop oldest)

        self.monitor: HilMonitorWindow | None = None
        self._build_hil_toolbar()

    # ------------------------------------------------------------- HIL toolbar
    def _build_hil_toolbar(self) -> None:
        bar = QtWidgets.QToolBar("HIL")
        bar.setMovable(False)
        bar.setStyleSheet("QToolBar { background: #11151d; border-bottom: 1px solid #2a3242; padding: 4px; }")
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, bar)

        self.check_btn = QtWidgets.QPushButton("Check NI Devices")
        self.check_btn.clicked.connect(self._on_check_devices)
        bar.addWidget(self.check_btn)

        self.hil_start_btn = QtWidgets.QPushButton("Start HIL")
        self.hil_start_btn.setStyleSheet("font-weight: 700; color: white; background: #1f8f4a; border-radius: 6px; padding: 4px 10px;")
        self.hil_start_btn.clicked.connect(self._on_start_hil)
        bar.addWidget(self.hil_start_btn)

        self.hil_stop_btn = QtWidgets.QPushButton("Stop HIL")
        self.hil_stop_btn.setStyleSheet("font-weight: 700; color: white; background: #dc2626; border-radius: 6px; padding: 4px 10px;")
        self.hil_stop_btn.setEnabled(False)
        self.hil_stop_btn.clicked.connect(self._on_stop_hil)
        bar.addWidget(self.hil_stop_btn)

        self.monitor_btn = QtWidgets.QPushButton("Show Monitor")
        self.monitor_btn.clicked.connect(self._show_monitor)
        bar.addWidget(self.monitor_btn)

        self.hil_status = QtWidgets.QLabel("  HIL idle.")
        self.hil_status.setStyleSheet("color: #8b93a7; padding-left: 8px;")
        bar.addWidget(self.hil_status)

    # ------------------------------------------------------------------ devices
    def _on_check_devices(self) -> None:
        status = NiHilIO.check_devices()
        lines = "; ".join(status.details) if status.details else "(no details)"
        mode = "hardware OK" if (status.ai_present and status.ao_present) else "simulation"
        self.hil_status.setText(
            f"  NI-9222(AI)={'yes' if status.ai_present else 'no'} "
            f"NI-9263(AO)={'yes' if status.ao_present else 'no'} -> {mode}.  {lines}"
        )

    # --------------------------------------------------------------- start/stop
    def _on_start_hil(self) -> None:
        if self._hil_running:
            return
        status = NiHilIO.check_devices()
        hardware_ok = status.ai_present and status.ao_present and status.driver_available
        force_sim = not hardware_ok

        ai_cfg = AiConfig(device_name=status.ai_device_name, channel=0)
        oversample = max(1, int(self._ao_oversample))
        ao_cfg = AoConfig(device_name=status.ao_device_name, channel=self.mapping_ao_channel_index(),
                          sample_rate_hz=float(self.fs) * oversample)

        # Open the monitor first so it reflects the resolved I/O mode.
        self._ensure_monitor(simulation_mode=force_sim)

        # Reset interpolation state so the first AO block starts cleanly.
        self._ao_prev_sample = None

        # external_ao=True: the generation thread paces AO itself via write_ao_block.
        self._io.start(ai_cfg, ao_cfg, force_simulation=force_sim, external_ao=True)

        # Flip the flag BEFORE starting the generator so the GUI tick stops
        # generating (it will only consume from the display queue from now on).
        with self._disp_lock:
            self._disp_queue = []
        self._hil_running = True

        self._gen_stop.clear()
        self._gen_thread = threading.Thread(target=self._gen_loop, daemon=True)
        self._gen_thread.start()

        # Keep the GUI timer running purely for display; it no longer generates.
        if not self.timer.isActive():
            self._start_stream()

        self._set_stim_controls_enabled(False)
        self._lock_transport_for_hil()
        self.hil_start_btn.setEnabled(False)
        self.hil_stop_btn.setEnabled(True)

    def _on_stop_hil(self) -> None:
        if not self._hil_running:
            return
        # Stop the generator and I/O FIRST (while the GUI still treats the run as
        # active, so it does not start generating), then clear the flag.
        self._gen_stop.set()
        self._io.stop()                       # unblocks any pending AO write
        if self._gen_thread is not None:
            self._gen_thread.join(timeout=2.0)
            self._gen_thread = None
        self._hil_running = False

        self._set_stim_controls_enabled(True)
        # Restore the manual stim state from the (re-enabled) widgets so stimulation
        # does not linger at the last input-driven value.
        self._sync_side("left")
        self._sync_side("right")
        self._unlock_transport_after_hil()
        self.hil_start_btn.setEnabled(True)
        self.hil_stop_btn.setEnabled(False)
        self.hil_status.setText("  HIL stopped. Manual stimulation controls re-enabled.")
        if self.monitor is not None:
            self.monitor.update_readouts(self._last_meas, self._hil_drive, 0.0, running=False)

    def _lock_transport_for_hil(self) -> None:
        # The run is controlled by Stop HIL; lock the manual transport buttons.
        for button in (self.start_button, self.pause_button, self.end_button):
            button.setEnabled(False)

    def _unlock_transport_after_hil(self) -> None:
        # The GUI stream keeps running in manual mode after HIL stops.
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.end_button.setEnabled(True)

    def mapping_ao_channel_index(self) -> int:
        """Physical NI-9263 output channel to use (the analog channel, not the
        NODES channel being routed)."""
        return 0

    # -------------------------------------------------------------- monitor win
    def _ensure_monitor(self, simulation_mode: bool) -> None:
        if self.monitor is None:
            self.monitor = HilMonitorWindow(
                channel_names=self.channel_order,
                default_channel=self._ao_channel,
                ao_gain=self._ao_gain,
                simulation_mode=simulation_mode,
                ao_oversample=self._ao_oversample,
            )
            self.monitor.channel_changed.connect(self._on_ao_channel_changed)
            self.monitor.ao_gain_changed.connect(self._on_ao_gain_changed)
            self.monitor.oversample_changed.connect(self._on_oversample_changed)
            self.monitor.sim_amp_changed.connect(lambda v: setattr(self._io, "sim_amplitude_v", float(v)))
            self.monitor.sim_freq_changed.connect(lambda v: setattr(self._io, "sim_frequency_hz", float(v)))
        else:
            self.monitor.set_simulation_mode(simulation_mode)
        self.monitor.show()
        self.monitor.raise_()

    def _show_monitor(self) -> None:
        self._ensure_monitor(simulation_mode=self._io.simulation_mode)

    def _on_ao_channel_changed(self, name: str) -> None:
        if name in self.buffers:
            self._ao_channel = name

    def _on_ao_gain_changed(self, value: float) -> None:
        self._ao_gain = float(value)

    def _on_oversample_changed(self, value: int) -> None:
        # Changing the AO sample rate requires reconfiguring the DAQ task, so this
        # takes effect on the next Start HIL (the spinbox tooltip says so).
        self._ao_oversample = max(1, int(value))
        if self._hil_running:
            self.hil_status.setText(
                f"  AO oversample set to x{self._ao_oversample}; applies on next Start HIL."
            )

    # ----------------------------------------------------------- control gating
    def _set_stim_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable the manual Stimulation controls. Behavioral State and
        all other controls are intentionally left untouched."""
        for attr in ("left_freq_slider", "right_freq_slider",
                     "left_enabled", "right_enabled",
                     "left_contact", "right_contact",
                     "left_amp", "right_amp"):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.setEnabled(enabled)

    # ------------------------------------------------------------------- status
    def _on_io_status(self, text: str) -> None:
        self.hil_status.setText(f"  {text}")

    def _on_io_error(self, text: str) -> None:
        self.hil_status.setText(f"  ERROR: {text}")

    # --------------------------------------------------------------------- tick
    def _on_tick(self) -> None:
        if not self.timer.isActive():
            return
        if not self._hil_running:
            # Normal NODES behaviour: the base tick generates + redraws.
            super()._on_tick()
            return

        # HIL display path: the generation thread produces and drives AO. Here we
        # only consume the chunks it has stashed, update the rolling buffers, and
        # redraw — never generate (that would double-advance the model state).
        chunks = self._drain_disp_chunks()
        for out in chunks:
            self._last_out = out
            self._record_chunk(out)            # no-op unless recording
            self._update_monopolar_buffers(out)
            self._update_bipolar(out)
        self.tick += 1
        heavy = (self.tick % 10 == 0)
        self._redraw_all(force_heavy=heavy)
        self._update_monitor_only()

    def _drain_disp_chunks(self) -> list[dict]:
        with self._disp_lock:
            chunks = self._disp_queue
            self._disp_queue = []
        return chunks

    # ------------------------------------------------------- generation thread
    def _gen_loop(self) -> None:
        """Owns the model during a HIL run. Generates one block, writes the
        selected channel to AO (blocking -> paces this loop to the hardware
        clock), and stashes all channels for the GUI to display."""
        block = self._gen_block
        while not self._gen_stop.is_set():
            self._update_hil_drive()           # measure AI -> stim drive
            commands = drive_to_commands(self._hil_drive)
            out = self.model.simulate_chunk(
                stim_commands=commands,
                n_samples=block,
                include_subharmonics=False,
                state=self.state,
            )

            with self._disp_lock:
                self._disp_queue.append(out)
                if len(self._disp_queue) > self._disp_cap:
                    self._disp_queue = self._disp_queue[-self._disp_cap:]

            sig_uv = out.get(self._ao_channel)
            if sig_uv is None:
                sig_uv = next(iter(out.values()))
            sig_up = self._upsample_for_ao(np.asarray(sig_uv, dtype=np.float64))
            sig_v = sig_up * 1e-6 * self._ao_gain
            if not self._io.write_ao_block(sig_v):   # blocks; paces the loop
                break

    def _upsample_for_ao(self, sig: np.ndarray) -> np.ndarray:
        """Interpolate a base-rate (1024 Hz) block to 1024 x N Hz for AO output.

        Uses piecewise-linear interpolation carried across block boundaries (the
        previous block's last sample seeds this block) so the output is continuous
        with no per-block discontinuity. Linear interpolation has a sinc^2 response
        that suppresses the DAC images near 1024 Hz by >60 dB for low-frequency
        content — enough that, combined with an analog reconstruction filter, the
        no-AAF IPG samples clean baseband. Returns a block N times longer."""
        n = sig.size
        scale = max(1, int(self._ao_oversample))
        if scale <= 1 or n == 0:
            if n:
                self._ao_prev_sample = float(sig[-1])
            return sig.astype(np.float64)
        prev = self._ao_prev_sample
        if prev is None:
            prev = float(sig[0])                       # first block: start flat
        anchored = np.concatenate(([prev], sig))       # positions 0..n
        positions = (np.arange(n * scale) + 1) / scale  # 1/N .. n, ending on sig[-1]
        out = np.interp(positions, np.arange(n + 1), anchored)
        self._ao_prev_sample = float(sig[-1])
        return out

    def _update_hil_drive(self) -> None:
        window, fs = self._io.get_ai_window()
        meas = measure_signal(window, fs)
        if meas.valid:
            self._last_meas = meas
        drive = resolve_drive(self._last_meas.amplitude_v, self._last_meas.frequency_hz, self.mapping)
        self._hil_drive = drive

        # Mirror the drive into the simulator's stim state so _build_stim_commands(),
        # the status line, and recordings stay consistent. (Plain attribute writes;
        # the GUI thread only reads these, so GIL-atomic access is sufficient.)
        target = self.left if drive.side == "left" else self.right
        other = self.right if drive.side == "left" else self.left
        target.enabled = drive.enabled
        target.contact_index = drive.contact_index
        target.amplitude_ma = drive.amplitude_ma
        other.enabled = False
        if drive.side == "left":
            self.left_frequency_hz = drive.frequency_hz
        else:
            self.right_frequency_hz = drive.frequency_hz

    def _update_monitor_only(self) -> None:
        if self.monitor is None or not self.monitor.isVisible():
            return
        ch = self._ao_channel
        self.monitor.update_output(
            self.buffers.get(ch, np.zeros(0, dtype=np.float32)),
            self.spec_buffers.get(ch, np.zeros(0, dtype=np.float32)),
            self.fs,
        )
        ai_window, ai_fs = self._io.get_ai_window()
        self.monitor.update_input_trace(ai_window, ai_fs)
        out = self._last_out
        ao_peak = 0.0
        if out is not None and ch in out:
            v = np.asarray(out[ch], dtype=np.float64) * 1e-6 * self._ao_gain
            ao_peak = float(np.max(np.abs(v))) if v.size else 0.0
        self.monitor.update_readouts(self._last_meas, self._hil_drive, ao_peak, running=True)

    # ------------------------------------------------------------------ cleanup
    def _end_stream(self) -> None:
        # End must not run while the generation thread owns the model.
        if self._hil_running:
            self._on_stop_hil()
        super()._end_stream()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._hil_running:
            self._gen_stop.set()
            self._io.stop()
            if self._gen_thread is not None:
                self._gen_thread.join(timeout=2.0)
                self._gen_thread = None
            self._hil_running = False
        if self.monitor is not None:
            self.monitor.close()
        super().closeEvent(event)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("NODES HIL")
    window = NodesHilWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
