#!/usr/bin/env python3
"""Traceability window for the NODES HIL loop.

Shows, side by side and updated live with the simulator tick:

1. **Output** - the selected NODES channel that is being streamed to the NI-9263
   analog output (plotted as raw microvolts; the volts actually driven are the
   readout below, after the AO gain).
2. **Input** - the mock-stimulation waveform sampled on the NI-9222 analog input,
   in volts, with its measured peak amplitude and dominant frequency.
3. The resulting **stim drive** (mA / Hz) the loop is sending into NODES, so the
   input -> parameter mapping is visible at a glance.
"""

from __future__ import annotations

from typing import List

import numpy as np
import pyqtgraph as pg
from matplotlib import colormaps
from PySide6 import QtCore, QtWidgets
from scipy.signal import spectrogram, welch

from hil_mapping import StimDrive
from signal_metrics import SignalMeasurement


def _make_lut(name: str) -> np.ndarray:
    cmap = colormaps[name]
    return (np.asarray(cmap(np.linspace(0.0, 1.0, 256)))[:, :3] * 255).astype(np.uint8)


def _downsample_for_display(signal: np.ndarray, max_points: int = 700) -> np.ndarray:
    if len(signal) <= max_points:
        return signal
    step = max(1, len(signal) // max_points)
    return signal[::step]


class HilMonitorWindow(QtWidgets.QWidget):
    channel_changed = QtCore.Signal(str)
    ao_gain_changed = QtCore.Signal(float)
    oversample_changed = QtCore.Signal(int)
    sim_amp_changed = QtCore.Signal(float)
    sim_freq_changed = QtCore.Signal(float)

    def __init__(self, channel_names: List[str], default_channel: str,
                 ao_gain: float, simulation_mode: bool, ao_oversample: int = 8,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("NODES HIL - Loop Monitor")
        self.resize(900, 620)
        self.setStyleSheet(
            "QWidget { background: #0b0e14; color: #d5d9e0; }"
            "QGroupBox { border: 1px solid #2a3242; border-radius: 8px; margin-top: 10px; padding-top: 10px; font-weight: 600; }"
            "QComboBox, QDoubleSpinBox { background: #151a24; border: 1px solid #2a3242; border-radius: 6px; padding: 3px; }"
        )

        root = QtWidgets.QVBoxLayout(self)

        # ---- top control row: channel selector + AO gain ----
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("AO channel:"))
        self.channel_combo = QtWidgets.QComboBox()
        self.channel_combo.addItems(channel_names)
        if default_channel in channel_names:
            self.channel_combo.setCurrentText(default_channel)
        self.channel_combo.currentTextChanged.connect(self.channel_changed.emit)
        top.addWidget(self.channel_combo)

        top.addSpacing(16)
        top.addWidget(QtWidgets.QLabel("AO gain (x, on volts):"))
        self.gain_spin = QtWidgets.QDoubleSpinBox()
        self.gain_spin.setRange(1.0, 1_000_000.0)
        self.gain_spin.setDecimals(0)
        self.gain_spin.setSingleStep(1000.0)
        self.gain_spin.setValue(ao_gain)
        self.gain_spin.setToolTip(
            "Multiplier applied to the neural signal in volts (raw uV x 1e-6 x gain) "
            "before it is sent to NI-9263. Default 10000: ~50 uV -> ~0.5 V."
        )
        self.gain_spin.valueChanged.connect(self.ao_gain_changed.emit)
        top.addWidget(self.gain_spin)

        top.addSpacing(16)
        top.addWidget(QtWidgets.QLabel("AO oversample x:"))
        self.oversample_spin = QtWidgets.QSpinBox()
        self.oversample_spin.setRange(1, 32)
        self.oversample_spin.setValue(int(ao_oversample))
        self.oversample_spin.setToolTip(
            "Interpolated upsampling of the AO output (1024 Hz x N). Smooths the DAC "
            "staircase and pushes its spectral images out of the IPG's band. Applies on "
            "the next Start HIL."
        )
        self.oversample_spin.valueChanged.connect(self.oversample_changed.emit)
        top.addWidget(self.oversample_spin)
        top.addStretch(1)
        root.addLayout(top)

        # ---- output (selected channel -> AO) plot, with Raw/PSD/Spectrogram ----
        out_box = QtWidgets.QGroupBox("Output - selected channel -> NI-9263")
        out_l = QtWidgets.QVBoxLayout(out_box)
        out_head = QtWidgets.QHBoxLayout()
        out_head.addWidget(QtWidgets.QLabel("View:"))
        self.out_mode_combo = QtWidgets.QComboBox()
        self.out_mode_combo.addItems(["Raw", "PSD", "Spectrogram"])
        self.out_mode_combo.setMinimumWidth(120)
        self.out_mode_combo.currentTextChanged.connect(self._on_out_mode_changed)
        out_head.addWidget(self.out_mode_combo)
        out_head.addStretch(1)
        out_l.addLayout(out_head)

        self.out_mode = "Raw"
        self.out_lut = _make_lut("viridis")
        self.out_plot = self._make_plot("#67e8f9")
        self.out_curve = self.out_plot.plot(pen=pg.mkPen("#67e8f9", width=1.3))
        self.out_image = pg.ImageItem()
        self.out_plot.addItem(self.out_image)
        self.out_image.hide()
        out_l.addWidget(self.out_plot)
        root.addWidget(out_box)

        # Cached buffers for the selected channel so a mode switch re-renders
        # immediately without waiting for the next simulator tick.
        self._out_buf = np.zeros(0, dtype=np.float32)        # ~5 s rolling (Raw/PSD)
        self._out_spec_buf = np.zeros(0, dtype=np.float32)   # ~15 s rolling (Spectrogram)
        self._out_fs = 1024.0

        # ---- input (mock stim -> AI) plot ----
        in_box = QtWidgets.QGroupBox("Input - mock stimulation <- NI-9222 (volts)")
        in_l = QtWidgets.QVBoxLayout(in_box)
        self.in_plot = self._make_plot("#fdba74")
        # Peak-preserving downsampling: at the high AI rate a window is tens of
        # thousands of samples; draw only what the view needs while keeping narrow
        # pulses visible (mode="peak" plots each pixel's min/max, so spikes survive).
        self.in_plot.setDownsampling(auto=True, mode="peak")
        self.in_plot.setClipToView(True)
        self.in_curve = self.in_plot.plot(pen=pg.mkPen("#fdba74", width=1.3))
        in_l.addWidget(self.in_plot)
        root.addWidget(in_box)

        # ---- live readouts ----
        read_box = QtWidgets.QGroupBox("Measurement -> Stim drive")
        grid = QtWidgets.QGridLayout(read_box)
        self.lbl_in_amp = self._readout(grid, 0, 0, "Input amplitude", "0.000 V")
        self.lbl_in_freq = self._readout(grid, 0, 1, "Input frequency", "0.0 Hz")
        self.lbl_in_pw = self._readout(grid, 0, 2, "Input pulse width", "-")
        self.lbl_stim_ma = self._readout(grid, 0, 3, "Stim amplitude", "0.00 mA")
        self.lbl_stim_hz = self._readout(grid, 1, 0, "Stim frequency", "0.0 Hz")
        self.lbl_ao_v = self._readout(grid, 1, 1, "AO peak out", "0.000 V")
        self.lbl_target = self._readout(grid, 1, 2, "Stim target", "-")
        self.lbl_mode = self._readout(grid, 1, 3, "I/O mode", "SIMULATION" if simulation_mode else "HARDWARE")
        self.lbl_state = self._readout(grid, 2, 0, "Loop", "idle")
        root.addWidget(read_box)

        # ---- simulation-only synthetic input controls ----
        self.sim_box = QtWidgets.QGroupBox("Simulated input (no hardware)")
        sim_l = QtWidgets.QHBoxLayout(self.sim_box)
        sim_l.addWidget(QtWidgets.QLabel("Amp (V):"))
        self.sim_amp = QtWidgets.QDoubleSpinBox()
        self.sim_amp.setRange(0.0, 10.0)
        self.sim_amp.setSingleStep(0.1)
        self.sim_amp.setValue(1.5)
        self.sim_amp.valueChanged.connect(self.sim_amp_changed.emit)
        sim_l.addWidget(self.sim_amp)
        sim_l.addSpacing(12)
        sim_l.addWidget(QtWidgets.QLabel("Freq (Hz):"))
        self.sim_freq = QtWidgets.QDoubleSpinBox()
        self.sim_freq.setRange(1.0, 1000.0)
        self.sim_freq.setSingleStep(1.0)
        self.sim_freq.setValue(60.0)
        self.sim_freq.valueChanged.connect(self.sim_freq_changed.emit)
        sim_l.addWidget(self.sim_freq)
        sim_l.addStretch(1)
        self.sim_box.setVisible(simulation_mode)
        root.addWidget(self.sim_box)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _make_plot(color: str) -> pg.PlotWidget:
        pw = pg.PlotWidget()
        pw.setMenuEnabled(False)
        pw.hideButtons()
        pw.setMouseEnabled(False, False)
        pw.showGrid(x=True, y=True, alpha=0.15)
        pw.setBackground("#0f1117")
        pw.setFixedHeight(170)
        return pw

    def _readout(self, grid: QtWidgets.QGridLayout, row: int, col: int, title: str, value: str) -> QtWidgets.QLabel:
        cell = QtWidgets.QVBoxLayout()
        t = QtWidgets.QLabel(title)
        t.setStyleSheet("color: #8b93a7; font-size: 10px;")
        v = QtWidgets.QLabel(value)
        v.setStyleSheet("color: #d5d9e0; font-size: 18px; font-weight: 700;")
        cell.addWidget(t)
        cell.addWidget(v)
        wrap = QtWidgets.QWidget()
        wrap.setLayout(cell)
        grid.addWidget(wrap, row, col)
        return v

    def set_simulation_mode(self, simulation_mode: bool) -> None:
        self.sim_box.setVisible(simulation_mode)
        self.lbl_mode.setText("SIMULATION" if simulation_mode else "HARDWARE")

    # -------------------------------------------------------------- live update
    def _on_out_mode_changed(self, text: str) -> None:
        self.out_mode = text
        self._render_output()

    def update_output(self, buf_uv: np.ndarray, spec_buf_uv: np.ndarray, fs: float) -> None:
        """Feed the selected channel's rolling buffers (raw/PSD window and the
        longer spectrogram window) and render in the current view mode."""
        self._out_buf = np.asarray(buf_uv, dtype=np.float32)
        self._out_spec_buf = np.asarray(spec_buf_uv, dtype=np.float32)
        self._out_fs = float(fs)
        self._render_output()

    def _render_output(self) -> None:
        mode = self.out_mode
        fs = self._out_fs
        signal = self._out_spec_buf if mode == "Spectrogram" else self._out_buf
        if signal.size < 10:
            return

        if mode == "Raw":
            self.out_plot.setLogMode(False, False)
            self.out_image.hide()
            self.out_curve.show()
            t = np.arange(signal.size, dtype=np.float64) / fs
            self.out_curve.setData(_downsample_for_display(t), _downsample_for_display(signal))
            lo = float(np.percentile(signal, 2))
            hi = float(np.percentile(signal, 98))
            pad = max(1.0, 0.15 * (hi - lo if hi > lo else 1.0))
            self.out_plot.setXRange(float(t[0]), float(t[-1]), padding=0)
            self.out_plot.setYRange(lo - pad, hi + pad, padding=0)
        elif mode == "PSD":
            self.out_plot.setLogMode(False, False)
            self.out_image.hide()
            self.out_curve.show()
            nperseg = min(512, signal.size)
            f, pxx = welch(signal, fs=fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2)
            m = f <= 100
            pxx_db = 10.0 * np.log10(np.maximum(pxx[m], 1e-12))
            self.out_curve.setData(_downsample_for_display(f[m]), _downsample_for_display(pxx_db))
            if pxx_db.size:
                lo = float(np.percentile(pxx_db, 5))
                hi = float(np.percentile(pxx_db, 98))
                pad = max(1.0, 0.10 * (hi - lo if hi > lo else 1.0))
                self.out_plot.setXRange(0, 100, padding=0)
                self.out_plot.setYRange(lo - pad, hi + pad, padding=0)
        else:  # Spectrogram
            nperseg = 256
            noverlap = 192
            if signal.size < nperseg:
                return
            self.out_plot.setLogMode(False, False)
            self.out_curve.hide()
            self.out_image.show()
            f, t, sxx = spectrogram(signal, fs=fs, window="hann", nperseg=nperseg,
                                    noverlap=noverlap, scaling="density", mode="psd")
            m = f <= 100
            sxx_db = 10.0 * np.log10(sxx[m] + 1e-12)
            self.out_image.setImage(sxx_db, autoLevels=True)
            self.out_image.setLookupTable(self.out_lut)
            if t.size > 1 and f[m].size > 1:
                fm = f[m]
                self.out_image.setRect(QtCore.QRectF(float(t[0]), float(fm[0]),
                                                     float(t[-1] - t[0]), float(fm[-1] - fm[0])))
                self.out_plot.setXRange(float(t[0]), float(t[-1]), padding=0)
                self.out_plot.setYRange(float(fm[0]), float(fm[-1]), padding=0)

    def update_input_trace(self, signal_v: np.ndarray, fs: float) -> None:
        n = signal_v.size
        if n == 0:
            return
        t = np.arange(n, dtype=np.float64) / fs
        self.in_curve.setData(t, signal_v)

    def update_readouts(self, meas: SignalMeasurement, drive: StimDrive, ao_peak_v: float, running: bool) -> None:
        self.lbl_in_amp.setText(f"{meas.amplitude_v:.3f} V")
        self.lbl_in_freq.setText(f"{meas.frequency_hz:.1f} Hz")
        if meas.is_pulsatile and meas.pulse_width_s > 0.0:
            self.lbl_in_pw.setText(f"{meas.pulse_width_s * 1e6:.0f} us")
        else:
            self.lbl_in_pw.setText("- (continuous)")
        self.lbl_stim_ma.setText(f"{drive.amplitude_ma:.2f} mA")
        self.lbl_stim_hz.setText(f"{drive.frequency_hz:.1f} Hz")
        self.lbl_ao_v.setText(f"{ao_peak_v:.3f} V")
        self.lbl_target.setText(f"{drive.side} c{drive.contact_index + 1}" + ("" if drive.enabled else " (off)"))
        self.lbl_state.setText("running" if running else "idle")
