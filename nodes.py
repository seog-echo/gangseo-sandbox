#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from matplotlib import colormaps
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from scipy.signal import coherence, spectrogram, welch

sys.path.insert(0, os.path.dirname(__file__))

from simulator.bipolar_converter import BipolarSelection, convert_bipolar
from simulator.config import BEHAVIORAL_STATES
from simulator.model import DBSArrayModel, StimulationCommand
from simulator.recorder import ParquetRecorder

RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), "recordings")


# Channel pairs and bands for the coherence matrix. Each pair is compared at the
# two leads' hotspot contacts (depth contact 4, paddle contact 3).
COHERENCE_PAIRS = [
    ("L-STN ↔ R-STN", "left_depth_4", "right_depth_4"),
    ("L-M1 ↔ R-M1", "left_paddle_3", "right_paddle_3"),
    ("L-STN ↔ L-M1", "left_depth_4", "left_paddle_3"),
    ("R-STN ↔ R-M1", "right_depth_4", "right_paddle_3"),
]
COHERENCE_VIEW_BANDS = [("δ", 1.0, 4.0), ("β", 13.0, 30.0), ("γ", 60.0, 100.0)]


pg.setConfigOptions(antialias=False, background="#0f1117", foreground="#d5d9e0", imageAxisOrder="row-major")


GROUP_THEMES = {
    "left_paddle": {"accent": "#67e8f9", "border": "#155e75", "panel": "#0f172a", "curve": "#67e8f9", "cmap": "viridis"},
    "right_paddle": {"accent": "#86efac", "border": "#14532d", "panel": "#0f1a12", "curve": "#86efac", "cmap": "cividis"},
    "left_depth": {"accent": "#fdba74", "border": "#9a3412", "panel": "#1a120c", "curve": "#fdba74", "cmap": "magma"},
    "right_depth": {"accent": "#f9a8d4", "border": "#831843", "panel": "#170d15", "curve": "#f9a8d4", "cmap": "plasma"},
}


def _make_lut(name: str) -> np.ndarray:
    cmap = colormaps[name]
    return (np.asarray(cmap(np.linspace(0.0, 1.0, 256)))[:, :3] * 255).astype(np.uint8)


def _downsample_for_display(signal: np.ndarray, max_points: int = 700) -> np.ndarray:
    if len(signal) <= max_points:
        return signal
    step = max(1, len(signal) // max_points)
    return signal[::step]


@dataclass(slots=True)
class SideControl:
    enabled: bool = False
    contact_index: int = 3
    amplitude_ma: float = 0.0


class GroupPanel(QtWidgets.QGroupBox):
    def __init__(self, title: str, channel_names: List[str], theme: dict[str, str], parent=None):
        super().__init__(title, parent)
        self.channel_names = channel_names
        self.theme = theme
        self.mode = "Raw"
        self.lut = _make_lut(theme["cmap"])

        root = QtWidgets.QVBoxLayout(self)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(QtWidgets.QLabel("View:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Raw", "PSD", "Spectrogram"])
        self.mode_combo.setMinimumWidth(116)
        self.mode_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        head.addWidget(self.mode_combo)
        head.addStretch(1)
        root.addLayout(head)

        grid = QtWidgets.QGridLayout()
        self.plots: Dict[str, pg.PlotWidget] = {}
        self.curves: Dict[str, pg.PlotDataItem] = {}
        self.images: Dict[str, pg.ImageItem] = {}

        for idx, name in enumerate(channel_names):
            r, c = divmod(idx, 4)
            pw = pg.PlotWidget()
            pw.setMenuEnabled(False)
            pw.hideButtons()
            pw.setMouseEnabled(False, False)
            pw.setClipToView(True)
            pw.showGrid(x=True, y=True, alpha=0.15)
            pw.setBackground(theme["panel"])
            # Fixed height (taller than the original compact size, kept just
            # under column 1's total so the monopolar grid roughly matches it).
            pw.setFixedHeight(196)
            pw.setTitle(f"Ch{idx + 1}", color=theme["accent"], size="12pt")
            curve = pw.plot(pen=pg.mkPen(theme["curve"], width=1.2))
            image = pg.ImageItem()
            pw.addItem(image)
            image.hide()
            self.plots[name] = pw
            self.curves[name] = curve
            self.images[name] = image
            grid.addWidget(pw, r, c)

        root.addLayout(grid)
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self.setStyleSheet(
            f"""
            QGroupBox {{
                border: 2px solid {theme['border']};
                border-radius: 10px;
                margin-top: 12px;
                padding-top: 10px;
                background: {theme['panel']};
                color: {theme['accent']};
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }}
            """
        )

    def _on_mode_changed(self, text: str) -> None:
        self.mode = text

    def _reset_plot_state(self, name: str) -> None:
        pw = self.plots[name]
        pw.setLogMode(False, False)
        self.images[name].hide()
        self.curves[name].show()

    def set_raw(self, name: str, t: np.ndarray, signal: np.ndarray) -> None:
        pw = self.plots[name]
        self._reset_plot_state(name)
        self.curves[name].setData(_downsample_for_display(t), _downsample_for_display(signal))
        if len(signal) > 0:
            lo = float(np.percentile(signal, 2))
            hi = float(np.percentile(signal, 98))
            pad = max(1.0, 0.15 * (hi - lo if hi > lo else 1.0))
            pw.setXRange(float(t[0]), float(t[-1]) if len(t) else 5.0, padding=0)
            pw.setYRange(lo - pad, hi + pad, padding=0)

    def set_psd(self, name: str, f: np.ndarray, pxx: np.ndarray) -> None:
        pw = self.plots[name]
        self._reset_plot_state(name)
        pxx_db = 10.0 * np.log10(np.maximum(pxx, 1e-12))
        self.curves[name].setData(_downsample_for_display(f), _downsample_for_display(pxx_db))
        if len(pxx_db) > 0:
            lo = float(np.percentile(pxx_db, 5))
            hi = float(np.percentile(pxx_db, 98))
            pad = max(1.0, 0.10 * (hi - lo if hi > lo else 1.0))
            pw.setXRange(0, 100, padding=0)
            pw.setYRange(lo - pad, hi + pad, padding=0)

    def set_spec(self, name: str, f: np.ndarray, t: np.ndarray, sxx_db: np.ndarray) -> None:
        pw = self.plots[name]
        pw.setLogMode(False, False)
        self.curves[name].hide()
        img = self.images[name]
        img.show()
        img.setImage(sxx_db, autoLevels=True)
        img.setLookupTable(self.lut)
        if len(t) > 1 and len(f) > 1:
            img.setRect(QtCore.QRectF(float(t[0]), float(f[0]), float(t[-1] - t[0]), float(f[-1] - f[0])))
            pw.setXRange(float(t[0]), float(t[-1]), padding=0)
            pw.setYRange(float(f[0]), float(f[-1]), padding=0)


class BipolarPanel(QtWidgets.QGroupBox):
    def __init__(self, title: str, side: str, lead_kind: str, theme: dict[str, str], parent=None):
        super().__init__(title, parent)
        self.side = side
        self.lead_kind = lead_kind
        self.theme = theme
        self.mode = "Raw"
        self.current_title = title
        self.lut = _make_lut(theme["cmap"])

        root = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["Raw", "PSD", "Spec"])
        self.mode_combo.setMinimumWidth(84)
        self.mode_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.enable = QtWidgets.QCheckBox("Enable")
        self.contact_a = QtWidgets.QSpinBox()
        self.contact_a.setRange(1, 8)
        self.contact_a.setValue(3)
        self.contact_a.setFixedWidth(48)
        self.contact_b = QtWidgets.QSpinBox()
        self.contact_b.setRange(1, 8)
        self.contact_b.setValue(4)
        self.contact_b.setFixedWidth(48)
        controls.addWidget(self.mode_combo)
        controls.addWidget(self.enable)
        controls.addSpacing(14)
        controls.addWidget(QtWidgets.QLabel("A:"))
        controls.addWidget(self.contact_a)
        controls.addWidget(QtWidgets.QLabel("B:"))
        controls.addWidget(self.contact_b)
        controls.addStretch(1)
        root.addLayout(controls)

        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(False, False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setBackground(theme["panel"])
        # Fixed, compact height (matches the monopolar fixed-height approach).
        self.plot.setFixedHeight(62)
        self.curve = self.plot.plot(pen=pg.mkPen(theme["curve"], width=1.4))
        self.image = pg.ImageItem()
        self.plot.addItem(self.image)
        self.image.hide()
        root.addWidget(self.plot)
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        self.setStyleSheet(
            f"""
            QGroupBox {{
                border: 2px solid {theme['border']};
                border-radius: 10px;
                margin-top: 8px;
                padding-top: 6px;
                background: {theme['panel']};
                color: {theme['accent']};
                font-weight: 700;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
            }}
            """
        )

    def _on_mode_changed(self, text: str) -> None:
        self.mode = "Spectrogram" if text == "Spec" else text

    def set_mode(self, mode: str) -> None:
        self.mode = "Spectrogram" if mode == "Spec" else mode

    def set_raw(self, t: np.ndarray, signal: np.ndarray) -> None:
        self.plot.setLogMode(False, False)
        self.image.hide()
        self.curve.show()
        self.curve.setData(_downsample_for_display(t), _downsample_for_display(signal))
        if len(signal) > 0:
            lo = float(np.percentile(signal, 2))
            hi = float(np.percentile(signal, 98))
            pad = max(1.0, 0.15 * (hi - lo if hi > lo else 1.0))
            self.plot.setXRange(float(t[0]), float(t[-1]) if len(t) else 5.0, padding=0)
            self.plot.setYRange(lo - pad, hi + pad, padding=0)

    def set_psd(self, f: np.ndarray, pxx: np.ndarray) -> None:
        self.plot.setLogMode(False, False)
        self.image.hide()
        self.curve.show()
        pxx_db = 10.0 * np.log10(np.maximum(pxx, 1e-12))
        self.curve.setData(_downsample_for_display(f), _downsample_for_display(pxx_db))
        if len(pxx_db) > 0:
            lo = float(np.percentile(pxx_db, 5))
            hi = float(np.percentile(pxx_db, 98))
            pad = max(1.0, 0.10 * (hi - lo if hi > lo else 1.0))
            self.plot.setXRange(0, 100, padding=0)
            self.plot.setYRange(lo - pad, hi + pad, padding=0)

    def set_spec(self, f: np.ndarray, t: np.ndarray, sxx_db: np.ndarray) -> None:
        self.plot.setLogMode(False, False)
        self.curve.hide()
        self.image.show()
        self.image.setImage(sxx_db, autoLevels=True)
        self.image.setLookupTable(self.lut)
        if len(t) > 1 and len(f) > 1:
            self.image.setRect(QtCore.QRectF(float(t[0]), float(f[0]), float(t[-1] - t[0]), float(f[-1] - f[0])))
            self.plot.setXRange(float(t[0]), float(t[-1]), padding=0)
            self.plot.setYRange(float(f[0]), float(f[-1]), padding=0)


class UnifiedDBSWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NODES — Unified DBS Simulator")
        self.resize(1900, 1040)

        self.model = DBSArrayModel(seed=42)
        self.fs = self.model.fs
        self.chunk_samples = round(self.fs * 0.1)  # ~100 ms update, tied to fs
        self.window_s = 5.0
        self.spec_window_s = 15.0
        self.max_samples = int(self.fs * self.window_s)
        self.max_spec_samples = int(self.fs * self.spec_window_s)
        self.tick = 0

        self.channels = {
            "left_paddle": [f"left_paddle_{i}" for i in range(1, 9)],
            "right_paddle": [f"right_paddle_{i}" for i in range(1, 9)],
            "left_depth": [f"left_depth_{i}" for i in range(1, 9)],
            "right_depth": [f"right_depth_{i}" for i in range(1, 9)],
        }

        self.buffers: Dict[str, np.ndarray] = {
            ch: np.zeros(0, dtype=np.float32)
            for group in self.channels.values()
            for ch in group
        }
        self.bipolar_buffers: Dict[str, np.ndarray] = {
            "left_depth": np.zeros(0, dtype=np.float32),
            "right_depth": np.zeros(0, dtype=np.float32),
            "left_paddle": np.zeros(0, dtype=np.float32),
            "right_paddle": np.zeros(0, dtype=np.float32),
        }
        self.spec_buffers: Dict[str, np.ndarray] = {
            ch: np.zeros(0, dtype=np.float32)
            for group in self.channels.values()
            for ch in group
        }
        self.bipolar_spec_buffers: Dict[str, np.ndarray] = {
            "left_depth": np.zeros(0, dtype=np.float32),
            "right_depth": np.zeros(0, dtype=np.float32),
            "left_paddle": np.zeros(0, dtype=np.float32),
            "right_paddle": np.zeros(0, dtype=np.float32),
        }

        self.left = SideControl()
        self.right = SideControl()
        self.left_frequency_hz = 130.0
        self.right_frequency_hz = 130.0
        self.state = "Rest"

        # Fixed channel order written to recordings (monopolar only).
        self.channel_order = [
            ch for key in ("left_paddle", "right_paddle", "left_depth", "right_depth")
            for ch in self.channels[key]
        ]
        # Recording state (background Parquet writer; see simulator/recorder.py).
        self._recorder: ParquetRecorder | None = None
        self._recording = False
        self._record_samples = 0
        self._record_duration_s = 0
        self._rec_poll: QtCore.QTimer | None = None

        self._build_ui()
        self._apply_style()
        self._style_transport_buttons()

        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.end_button.setEnabled(False)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        # self.timer.start(100)
        # self._start_stream()

    def _build_ui(self) -> None:
        # Scrollable content so the dense layout stays usable at any window size.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)
        cw = QtWidgets.QWidget()
        # Minimum width keeps columns from squishing (horizontal scroll if
        # narrower); plot heights are fixed, so vertical scroll appears when the
        # window is shorter than the content.
        cw.setMinimumSize(1500, 820)
        scroll.setWidget(cw)
        root = QtWidgets.QHBoxLayout(cw)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # Left control rail
        ctrl = QtWidgets.QFrame()
        ctrl.setFixedWidth(390)
        ctrl_l = QtWidgets.QVBoxLayout(ctrl)
        ctrl_l.setContentsMargins(8, 8, 8, 8)
        ctrl_l.setSpacing(6)

        button_row = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start")
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.end_button = QtWidgets.QPushButton("End")
        self.start_button.setObjectName("startButton")
        self.pause_button.setObjectName("pauseButton")
        self.end_button.setObjectName("endButton")
        for button in (self.start_button, self.pause_button, self.end_button):
            button.setMinimumHeight(36)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.pause_button)
        button_row.addWidget(self.end_button)
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.end_button.setEnabled(False)
        ctrl_l.addLayout(button_row)

        self.start_button.clicked.connect(self._start_stream)
        self.pause_button.clicked.connect(self._pause_stream)
        self.end_button.clicked.connect(self._end_stream)

        ctrl_l.addWidget(self._build_state_section())
        ctrl_l.addWidget(self._build_stim_section())
        ctrl_l.addWidget(self._build_coherence_section())

        bipolar_box = QtWidgets.QGroupBox("Bipolar Channels")
        bp_l = QtWidgets.QVBoxLayout(bipolar_box)
        bp_l.setContentsMargins(6, 10, 6, 6)
        bp_l.setSpacing(4)
        self.bp_panels = {
            "left_depth": BipolarPanel("Left Depth Bipolar", "left", "depth", GROUP_THEMES["left_depth"]),
            "right_depth": BipolarPanel("Right Depth Bipolar", "right", "depth", GROUP_THEMES["right_depth"]),
            "left_paddle": BipolarPanel("Left Paddle Bipolar", "left", "paddle", GROUP_THEMES["left_paddle"]),
            "right_paddle": BipolarPanel("Right Paddle Bipolar", "right", "paddle", GROUP_THEMES["right_paddle"]),
        }
        for key in ["left_paddle", "right_paddle", "left_depth", "right_depth"]:
            bp_l.addWidget(self.bp_panels[key])

        # Bipolar moves up to fill the space the status log used to occupy.
        ctrl_l.addWidget(bipolar_box)
        ctrl_l.addStretch(1)

        # Right plotting area
        plot_area = QtWidgets.QWidget()
        main_v = QtWidgets.QVBoxLayout(plot_area)

        group_widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(group_widget)
        self.group_panels = {
            "left_paddle": GroupPanel("Left Paddle (8 monopolar)", self.channels["left_paddle"], GROUP_THEMES["left_paddle"]),
            "right_paddle": GroupPanel("Right Paddle (8 monopolar)", self.channels["right_paddle"], GROUP_THEMES["right_paddle"]),
            "left_depth": GroupPanel("Left Depth (8 monopolar)", self.channels["left_depth"], GROUP_THEMES["left_depth"]),
            "right_depth": GroupPanel("Right Depth (8 monopolar)", self.channels["right_depth"], GROUP_THEMES["right_depth"]),
        }
        grid.addWidget(self.group_panels["left_paddle"], 0, 0)
        grid.addWidget(self.group_panels["right_paddle"], 0, 1)
        grid.addWidget(self.group_panels["left_depth"], 1, 0)
        grid.addWidget(self.group_panels["right_depth"], 1, 1)

        # Status / text log placed below the LEFT monopolar column.
        self.status = QtWidgets.QLabel("Ready")
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(42)
        grid.addWidget(self.status, 2, 0)

        # Recording controls in the empty space below the RIGHT monopolar column.
        grid.addWidget(self._build_recording_bar(), 2, 1)

        main_v.addWidget(group_widget)
        main_v.addStretch(1)

        root.addWidget(ctrl)
        root.addWidget(plot_area, stretch=1)

        for key, panel in self.group_panels.items():
            panel.mode_combo.currentTextChanged.connect(lambda _mode, k=key: self._redraw_group(k))
        for key, panel in self.bp_panels.items():
            panel.mode_combo.currentTextChanged.connect(lambda _mode, k=key: self._redraw_bipolar(k))

        self.left_freq_slider.valueChanged.connect(self._on_left_freq_change)
        self.right_freq_slider.valueChanged.connect(self._on_right_freq_change)

    def _build_state_section(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Behavioral State")
        box.setStyleSheet("QGroupBox { margin-top: 14px; padding-top: 12px; }")
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(8, 16, 8, 6)
        lay.setSpacing(10)
        self.state_group = QtWidgets.QButtonGroup(self)
        self.state_buttons: Dict[str, QtWidgets.QRadioButton] = {}
        for state in BEHAVIORAL_STATES:
            radio = QtWidgets.QRadioButton(state)
            radio.setChecked(state == self.state)
            self.state_group.addButton(radio)
            self.state_buttons[state] = radio
            lay.addWidget(radio)
            radio.toggled.connect(lambda checked, s=state: self._on_state_change(s, checked))
        lay.addStretch(1)
        return box

    def _on_state_change(self, state: str, checked: bool) -> None:
        if not checked:
            return
        self.state = state
        # State scalars ramp in over the next ticks; reflect the request immediately.
        self.status.setText(f"Behavioral state: {state}")

    # ---- recording controls (below the right monopolar column) ----
    def _build_recording_bar(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        box.setMaximumHeight(42)
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)

        lay.addWidget(QtWidgets.QLabel("Record"))
        self.rec_duration = QtWidgets.QSpinBox()
        self.rec_duration.setRange(0, 36000)
        self.rec_duration.setValue(0)
        self.rec_duration.setSuffix(" s")
        self.rec_duration.setFixedWidth(86)
        self.rec_duration.setToolTip("Auto-stop after this many seconds (0 = manual stop)")
        lay.addWidget(self.rec_duration)

        self.rec_button = QtWidgets.QPushButton("● REC")
        self.rec_button.setCheckable(True)
        self.rec_button.setEnabled(False)
        self.rec_button.setFixedWidth(86)
        self.rec_button.setToolTip("Start/stop recording (available while streaming)")
        self.rec_button.toggled.connect(self._on_record_toggled)
        lay.addWidget(self.rec_button)

        self.rec_clock = QtWidgets.QLabel("00:00")
        self.rec_clock.setFixedWidth(48)
        # Name real fixed-width fonts (per platform) rather than the generic
        # "monospace" alias, which triggers a slow Qt font-alias lookup/warning.
        clock_font = self.rec_clock.font()
        clock_font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        clock_font.setFamilies(["Menlo", "Monaco", "Courier New", "DejaVu Sans Mono"])
        self.rec_clock.setFont(clock_font)
        lay.addWidget(self.rec_clock)

        self.rec_indicator = QtWidgets.QLabel("●")
        self.rec_indicator.setStyleSheet("color: #555;")
        self.rec_indicator.setToolTip("Recording indicator")
        lay.addWidget(self.rec_indicator)
        lay.addStretch(1)
        return box

    def _on_record_toggled(self, checked: bool) -> None:
        if checked:
            if not self.timer.isActive():
                # Recording requires an active stream; revert the toggle.
                self.rec_button.setChecked(False)
                self.status.setText("Start streaming before recording.")
                return
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        ts = datetime.now()
        path = os.path.join(RECORDINGS_DIR, f"nodes_{ts:%Y%m%d_%H%M%S}.parquet")
        self._recorder = ParquetRecorder(path, self.channel_order, self.fs, ts.isoformat())
        self._recorder.start()
        self._recording = True
        self._record_samples = 0
        self._record_duration_s = int(self.rec_duration.value())
        self.rec_duration.setEnabled(False)
        self.rec_clock.setText("00:00")
        self.rec_indicator.setStyleSheet("color: #e02424;")
        self.rec_button.setText("■ STOP")
        self.status.setText(f"Recording → {os.path.basename(path)}")

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.rec_duration.setEnabled(True)
        self.rec_indicator.setStyleSheet("color: #555;")
        self.rec_button.setText("● REC")
        rec = self._recorder
        if rec is None:
            return
        rec.stop()  # finalize + flush + close on the worker thread
        self.status.setText("Saving recording…")
        self._rec_poll = QtCore.QTimer(self)
        self._rec_poll.timeout.connect(lambda: self._check_recording_saved(rec))
        self._rec_poll.start(150)

    def _check_recording_saved(self, rec: ParquetRecorder) -> None:
        if rec.is_running():
            return
        if self._rec_poll is not None:
            self._rec_poll.stop()
            self._rec_poll = None
        if rec.error:
            self.status.setText(f"Recording save FAILED: {rec.error}")
        else:
            self.status.setText(f"Saved {rec.rows_written} samples → {rec.saved_path}")
        if self._recorder is rec:
            self._recorder = None

    def _record_chunk(self, out: Dict[str, np.ndarray]) -> None:
        if not self._recording or self._recorder is None:
            return
        n = len(out[self.channel_order[0]])
        t = (self._record_samples + np.arange(n, dtype=np.float64)) / self.fs
        self._record_samples += n
        data = np.stack([out[ch] for ch in self.channel_order], axis=1).astype(np.float32)
        stim = (
            bool(self.left.enabled), int(self.left.contact_index + 1),
            float(self.left.amplitude_ma), float(self.left_frequency_hz),
            bool(self.right.enabled), int(self.right.contact_index + 1),
            float(self.right.amplitude_ma), float(self.right_frequency_hz),
        )
        self._recorder.submit(t, data, stim, self.state)

        elapsed = self._record_samples / self.fs
        self.rec_clock.setText(f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}")
        if self._record_duration_s > 0 and elapsed >= self._record_duration_s:
            self.rec_button.setChecked(False)  # triggers _stop_recording

    def _build_coherence_section(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Network Coherence (MSC)")
        box.setStyleSheet("QGroupBox { margin-top: 14px; padding-top: 12px; }")
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(8, 14, 8, 6)
        lay.setSpacing(4)

        table = QtWidgets.QTableWidget(len(COHERENCE_PAIRS), len(COHERENCE_VIEW_BANDS))
        table.setHorizontalHeaderLabels([b[0] for b in COHERENCE_VIEW_BANDS])
        table.setVerticalHeaderLabels([p[0] for p in COHERENCE_PAIRS])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.setFixedHeight(112)
        for r in range(len(COHERENCE_PAIRS)):
            for c in range(len(COHERENCE_VIEW_BANDS)):
                item = QtWidgets.QTableWidgetItem("--")
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                table.setItem(r, c, item)
        self.coh_table = table
        lay.addWidget(table)
        return box

    @staticmethod
    def _coherence_color(value: float) -> QtGui.QColor:
        v = max(0.0, min(1.0, value))
        # dark gray (low) -> bright green (high)
        return QtGui.QColor(int(28 + v * 24), int(28 + v * 188), int(28 + v * 70))

    def _update_coherence_table(self) -> None:
        for row, (_label, ch_a, ch_b) in enumerate(COHERENCE_PAIRS):
            xa = self.spec_buffers.get(ch_a)
            xb = self.spec_buffers.get(ch_b)
            n = 0 if xa is None or xb is None else min(len(xa), len(xb))
            if n < 2048:
                for col in range(len(COHERENCE_VIEW_BANDS)):
                    self.coh_table.item(row, col).setText("--")
                    self.coh_table.item(row, col).setBackground(self._coherence_color(0.0))
                    self.coh_table.item(row, col).setForeground(QtGui.QBrush(QtGui.QColor("#d5d9e0")))
                continue
            nperseg = min(2048, n)
            f, cxy = coherence(xa[-n:], xb[-n:], fs=self.fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2)
            for col, (_name, lo, hi) in enumerate(COHERENCE_VIEW_BANDS):
                mask = (f >= lo) & (f <= hi)
                val = float(np.mean(cxy[mask])) if np.any(mask) else 0.0
                item = self.coh_table.item(row, col)
                item.setText(f"{val:.2f}")
                item.setBackground(self._coherence_color(val))
                item.setForeground(QtGui.QBrush(QtGui.QColor("#ffffff")))

    def _build_stim_section(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Stimulation")
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        self.left_freq_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.left_freq_slider.setRange(10, 200)
        self.left_freq_slider.setValue(130)
        self.left_freq_slider.setFixedWidth(118)
        self.left_freq_value = QtWidgets.QLabel("130")
        self.left_freq_value.setMinimumWidth(28)

        self.right_freq_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.right_freq_slider.setRange(10, 200)
        self.right_freq_slider.setValue(130)
        self.right_freq_slider.setFixedWidth(118)
        self.right_freq_value = QtWidgets.QLabel("130")
        self.right_freq_value.setMinimumWidth(28)

        freq_row = QtWidgets.QHBoxLayout()
        freq_row.setSpacing(6)
        freq_row.addWidget(QtWidgets.QLabel("L Hz"))
        freq_row.addWidget(self.left_freq_slider)
        freq_row.addWidget(self.left_freq_value)
        freq_row.addSpacing(8)
        freq_row.addWidget(QtWidgets.QLabel("R Hz"))
        freq_row.addWidget(self.right_freq_slider)
        freq_row.addWidget(self.right_freq_value)
        freq_row.addStretch(1)
        lay.addLayout(freq_row)

        lay.addLayout(self._build_stim_row("L", "left"))
        lay.addLayout(self._build_stim_row("R", "right"))
        return box

    def _build_stim_row(self, label: str, side: str) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        side_label = QtWidgets.QLabel(label)
        side_label.setMinimumWidth(14)

        enabled = QtWidgets.QCheckBox("On")
        contact = QtWidgets.QComboBox()
        contact.addItems([str(i) for i in range(1, 9)])
        contact.setMinimumWidth(64)
        contact.setMaximumWidth(72)
        contact.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)

        amp_minus = QtWidgets.QToolButton()
        amp_minus.setText("-")
        amp_minus.setAutoRaise(False)
        amp_minus.setFixedWidth(26)
        amp_plus = QtWidgets.QToolButton()
        amp_plus.setText("+")
        amp_plus.setAutoRaise(False)
        amp_plus.setFixedWidth(26)

        amp = QtWidgets.QDoubleSpinBox()
        amp.setRange(0.0, 4.0)
        amp.setSingleStep(0.1)
        amp.setDecimals(2)
        amp.setFixedWidth(102)
        amp.setSuffix(" mA")

        amp_minus.clicked.connect(lambda: amp.setValue(max(0.0, amp.value() - 0.1)))
        amp_plus.clicked.connect(lambda: amp.setValue(min(4.0, amp.value() + 0.1)))

        row.addWidget(side_label)
        row.addWidget(enabled)
        row.addWidget(QtWidgets.QLabel("Ch"))
        row.addWidget(contact)
        row.addWidget(amp_minus)
        row.addWidget(amp_plus)
        row.addWidget(amp)
        row.addStretch(1)

        setattr(self, f"{side}_enabled", enabled)
        setattr(self, f"{side}_contact", contact)
        setattr(self, f"{side}_amp", amp)

        enabled.toggled.connect(lambda _v, s=side: self._sync_side(s))
        contact.currentIndexChanged.connect(lambda _v, s=side: self._sync_side(s))
        amp.valueChanged.connect(lambda _v, s=side: self._sync_side(s))
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #0b0e14; color: #d5d9e0; }
            QGroupBox { border: 1px solid #2a3242; border-radius: 8px; margin-top: 8px; padding-top: 8px; font-weight: 600; }
            QLabel, QCheckBox, QComboBox, QDoubleSpinBox, QToolButton { color: #d5d9e0; }
            QComboBox, QDoubleSpinBox { background: #151a24; border: 1px solid #2a3242; border-radius: 6px; padding: 4px; }
            QToolButton { background: #151a24; border: 1px solid #2a3242; border-radius: 4px; padding: 2px 4px; font-weight: 700; }
            QSlider::groove:horizontal { background: #2a3242; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #5ca7ff; width: 14px; border-radius: 7px; margin: -4px 0; }
            QPushButton#startButton { background: #1f8f4a; color: white; border-radius: 8px; }
            QPushButton#pauseButton { background: #d97706; color: white; border-radius: 8px; }
            QPushButton#endButton { background: #dc2626; color: white; border-radius: 8px; }
            QPushButton#startButton:hover, QPushButton#pauseButton:hover, QPushButton#endButton:hover { border: 1px solid #ffffff55; }
            """
        )

    def _style_transport_buttons(self) -> None:
        self.start_button.setStyleSheet("font-weight: 800; font-size: 12px; padding: 8px 10px;")
        self.pause_button.setStyleSheet("font-weight: 800; font-size: 12px; padding: 8px 10px;")
        self.end_button.setStyleSheet("font-weight: 800; font-size: 12px; padding: 8px 10px;")

    def _on_left_freq_change(self, value: int) -> None:
        self.left_frequency_hz = float(value)
        self.left_freq_value.setText(str(value))

    def _on_right_freq_change(self, value: int) -> None:
        self.right_frequency_hz = float(value)
        self.right_freq_value.setText(str(value))

    def _sync_side(self, side: str) -> None:
        enabled = getattr(self, f"{side}_enabled").isChecked()
        contact_idx = getattr(self, f"{side}_contact").currentIndex()
        amp = float(getattr(self, f"{side}_amp").value())

        state = self.left if side == "left" else self.right
        state.enabled = enabled and amp > 0
        state.contact_index = int(contact_idx)
        state.amplitude_ma = amp

        # Prevent selecting stimulated depth contact in depth bipolar panel.
        panel_key = f"{side}_depth"
        bp = self.bp_panels[panel_key]
        if state.enabled:
            stim_contact = state.contact_index + 1
            if bp.contact_a.value() == stim_contact:
                bp.contact_a.setValue(max(1, stim_contact - 1))
            if bp.contact_b.value() == stim_contact:
                bp.contact_b.setValue(min(8, stim_contact + 1 if stim_contact < 8 else stim_contact - 1))

        if self.timer.isActive():
            self._redraw_all(force_heavy=False)
        else:
            self._redraw_all(force_heavy=True)

    def _build_stim_commands(self) -> Dict[str, StimulationCommand]:
        cmd: Dict[str, StimulationCommand] = {}
        if self.left.enabled:
            cmd["left"] = StimulationCommand("left", self.left.contact_index, self.left.amplitude_ma, self.left_frequency_hz)
        if self.right.enabled:
            cmd["right"] = StimulationCommand("right", self.right.contact_index, self.right.amplitude_ma, self.right_frequency_hz)
        return cmd

    def _update_monopolar_buffers(self, out: Dict[str, np.ndarray]) -> None:
        for ch, chunk in out.items():
            sig = np.concatenate([self.buffers[ch], chunk.astype(np.float32)])
            if len(sig) > self.max_samples:
                sig = sig[-self.max_samples :]
            self.buffers[ch] = sig

            spec_sig = np.concatenate([self.spec_buffers[ch], chunk.astype(np.float32)])
            if len(spec_sig) > self.max_spec_samples:
                spec_sig = spec_sig[-self.max_spec_samples :]
            self.spec_buffers[ch] = spec_sig

    def _append_bipolar_buffer(self, key: str, chunk: np.ndarray) -> None:
        sig = np.concatenate([self.bipolar_buffers[key], chunk.astype(np.float32)])
        if len(sig) > self.max_samples:
            sig = sig[-self.max_samples :]
        self.bipolar_buffers[key] = sig

        spec_sig = np.concatenate([self.bipolar_spec_buffers[key], chunk.astype(np.float32)])
        if len(spec_sig) > self.max_spec_samples:
            spec_sig = spec_sig[-self.max_spec_samples :]
        self.bipolar_spec_buffers[key] = spec_sig

    def _render_bipolar_panel(self, key: str, force_heavy: bool) -> None:
        panel = self.bp_panels[key]
        signal = self.bipolar_spec_buffers[key] if panel.mode == "Spectrogram" else self.bipolar_buffers[key]
        if len(signal) < 10:
            return
        mode = panel.mode
        if mode == "Raw":
            t = np.arange(len(signal), dtype=np.float32) / self.fs
            panel.set_raw(t, signal)
        elif mode == "PSD":
            if not force_heavy:
                return
            nperseg = min(512, len(signal))
            f, pxx = welch(signal, fs=self.fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2)
            m = f <= 100
            panel.set_psd(f[m], pxx[m])
        else:
            if not force_heavy:
                return
            nperseg = 256
            noverlap = 192
            if len(signal) < nperseg:
                return
            f, t, sxx = spectrogram(signal, fs=self.fs, window="hann", nperseg=nperseg, noverlap=noverlap, scaling="density", mode="psd")
            m = f <= 100
            sxx_db = 10.0 * np.log10(sxx[m] + 1e-12)
            panel.set_spec(f[m], t, sxx_db)

    def _redraw_group(self, key: str) -> None:
        self._render_group(key, force_heavy=True)

    def _redraw_bipolar(self, key: str) -> None:
        self._render_bipolar_panel(key, force_heavy=True)

    def _redraw_all(self, force_heavy: bool = False) -> None:
        group_keys = ["left_paddle", "right_paddle", "left_depth", "right_depth"]
        bipolar_keys = ["left_depth", "right_depth", "left_paddle", "right_paddle"]

        if force_heavy:
            for key in group_keys:
                self._render_group(key, force_heavy=True)
            for key in bipolar_keys:
                self._render_bipolar_panel(key, force_heavy=True)
            self._update_coherence_table()
            return

        # Lightweight mode: update only half of groups and half of bipolars each tick.
        g_phase = self.tick % 2
        b_phase = (self.tick // 2) % 2
        for i, key in enumerate(group_keys):
            if i % 2 == g_phase:
                self._render_group(key, force_heavy=False)
        for i, key in enumerate(bipolar_keys):
            if i % 2 == b_phase:
                self._render_bipolar_panel(key, force_heavy=False)

    def _render_group(self, key: str, force_heavy: bool) -> None:
        panel = self.group_panels[key]
        mode = panel.mode
        for ch in self.channels[key]:
            sig = self.spec_buffers[ch] if mode == "Spectrogram" else self.buffers[ch]
            if len(sig) < 10:
                continue
            if mode == "Raw":
                t = np.arange(len(sig), dtype=np.float32) / self.fs
                panel.set_raw(ch, t, sig)
            elif mode == "PSD":
                if not force_heavy:
                    return
                nperseg = min(512, len(sig))
                f, pxx = welch(sig, fs=self.fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2)
                m = f <= 100
                panel.set_psd(ch, f[m], pxx[m])
            else:
                if not force_heavy:
                    return
                nperseg = 256
                noverlap = 192
                if len(sig) < nperseg:
                    continue
                f, t, sxx = spectrogram(sig, fs=self.fs, window="hann", nperseg=nperseg, noverlap=noverlap, scaling="density", mode="psd")
                m = f <= 100
                sxx_db = 10.0 * np.log10(sxx[m] + 1e-12)
                panel.set_spec(ch, f[m], t, sxx_db)

    def _update_bipolar(self, out_chunk: Dict[str, np.ndarray]) -> None:
        parts = []
        bipolar_chunks: Dict[str, np.ndarray | None] = {}
        for key, panel in self.bp_panels.items():
            if not panel.enable.isChecked():
                bipolar_chunks[key] = None
                self.bipolar_buffers[key] = np.zeros(0, dtype=np.float32)
                self.bipolar_spec_buffers[key] = np.zeros(0, dtype=np.float32)
                continue

            side = panel.side
            lead_kind = panel.lead_kind
            a = panel.contact_a.value() - 1
            b = panel.contact_b.value() - 1
            if a == b:
                bipolar_chunks[key] = None
                self.bipolar_buffers[key] = np.zeros(0, dtype=np.float32)
                self.bipolar_spec_buffers[key] = np.zeros(0, dtype=np.float32)
                continue

            # block selecting stimulated contact for depth bipolar
            state = self.left if side == "left" else self.right
            if lead_kind == "depth" and state.enabled and state.contact_index in {a, b}:
                bipolar_chunks[key] = None
                self.bipolar_buffers[key] = np.zeros(0, dtype=np.float32)
                self.bipolar_spec_buffers[key] = np.zeros(0, dtype=np.float32)
                parts.append(f"{key}: blocked (uses stim contact)")
                continue

            selection = BipolarSelection(side=side, lead_kind=lead_kind, contact_a=a, contact_b=b, normalize=True)
            geom = self.model.config.lead_geometries[(side, lead_kind)]
            sig, meta = convert_bipolar(out_chunk, selection, geom)
            bipolar_chunks[key] = sig
            parts.append(f"{key}:{a+1}-{b+1}")

        for key, sig in bipolar_chunks.items():
            if sig is None:
                continue
            self._append_bipolar_buffer(key, sig)

        if parts:
            self.status.setText(
                f"5s rolling | fL={self.left_frequency_hz:.1f} Hz fR={self.right_frequency_hz:.1f} Hz | "
                f"L stim={'on' if self.left.enabled else 'off'} c{self.left.contact_index+1} {self.left.amplitude_ma:.2f}mA | "
                f"R stim={'on' if self.right.enabled else 'off'} c{self.right.contact_index+1} {self.right.amplitude_ma:.2f}mA\n"
                + "Bipolar: " + ", ".join(parts)
            )
        else:
            self.status.setText(
                f"5s rolling | fL={self.left_frequency_hz:.1f} Hz fR={self.right_frequency_hz:.1f} Hz | "
                f"L stim={'on' if self.left.enabled else 'off'} c{self.left.contact_index+1} {self.left.amplitude_ma:.2f}mA | "
                f"R stim={'on' if self.right.enabled else 'off'} c{self.right.contact_index+1} {self.right.amplitude_ma:.2f}mA"
            )

    def _on_tick(self) -> None:
        if not self.timer.isActive():
            return
        self.tick += 1
        out = self.model.simulate_chunk(
            stim_commands=self._build_stim_commands(),
            n_samples=self.chunk_samples,
            include_subharmonics=False,
            state=self.state,
        )
        self._record_chunk(out)  # cheap queue push; no-op when not recording
        self._update_monopolar_buffers(out)
        self._update_bipolar(out)
        heavy = (self.tick % 10 == 0)
        self._redraw_all(force_heavy=heavy)

    def _start_stream(self) -> None:
        if not self.timer.isActive():
            self.timer.start(100)
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.end_button.setEnabled(True)
        self.rec_button.setEnabled(True)  # recording allowed while streaming
        if not self._recording:
            self.status.setText("Streaming started")
        self._on_tick()

    def _pause_stream(self) -> None:
        self.timer.stop()
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        # Recording rides on ticks, so it pauses automatically; keep REC enabled
        # only if a recording is in progress (so the user can stop it).
        self.rec_button.setEnabled(self._recording)
        self.status.setText("Streaming paused" + (" (recording paused)" if self._recording else ""))

    def _end_stream(self) -> None:
        # Finalize and save any in-progress recording before resetting.
        if self._recording:
            self.rec_button.setChecked(False)  # triggers _stop_recording (saves)
        self.rec_button.setEnabled(False)
        self.timer.stop()
        self.model.reset()
        for key in self.buffers:
            self.buffers[key] = np.zeros(0, dtype=np.float32)
            self.spec_buffers[key] = np.zeros(0, dtype=np.float32)
        for key in self.bipolar_buffers:
            self.bipolar_buffers[key] = np.zeros(0, dtype=np.float32)
            self.bipolar_spec_buffers[key] = np.zeros(0, dtype=np.float32)
        for key, panel in self.group_panels.items():
            for ch in self.channels[key]:
                panel.curves[ch].setData([], [])
                panel.images[ch].hide()
        for panel in self.bp_panels.values():
            panel.curve.setData([], [])
            panel.image.hide()
        self.tick = 0
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.end_button.setEnabled(False)
        self.status.setText("Streaming ended and reset")


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("NODES")
    w = UnifiedDBSWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
