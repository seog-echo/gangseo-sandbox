from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except ImportError:
    np = None

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
except ImportError:
    nidaqmx = None
    AcquisitionType = None
    TerminalConfiguration = None

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg


DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")
MOCK_DATA_ROOT = Path(__file__).with_name("mock_data") / "mock_data"
IPG_SAMPLE_RATE_HZ = 1024
IPG_ADC_LSB_UV = 0.6
REPLAY_FORCED_AO_SAMPLE_RATE_HZ = 512
MAX_AO_WAVEFORM_BUFFER_SECONDS = 5.0
MAX_AO_LOW_FREQ_FULL_PERIOD_MAX_SECONDS = 40.0
MAX_IPG_CHANNELS = 10
MAX_STIM_CHANNELS = 8
MAX_NI_AI_CHANNELS = 4
MAX_NI_AO_CHANNELS = 4
MANUAL_REPORT_TIMEOUT_S = 300
AUTO_REPORT_TIMEOUT_S = 600


AUTO_IPG_TEST_CONFIG_VERSION = 2


def default_auto_ipg_test_config() -> Dict[str, Any]:
    return {
        "version": AUTO_IPG_TEST_CONFIG_VERSION,
        "enabled": False,
        "session_name": "auto_ipg_test",
        "mode": "frequency",
        "inter_block_delay_s": 1.0,
        "mapping": {str(channel): [] for channel in range(1, MAX_NI_AO_CHANNELS + 1)},
        "ni_ai_reference": {
            "enabled": False,
            "mapping": [
                {
                    "ni_ai_channel": 1,
                    "ipg_rec_channels": [1],
                }
            ],
        },
        "frequency_sweep": {
            "fixed_amplitude_v": 2.0,
            "blocks": [
                {
                    "duration_s": 30.0,
                    "frequency_hz": 10.0,
                    "ao_values": {str(channel): 10.0 for channel in range(1, MAX_NI_AO_CHANNELS + 1)},
                }
            ],
        },
        "amplitude_sweep": {
            "fixed_frequency_hz": 10.0,
            "blocks": [
                {
                    "duration_s": 30.0,
                    "amplitude_v": 2.0,
                    "ao_values": {str(channel): 2.0 for channel in range(1, MAX_NI_AO_CHANNELS + 1)},
                }
            ],
        },
        "analysis": {
            "voltage_divider_ratio": 1000.0,
            "trim_edge_seconds": 2.0,
            "ipg_adc_lsb_uV": IPG_ADC_LSB_UV,
        },
    }


def merge_auto_ipg_test_config(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = default_auto_ipg_test_config()
    if not isinstance(payload, dict):
        return merged

    merged["enabled"] = bool(payload.get("enabled", merged["enabled"]))
    merged["session_name"] = str(payload.get("session_name", merged["session_name"]))
    merged["mode"] = str(payload.get("mode", merged["mode"]))
    merged["inter_block_delay_s"] = float(payload.get("inter_block_delay_s", merged["inter_block_delay_s"]))

    mapping = payload.get("mapping", {})
    if isinstance(mapping, dict):
        for ao_channel in range(1, MAX_NI_AO_CHANNELS + 1):
            raw = mapping.get(str(ao_channel), [])
            cleaned = []
            if isinstance(raw, list):
                for value in raw:
                    try:
                        rec_channel = int(value)
                    except Exception:
                        continue
                    if 1 <= rec_channel <= MAX_IPG_CHANNELS:
                        cleaned.append(rec_channel)
            merged["mapping"][str(ao_channel)] = sorted(set(cleaned))

    ni_ai_reference = payload.get("ni_ai_reference", {})
    if isinstance(ni_ai_reference, dict):
        merged["ni_ai_reference"]["enabled"] = bool(
            ni_ai_reference.get("enabled", merged["ni_ai_reference"]["enabled"])
        )
        raw_mappings = ni_ai_reference.get("mapping", [])
        if isinstance(raw_mappings, list):
            cleaned_mappings: List[Dict[str, Any]] = []
            # Backward-compatibility: internal representation is 1..MAX where 1 maps to AI0.
            # If a legacy/custom config includes channel 0, treat the mapping as zero-based.
            raw_channel_values: List[int] = []
            for entry in raw_mappings:
                if not isinstance(entry, dict):
                    continue
                try:
                    raw_channel_values.append(int(entry.get("ni_ai_channel", 1)))
                except Exception:
                    continue
            treat_as_zero_based = any(value == 0 for value in raw_channel_values)
            for entry in raw_mappings:
                if not isinstance(entry, dict):
                    continue
                try:
                    ni_ai_channel = int(entry.get("ni_ai_channel", 1))
                except Exception:
                    continue
                if treat_as_zero_based:
                    ni_ai_channel += 1
                if not (1 <= ni_ai_channel <= MAX_NI_AI_CHANNELS):
                    continue
                raw_ipg_channels = entry.get("ipg_rec_channels", [])
                cleaned_ipg_channels: List[int] = []
                if isinstance(raw_ipg_channels, list):
                    for value in raw_ipg_channels:
                        try:
                            rec_channel = int(value)
                        except Exception:
                            continue
                        if 1 <= rec_channel <= MAX_IPG_CHANNELS:
                            cleaned_ipg_channels.append(rec_channel)
                cleaned_mappings.append(
                    {
                        "ni_ai_channel": ni_ai_channel,
                        "ipg_rec_channels": sorted(set(cleaned_ipg_channels)),
                    }
                )
            if cleaned_mappings:
                merged["ni_ai_reference"]["mapping"] = cleaned_mappings

    for section_name in ("frequency_sweep", "amplitude_sweep"):
        section_payload = payload.get(section_name, {})
        if not isinstance(section_payload, dict):
            continue
        section = merged[section_name]
        for key in list(section.keys()):
            if key == "blocks":
                continue
            if key in section_payload:
                section[key] = float(section_payload[key])
        blocks = section_payload.get("blocks", [])
        if isinstance(blocks, list):
            cleaned_blocks: List[Dict[str, Any]] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if section_name == "frequency_sweep":
                    fallback_value = float(block.get("frequency_hz", 10.0))
                    ao_values: Dict[str, float] = {}
                    raw_ao_values = block.get("ao_values")
                    if isinstance(raw_ao_values, dict) and raw_ao_values:
                        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
                            ao_values[str(channel)] = float(raw_ao_values.get(str(channel), 0.0))
                    else:
                        ao_values = {str(channel): fallback_value for channel in range(1, MAX_NI_AO_CHANNELS + 1)}
                    cleaned_blocks.append(
                        {
                            "duration_s": float(block.get("duration_s", 30.0)),
                            "frequency_hz": fallback_value,
                            "ao_values": ao_values,
                        }
                    )
                else:
                    fallback_value = float(block.get("amplitude_v", 2.0))
                    ao_values = {}
                    raw_ao_values = block.get("ao_values")
                    if isinstance(raw_ao_values, dict) and raw_ao_values:
                        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
                            ao_values[str(channel)] = float(raw_ao_values.get(str(channel), 0.0))
                    else:
                        ao_values = {str(channel): fallback_value for channel in range(1, MAX_NI_AO_CHANNELS + 1)}
                    cleaned_blocks.append(
                        {
                            "duration_s": float(block.get("duration_s", 30.0)),
                            "amplitude_v": fallback_value,
                            "ao_values": ao_values,
                        }
                    )
            if cleaned_blocks:
                section["blocks"] = cleaned_blocks

    analysis = payload.get("analysis", {})
    if isinstance(analysis, dict):
        merged["analysis"]["voltage_divider_ratio"] = float(
            analysis.get("voltage_divider_ratio", merged["analysis"]["voltage_divider_ratio"])
        )
        merged["analysis"]["trim_edge_seconds"] = float(analysis.get("trim_edge_seconds", merged["analysis"]["trim_edge_seconds"]))
        merged["analysis"]["ipg_adc_lsb_uV"] = float(analysis.get("ipg_adc_lsb_uV", merged["analysis"]["ipg_adc_lsb_uV"]))

    return merged


def auto_ipg_test_templates() -> Dict[str, Dict[str, Any]]:
    base = default_auto_ipg_test_config()

    hpf_template = merge_auto_ipg_test_config(base)
    hpf_template["session_name"] = "hpf_validation"
    hpf_template["mode"] = "frequency"
    hpf_template["frequency_sweep"]["fixed_amplitude_v"] = 2.0
    hpf_template["frequency_sweep"]["blocks"] = [
        {"duration_s": 300.0, "frequency_hz": 0.05},
        {"duration_s": 300.0, "frequency_hz": 0.1},
        {"duration_s": 120.0, "frequency_hz": 0.2},
        {"duration_s": 30.0, "frequency_hz": 1.0},
        {"duration_s": 30.0, "frequency_hz": 5.0},
    ]

    lpf_template = merge_auto_ipg_test_config(base)
    lpf_template["session_name"] = "lpf_validation"
    lpf_template["mode"] = "frequency"
    lpf_template["frequency_sweep"]["fixed_amplitude_v"] = 2.0
    lpf_template["frequency_sweep"]["blocks"] = [
        {"duration_s": 30.0, "frequency_hz": 50.0},
        {"duration_s": 30.0, "frequency_hz": 100.0},
        {"duration_s": 30.0, "frequency_hz": 200.0},
        {"duration_s": 30.0, "frequency_hz": 300.0},
        {"duration_s": 30.0, "frequency_hz": 500.0},
    ]

    both_template = merge_auto_ipg_test_config(base)
    both_template["session_name"] = "amp_then_freq"
    both_template["mode"] = "both"
    both_template["amplitude_sweep"]["fixed_frequency_hz"] = 10.0
    both_template["amplitude_sweep"]["blocks"] = [
        {"duration_s": 30.0, "amplitude_v": 0.5},
        {"duration_s": 30.0, "amplitude_v": 1.0},
        {"duration_s": 30.0, "amplitude_v": 2.0},
    ]
    both_template["frequency_sweep"]["fixed_amplitude_v"] = 2.0
    both_template["frequency_sweep"]["blocks"] = [
        {"duration_s": 30.0, "frequency_hz": 2.0},
        {"duration_s": 30.0, "frequency_hz": 10.0},
        {"duration_s": 30.0, "frequency_hz": 50.0},
    ]

    bandpass_template = merge_auto_ipg_test_config(base)
    bandpass_template["session_name"] = "bandpass_sweep_fine"
    bandpass_template["mode"] = "frequency"
    bandpass_template["frequency_sweep"]["fixed_amplitude_v"] = 2.0
    bandpass_template["frequency_sweep"]["blocks"] = [
        # Low frequency range (HPF corner focus) — dense around 0.1 Hz and 0.2 Hz
        {"duration_s": 100.0, "frequency_hz": 0.05},
        {"duration_s": 80.0, "frequency_hz": 0.06},
        {"duration_s": 70.0, "frequency_hz": 0.07},
        {"duration_s": 60.0, "frequency_hz": 0.08},
        {"duration_s": 55.0, "frequency_hz": 0.09},
        {"duration_s": 55.0, "frequency_hz": 0.10},
        {"duration_s": 45.0, "frequency_hz": 0.11},
        {"duration_s": 40.0, "frequency_hz": 0.12},
        {"duration_s": 35.0, "frequency_hz": 0.14},
        {"duration_s": 32.0, "frequency_hz": 0.16},
        {"duration_s": 30.0, "frequency_hz": 0.18},
        {"duration_s": 30.0, "frequency_hz": 0.20},
        {"duration_s": 25.0, "frequency_hz": 0.22},
        {"duration_s": 25.0, "frequency_hz": 0.25},
        {"duration_s": 22.0, "frequency_hz": 0.30},
        {"duration_s": 18.0, "frequency_hz": 0.40},
        # Lower-mid range (HPF corner focus) — dense around 1 Hz and 4 Hz
        {"duration_s": 15.0, "frequency_hz": 0.60},
        {"duration_s": 15.0, "frequency_hz": 0.70},
        {"duration_s": 15.0, "frequency_hz": 0.80},
        {"duration_s": 15.0, "frequency_hz": 0.90},
        {"duration_s": 15.0, "frequency_hz": 1.0},
        {"duration_s": 15.0, "frequency_hz": 1.10},
        {"duration_s": 15.0, "frequency_hz": 1.25},
        {"duration_s": 15.0, "frequency_hz": 1.50},
        {"duration_s": 15.0, "frequency_hz": 2.0},
        {"duration_s": 15.0, "frequency_hz": 2.50},
        {"duration_s": 15.0, "frequency_hz": 3.0},
        {"duration_s": 15.0, "frequency_hz": 3.50},
        {"duration_s": 15.0, "frequency_hz": 4.0},
        {"duration_s": 15.0, "frequency_hz": 4.50},
        {"duration_s": 15.0, "frequency_hz": 5.0},
        {"duration_s": 10.0, "frequency_hz": 10.0},
        {"duration_s": 10.0, "frequency_hz": 20.0},
        {"duration_s": 10.0, "frequency_hz": 50.0},
        # High frequency range (LPF corner focus) — finer resolution with cycle-aware dwell
        {"duration_s": 10.0, "frequency_hz": 62.0},
        {"duration_s": 10.0, "frequency_hz": 71.0},
        {"duration_s": 10.0, "frequency_hz": 80.0},
        {"duration_s": 10.0, "frequency_hz": 90.0},
        {"duration_s": 10.0, "frequency_hz": 100.0},
        {"duration_s": 10.0, "frequency_hz": 112.0},
        {"duration_s": 10.0, "frequency_hz": 125.0},
        {"duration_s": 10.0, "frequency_hz": 140.0},
        {"duration_s": 10.0, "frequency_hz": 160.0},
        {"duration_s": 10.0, "frequency_hz": 180.0},
        {"duration_s": 10.0, "frequency_hz": 200.0},
        {"duration_s": 10.0, "frequency_hz": 225.0},
        {"duration_s": 10.0, "frequency_hz": 250.0},
        {"duration_s": 10.0, "frequency_hz": 280.0},
        {"duration_s": 10.0, "frequency_hz": 315.0},
        {"duration_s": 10.0, "frequency_hz": 355.0},
        {"duration_s": 10.0, "frequency_hz": 375.0},
        {"duration_s": 10.0, "frequency_hz": 400.0},
        {"duration_s": 10.0, "frequency_hz": 450.0},
        {"duration_s": 10.0, "frequency_hz": 500.0},
    ]

    bandpass_balanced_template = merge_auto_ipg_test_config(base)
    bandpass_balanced_template["session_name"] = "bandpass_sweep_balanced"
    bandpass_balanced_template["mode"] = "frequency"
    bandpass_balanced_template["frequency_sweep"]["fixed_amplitude_v"] = 2.0
    bandpass_balanced_template["frequency_sweep"]["blocks"] = [
        {"duration_s": 180.0, "frequency_hz": 0.05},
        {"duration_s": 150.0, "frequency_hz": 0.07},
        {"duration_s": 120.0, "frequency_hz": 0.10},
        {"duration_s": 90.0, "frequency_hz": 0.14},
        {"duration_s": 75.0, "frequency_hz": 0.20},
        {"duration_s": 60.0, "frequency_hz": 0.30},
        {"duration_s": 45.0, "frequency_hz": 0.40},
        {"duration_s": 40.0, "frequency_hz": 0.60},
        {"duration_s": 40.0, "frequency_hz": 0.80},
        {"duration_s": 40.0, "frequency_hz": 1.0},
        {"duration_s": 35.0, "frequency_hz": 1.25},
        {"duration_s": 35.0, "frequency_hz": 1.50},
        {"duration_s": 35.0, "frequency_hz": 2.0},
        {"duration_s": 35.0, "frequency_hz": 2.50},
        {"duration_s": 35.0, "frequency_hz": 3.0},
        {"duration_s": 30.0, "frequency_hz": 3.50},
        {"duration_s": 30.0, "frequency_hz": 4.0},
        {"duration_s": 30.0, "frequency_hz": 4.50},
        {"duration_s": 30.0, "frequency_hz": 5.0},
        {"duration_s": 25.0, "frequency_hz": 7.5},
        {"duration_s": 25.0, "frequency_hz": 10.0},
        {"duration_s": 20.0, "frequency_hz": 20.0},
        {"duration_s": 20.0, "frequency_hz": 50.0},
        {"duration_s": 15.0, "frequency_hz": 62.0},
        {"duration_s": 15.0, "frequency_hz": 71.0},
        {"duration_s": 15.0, "frequency_hz": 80.0},
        {"duration_s": 15.0, "frequency_hz": 90.0},
        {"duration_s": 15.0, "frequency_hz": 100.0},
        {"duration_s": 15.0, "frequency_hz": 112.0},
        {"duration_s": 15.0, "frequency_hz": 125.0},
        {"duration_s": 15.0, "frequency_hz": 140.0},
        {"duration_s": 15.0, "frequency_hz": 160.0},
        {"duration_s": 15.0, "frequency_hz": 180.0},
        {"duration_s": 15.0, "frequency_hz": 200.0},
        {"duration_s": 15.0, "frequency_hz": 225.0},
        {"duration_s": 15.0, "frequency_hz": 250.0},
        {"duration_s": 15.0, "frequency_hz": 280.0},
        {"duration_s": 15.0, "frequency_hz": 315.0},
        {"duration_s": 15.0, "frequency_hz": 355.0},
        {"duration_s": 15.0, "frequency_hz": 375.0},
        {"duration_s": 15.0, "frequency_hz": 400.0},
        {"duration_s": 15.0, "frequency_hz": 450.0},
        {"duration_s": 15.0, "frequency_hz": 500.0},
    ]

    return {
        "HPF Validation": hpf_template,
        "LPF Validation": lpf_template,
        "Amp + Freq Sweep": both_template,
        "Bandpass Sweep (Fine)": bandpass_template,
        "Bandpass Sweep (Balanced)": bandpass_balanced_template,
    }


class AutoIpgTestingDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, config: Dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Auto IPG Testing Configuration")
        self.resize(1060, 900)
        self._templates = auto_ipg_test_templates()
        self._config = merge_auto_ipg_test_config(config)

        layout = QtWidgets.QVBoxLayout(self)

        header_group = QtWidgets.QGroupBox("Testing Configuration")
        header_layout = QtWidgets.QGridLayout(header_group)
        self.save_config_button = QtWidgets.QPushButton("Save Testing Config")
        self.load_config_button = QtWidgets.QPushButton("Load Testing Config")
        self.template_combo = QtWidgets.QComboBox()
        self.template_combo.addItems(list(self._templates.keys()))
        self.apply_template_button = QtWidgets.QPushButton("Apply Template")
        self.session_name_edit = QtWidgets.QLineEdit()
        self.session_name_edit.setPlaceholderText("Session name")

        self.mode_frequency_radio = QtWidgets.QRadioButton("Frequency Sweep")
        self.mode_amplitude_radio = QtWidgets.QRadioButton("Amplitude Sweep")
        self.mode_both_radio = QtWidgets.QRadioButton("Both (Amplitude then Frequency)")
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(self.mode_frequency_radio)
        mode_row.addWidget(self.mode_amplitude_radio)
        mode_row.addWidget(self.mode_both_radio)
        mode_row.addStretch(1)

        header_layout.addWidget(self.save_config_button, 0, 0)
        header_layout.addWidget(self.load_config_button, 0, 1)
        header_layout.addWidget(QtWidgets.QLabel("Template"), 0, 2)
        header_layout.addWidget(self.template_combo, 0, 3)
        header_layout.addWidget(self.apply_template_button, 0, 4)
        header_layout.addWidget(QtWidgets.QLabel("Session Name"), 1, 0)
        header_layout.addWidget(self.session_name_edit, 1, 1, 1, 2)
        header_layout.addLayout(mode_row, 2, 0, 1, 5)
        layout.addWidget(header_group)

        mapping_group = QtWidgets.QGroupBox("Channel Mapping (NI AO -> IPG Rec, one-to-many)")
        mapping_layout = QtWidgets.QVBoxLayout(mapping_group)
        self.mapping_table = QtWidgets.QTableWidget(MAX_NI_AO_CHANNELS, 2)
        self.mapping_table.setHorizontalHeaderLabels(["NI AO Channel", "IPG Rec Channel Selection"])
        self.mapping_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.mapping_table.verticalHeader().setVisible(False)
        self.mapping_checkboxes: Dict[int, List[QtWidgets.QCheckBox]] = {}
        mapping_layout.addWidget(self.mapping_table)
        layout.addWidget(mapping_group)

        self.ni_ai_reference_group = QtWidgets.QGroupBox("NI AI Reference Mapping (NI AI -> IPG Rec)")
        ni_ai_ref_layout = QtWidgets.QVBoxLayout(self.ni_ai_reference_group)
        self.ni_ai_reference_checkbox = QtWidgets.QCheckBox("Enable NI AI reference for report generation")
        ni_ai_ref_layout.addWidget(self.ni_ai_reference_checkbox)
        self.ni_ai_reference_table = QtWidgets.QTableWidget(0, 2)
        self.ni_ai_reference_table.setHorizontalHeaderLabels(["NI AI Channel", "IPG Rec Channel Selection"])
        self.ni_ai_reference_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.ni_ai_reference_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.ni_ai_reference_table.verticalHeader().setVisible(False)
        self.ni_ai_reference_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ni_ai_reference_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.ni_ai_reference_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ni_ai_reference_table.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        ni_ai_ref_layout.addWidget(self.ni_ai_reference_table)
        ni_ai_ref_btn_row = QtWidgets.QHBoxLayout()
        self.ni_ai_reference_add_button = QtWidgets.QPushButton("Add")
        self.ni_ai_reference_delete_button = QtWidgets.QPushButton("Delete")
        ni_ai_ref_btn_row.addWidget(self.ni_ai_reference_add_button)
        ni_ai_ref_btn_row.addWidget(self.ni_ai_reference_delete_button)
        ni_ai_ref_btn_row.addStretch(1)
        ni_ai_ref_layout.addLayout(ni_ai_ref_btn_row)
        layout.addWidget(self.ni_ai_reference_group)

        blocks_group = QtWidgets.QGroupBox("Test Blocks")
        blocks_group.setMinimumHeight(520)
        blocks_layout = QtWidgets.QVBoxLayout(blocks_group)
        runtime_row = QtWidgets.QHBoxLayout()
        runtime_row.addWidget(QtWidgets.QLabel("Estimated Total Runtime"))
        self.total_runtime_label = QtWidgets.QLabel("00:00:00")
        runtime_row.addWidget(self.total_runtime_label)
        runtime_row.addStretch(1)
        runtime_row.addWidget(QtWidgets.QLabel("Inter-block Delay (s)"))
        self.inter_block_delay_spin = QtWidgets.QDoubleSpinBox()
        self.inter_block_delay_spin.setRange(0.0, 60.0)
        self.inter_block_delay_spin.setDecimals(1)
        runtime_row.addWidget(self.inter_block_delay_spin)
        blocks_layout.addLayout(runtime_row)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        blocks_layout.addWidget(split, 1)

        self.amp_group = QtWidgets.QGroupBox("Amplitude Sweep Blocks")
        amp_layout = QtWidgets.QVBoxLayout(self.amp_group)
        amp_cfg_row = QtWidgets.QHBoxLayout()
        amp_cfg_row.addWidget(QtWidgets.QLabel("Fixed Frequency (Hz)"))
        self.amp_fixed_frequency_spin = QtWidgets.QDoubleSpinBox()
        self.amp_fixed_frequency_spin.setRange(0.01, 10000.0)
        self.amp_fixed_frequency_spin.setDecimals(3)
        amp_cfg_row.addWidget(self.amp_fixed_frequency_spin)
        amp_cfg_row.addStretch(1)
        amp_layout.addLayout(amp_cfg_row)
        self.amp_table = QtWidgets.QTableWidget(0, 1 + MAX_NI_AO_CHANNELS)
        self.amp_table.setHorizontalHeaderLabels(["Duration (s)"] + [f"AO{channel} Amp (V)" for channel in range(1, MAX_NI_AO_CHANNELS + 1)])
        self.amp_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.amp_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.amp_table.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.amp_table.setDragDropOverwriteMode(False)
        amp_layout.addWidget(self.amp_table)
        amp_btn_row = QtWidgets.QHBoxLayout()
        self.amp_add_button = QtWidgets.QPushButton("Add")
        self.amp_delete_button = QtWidgets.QPushButton("Delete")
        self.amp_up_button = QtWidgets.QPushButton("Up")
        self.amp_down_button = QtWidgets.QPushButton("Down")
        for button in (self.amp_add_button, self.amp_delete_button, self.amp_up_button, self.amp_down_button):
            amp_btn_row.addWidget(button)
        amp_btn_row.addStretch(1)
        amp_layout.addLayout(amp_btn_row)

        self.freq_group = QtWidgets.QGroupBox("Frequency Sweep Blocks")
        freq_layout = QtWidgets.QVBoxLayout(self.freq_group)
        freq_cfg_row = QtWidgets.QHBoxLayout()
        freq_cfg_row.addWidget(QtWidgets.QLabel("Fixed Amplitude (V)"))
        self.freq_fixed_amplitude_spin = QtWidgets.QDoubleSpinBox()
        self.freq_fixed_amplitude_spin.setRange(0.0, 10.0)
        self.freq_fixed_amplitude_spin.setDecimals(3)
        freq_cfg_row.addWidget(self.freq_fixed_amplitude_spin)
        freq_cfg_row.addStretch(1)
        freq_layout.addLayout(freq_cfg_row)
        self.freq_table = QtWidgets.QTableWidget(0, 1 + MAX_NI_AO_CHANNELS)
        self.freq_table.setHorizontalHeaderLabels(["Duration (s)"] + [f"AO{channel} Freq (Hz)" for channel in range(1, MAX_NI_AO_CHANNELS + 1)])
        self.freq_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.freq_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.freq_table.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.freq_table.setDragDropOverwriteMode(False)
        freq_layout.addWidget(self.freq_table)
        freq_btn_row = QtWidgets.QHBoxLayout()
        self.freq_add_button = QtWidgets.QPushButton("Add")
        self.freq_delete_button = QtWidgets.QPushButton("Delete")
        self.freq_up_button = QtWidgets.QPushButton("Up")
        self.freq_down_button = QtWidgets.QPushButton("Down")
        for button in (self.freq_add_button, self.freq_delete_button, self.freq_up_button, self.freq_down_button):
            freq_btn_row.addWidget(button)
        freq_btn_row.addStretch(1)
        freq_layout.addLayout(freq_btn_row)

        split.addWidget(self.amp_group)
        split.addWidget(self.freq_group)
        split.setSizes([480, 480])
        layout.addWidget(blocks_group, 1)

        buttons = QtWidgets.QDialogButtonBox()
        self.done_button = buttons.addButton("Done", QtWidgets.QDialogButtonBox.AcceptRole)
        self.leave_button = buttons.addButton("Leave", QtWidgets.QDialogButtonBox.RejectRole)
        layout.addWidget(buttons)

        self.save_config_button.clicked.connect(self._save_dialog)
        self.load_config_button.clicked.connect(self._load_dialog)
        self.apply_template_button.clicked.connect(self._apply_template)
        self.mode_frequency_radio.toggled.connect(self._update_mode_visibility)
        self.mode_amplitude_radio.toggled.connect(self._update_mode_visibility)
        self.mode_both_radio.toggled.connect(self._update_mode_visibility)
        self.ni_ai_reference_checkbox.toggled.connect(self._update_ni_ai_reference_group_state)
        self.ni_ai_reference_add_button.clicked.connect(self._add_ni_ai_reference_row)
        self.ni_ai_reference_delete_button.clicked.connect(self._delete_selected_ni_ai_reference_row)
        self.inter_block_delay_spin.valueChanged.connect(self._update_runtime_label)
        self.done_button.clicked.connect(self._on_done)
        self.leave_button.clicked.connect(self._on_leave)

        self.amp_add_button.clicked.connect(lambda: self._add_row(self.amp_table, [30.0] + [2.0 for _ in range(MAX_NI_AO_CHANNELS)]))
        self.amp_delete_button.clicked.connect(lambda: self._delete_selected_row(self.amp_table))
        self.amp_up_button.clicked.connect(lambda: self._move_selected_row(self.amp_table, -1))
        self.amp_down_button.clicked.connect(lambda: self._move_selected_row(self.amp_table, 1))

        self.freq_add_button.clicked.connect(lambda: self._add_row(self.freq_table, [30.0] + [10.0 for _ in range(MAX_NI_AO_CHANNELS)]))
        self.freq_delete_button.clicked.connect(lambda: self._delete_selected_row(self.freq_table))
        self.freq_up_button.clicked.connect(lambda: self._move_selected_row(self.freq_table, -1))
        self.freq_down_button.clicked.connect(lambda: self._move_selected_row(self.freq_table, 1))
        self.amp_table.itemChanged.connect(lambda _: self._update_runtime_label())
        self.freq_table.itemChanged.connect(lambda _: self._update_runtime_label())
        self.session_name_edit.textChanged.connect(lambda _: self._update_runtime_label())

        self._apply_to_widgets(self._config)

    def _apply_to_widgets(self, config: Dict[str, Any]) -> None:
        mode = str(config.get("mode", "frequency"))
        self.mode_frequency_radio.setChecked(mode == "frequency")
        self.mode_amplitude_radio.setChecked(mode == "amplitude")
        self.mode_both_radio.setChecked(mode == "both")
        self.session_name_edit.setText(str(config.get("session_name", "auto_ipg_test")))
        self.inter_block_delay_spin.setValue(float(config.get("inter_block_delay_s", 1.0)))
        self.amp_fixed_frequency_spin.setValue(float(config["amplitude_sweep"].get("fixed_frequency_hz", 10.0)))
        self.freq_fixed_amplitude_spin.setValue(float(config["frequency_sweep"].get("fixed_amplitude_v", 2.0)))
        ni_ai_reference = config.get("ni_ai_reference", {}) if isinstance(config, dict) else {}
        self.ni_ai_reference_checkbox.blockSignals(True)
        self.ni_ai_reference_checkbox.setChecked(bool(ni_ai_reference.get("enabled", False)))
        self.ni_ai_reference_checkbox.blockSignals(False)

        self.mapping_table.blockSignals(True)
        for row in range(MAX_NI_AO_CHANNELS):
            ao_channel = row + 1
            channel_item = QtWidgets.QTableWidgetItem(f"AO {ao_channel}")
            channel_item.setFlags(channel_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.mapping_table.setItem(row, 0, channel_item)
            mapped = config.get("mapping", {}).get(str(ao_channel), [])
            box = QtWidgets.QWidget()
            box_layout = QtWidgets.QHBoxLayout(box)
            box_layout.setContentsMargins(2, 2, 2, 2)
            box_layout.setSpacing(4)
            checkboxes: List[QtWidgets.QCheckBox] = []
            for ipg_ch in range(1, MAX_IPG_CHANNELS + 1):
                cb = QtWidgets.QCheckBox(str(ipg_ch))
                cb.setChecked(ipg_ch in mapped)
                cb.toggled.connect(lambda _checked, self=self: (self._update_table_column_enable_state(), self._update_runtime_label()))
                box_layout.addWidget(cb)
                checkboxes.append(cb)
            box_layout.addStretch(1)
            self.mapping_table.setCellWidget(row, 1, box)
            self.mapping_checkboxes[ao_channel] = checkboxes
        self.mapping_table.blockSignals(False)

        self._set_ni_ai_reference_rows(
            ni_ai_reference.get("mapping", []) if isinstance(ni_ai_reference, dict) else []
        )

        self._set_table_rows(
            self.amp_table,
            [
                [block.get("duration_s", 30.0)]
                + [
                    (block.get("ao_values", {}).get(str(channel), block.get("amplitude_v", 2.0)))
                    for channel in range(1, MAX_NI_AO_CHANNELS + 1)
                ]
                for block in config["amplitude_sweep"].get("blocks", [])
            ],
        )
        self._set_table_rows(
            self.freq_table,
            [
                [block.get("duration_s", 30.0)]
                + [
                    (block.get("ao_values", {}).get(str(channel), block.get("frequency_hz", 10.0)))
                    for channel in range(1, MAX_NI_AO_CHANNELS + 1)
                ]
                for block in config["frequency_sweep"].get("blocks", [])
            ],
        )
        self._update_mode_visibility()
        self._update_table_column_enable_state()
        self._update_ni_ai_reference_group_state(self.ni_ai_reference_checkbox.isChecked())
        self._update_runtime_label()

    def _set_table_rows(self, table: QtWidgets.QTableWidget, rows: List[List[float]]) -> None:
        table.setRowCount(0)
        for row_values in rows:
            self._add_row_from_config(table, row_values)

    def _add_row_from_config(self, table: QtWidgets.QTableWidget, values: List[float]) -> None:
        """Add a row with values from saved config (use values as-is, no N/A substitution)."""
        row = table.rowCount()
        table.insertRow(row)
        for col, value in enumerate(values):
            item = QtWidgets.QTableWidgetItem(str(value))
            table.setItem(row, col, item)
        self._update_table_column_enable_state()
        self._update_runtime_label()

    def _add_row(self, table: QtWidgets.QTableWidget, values: List[float]) -> None:
        """Add a new row with N/A placeholder for unmapped channels."""
        row = table.rowCount()
        table.insertRow(row)
        
        # Build set of currently mapped AO channels
        mapped_ao_channels: set[int] = set()
        for ao_channel in range(1, MAX_NI_AO_CHANNELS + 1):
            checkboxes = self.mapping_checkboxes.get(ao_channel, [])
            if any(cb.isChecked() for cb in checkboxes):
                mapped_ao_channels.add(ao_channel)
        
        for col, value in enumerate(values):
            # Column 0 is duration (always editable)
            if col == 0:
                item = QtWidgets.QTableWidgetItem(str(value))
            else:
                # AO columns: show value if mapped, N/A if not mapped
                ao_channel = col
                if ao_channel in mapped_ao_channels:
                    item = QtWidgets.QTableWidgetItem(str(value))
                else:
                    item = QtWidgets.QTableWidgetItem("N/A")
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, col, item)
        self._update_table_column_enable_state()
        self._update_runtime_label()

    def _ni_ai_channel_label(self, channel: int) -> str:
        # Internal value 1 maps to physical/display AI0.
        return f"AI {channel - 1}"

    def _next_available_ni_ai_reference_channel(self) -> int:
        used = set()
        for row in range(self.ni_ai_reference_table.rowCount()):
            combo = self.ni_ai_reference_table.cellWidget(row, 0)
            if isinstance(combo, QtWidgets.QComboBox):
                used.add(int(combo.currentIndex()) + 1)
        for channel in range(1, MAX_NI_AI_CHANNELS + 1):
            if channel not in used:
                return channel
        return 1

    def _add_ni_ai_reference_row(
        self,
        ni_ai_channel: Optional[int] = None,
        ipg_rec_channels: Optional[List[int]] = None,
    ) -> None:
        row = self.ni_ai_reference_table.rowCount()
        self.ni_ai_reference_table.insertRow(row)

        combo = QtWidgets.QComboBox()
        combo.addItems([self._ni_ai_channel_label(channel) for channel in range(1, MAX_NI_AI_CHANNELS + 1)])
        default_channel = ni_ai_channel or self._next_available_ni_ai_reference_channel()
        combo.setCurrentIndex(max(0, min(MAX_NI_AI_CHANNELS - 1, int(default_channel) - 1)))
        combo.currentIndexChanged.connect(lambda _index, self=self: self._update_runtime_label())
        self.ni_ai_reference_table.setCellWidget(row, 0, combo)

        box = QtWidgets.QWidget()
        box_layout = QtWidgets.QHBoxLayout(box)
        box_layout.setContentsMargins(2, 2, 2, 2)
        box_layout.setSpacing(4)
        checkboxes: List[QtWidgets.QCheckBox] = []
        mapped = {int(value) for value in (ipg_rec_channels or [1]) if 1 <= int(value) <= MAX_IPG_CHANNELS}
        for ipg_ch in range(1, MAX_IPG_CHANNELS + 1):
            cb = QtWidgets.QCheckBox(str(ipg_ch))
            cb.setChecked(ipg_ch in mapped)
            cb.toggled.connect(lambda _checked, self=self: self._update_runtime_label())
            box_layout.addWidget(cb)
            checkboxes.append(cb)
        box_layout.addStretch(1)
        self.ni_ai_reference_table.setCellWidget(row, 1, box)

        self._update_ni_ai_reference_group_state(self.ni_ai_reference_checkbox.isChecked())
        self._update_runtime_label()

    def _set_ni_ai_reference_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.ni_ai_reference_table.setRowCount(0)
        if not rows:
            rows = [{"ni_ai_channel": 1, "ipg_rec_channels": [1]}]
        for entry in rows:
            ipg_rec_channels = [int(value) for value in entry.get("ipg_rec_channels", []) if 1 <= int(value) <= MAX_IPG_CHANNELS]
            self._add_ni_ai_reference_row(
                ni_ai_channel=int(entry.get("ni_ai_channel", 1)),
                ipg_rec_channels=ipg_rec_channels,
            )

    def _delete_selected_ni_ai_reference_row(self) -> None:
        row = self.ni_ai_reference_table.currentRow()
        if row < 0:
            return
        if self.ni_ai_reference_table.rowCount() <= 1:
            combo = self.ni_ai_reference_table.cellWidget(0, 0)
            if isinstance(combo, QtWidgets.QComboBox):
                combo.setCurrentIndex(0)
            widget = self.ni_ai_reference_table.cellWidget(0, 1)
            if isinstance(widget, QtWidgets.QWidget):
                checkboxes = widget.findChildren(QtWidgets.QCheckBox)
                for index, cb in enumerate(checkboxes):
                    cb.setChecked(index == 0)
            self._update_runtime_label()
            return
        self.ni_ai_reference_table.removeRow(row)
        self._update_runtime_label()

    def _collect_ni_ai_reference_rows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in range(self.ni_ai_reference_table.rowCount()):
            combo = self.ni_ai_reference_table.cellWidget(row, 0)
            widget = self.ni_ai_reference_table.cellWidget(row, 1)
            if not isinstance(combo, QtWidgets.QComboBox) or not isinstance(widget, QtWidgets.QWidget):
                continue
            ni_ai_channel = int(combo.currentIndex()) + 1
            checkboxes = widget.findChildren(QtWidgets.QCheckBox)
            ipg_rec_channels = [
                index + 1
                for index, cb in enumerate(checkboxes)
                if cb.isChecked()
            ]
            rows.append({"ni_ai_channel": ni_ai_channel, "ipg_rec_channels": ipg_rec_channels})
        return rows

    def _update_ni_ai_reference_group_state(self, enabled: bool) -> None:
        self.ni_ai_reference_table.setEnabled(enabled)
        self.ni_ai_reference_add_button.setEnabled(enabled)
        self.ni_ai_reference_delete_button.setEnabled(enabled)

    def _delete_selected_row(self, table: QtWidgets.QTableWidget) -> None:
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)
        self._update_runtime_label()

    def _move_selected_row(self, table: QtWidgets.QTableWidget, step: int) -> None:
        row = table.currentRow()
        if row < 0:
            return
        target = row + step
        if target < 0 or target >= table.rowCount():
            return
        values = []
        for col in range(table.columnCount()):
            source_item = table.item(row, col)
            target_item = table.item(target, col)
            source_text = source_item.text() if source_item else ""
            target_text = target_item.text() if target_item else ""
            values.append((source_text, target_text))
        for col, (source_text, target_text) in enumerate(values):
            table.setItem(row, col, QtWidgets.QTableWidgetItem(target_text))
            table.setItem(target, col, QtWidgets.QTableWidgetItem(source_text))
        table.selectRow(target)
        self._update_runtime_label()

    def _save_dialog(self) -> None:
        config = self._collect_config_or_show_error()
        if config is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Auto IPG Test Config", "", "JSON Files (*.json)")
        if not path:
            return
        save_json(Path(path), config)

    def _load_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Auto IPG Test Config", "", "JSON Files (*.json)")
        if not path:
            return
        loaded = merge_auto_ipg_test_config(load_json(Path(path)))
        self._config = loaded
        self._apply_to_widgets(loaded)

    def _apply_template(self) -> None:
        name = self.template_combo.currentText()
        template = self._templates.get(name)
        if template is None:
            return

        # Keep user's existing AO->IPG mapping; template content should only
        # fill sweep/session settings, not clear mapping selections.
        current_mapping: Dict[str, List[int]] = {}
        has_mapping = False
        for ao_channel in range(1, MAX_NI_AO_CHANNELS + 1):
            selected = [idx + 1 for idx, cb in enumerate(self.mapping_checkboxes.get(ao_channel, [])) if cb.isChecked()]
            current_mapping[str(ao_channel)] = selected
            if selected:
                has_mapping = True

        if not has_mapping:
            QtWidgets.QMessageBox.information(
                self,
                "Apply Template",
                "Select AO-to-IPG channel mapping first, then apply template so values are populated only for mapped AO channels.",
            )
            return

        merged = merge_auto_ipg_test_config(template)
        merged["mapping"] = current_mapping
        self._config = merged
        self._apply_to_widgets(self._config)

    def _row_default_value_for_enabled_column(self, table: QtWidgets.QTableWidget, row: int, fallback: float = 0.0) -> float:
        """Pick a sensible per-row default from existing AO values before falling back to zero."""
        for col in range(1, table.columnCount()):
            item = table.item(row, col)
            text = item.text().strip() if item and item.text() else ""
            if not text or text.upper() == "N/A":
                continue
            try:
                return float(text)
            except Exception:
                continue
        return float(fallback)

    def _update_mode_visibility(self) -> None:
        mode = self._selected_mode()
        self.amp_group.setVisible(mode in {"amplitude", "both"})
        self.freq_group.setVisible(mode in {"frequency", "both"})
        self._update_runtime_label()

    def _update_table_column_enable_state(self) -> None:
        """Enable/disable AO columns in amplitude and frequency tables based on channel mapping."""
        # Build a set of AO channels that are currently mapped
        mapped_ao_channels: set[int] = set()
        for ao_channel in range(1, MAX_NI_AO_CHANNELS + 1):
            checkboxes = self.mapping_checkboxes.get(ao_channel, [])
            if any(cb.isChecked() for cb in checkboxes):
                mapped_ao_channels.add(ao_channel)
        
        # Update amplitude table columns
        for col in range(1, self.amp_table.columnCount()):
            ao_channel = col  # Column index corresponds to AO channel number
            enabled = ao_channel in mapped_ao_channels
            for row in range(self.amp_table.rowCount()):
                item = self.amp_table.item(row, col)
                if item is not None:
                    flags = item.flags()
                    if enabled:
                        # Enable: restore a row-consistent numeric value if currently N/A.
                        if item.text() == "N/A":
                            item.setText(str(self._row_default_value_for_enabled_column(self.amp_table, row, fallback=0.0)))
                        flags |= QtCore.Qt.ItemIsEditable
                    else:
                        # Disable: set text to "N/A"
                        item.setText("N/A")
                        flags &= ~QtCore.Qt.ItemIsEditable
                    item.setFlags(flags)
        
        # Update frequency table columns
        for col in range(1, self.freq_table.columnCount()):
            ao_channel = col  # Column index corresponds to AO channel number
            enabled = ao_channel in mapped_ao_channels
            for row in range(self.freq_table.rowCount()):
                item = self.freq_table.item(row, col)
                if item is not None:
                    flags = item.flags()
                    if enabled:
                        # Enable: restore a row-consistent numeric value if currently N/A.
                        if item.text() == "N/A":
                            item.setText(str(self._row_default_value_for_enabled_column(self.freq_table, row, fallback=0.0)))
                        flags |= QtCore.Qt.ItemIsEditable
                    else:
                        # Disable: set text to "N/A"
                        item.setText("N/A")
                        flags &= ~QtCore.Qt.ItemIsEditable
                    item.setFlags(flags)

    def _selected_mode(self) -> str:
        if self.mode_both_radio.isChecked():
            return "both"
        if self.mode_amplitude_radio.isChecked():
            return "amplitude"
        return "frequency"

    def _row_has_active_mapped_ao(
        self,
        table: QtWidgets.QTableWidget,
        row: int,
        mapped_ao_channels: set[int],
    ) -> bool:
        for channel in mapped_ao_channels:
            item = table.item(row, channel)
            text = item.text().strip() if item and item.text() else ""
            if text and text.upper() != "N/A":
                return True
        return False

    def _active_row_indices(self, table: QtWidgets.QTableWidget, mapped_ao_channels: set[int]) -> List[int]:
        return [
            row
            for row in range(table.rowCount())
            if self._row_has_active_mapped_ao(table, row, mapped_ao_channels)
        ]

    def _collect_blocks(self, table: QtWidgets.QTableWidget, x_key: str) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for row in range(table.rowCount()):
            duration_item = table.item(row, 0)
            duration = float(duration_item.text()) if duration_item and duration_item.text().strip() else 0.0
            ao_values: Dict[str, float] = {}
            for channel in range(1, MAX_NI_AO_CHANNELS + 1):
                item = table.item(row, channel)
                text = item.text() if item else ""
                # Treat "N/A" as 0.0
                if text.strip() == "N/A" or not text.strip():
                    value = 0.0
                else:
                    value = float(text)
                ao_values[str(channel)] = value
            representative = ao_values.get("1", 0.0)
            blocks.append({"duration_s": duration, x_key: representative, "ao_values": ao_values})
        return blocks

    def _collect_config_or_show_error(self) -> Optional[Dict[str, Any]]:
        try:
            config = self._collect_config()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid Configuration", str(exc))
            return None
        return config

    def _collect_config(self) -> Dict[str, Any]:
        mode = self._selected_mode()
        mapping: Dict[str, List[int]] = {}
        mapped_ao_channels: set[int] = set()
        for ao_channel in range(1, MAX_NI_AO_CHANNELS + 1):
            selected = [idx + 1 for idx, cb in enumerate(self.mapping_checkboxes.get(ao_channel, [])) if cb.isChecked()]
            mapping[str(ao_channel)] = selected
            if selected:
                mapped_ao_channels.add(ao_channel)

        ni_ai_reference_enabled = self.ni_ai_reference_checkbox.isChecked()
        ni_ai_reference_rows = self._collect_ni_ai_reference_rows()

        amp_blocks = self._collect_blocks(self.amp_table, "amplitude_v")
        freq_blocks = self._collect_blocks(self.freq_table, "frequency_hz")

        if mode in {"amplitude", "both"} and not amp_blocks:
            raise ValueError("Amplitude sweep requires at least one block")
        if mode in {"frequency", "both"} and not freq_blocks:
            raise ValueError("Frequency sweep requires at least one block")
        if not any(mapping.values()):
            raise ValueError("At least one AO-to-IPG mapping is required")

        amp_active_rows = self._active_row_indices(self.amp_table, mapped_ao_channels)
        freq_active_rows = self._active_row_indices(self.freq_table, mapped_ao_channels)

        if mode in {"amplitude", "both"} and not amp_active_rows:
            raise ValueError("Configure at least one amplitude block with a mapped AO field (non-N/A)")
        if mode in {"frequency", "both"} and not freq_active_rows:
            raise ValueError("Configure at least one frequency block with a mapped AO field (non-N/A)")

        if ni_ai_reference_enabled:
            if not ni_ai_reference_rows:
                raise ValueError("Enable NI AI reference requires at least one NI AI mapping row")
            seen_ai_channels: set[int] = set()
            seen_ipg_channels: set[int] = set()
            for row in ni_ai_reference_rows:
                ni_ai_channel = int(row.get("ni_ai_channel", 0))
                ipg_rec_channels = [
                    int(value)
                    for value in row.get("ipg_rec_channels", [])
                    if isinstance(value, int) and 1 <= int(value) <= MAX_IPG_CHANNELS
                ]
                if ni_ai_channel in seen_ai_channels:
                    raise ValueError("Each NI AI channel may only appear once in the NI AI reference mapping")
                seen_ai_channels.add(ni_ai_channel)
                if not ipg_rec_channels:
                    raise ValueError(f"NI AI channel {ni_ai_channel - 1} must map to at least one IPG Rec channel")
                for ipg_channel in ipg_rec_channels:
                    if ipg_channel in seen_ipg_channels:
                        raise ValueError(f"IPG Rec channel {ipg_channel} is mapped more than once in the NI AI reference mapping")
                    seen_ipg_channels.add(ipg_channel)

        for row in amp_active_rows:
            block = amp_blocks[row]
            if block["duration_s"] <= 0:
                raise ValueError("All amplitude blocks must have duration > 0")
        for row in freq_active_rows:
            block = freq_blocks[row]
            if block["duration_s"] <= 0:
                raise ValueError("All frequency blocks must have duration > 0")

        return {
            "version": AUTO_IPG_TEST_CONFIG_VERSION,
            "enabled": True,
            "session_name": self.session_name_edit.text().strip() or "auto_ipg_test",
            "mode": mode,
            "inter_block_delay_s": float(self.inter_block_delay_spin.value()),
            "mapping": mapping,
            "frequency_sweep": {
                "fixed_amplitude_v": float(self.freq_fixed_amplitude_spin.value()),
                "blocks": freq_blocks,
            },
            "amplitude_sweep": {
                "fixed_frequency_hz": float(self.amp_fixed_frequency_spin.value()),
                "blocks": amp_blocks,
            },
            "ni_ai_reference": {
                "enabled": bool(ni_ai_reference_enabled),
                "mapping": ni_ai_reference_rows,
            },
            "analysis": {
                "voltage_divider_ratio": 1000.0,
                "trim_edge_seconds": 2.0,
                "ipg_adc_lsb_uV": IPG_ADC_LSB_UV,
            },
        }

    def _update_runtime_label(self) -> None:
        config = self._collect_config_quiet()
        if config is None:
            self.total_runtime_label.setText("--")
            self.done_button.setEnabled(False)
            return
        total_seconds = 0.0
        mode = config["mode"]
        mapped_ao_channels = {int(channel) for channel, targets in config.get("mapping", {}).items() if targets}
        total_blocks = 0
        if mode in {"amplitude", "both"}:
            blocks = config["amplitude_sweep"]["blocks"]
            active_rows = self._active_row_indices(self.amp_table, mapped_ao_channels)
            total_seconds += sum(blocks[row]["duration_s"] for row in active_rows)
            total_blocks += len(active_rows)
        if mode in {"frequency", "both"}:
            blocks = config["frequency_sweep"]["blocks"]
            active_rows = self._active_row_indices(self.freq_table, mapped_ao_channels)
            total_seconds += sum(blocks[row]["duration_s"] for row in active_rows)
            total_blocks += len(active_rows)
        if total_blocks > 1:
            total_seconds += float(config["inter_block_delay_s"]) * (total_blocks - 1)
        self.total_runtime_label.setText(format_hms(total_seconds))
        self.done_button.setEnabled(True)

    def _collect_config_quiet(self) -> Optional[Dict[str, Any]]:
        try:
            return self._collect_config()
        except Exception:
            return None

    def _on_done(self) -> None:
        config = self._collect_config_or_show_error()
        if config is None:
            return
        self._config = config
        self.accept()

    def _on_leave(self) -> None:
        reply = QtWidgets.QMessageBox.question(
            self,
            "Leave Auto Test Configuration",
            "Save current auto-test configuration before leaving?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Yes,
        )
        if reply == QtWidgets.QMessageBox.Cancel:
            return
        if reply == QtWidgets.QMessageBox.Yes:
            self._save_dialog()
        self.reject()

    def result_config(self) -> Dict[str, Any]:
        return merge_auto_ipg_test_config(self._config)


def get_differential_terminal_config() -> Any:
    if TerminalConfiguration is None:
        return None
    return getattr(TerminalConfiguration, "DIFFERENTIAL", getattr(TerminalConfiguration, "DIFF", None))


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def parse_int_list(text: str, lower: int, upper: int) -> List[int]:
    values: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value < lower or value > upper:
            raise ValueError(f"Value {value} out of range {lower}-{upper}")
        values.append(value)
    return sorted(set(values))


def channels_to_hex_mask(channels: List[int]) -> str:
    mask = 0
    for channel in channels:
        mask |= 1 << (channel - 1)
    return f"{mask:X}"


def channel_text_from_list(channels: List[int]) -> str:
    return ", ".join(str(channel) for channel in channels)


def timestamped_stem(prefix: str, config: Dict[str, Any]) -> str:
    fmt = config["logging"]["timestamp_format"]
    return f"{prefix}_{datetime.now().strftime(fmt)}"


def split_hms(total_seconds: int) -> tuple[int, int, int]:
    total = max(0, int(total_seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return hours, minutes, seconds


def format_hms(total_seconds: float) -> str:
    hours, minutes, seconds = split_hms(int(total_seconds))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def repo_root_from_config(config: Dict[str, Any]) -> Path:
    repo_root = Path(config["app"]["ipg_reference_root"])
    if not repo_root.is_absolute():
        repo_root = (Path(__file__).parent / repo_root).resolve()
    return repo_root


def ensure_ipg_repo_on_path(repo_root: Path) -> None:
    repo_root_str = str(repo_root)
    if repo_root.exists() and repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def load_ipg_modules(repo_root: Path) -> Dict[str, Any]:
    ensure_ipg_repo_on_path(repo_root)
    import ctypes  # type: ignore
    import Blep  # type: ignore
    import SierraAlcp  # type: ignore
    from lib.ble_utils import get_device_address_by_name  # type: ignore
    from lib.recording_decode import BUFFER_SIZE, RecordingDecoder, enabled_channels_from_mask  # type: ignore

    return {
        "ctypes": ctypes,
        "Blep": Blep,
        "SierraAlcp": SierraAlcp,
        "get_device_address_by_name": get_device_address_by_name,
        "BUFFER_SIZE": BUFFER_SIZE,
        "RecordingDecoder": RecordingDecoder,
        "enabled_channels_from_mask": enabled_channels_from_mask,
    }


def resolve_ipg_address(device_number: int, config: Dict[str, Any]) -> str:
    # Updated IPGs are re-flashed, so the legacy address.py book is stale and no
    # longer maps device numbers to BLE addresses. Resolve live by scanning for
    # the device's advertised name (e.g. "SIPGBB 18"), matching the new tool's
    # get_device_address_by_name workflow.
    repo_root = repo_root_from_config(config)
    modules = load_ipg_modules(repo_root)
    scan_timeout_s = config["ipg"]["connection"]["scan_timeout_s"]
    device_name = f"SIPGBB {int(device_number):02d}"
    address = modules["get_device_address_by_name"](
        device_name,
        timeout=scan_timeout_s,
        ensure_loop=True,
    )
    if not address:
        raise RuntimeError(
            f"Unable to resolve IPG address for device {device_number}: "
            f"no BLE device advertising '{device_name}' was found within {scan_timeout_s:g}s. "
            f"Confirm the device is powered, in range, and advertising."
        )
    return address


class NIDaqWorker(QtCore.QObject):
    device_status = QtCore.Signal(dict)
    ai_chunk_ready = QtCore.Signal(dict)
    ai_started = QtCore.Signal()
    ao_started = QtCore.Signal()
    run_finished = QtCore.Signal()
    error = QtCore.Signal(str)
    status_text = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._config: Dict[str, Any] = {}
        self._duration_s = 0.0
        self._mode = ""
        self._stop_event = threading.Event()
        self._ai_thread: Optional[threading.Thread] = None
        self._ao_task = None
        self._ai_task = None

    @QtCore.Slot()
    def check_devices(self) -> None:
        status = {
            "NI-9222": False,
            "NI-9263": False,
            "ai_device_name": "",
            "ao_device_name": "",
            "details": [],
            "driver_available": nidaqmx is not None,
        }
        if nidaqmx is None:
            status["details"].append("nidaqmx Python package not installed")
            self.device_status.emit(status)
            return

        try:
            system = nidaqmx.system.System.local()
            devices = list(system.devices)
            if not devices:
                status["details"].append("No NI-DAQmx devices found. Check NI-MAX and cable connections.")
            for device in devices:
                product_type = getattr(device, "product_type", "")
                name = getattr(device, "name", "")
                status["details"].append(f"{name}: {product_type}")
                if "9222" in product_type:
                    status["NI-9222"] = True
                    status["ai_device_name"] = name
                if "9263" in product_type:
                    status["NI-9263"] = True
                    status["ao_device_name"] = name
        except Exception as exc:
            status["details"].append(f"DAQmx scan error: {exc}")

        self.device_status.emit(status)

    @QtCore.Slot(dict, float, str)
    def prepare_run(self, config: Dict[str, Any], duration_s: float, mode: str) -> None:
        self._config = config
        self._duration_s = duration_s
        self._mode = mode
        self._stop_event.clear()
        self._close_tasks()

    @QtCore.Slot()
    def start_ai(self) -> None:
        if not self._mode_uses_ai():
            self.ai_started.emit()
            return

        ai_config = self._config.get("ni", {}).get("ai", {})
        active_channels = ai_config.get("active_channels", [])
        if not active_channels:
            self.status_text.emit("NI AI skipped: no AI channels are enabled")
            self.ai_started.emit()
            return

        try:
            self._configure_ai_task()
        except Exception as exc:
            self.error.emit(f"Failed to configure AI task: {exc}")
            return

        self._ai_thread = threading.Thread(target=self._ai_loop, daemon=True)
        self._ai_thread.start()
        self.ai_started.emit()
        channels_str = ", ".join(str(c) for c in active_channels)
        self.status_text.emit(f"NI AI task started: streaming channels [{channels_str}] at {ai_config.get('sample_rate_hz', 0)} Hz")

    @QtCore.Slot()
    def start_ao(self) -> None:
        if not self._mode_uses_ao():
            self.ao_started.emit()
            return

        ao_config = self._config.get("ni", {}).get("ao", {})
        if not ao_config.get("active_channels"):
            self.status_text.emit("NI AO skipped: no AO channels are enabled")
            self.ao_started.emit()
            return

        try:
            self._configure_ao_task()
            if self._ao_task is not None:
                self._ao_task.start()
                ao_cfg = self._config["ni"]["ao"]
                waveforms = ao_cfg.get("waveforms", {})
                has_replay = any(
                    str(waveforms.get(str(channel), waveforms.get("0", {})).get("output_mode", "waveform")) == "replay"
                    for channel in ao_cfg["active_channels"]
                )
                applied_sample_rate = REPLAY_FORCED_AO_SAMPLE_RATE_HZ if has_replay else ao_cfg["sample_rate_hz"]
                self.status_text.emit(
                    "NI AO applied: "
                    f"device={self._config['ni']['devices']['ao_device_name']}, "
                    f"channels={ao_cfg['active_channels']}, "
                    f"sample_rate={applied_sample_rate} Hz, "
                    f"range=[{ao_cfg['voltage_range']['min']}, {ao_cfg['voltage_range']['max']}] V"
                )
                for channel in ao_cfg["active_channels"]:
                    definition = waveforms.get(str(channel), waveforms.get("0", {}))
                    if str(definition.get("output_mode", "waveform")) == "replay":
                        replay_file = str(definition.get("replay_file_path", ""))
                        self.status_text.emit(
                            "NI AO channel applied: "
                            f"ch={channel}, "
                            "mode=replay, "
                            f"file={Path(replay_file).name if replay_file else '(unset)'}, "
                            f"offset={float(definition.get('offset_v', 0.0)):.3f} V, "
                            f"on={float(definition.get('on_time_s', 0.0)):.2f} s, "
                            f"off={float(definition.get('off_time_s', 0.0)):.2f} s"
                        )
                    else:
                        self.status_text.emit(
                            "NI AO channel applied: "
                            f"ch={channel}, "
                            "mode=waveform, "
                            f"type={definition.get('type', 'sine')}, "
                            f"freq={float(definition.get('frequency_hz', 10.0)):.2f} Hz, "
                            f"amp={float(definition.get('amplitude_v', 0.5)):.3f} V, "
                            f"offset={float(definition.get('offset_v', 0.0)):.3f} V, "
                            f"on={float(definition.get('on_time_s', 0.0)):.2f} s, "
                            f"off={float(definition.get('off_time_s', 0.0)):.2f} s"
                        )
        except Exception as exc:
            self.error.emit(f"Failed to configure AO task: {exc}")
            return

        self.ao_started.emit()
        self.status_text.emit("NI AO task started")

    @QtCore.Slot()
    def stop(self) -> None:
        self._stop_event.set()
        # Avoid closing task objects immediately; the AI loop may still be
        # inside nidaqmx.read(). The loop owns final cleanup.
        try:
            if self._ai_task is not None:
                self._ai_task.stop()
        except Exception:
            pass
        try:
            if self._ao_task is not None:
                self._ao_task.stop()
        except Exception:
            pass
        self.status_text.emit("NI tasks stopped")

    @QtCore.Slot()
    def close_tasks(self) -> None:
        self._close_tasks()

    def _mode_uses_ai(self) -> bool:
        return self._mode in {"IPG Rec Only", "IPG Stim Only", "Simultaneous Rec & Stim"}

    def _mode_uses_ao(self) -> bool:
        return self._mode in {"IPG Rec Only", "Simultaneous Rec & Stim"}

    def _configure_ai_task(self) -> None:
        if nidaqmx is None:
            self._ai_task = None
            return

        ai_config = self._config["ni"]["ai"]
        device_name = self._config["ni"]["devices"]["ai_device_name"]
        channel_settings = ai_config.get("channel_settings", {})
        default_min = ai_config["voltage_range"]["min"]
        default_max = ai_config["voltage_range"]["max"]
        terminal_config = get_differential_terminal_config()
        if terminal_config is None:
            raise RuntimeError("Differential terminal configuration is not available in this nidaqmx version")
        task = nidaqmx.Task()
        for channel in ai_config["active_channels"]:
            channel_cfg = channel_settings.get(str(int(channel) + 1), {})
            channel_min = float(channel_cfg.get("min_v", default_min))
            channel_max = float(channel_cfg.get("max_v", default_max))
            task.ai_channels.add_ai_voltage_chan(
                f"{device_name}/ai{channel}",
                min_val=channel_min,
                max_val=channel_max,
                terminal_config=terminal_config,
            )
        # Buffer several seconds of data to tolerate short scheduling stalls while
        # IPG decoding, plotting, and logging run concurrently.
        ai_rate = ai_config["sample_rate_hz"]
        ai_buf = max(ai_config["chunk_samples_per_channel"] * 40, int(ai_rate * 5))
        task.timing.cfg_samp_clk_timing(
            rate=ai_rate,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=ai_buf,
        )
        task.start()
        self._ai_task = task

    def _configure_ao_task(self) -> None:
        if nidaqmx is None:
            self._ao_task = None
            return

        ao_config = self._config["ni"]["ao"]
        device_name = self._config["ni"]["devices"]["ao_device_name"]
        task = nidaqmx.Task()
        active_channels = ao_config["active_channels"]
        if not active_channels:
            raise RuntimeError("No NI AO channels are enabled")
        for channel in active_channels:
            task.ao_channels.add_ao_voltage_chan(
                f"{device_name}/ao{channel}",
                min_val=ao_config["voltage_range"]["min"],
                max_val=ao_config["voltage_range"]["max"],
            )

        sample_rate = float(ao_config["sample_rate_hz"])
        waveforms = ao_config.get("waveforms", {})
        replay_channels = []
        for channel in active_channels:
            definition = waveforms.get(str(channel), waveforms.get("0", {}))
            if str(definition.get("output_mode", "waveform")) == "replay":
                replay_channels.append(channel)
        if replay_channels and sample_rate != REPLAY_FORCED_AO_SAMPLE_RATE_HZ:
            sample_rate = float(REPLAY_FORCED_AO_SAMPLE_RATE_HZ)
            self.status_text.emit(
                f"NI AO replay mode detected on channels {replay_channels}; forcing sample rate to {REPLAY_FORCED_AO_SAMPLE_RATE_HZ} Hz"
            )

        replay_lengths = []
        prepared_definitions: Dict[int, Dict[str, Any]] = {}
        for channel in active_channels:
            definition = dict(waveforms.get(str(channel), waveforms.get("0", {})))
            if str(definition.get("output_mode", "waveform")) == "replay":
                replay_path = str(definition.get("replay_file_path", "")).strip()
                if not replay_path:
                    raise RuntimeError(f"AO channel {channel} replay mode selected but no replay file path is set")
                replay_values = self._load_replay_signal(Path(replay_path), sample_rate)
                if not replay_values:
                    raise RuntimeError(f"AO channel {channel} replay file has no usable samples: {replay_path}")
                definition["_replay_values_v"] = replay_values
                replay_lengths.append(len(replay_values))
            prepared_definitions[channel] = definition


        # --- Patch: Ensure integer number of periods for all waveform channels ---
        def lcm(a, b):
            import math
            return abs(a * b) // math.gcd(int(a), int(b)) if a and b else max(a, b)

        cycle_durations_s = []
        period_samples_list = []
        min_waveform_freq_hz: Optional[float] = None
        for channel in active_channels:
            definition = prepared_definitions[channel]
            on_time_s = max(float(definition.get("on_time_s", 0.0)), 0.0)
            off_time_s = max(float(definition.get("off_time_s", 0.0)), 0.0)
            if on_time_s > 0.0 and off_time_s > 0.0:
                cycle_durations_s.append(on_time_s + off_time_s)
            if str(definition.get("output_mode", "waveform")) == "waveform":
                freq_hz = max(float(definition.get("frequency_hz", 0.0)), 0.0)
                if freq_hz > 0.0:
                    if min_waveform_freq_hz is None or freq_hz < min_waveform_freq_hz:
                        min_waveform_freq_hz = freq_hz
                    period_samples = int(round(sample_rate / freq_hz))
                    period_samples_list.append(period_samples)

        # Compute LCM of all period_samples to get integer cycles for all channels
        lcm_period_samples = 1
        for ps in period_samples_list:
            lcm_period_samples = lcm(lcm_period_samples, ps)

        # Buffer must be at least as long as the longest gating cycle or replay length
        min_samples = 32
        if cycle_durations_s:
            min_samples = max(min_samples, int(sample_rate * max(cycle_durations_s)))
        if replay_lengths:
            min_samples = max(min_samples, max(replay_lengths))

        # Cap the generated AO buffer so odd frequency combinations cannot
        # explode startup time or memory usage.
        max_samples = max(min_samples, int(sample_rate * MAX_AO_WAVEFORM_BUFFER_SECONDS))

        # For very low waveform frequencies, ensure at least one full period so
        # the regeneration boundary does not create periodic reset artifacts.
        if min_waveform_freq_hz is not None and min_waveform_freq_hz > 0.0:
            one_period_samples = int(round(sample_rate / min_waveform_freq_hz))
            low_freq_limit_samples = int(sample_rate * MAX_AO_LOW_FREQ_FULL_PERIOD_MAX_SECONDS)
            if one_period_samples <= low_freq_limit_samples:
                if one_period_samples > max_samples:
                    self.status_text.emit(
                        "NI AO low-frequency mode: expanding waveform buffer to one full period "
                        f"({one_period_samples} samples, {one_period_samples / sample_rate:.2f} s)"
                    )
                max_samples = max(max_samples, one_period_samples)
            elif one_period_samples > max_samples:
                self.status_text.emit(
                    "NI AO low-frequency warning: one full period exceeds safety limit; "
                    f"using capped buffer ({max_samples} samples, {max_samples / sample_rate:.2f} s). "
                    f"Requested period would be {one_period_samples / sample_rate:.2f} s"
                )

        # Final buffer length: LCM of periods, at least min_samples
        samples = max(lcm_period_samples, min_samples)
        # If LCM is less than min_samples, pad to next multiple of LCM
        if lcm_period_samples > 0 and samples % lcm_period_samples != 0:
            samples = ((samples // lcm_period_samples) + 1) * lcm_period_samples
        if samples > max_samples:
            self.status_text.emit(
                f"NI AO waveform buffer capped at {max_samples} samples to avoid excessive startup delay"
            )
            samples = max_samples

        waveform_block = []
        for channel in active_channels:
            definition = prepared_definitions[channel]
            waveform_block.append(self._build_waveform(definition, sample_rate, samples))

        task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=samples,
        )
        if len(active_channels) == 1:
            task.write(waveform_block[0], auto_start=False)
        else:
            task.write(waveform_block, auto_start=False)
        self._ao_task = task

    def _load_replay_signal(self, csv_path: Path, target_sample_rate_hz: float) -> List[float]:
        if not csv_path.exists():
            raise RuntimeError(f"Replay file not found: {csv_path}")

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise RuntimeError(f"Replay file has no header: {csv_path}")

            signal_key = "signal_v" if "signal_v" in reader.fieldnames else ("ao_v" if "ao_v" in reader.fieldnames else None)
            if signal_key is None:
                raise RuntimeError(f"Replay file missing signal column (expected signal_v or ao_v): {csv_path}")

            has_time = "time_s" in reader.fieldnames
            signal_values: List[float] = []
            time_values: List[float] = []
            for row in reader:
                if not row:
                    continue
                raw_signal = row.get(signal_key, "")
                if raw_signal in (None, ""):
                    continue
                signal_values.append(float(raw_signal))
                if has_time:
                    raw_time = row.get("time_s", "")
                    if raw_time not in (None, ""):
                        time_values.append(float(raw_time))

        if len(signal_values) < 2:
            return signal_values

        if np is None or len(time_values) != len(signal_values):
            return signal_values

        signal_array = np.asarray(signal_values, dtype=float)
        time_array = np.asarray(time_values, dtype=float)
        time_deltas = np.diff(time_array)
        finite_deltas = time_deltas[np.isfinite(time_deltas)]
        finite_deltas = finite_deltas[finite_deltas > 0]
        if finite_deltas.size == 0:
            return signal_values

        native_dt = float(np.median(finite_deltas))
        native_rate = 1.0 / native_dt if native_dt > 0 else 0.0
        if native_rate <= 0.0 or abs(native_rate - target_sample_rate_hz) < 0.5:
            return signal_values

        duration_s = float(time_array[-1] - time_array[0])
        if duration_s <= 0.0:
            return signal_values
        target_count = max(int(round(duration_s * target_sample_rate_hz)) + 1, 2)
        resampled_time = np.arange(target_count, dtype=float) / target_sample_rate_hz + float(time_array[0])
        resampled_values = np.interp(resampled_time, time_array, signal_array)
        return list(resampled_values)

    def _build_waveform(self, definition: Dict[str, Any], sample_rate: float, samples: int) -> List[float]:
        on_time_s = max(float(definition.get("on_time_s", 0.0)), 0.0)
        off_time_s = max(float(definition.get("off_time_s", 0.0)), 0.0)
        cycle_s = on_time_s + off_time_s
        output_mode = str(definition.get("output_mode", "waveform"))

        if np is not None:
            if output_mode == "replay":
                replay_values = np.asarray(definition.get("_replay_values_v", []), dtype=float)
                if replay_values.size == 0:
                    signal_values = np.zeros(samples, dtype=float)
                else:
                    index = np.mod(np.arange(samples, dtype=int), replay_values.size)
                    signal_values = replay_values[index] + definition.get("offset_v", 0.0)
            else:
                t = np.arange(samples, dtype=float) / sample_rate
                if definition["type"] == "simulated_spike_array":
                    carrier = np.sin(2.0 * np.pi * definition["frequency_hz"] * t)
                    signal_values = np.where(carrier > 0.95, definition["amplitude_v"], 0.0) + definition.get("offset_v", 0.0)
                else:
                    signal_values = (
                        definition["amplitude_v"] * np.sin(2.0 * np.pi * definition["frequency_hz"] * t)
                        + definition.get("offset_v", 0.0)
                    )

            t = np.arange(samples, dtype=float) / sample_rate

            if off_time_s > 0.0:
                if on_time_s <= 0.0 or cycle_s <= 0.0:
                    signal_values = np.zeros_like(signal_values)
                else:
                    phase = np.mod(t, cycle_s)
                    gate = phase < on_time_s
                    signal_values = np.where(gate, signal_values, 0.0)

            return list(signal_values)

        values = []
        for index in range(samples):
            timestamp = index / sample_rate
            if output_mode == "replay":
                replay_values = definition.get("_replay_values_v", [])
                if replay_values:
                    replay_index = index % len(replay_values)
                    value = float(replay_values[replay_index])
                else:
                    value = 0.0
            else:
                if definition["type"] == "simulated_spike_array":
                    carrier = math.sin(2.0 * math.pi * definition["frequency_hz"] * timestamp)
                    value = definition["amplitude_v"] if carrier > 0.95 else 0.0
                else:
                    value = definition["amplitude_v"] * math.sin(2.0 * math.pi * definition["frequency_hz"] * timestamp)

            value += definition.get("offset_v", 0.0)
            if off_time_s > 0.0:
                if on_time_s <= 0.0 or cycle_s <= 0.0:
                    value = 0.0
                else:
                    phase = timestamp % cycle_s
                    if phase >= on_time_s:
                        value = 0.0
            values.append(value)
        return values

    def _ai_loop(self) -> None:
        ai_config = self._config["ni"]["ai"]
        channels = ai_config["active_channels"]
        sample_rate = ai_config["sample_rate_hz"]
        chunk_samples = ai_config["chunk_samples_per_channel"]
        start_time = time.monotonic()
        phase = 0.0

        while not self._stop_event.is_set() and time.monotonic() - start_time < self._duration_s:
            if self._ai_task is not None:
                try:
                    data = self._ai_task.read(number_of_samples_per_channel=chunk_samples, timeout=1.0)
                    if channels and isinstance(data[0], (float, int)):
                        data = [data]
                except Exception as exc:
                    if self._stop_event.is_set() or "Task specified is invalid or does not exist" in str(exc):
                        break
                    self.error.emit(f"NI AI read failed: {exc}")
                    break
            else:
                data = []
                for offset, _channel in enumerate(channels):
                    samples = []
                    for sample_index in range(chunk_samples):
                        t = phase + sample_index / sample_rate
                        samples.append(0.5 * math.sin(2.0 * math.pi * (20 + offset) * t))
                    data.append(samples)
                phase += chunk_samples / sample_rate
                time.sleep(chunk_samples / sample_rate)

            timestamps = [time.monotonic() - start_time + (index / sample_rate) for index in range(chunk_samples)]
            payload = {
                "sample_rate_hz": sample_rate,
                "channels": {channel: list(map(float, data[index])) for index, channel in enumerate(channels)},
                "timestamps_s": timestamps,
            }
            self.ai_chunk_ready.emit(payload)

        self._close_tasks()
        self.run_finished.emit()

    def _close_tasks(self) -> None:
        for task in (self._ai_task, self._ao_task):
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
        self._ai_task = None
        self._ao_task = None


class IpgWorker(QtCore.QObject):
    connection_changed = QtCore.Signal(bool, str)
    one_time_status = QtCore.Signal(dict)
    battery_status = QtCore.Signal(dict)
    recording_started = QtCore.Signal()
    recording_chunk_ready = QtCore.Signal(dict)
    run_finished = QtCore.Signal()
    error = QtCore.Signal(str)
    status_text = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._config: Dict[str, Any] = {}
        self._duration_s = 0.0
        self._mode = ""
        self._modules: Optional[Dict[str, Any]] = None
        self._ipg = None
        self._address = ""
        self._stop_event = threading.Event()
        self._test_thread: Optional[threading.Thread] = None
        self._recording_thread: Optional[threading.Thread] = None
        self._temperature_decode_mode: Optional[str] = None
        self._last_temperature_c: Optional[float] = None
        self._test_active = False

    @QtCore.Slot(dict, float, str)
    def prepare_run(self, config: Dict[str, Any], duration_s: float, mode: str) -> None:
        self._config = config
        self._duration_s = duration_s
        self._mode = mode
        self._stop_event.clear()

    @QtCore.Slot(str, dict)
    def connect_device(self, device_number_text: str, config: Dict[str, Any]) -> None:
        last_error: Optional[Exception] = None
        try:
            self._config = config
            self._temperature_decode_mode = None
            self._last_temperature_c = None
            device_number = int(device_number_text)
            repo_root = repo_root_from_config(config)
            self._modules = load_ipg_modules(repo_root)

            # Python 3.10+ asyncio event loop fix: Bleak needs an event loop in the thread
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            for attempt in (1, 2):
                try:
                    self._address = resolve_ipg_address(device_number, config)
                    self._ipg = self._modules["SierraAlcp"].SierraAlcp(self._address)
                    self._ipg.connect()
                    # Session role setup remains required by firmware before normal commands.
                    self._ipg.o_set_session_role()
                    status = self._ipg.o_interrogate().Status
                    self._stop_event.clear()
                    self.connection_changed.emit(True, self._address)
                    self.one_time_status.emit(self._status_to_dict(status))
                    self.status_text.emit("IPG connected")
                    return
                except Exception as exc:
                    last_error = exc
                    message = str(exc)
                    is_characteristic_missing = "Characteristic" in message and "was not found" in message
                    try:
                        if self._ipg is not None:
                            self._ipg.disconnect()
                    except Exception:
                        pass
                    self._ipg = None

                    if is_characteristic_missing and attempt < 2:
                        self.status_text.emit(
                            f"IPG connect retry {attempt}/1 after characteristic discovery miss"
                        )
                        time.sleep(0.8 * attempt)
                        continue
                    if attempt < 2:
                        self.status_text.emit(
                            f"IPG connect retry {attempt}/1 after {type(exc).__name__}"
                        )
                        time.sleep(0.5 * attempt)
                        continue
                    break
        except Exception as exc:
            last_error = exc

        self._ipg = None
        self.connection_changed.emit(False, "")
        if last_error is not None:
            self.error.emit(f"IPG connection failed ({type(last_error).__name__}): {repr(last_error)}")
        else:
            self.error.emit("IPG connection failed: unknown error")

    @QtCore.Slot()
    def disconnect_device(self) -> None:
        self.stop_test()
        if self._ipg is not None:
            try:
                self._ipg.disconnect()
            except Exception:
                pass
        self._ipg = None
        self._temperature_decode_mode = None
        self._last_temperature_c = None
        self.connection_changed.emit(False, "")
        self.status_text.emit("IPG disconnected")

    @QtCore.Slot(str, str)
    def start_test(self, recording_mask_hex: str, stim_mask_hex: str) -> None:
        if self._ipg is None:
            self.error.emit("IPG is not connected")
            return
        if self._test_thread is not None and self._test_thread.is_alive():
            self.error.emit("IPG test is already starting")
            return

        self._stop_event.clear()
        self._test_active = True
        duration_seconds = max(1, int(math.ceil(float(self._duration_s))))
        self.status_text.emit(
            f"IPG start command: duration={duration_seconds} s, rec_mask=0x{recording_mask_hex or '0'}, stim_mask=0x{stim_mask_hex or '0'}"
        )

        self._test_thread = threading.Thread(
            target=self._run_test_sequence,
            args=(recording_mask_hex, stim_mask_hex, duration_seconds),
            daemon=True,
        )
        self._test_thread.start()

    def _run_test_sequence(self, recording_mask_hex: str, stim_mask_hex: str, duration_seconds: int) -> None:
        if self._ipg is None:
            self._test_active = False
            self.error.emit("IPG is not connected")
            self.run_finished.emit()
            return

        # Reference behavior: collect battery/status as one-shot snapshots
        # around recording sessions rather than continuous polling during rec/stim.
        pre_payload = self._wait_for_status_snapshot("pre-test", timeout_s=12.0)
        if pre_payload is None and (self._ipg is None or self._stop_event.is_set()):
            self._test_active = False
            self.run_finished.emit()
            return

        # Always clear any latched therapy state before starting a new run.
        # If the device remains in a bad stimulation mode after a previous run,
        # this forces the next run to begin from a known-clean state.
        try:
            self._ipg.o_stop_therapy()
        except Exception:
            pass

        if self._mode in {"IPG Stim Only", "Simultaneous Rec & Stim"} and stim_mask_hex:
            try:
                self._ipg.o_stop_therapy()
            except Exception:
                pass
            try:
                self._ipg.o_start_therapy(int(stim_mask_hex, 16))
                self.status_text.emit(f"IPG stimulation started with mask 0x{stim_mask_hex}")
            except Exception as exc:
                self.error.emit(f"IPG stimulation start failed ({type(exc).__name__}): {repr(exc)}")
            if self._mode == "Simultaneous Rec & Stim":
                time.sleep(0.5)

        if self._mode in {"IPG Rec Only", "Simultaneous Rec & Stim"}:
            self._recording_thread = threading.Thread(
                target=self._recording_loop,
                args=(recording_mask_hex, duration_seconds),
                daemon=True,
            )
            self._recording_thread.start()
            self.recording_started.emit()
            self.status_text.emit("IPG recording started")
            return

        self.recording_started.emit()
        self.status_text.emit("IPG stimulation started")
        self._stim_only_loop()

    @QtCore.Slot()
    def stop_test(self) -> None:
        self._stop_event.set()
        self._test_active = False
        if self._ipg is not None:
            try:
                self._ipg.o_stop_recording()
            except Exception:
                pass
            try:
                self._ipg.o_stop_therapy()
            except Exception:
                pass
        self.status_text.emit("IPG test stopped")

    def _stim_only_loop(self) -> None:
        deadline = time.monotonic() + self._duration_s
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.1)
        self._test_active = False
        if self._ipg is not None:
            try:
                self._ipg.o_stop_therapy()
            except Exception:
                pass
        # Allow firmware to settle before querying status.
        time.sleep(1.5)
        self._wait_for_status_snapshot("post-test", timeout_s=12.0)
        self.run_finished.emit()

    def _read_status_once(self, phase: str) -> Optional[Dict[str, Any]]:
        if self._ipg is None:
            return None

        ready = threading.Event()
        holder: Dict[str, Any] = {}

        def callback(status: Any) -> None:
            holder["status"] = status
            ready.set()

        try:
            self._ipg.subscribe_to_status(callback, send_order=True)
            self._ipg.o_trigger_status_update()
            if not ready.wait(timeout=5.0):
                self.status_text.emit(f"IPG status snapshot timeout ({phase})")
                return None
            status = holder.get("status")
            if status is not None:
                payload = self._status_to_dict(status)
                payload["snapshot_phase"] = phase
                self.battery_status.emit(payload)
                self.status_text.emit(
                    f"IPG {phase} status: battery={payload['battery_voltage_mv']} mV, temp={float(payload.get('temperature_c', 0.0)):.1f} C"
                )
                return payload
        except Exception as exc:
            self.status_text.emit(f"IPG status snapshot warning ({phase}, {type(exc).__name__}): {repr(exc)}")
        finally:
            try:
                self._ipg.unsubscribe_from_status(callback, send_order=False)
            except Exception:
                pass

        return None

    def _wait_for_status_snapshot(self, phase: str, timeout_s: float = 12.0) -> Optional[Dict[str, Any]]:
        self.status_text.emit(f"Waiting for {phase} status...")
        attempts = 0
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while self._ipg is not None:
            if phase == "pre-test" and self._stop_event.is_set():
                return None
            payload = self._read_status_once(phase)
            if payload is not None:
                return payload
            attempts += 1
            if attempts % 3 == 0:
                self.status_text.emit(f"Still waiting for {phase} status...")
            if timeout_s > 0.0 and time.monotonic() >= deadline:
                self.status_text.emit(f"IPG {phase} status snapshot timed out after {timeout_s:.0f} s")
                return None
            time.sleep(1.0)
        return None

    def _recording_loop(self, recording_mask_hex: str, duration_seconds: int) -> None:
        assert self._ipg is not None
        assert self._modules is not None

        channel_bit = int(recording_mask_hex, 16)
        enabled_channels = self._modules["enabled_channels_from_mask"](channel_bit)
        if not enabled_channels:
            self.error.emit("No IPG recording channels selected")
            self.run_finished.emit()
            return

        decoder = self._modules["RecordingDecoder"](
            len(enabled_channels),
            buffer_size=self._modules["BUFFER_SIZE"],
        )
        loop_ready = threading.Event()
        loop_end = threading.Event()
        error_holder: Dict[str, Exception] = {}
        blep_module = self._modules["Blep"]
        sierra_module = self._modules["SierraAlcp"]
        ctypes_module = self._modules["ctypes"]
        first_chunk_logged = False
        chunk_count = 0
        chunk_sample_total = 0
        stats_window_start = time.monotonic()
        loop_start_wall_time: Optional[float] = None  # wall-clock reference for CSV timestamps

        def on_chunk(**kwargs: Any) -> None:
            nonlocal first_chunk_logged
            nonlocal chunk_count, chunk_sample_total, stats_window_start
            nonlocal loop_start_wall_time
            try:
                if kwargs.get("message_type") == blep_module.BlepMessageType.BG_NACK:
                    error_holder["error"] = sierra_module.SierraAlcp.process_nack_message(**kwargs)
                    loop_ready.set()
                    return

                header = sierra_module.RecordingChunkNotificationHeader.from_buffer_copy(kwargs["payload"])
                header_size = ctypes_module.sizeof(sierra_module.RecordingChunkNotificationHeader)
                chunk_data = kwargs["payload"][header_size : header_size + header.ChunkSize]

                if not first_chunk_logged:
                    self.status_text.emit(
                        f"IPG chunk: type={header.ChunkType} size={header.ChunkSize} addr={header.BufferAddress}"
                    )
                    first_chunk_logged = True

                if not chunk_data:
                    if header.IsLastChunk:
                        loop_end.set()
                    loop_ready.set()
                    return

                now = time.monotonic()
                if loop_start_wall_time is None:
                    loop_start_wall_time = now
                chunk_wall_time = now - loop_start_wall_time

                # New RecordingDecoder.process_chunk returns (channel_lists, dubious_lists);
                # the GUI only consumes the sample lists.
                channel_lists, _dubious_lists = decoder.process_chunk(header.BufferAddress, chunk_data)
                chunk_count += 1
                if channel_lists:
                    chunk_sample_total += len(channel_lists[0])
                elapsed = now - stats_window_start
                if elapsed >= 2.0:
                    avg_samples = (chunk_sample_total / chunk_count) if chunk_count else 0.0
                    chunk_rate_hz = (chunk_count / elapsed) if elapsed > 0 else 0.0
                    effective_sample_rate_hz = chunk_rate_hz * avg_samples
                    self.status_text.emit(
                        "IPG streaming: "
                        f"chunk_rate={chunk_rate_hz:.1f} Hz, "
                        f"avg_samples_per_chunk={avg_samples:.1f}, "
                        f"effective_rate={effective_sample_rate_hz:.0f} Hz"
                    )
                    chunk_count = 0
                    chunk_sample_total = 0
                    stats_window_start = now
                n_chunk_samples = len(channel_lists[0]) if channel_lists else 0
                payload = {
                    "sample_rate_hz": IPG_SAMPLE_RATE_HZ,
                    "chunk_wall_time": chunk_wall_time,
                    "chunk_sample_count": n_chunk_samples,
                    "channels": {
                        enabled_channels[index]: [float(sample) * IPG_ADC_LSB_UV for sample in samples]
                        for index, samples in enumerate(channel_lists)
                    },
                }
                self.recording_chunk_ready.emit(payload)
                if header.IsLastChunk:
                    loop_end.set()
            except Exception as exc:
                error_holder["error"] = exc
                loop_end.set()
            finally:
                loop_ready.set()

        try:
            start_exc: Optional[Exception] = None
            for attempt in (1, 2):
                try:
                    self._ipg.start_recording(duration_seconds, 0x01, channel_bit, 1, on_chunk)
                    start_exc = None
                    break
                except TimeoutError as exc:
                    start_exc = exc
                    if attempt == 1 and not self._stop_event.is_set():
                        self.status_text.emit("IPG start_recording timeout; retrying once")
                        try:
                            # Re-assert role before retrying if session state drifted.
                            self._ipg.o_set_session_role()
                        except Exception:
                            pass
                        time.sleep(0.2)
                        continue
                    raise
            if start_exc is not None:
                raise start_exc
            loop_ready.wait(timeout=5.0)
            loop_ready.clear()
            deadline = time.monotonic() + float(self._duration_s) + 0.5
            duration_reached = False
            while not self._stop_event.is_set() and not loop_end.is_set() and time.monotonic() < deadline:
                loop_ready.wait(timeout=1.0)
                loop_ready.clear()
            if not loop_end.is_set() and not self._stop_event.is_set() and time.monotonic() >= deadline:
                self.status_text.emit("IPG recording duration reached; stopping recording")
                duration_reached = True
                self._stop_event.set()
            if self._stop_event.is_set() and not duration_reached:
                try:
                    self._ipg.o_stop_recording()
                except Exception:
                    pass
            if "error" in error_holder:
                raise error_holder["error"]
        except Exception as exc:
            self.error.emit(f"IPG recording failed ({type(exc).__name__}): {repr(exc)}")
        finally:
            self._test_active = False
            # Ensure both recording and therapy are explicitly stopped.
            if self._ipg is not None:
                try:
                    self._ipg.o_stop_recording()
                except Exception:
                    pass
                try:
                    self._ipg.o_stop_therapy()
                except Exception:
                    pass
            # Allow firmware to settle before querying status.
            time.sleep(1.5)
            self._wait_for_status_snapshot("post-test", timeout_s=12.0)
            try:
                self._ipg._unsubscribe_from_channel_notifications(
                    self._modules["SierraAlcp"].Channels.RECORDING,
                    on_chunk,
                    send_order=False,
                )
            except Exception:
                pass
            self.run_finished.emit()

    def _status_to_dict(self, status: Any) -> Dict[str, Any]:
        temperature_raw = int(getattr(status, "Temperature", 0))
        status_temp_c = self._temperature_to_celsius(temperature_raw)
        direct_temp_c = None
        if temperature_raw == 0 or status_temp_c <= 1.0:
            # Some firmware reports status.Temperature as 0 while GetTemperature works.
            direct_temp_c = self._read_temperature_celsius()

        payload: Dict[str, Any] = {
            "battery_voltage_mv": int(getattr(status, "BatteryVoltage", 0)),
            "temperature_raw": temperature_raw,
            "temperature_c": direct_temp_c if direct_temp_c is not None else status_temp_c,
            "charge_level": int(getattr(status, "BatteryChargeLevel", 0)),
            "charging_voltage_mv": int(getattr(status, "ChargingVoltage", 0)),
            "is_charging": int(getattr(status, "IsCharging", 0)),
        }
        self._last_temperature_c = payload["temperature_c"]
        return payload

    def _read_temperature_celsius(self) -> Optional[float]:
        if self._ipg is None:
            return None
        try:
            measurement = self._ipg.o_get_temperature()
            temperature_ad = int(getattr(measurement, "TemperatureAD", 0))
            raw_degrees = int(getattr(measurement, "Degrees", 0))
            signed_value = raw_degrees if -128 <= raw_degrees <= 127 else ((raw_degrees & 0xFF) - 256)
            unsigned_value = raw_degrees & 0xFF

            candidates: Dict[str, float] = {
                "unsigned_deci": float(unsigned_value) / 10.0,
                "signed_deci": float(signed_value) / 10.0,
                "unsigned": float(unsigned_value),
                "signed": float(signed_value),
            }

            # Some firmware variants return a malformed Degrees byte; use ADC-based fallback.
            # This linear approximation is calibrated around room temperature and keeps values in
            # a practical physiologic/lab range for display.
            if 0 < temperature_ad < 4096:
                ad_linear_c = (float(temperature_ad) - 520.0) / 11.0
                candidates["ad_linear"] = ad_linear_c

            degrees_mirrors_ad_low = (unsigned_value == (temperature_ad & 0xFF)) and temperature_ad > 0

            def in_range(value: float, lo: float, hi: float) -> bool:
                return lo <= value <= hi

            # Keep decode mode stable once identified.
            if self._temperature_decode_mode in candidates:
                stable_value = candidates[self._temperature_decode_mode]
                if in_range(stable_value, -10.0, 60.0):
                    self._last_temperature_c = stable_value
                    return stable_value

            # If firmware appears to mirror AD low-byte into Degrees, prefer AD conversion path.
            if degrees_mirrors_ad_low and "ad_linear" in candidates:
                ad_value = candidates["ad_linear"]
                if in_range(ad_value, 5.0, 50.0):
                    self._temperature_decode_mode = "ad_linear"
                    self._last_temperature_c = ad_value
                    return ad_value

            # If Degrees byte is implausible but AD conversion is plausible, trust AD.
            degrees_implausible = not in_range(float(unsigned_value), 5.0, 50.0) and not in_range(float(signed_value), 5.0, 50.0)
            if degrees_implausible and "ad_linear" in candidates:
                ad_value = candidates["ad_linear"]
                if in_range(ad_value, 5.0, 50.0):
                    self._temperature_decode_mode = "ad_linear"
                    self._last_temperature_c = ad_value
                    return ad_value

            # Prefer practical operating range first.
            practical = [(mode, value) for mode, value in candidates.items() if in_range(value, 5.0, 50.0)]

            if practical:
                if self._last_temperature_c is not None:
                    mode, value = min(practical, key=lambda item: abs(item[1] - self._last_temperature_c))
                else:
                    # Warm-start around room temperature when no history exists.
                    mode, value = min(practical, key=lambda item: abs(item[1] - 25.0))
                self._temperature_decode_mode = mode
                self._last_temperature_c = value
                return value

            broad = [(mode, value) for mode, value in candidates.items() if in_range(value, -10.0, 60.0)]
            if broad:
                mode, value = broad[0]
                self._temperature_decode_mode = mode
                self._last_temperature_c = value
                return value

            return None
        except Exception:
            return None

    def _temperature_to_celsius(self, raw_temperature: Any) -> float:
        # Firmware variants encode this field as C, deci-C, or ADC-like values.
        # Prefer a plausible ambient/body range before falling back.
        try:
            raw_value = int(raw_temperature)
        except Exception:
            return 0.0

        signed_byte = raw_value if -128 <= raw_value <= 127 else ((raw_value & 0xFF) - 256)
        unsigned_byte = raw_value & 0xFF
        candidates = [
            float(unsigned_byte) / 10.0,
            float(signed_byte) / 10.0,
            float(raw_value) / 10.0,
            float(unsigned_byte),
            float(signed_byte),
            float(raw_value),
        ]

        for value in candidates:
            if 5.0 <= value <= 50.0:
                return value

        for value in candidates:
            if -10.0 <= value <= 60.0:
                return value

        return float(raw_value)


class ChannelPlotPanel(QtWidgets.QGroupBox):
    def __init__(
        self,
        title: str,
        channel_numbers: List[int],
        color_seed: int,
        columns: int = 1,
        extra_plot_titles: Optional[List[str]] = None,
        extra_plot_spans: Optional[Dict[str, int]] = None,
    ) -> None:
        super().__init__(title)
        self._plots: Dict[int, pg.PlotWidget] = {}
        self._curves: Dict[int, pg.PlotDataItem] = {}
        self._extra_plots: Dict[str, pg.PlotWidget] = {}
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)
        self._columns = max(1, columns)

        extras = extra_plot_titles or []
        extra_plot_spans = extra_plot_spans or {}
        total_extra_slots = sum(max(1, int(extra_plot_spans.get(title, 1))) for title in extras)
        total_plot_count = len(channel_numbers) + total_extra_slots
        rows = max(1, math.ceil(total_plot_count / self._columns))

        for index, channel in enumerate(channel_numbers):
            plot = pg.PlotWidget()
            plot.setMinimumHeight(100)
            plot.showGrid(x=True, y=True, alpha=0.22)
            axis_font = QtGui.QFont("Bahnschrift", 8)
            left_axis = plot.getAxis("left")
            bottom_axis = plot.getAxis("bottom")
            left_axis.setTextPen(pg.mkPen("#b9baff"))
            bottom_axis.setTextPen(pg.mkPen("#8f93d6"))
            left_axis.setTickFont(axis_font)
            bottom_axis.setTickFont(axis_font)
            left_axis.setPen(pg.mkPen("#4f548d"))
            bottom_axis.setPen(pg.mkPen("#4f548d"))
            plot.setLabel("left", f"Ch {channel}", color="#cfd2ff")
            plot.setMenuEnabled(False)
            neon_pastel_palette = [
                "#7DF9FF",  # cyan
                "#B8F2E6",  # mint
                "#FFB3C6",  # pink
                "#C8B6FF",  # lavender
                "#BDE0FE",  # baby blue
                "#FFD6A5",  # peach
                "#CAFFBF",  # light green
                "#F1C0E8",  # rose
                "#A0E7E5",  # aqua
                "#FDFFB6",  # lemon
            ]
            color = neon_pastel_palette[(channel + color_seed) % len(neon_pastel_palette)]
            curve = plot.plot(pen=pg.mkPen(color, width=1.5))
            # Column-major placement so channels fill top-to-bottom per column.
            row = index % rows
            column = index // rows
            layout.addWidget(plot, row, column)
            self._plots[channel] = plot
            self._curves[channel] = curve

        next_extra_slot = len(channel_numbers)
        for title_text in extras:
            index = next_extra_slot
            plot = pg.PlotWidget()
            plot.setMinimumHeight(100)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setLabel("left", title_text)
            plot.setMenuEnabled(False)
            row = index % rows
            column = index // rows
            row_span = max(1, int(extra_plot_spans.get(title_text, 1)))
            layout.addWidget(plot, row, column, row_span, 1)
            self._extra_plots[title_text] = plot
            next_extra_slot += row_span

        for column in range(self._columns):
            layout.setColumnStretch(column, 1)

    def set_visible_channels(self, visible_channels: List[int]) -> None:
        visible = set(visible_channels)
        for channel, plot in self._plots.items():
            plot.setVisible(channel in visible)

    def update_channel(self, channel: int, samples: List[float]) -> None:
        curve = self._curves.get(channel)
        if curve is None:
            return
        curve.setData(samples)

    def get_extra_plot(self, title_text: str) -> Optional[pg.PlotWidget]:
        return self._extra_plots.get(title_text)


class MainWindow(QtWidgets.QMainWindow):
    request_ni_scan = QtCore.Signal()
    request_ni_prepare = QtCore.Signal(dict, float, str)
    request_ni_start_ai = QtCore.Signal()
    request_ni_start_ao = QtCore.Signal()
    request_ni_stop = QtCore.Signal()
    request_ni_close = QtCore.Signal()

    request_ipg_connect = QtCore.Signal(str, dict)
    request_ipg_disconnect = QtCore.Signal()
    request_ipg_prepare = QtCore.Signal(dict, float, str)
    request_ipg_start = QtCore.Signal(str, str)
    request_ipg_stop = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.config = self._load_startup_config()
        self.setWindowTitle(self.config["app"]["window_title"])
        self.resize(1700, 980)

        self.ni_connected = False
        self.ipg_connected = False
        self.ni_run_done = False
        self.ipg_run_done = False
        self.run_active = False
        self.stop_requested_by_user = False
        self.run_started_monotonic = 0.0
        self.current_run_output_dir: Optional[Path] = None

        self.ni_data_queue: "queue.Queue[dict]" = queue.Queue()
        self.ipg_data_queue: "queue.Queue[dict]" = queue.Queue()


        self.ni_live_buffers = {channel: deque(maxlen=4000) for channel in range(MAX_NI_AI_CHANNELS)}
        self.ipg_live_buffers = {channel: deque(maxlen=2048) for channel in range(1, MAX_IPG_CHANNELS + 1)}
        self._session_pre_status: Dict[str, Any] = {}
        self._session_post_status: Dict[str, Any] = {}
        self.last_run_artifacts: Dict[str, Path] = {}

        self.logged_ni_rows: List[List[float]] = []
        self._pending_ni_payloads: List[Dict[str, Any]] = []
        self.logged_ipg_samples: List[List[float]] = []
        self.logged_battery_rows: List[List[float]] = []
        self.ni_elapsed_s = 0.0
        self.ipg_elapsed_s = 0.0
        self._ipg_first_chunk_wall_time: Optional[float] = None
        self._ipg_last_chunk_wall_time: Optional[float] = None
        self._matrix_sync_lock = False
        self._current_run_mode = ""
        self._bypass_ni_this_run = False
        self._bypass_ipg_this_run = False
        self._run_duration_s: float = 0.0
        self.auto_ipg_test_config: Dict[str, Any] = default_auto_ipg_test_config()
        self.auto_ipg_test_ready = False
        self._manual_channel_settings_backup: Optional[Dict[str, Any]] = None
        self.auto_test_active = False
        self.auto_test_plan: List[Dict[str, Any]] = []
        self.auto_test_current_index = -1
        self.auto_test_session_dir: Optional[Path] = None
        self.auto_test_pause_between_blocks = False
        self.auto_test_saved_artifacts: List[Dict[str, Any]] = []
        self._current_run_stem_override: Optional[str] = None
        self._current_auto_block: Optional[Dict[str, Any]] = None

        self.ni_ai_channel_settings = {
            channel: {"enabled": channel <= 8, "min_v": -10.0, "max_v": 10.0}
            for channel in range(1, MAX_NI_AI_CHANNELS + 1)
        }
        self.ni_ao_channel_settings = {
            channel: {
                "enabled": channel <= 4,
                "output_mode": "waveform",
                "type": "sine",
                "frequency_hz": 10.0,
                "amplitude_v": 0.5,
                "offset_v": 0.0,
                "on_time_s": 0.0,
                "off_time_s": 0.0,
                "replay_file_path": "",
            }
            for channel in range(1, MAX_NI_AO_CHANNELS + 1)
        }
        self.ipg_rec_channel_settings = {
            channel: {
                "enabled": channel <= 4,
                "lead": "Lead 1" if channel <= 2 else ("Lead 2" if channel <= 4 else ("Lead 3" if channel <= 6 else "Lead 4")),
                "positive_elec": 1,
                "negative_elec": 2,
                "label": f"Rec {channel}",
            }
            for channel in range(1, MAX_IPG_CHANNELS + 1)
        }
        self.ipg_stim_channel_settings = {
            channel: {
                "enabled": channel <= 2,
                "lead": "Lead 1",
                "anode": 1,
                "cathode": 2,
                "waveform": "square",
                "amplitude_ma": 2.5,
                "pulse_width_ms": 0.06,
                "frequency_hz": 130.0,
                "ratio": 1.0,
                "balance_ms": 0.0,
                "biphasic": True,
                "burst_on_s": 0.0,
                "burst_off_s": 0.0,
                "burst_ramp_up_s": 0.0,
                "burst_ramp_down_s": 0.0,
                "burst_type": "global",
            }
            for channel in range(1, MAX_STIM_CHANNELS + 1)
        }

        self._setup_workers()
        self._build_ui()
        self._connect_signals()
        self._apply_config_to_ui(self.config)
        self._refresh_mode_state()
        self._update_safety_interlock()
        self._update_enable_state()

        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setInterval(self.config["app"]["plot_refresh_interval_ms"])
        self.plot_timer.timeout.connect(self._drain_plot_queues)
        self.plot_timer.start()

        self.elapsed_timer = QtCore.QTimer(self)
        self.elapsed_timer.setInterval(200)
        self.elapsed_timer.timeout.connect(self._update_elapsed_label)

        self.request_ni_scan.emit()

    def _setup_workers(self) -> None:
        self.ni_thread = QtCore.QThread(self)
        self.ni_worker = NIDaqWorker()
        self.ni_worker.moveToThread(self.ni_thread)
        self.ni_thread.start()

        self.ipg_thread = QtCore.QThread(self)
        self.ipg_worker = IpgWorker()
        self.ipg_worker.moveToThread(self.ipg_thread)
        self.ipg_thread.start()

    def _connect_signals(self) -> None:
        self.request_ni_scan.connect(self.ni_worker.check_devices)
        self.request_ni_prepare.connect(self.ni_worker.prepare_run)
        self.request_ni_start_ai.connect(self.ni_worker.start_ai)
        self.request_ni_start_ao.connect(self.ni_worker.start_ao)
        self.request_ni_stop.connect(self.ni_worker.stop)
        self.request_ni_close.connect(self.ni_worker.close_tasks)

        self.request_ipg_connect.connect(self.ipg_worker.connect_device)
        self.request_ipg_disconnect.connect(self.ipg_worker.disconnect_device)
        self.request_ipg_prepare.connect(self.ipg_worker.prepare_run)
        self.request_ipg_start.connect(self.ipg_worker.start_test)
        self.request_ipg_stop.connect(self.ipg_worker.stop_test)

        self.ni_worker.device_status.connect(self._on_ni_status)
        self.ni_worker.ai_chunk_ready.connect(self.ni_data_queue.put)
        self.ni_worker.ai_started.connect(self._handle_ai_started)
        self.ni_worker.ao_started.connect(self._handle_ao_started)
        self.ni_worker.run_finished.connect(self._on_ni_run_finished)
        self.ni_worker.error.connect(self._append_status)
        self.ni_worker.status_text.connect(self._append_status)

        self.ipg_worker.connection_changed.connect(self._on_ipg_connection_changed)
        self.ipg_worker.one_time_status.connect(self._on_one_time_status)
        self.ipg_worker.battery_status.connect(self._on_battery_status)
        self.ipg_worker.recording_started.connect(lambda: None)
        self.ipg_worker.recording_chunk_ready.connect(self.ipg_data_queue.put)
        self.ipg_worker.run_finished.connect(self._on_ipg_run_finished)
        self.ipg_worker.error.connect(self._append_status)
        self.ipg_worker.status_text.connect(self._append_status)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        settings_bar = QtWidgets.QHBoxLayout()
        self.toggle_settings_button = QtWidgets.QToolButton()
        self.toggle_settings_button.setText("Hide Settings")
        self.toggle_settings_button.setCheckable(True)
        self.toggle_settings_button.setChecked(True)
        self.toggle_settings_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        settings_bar.addWidget(self.toggle_settings_button)
        settings_bar.addStretch(1)
        main_layout.addLayout(settings_bar)

        self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        main_layout.addWidget(self.main_splitter, 1)

        self.settings_panel = QtWidgets.QWidget()
        self.settings_panel.setMinimumWidth(420)
        self.settings_panel.setMaximumWidth(620)
        settings_panel_layout = QtWidgets.QVBoxLayout(self.settings_panel)
        settings_panel_layout.setContentsMargins(0, 0, 0, 0)
        settings_panel_layout.setSpacing(6)

        self.settings_scroll = QtWidgets.QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        settings_panel_layout.addWidget(self.settings_scroll, 1)

        settings_content = QtWidgets.QWidget()
        self.settings_scroll.setWidget(settings_content)
        settings_content_layout = QtWidgets.QVBoxLayout(settings_content)
        settings_content_layout.setContentsMargins(0, 0, 0, 0)

        self.settings_tabs = QtWidgets.QToolBox()
        self.settings_tabs.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        settings_content_layout.addWidget(self.settings_tabs)

        self.main_splitter.addWidget(self.settings_panel)

        plot_host = QtWidgets.QWidget()
        plot_host_layout = QtWidgets.QVBoxLayout(plot_host)
        plot_host_layout.setContentsMargins(0, 0, 0, 0)
        plot_host_layout.setSpacing(6)
        self.main_splitter.addWidget(plot_host)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([500, 1180])

        connection_page = QtWidgets.QWidget()
        connection_page_layout = QtWidgets.QVBoxLayout(connection_page)

        self.connection_group = QtWidgets.QGroupBox("Connection & Configuration")
        connection_layout = QtWidgets.QVBoxLayout(self.connection_group)
        connection_layout.setSpacing(8)
        connection_page_layout.addWidget(self.connection_group)

        # ── NI row ──────────────────────────────────────────────
        ni_conn_group = QtWidgets.QGroupBox("NI Devices")
        ni_conn_layout = QtWidgets.QVBoxLayout(ni_conn_group)
        ni_conn_layout.setSpacing(4)

        ni_indicator_row = QtWidgets.QHBoxLayout()
        self.ni_9222_indicator = QtWidgets.QLabel("NI-9222")
        self.ni_9263_indicator = QtWidgets.QLabel("NI-9263")
        self.ni_refresh_button = QtWidgets.QPushButton("Refresh")
        self.ni_refresh_button.setFixedWidth(80)
        ni_indicator_row.addWidget(self.ni_9222_indicator)
        ni_indicator_row.addWidget(self.ni_9263_indicator)
        ni_indicator_row.addStretch(1)
        ni_indicator_row.addWidget(self.ni_refresh_button)
        ni_conn_layout.addLayout(ni_indicator_row)

        self.ni_status_details = QtWidgets.QPlainTextEdit()
        self.ni_status_details.setMaximumHeight(56)
        self.ni_status_details.setReadOnly(True)
        self.ni_status_details.setStyleSheet("font-size: 11px;")
        ni_conn_layout.addWidget(self.ni_status_details)
        connection_layout.addWidget(ni_conn_group)

        # ── IPG row ─────────────────────────────────────────────
        ipg_conn_group = QtWidgets.QGroupBox("IPG Device")
        ipg_conn_layout = QtWidgets.QVBoxLayout(ipg_conn_group)
        ipg_conn_layout.setSpacing(4)

        ipg_top_row = QtWidgets.QHBoxLayout()
        self.ipg_indicator = QtWidgets.QLabel("IPG: Not Connected")
        self.ipg_indicator.setStyleSheet(
            "background: #b00020; color: white; padding: 4px 8px; border-radius: 6px;"
        )
        self.ipg_address_label = QtWidgets.QLabel("")
        self.ipg_address_label.setStyleSheet("color: #555; font-size: 11px;")
        ipg_top_row.addWidget(self.ipg_indicator)
        ipg_top_row.addWidget(self.ipg_address_label)
        ipg_top_row.addStretch(1)
        ipg_conn_layout.addLayout(ipg_top_row)

        ipg_ctrl_row = QtWidgets.QHBoxLayout()
        ipg_ctrl_row.addWidget(QtWidgets.QLabel("Device #"))
        self.ipg_device_number_edit = QtWidgets.QLineEdit()
        self.ipg_device_number_edit.setPlaceholderText("e.g. 21")
        self.ipg_device_number_edit.setFixedWidth(70)
        self.ipg_connect_button = QtWidgets.QPushButton("Connect")
        self.ipg_connect_button.setFixedWidth(100)
        self.ipg_disconnect_button = QtWidgets.QPushButton("Disconnect")
        self.ipg_disconnect_button.setFixedWidth(100)
        self.ipg_disconnect_button.setEnabled(False)
        ipg_ctrl_row.addWidget(self.ipg_device_number_edit)
        ipg_ctrl_row.addWidget(self.ipg_connect_button)
        ipg_ctrl_row.addWidget(self.ipg_disconnect_button)
        ipg_ctrl_row.addStretch(1)
        ipg_conn_layout.addLayout(ipg_ctrl_row)

        ipg_status_row = QtWidgets.QHBoxLayout()
        self.battery_label = QtWidgets.QLabel("Battery: -- mV")
        self.temperature_label = QtWidgets.QLabel("Temperature: --")
        ipg_status_row.addWidget(self.battery_label)
        ipg_status_row.addSpacing(16)
        ipg_status_row.addWidget(self.temperature_label)
        ipg_status_row.addStretch(1)
        ipg_conn_layout.addLayout(ipg_status_row)
        connection_layout.addWidget(ipg_conn_group)

        bypass_group = QtWidgets.QGroupBox("Run-Time Bypass")
        bypass_layout = QtWidgets.QHBoxLayout(bypass_group)
        self.bypass_ni_checkbox = QtWidgets.QCheckBox("Bypass NI (IPG only)")
        self.bypass_ipg_checkbox = QtWidgets.QCheckBox("Bypass IPG (NI only)")
        bypass_layout.addWidget(self.bypass_ni_checkbox)
        bypass_layout.addWidget(self.bypass_ipg_checkbox)
        bypass_layout.addStretch(1)
        connection_page_layout.addWidget(bypass_group)

        auto_test_group = QtWidgets.QGroupBox("Auto IPG Testing")
        auto_test_layout = QtWidgets.QVBoxLayout(auto_test_group)
        auto_test_top_row = QtWidgets.QHBoxLayout()
        self.auto_ipg_testing_checkbox = QtWidgets.QCheckBox("Enable Auto IPG Testing Mode")
        self.auto_ipg_testing_config_button = QtWidgets.QPushButton("Configure Auto Test")
        auto_test_top_row.addWidget(self.auto_ipg_testing_checkbox)
        auto_test_top_row.addWidget(self.auto_ipg_testing_config_button)
        auto_test_top_row.addStretch(1)
        auto_test_layout.addLayout(auto_test_top_row)

        self.auto_ipg_testing_summary_label = QtWidgets.QLabel("Auto IPG testing: not configured")
        self.auto_ipg_testing_summary_label.setWordWrap(True)
        self.auto_ipg_testing_summary_label.setStyleSheet("color: #9ca3af; font-size: 11px;")
        auto_test_layout.addWidget(self.auto_ipg_testing_summary_label)
        connection_page_layout.addWidget(auto_test_group)

        self.config_mgmt_group = QtWidgets.QGroupBox("Configuration Management")
        config_mgmt_layout = QtWidgets.QHBoxLayout(self.config_mgmt_group)
        self.save_config_button = QtWidgets.QPushButton("Save Config")
        self.load_config_button = QtWidgets.QPushButton("Load Config")
        config_mgmt_layout.addWidget(self.save_config_button)
        config_mgmt_layout.addWidget(self.load_config_button)
        connection_page_layout.addWidget(self.config_mgmt_group)

        connection_page_layout.addStretch(1)
        self.settings_tabs.addItem(connection_page, "Connection")

        ni_page = QtWidgets.QWidget()
        ni_page.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        ni_page_layout = QtWidgets.QVBoxLayout(ni_page)
        ni_page_layout.setContentsMargins(0, 0, 0, 0)

        self.ni_group = QtWidgets.QGroupBox("NI Device Configuration")
        self.ni_group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        ni_layout = QtWidgets.QVBoxLayout(self.ni_group)
        ni_page_layout.addWidget(self.ni_group)

        ni_device_row = QtWidgets.QHBoxLayout()
        self.ai_device_name_edit = QtWidgets.QLineEdit()
        self.ai_device_name_edit.setPlaceholderText("e.g. cDAQ1Mod1")
        self.ao_device_name_edit = QtWidgets.QLineEdit()
        self.ao_device_name_edit.setPlaceholderText("e.g. cDAQ1Mod2")
        ni_device_row.addWidget(QtWidgets.QLabel("AI Module"))
        ni_device_row.addWidget(self.ai_device_name_edit)
        ni_device_row.addSpacing(12)
        ni_device_row.addWidget(QtWidgets.QLabel("AO Module"))
        ni_device_row.addWidget(self.ao_device_name_edit)
        ni_device_row.addStretch(1)
        ni_layout.addLayout(ni_device_row)

        ni_global_row = QtWidgets.QHBoxLayout()
        self.ai_sample_rate_spin = QtWidgets.QSpinBox()
        self.ai_sample_rate_spin.setRange(1, 500000)
        self.ao_sample_rate_spin = QtWidgets.QSpinBox()
        self.ao_sample_rate_spin.setRange(1, 500000)
        ni_global_row.addWidget(QtWidgets.QLabel("AI Sample Rate (Hz)"))
        ni_global_row.addWidget(self.ai_sample_rate_spin)
        ni_global_row.addSpacing(12)
        ni_global_row.addWidget(QtWidgets.QLabel("AO Sample Rate (Hz)"))
        ni_global_row.addWidget(self.ao_sample_rate_spin)
        ni_global_row.addStretch(1)
        ni_layout.addLayout(ni_global_row)

        ni_editor_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        ni_editor_split.setMinimumHeight(360)
        ni_layout.addWidget(ni_editor_split, 1)

        ai_editor = QtWidgets.QWidget()
        ai_editor_layout = QtWidgets.QHBoxLayout(ai_editor)
        ai_editor_layout.setContentsMargins(0, 0, 0, 0)
        self.ni_ai_table = QtWidgets.QTableWidget(MAX_NI_AI_CHANNELS, 4)
        self.ni_ai_table.setHorizontalHeaderLabels(["Ch", "Enabled", "Min (V)", "Max (V)"])
        self.ni_ai_table.verticalHeader().setVisible(False)
        self.ni_ai_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ni_ai_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.ni_ai_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ni_ai_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.ni_ai_table.horizontalHeader().setStyleSheet("QHeaderView::section { font-size: 10px; }")
        ai_editor_layout.addWidget(self.ni_ai_table, 3)

        ai_inspector_box = QtWidgets.QGroupBox("AI Channel Inspector")
        ai_inspector = QtWidgets.QFormLayout(ai_inspector_box)
        self.ai_sel_label = QtWidgets.QLabel("NI ai0 (Differential)")
        self.ai_enabled_chk = QtWidgets.QCheckBox("Enabled")
        self.ai_chan_min_spin = QtWidgets.QDoubleSpinBox()
        self.ai_chan_min_spin.setRange(-10.0, 10.0)
        self.ai_chan_min_spin.setDecimals(3)
        self.ai_chan_max_spin = QtWidgets.QDoubleSpinBox()
        self.ai_chan_max_spin.setRange(-10.0, 10.0)
        self.ai_chan_max_spin.setDecimals(3)
        ai_inspector.addRow("Selected", self.ai_sel_label)
        ai_inspector.addRow("", self.ai_enabled_chk)
        ai_inspector.addRow("Min (V)", self.ai_chan_min_spin)
        ai_inspector.addRow("Max (V)", self.ai_chan_max_spin)
        ai_mode_note = QtWidgets.QLabel("Differential mode is automatic: each aiX uses aiX+/aiX- pins.")
        ai_mode_note.setStyleSheet("color: #555; font-size: 11px;")
        ai_mode_note.setWordWrap(True)
        ai_inspector.addRow("", ai_mode_note)
        ai_editor_layout.addWidget(ai_inspector_box, 2)
        ni_editor_split.addWidget(ai_editor)

        ao_editor = QtWidgets.QWidget()
        ao_editor_layout = QtWidgets.QHBoxLayout(ao_editor)
        ao_editor_layout.setContentsMargins(0, 0, 0, 0)
        self.ni_ao_table = QtWidgets.QTableWidget(MAX_NI_AO_CHANNELS, 8)
        self.ni_ao_table.setHorizontalHeaderLabels(["Ch", "Enabled", "Mode", "Type", "Replay", "Freq (Hz)", "On (s)", "Off (s)"])
        self.ni_ao_table.verticalHeader().setVisible(False)
        self.ni_ao_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ni_ao_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.ni_ao_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ni_ao_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.ni_ao_table.horizontalHeader().setStyleSheet("QHeaderView::section { font-size: 10px; }")
        ao_editor_layout.addWidget(self.ni_ao_table, 3)

        ao_inspector_box = QtWidgets.QGroupBox("AO Channel Inspector")
        ao_inspector = QtWidgets.QFormLayout(ao_inspector_box)
        self.ao_sel_label = QtWidgets.QLabel("NI ao0")
        self.ao_enabled_chk = QtWidgets.QCheckBox("Enabled")
        self.ao_output_mode_combo = QtWidgets.QComboBox()
        self.ao_output_mode_combo.addItems(["waveform", "replay"])
        self.ao_chan_type_combo = QtWidgets.QComboBox()
        self.ao_chan_type_combo.addItems(["sine", "simulated_spike_array"])
        self.ao_replay_file_edit = QtWidgets.QLineEdit()
        self.ao_replay_file_edit.setReadOnly(True)
        self.ao_replay_file_browse_button = QtWidgets.QPushButton("Browse")
        self.ao_replay_file_picker = QtWidgets.QWidget()
        ao_replay_file_picker_row = QtWidgets.QHBoxLayout(self.ao_replay_file_picker)
        ao_replay_file_picker_row.setContentsMargins(0, 0, 0, 0)
        ao_replay_file_picker_row.setSpacing(6)
        ao_replay_file_picker_row.addWidget(self.ao_replay_file_edit, 1)
        ao_replay_file_picker_row.addWidget(self.ao_replay_file_browse_button)
        self.ao_chan_freq_spin = QtWidgets.QDoubleSpinBox()
        self.ao_chan_freq_spin.setRange(0.01, 10000.0)
        self.ao_chan_freq_spin.setDecimals(2)
        self.ao_chan_amp_spin = QtWidgets.QDoubleSpinBox()
        self.ao_chan_amp_spin.setRange(0.0, 10.0)
        self.ao_chan_amp_spin.setDecimals(3)
        self.ao_chan_offset_spin = QtWidgets.QDoubleSpinBox()
        self.ao_chan_offset_spin.setRange(-10.0, 10.0)
        self.ao_chan_offset_spin.setDecimals(3)
        self.ao_chan_on_spin = QtWidgets.QDoubleSpinBox()
        self.ao_chan_on_spin.setRange(0.0, 3600.0)
        self.ao_chan_on_spin.setDecimals(2)
        self.ao_chan_off_spin = QtWidgets.QDoubleSpinBox()
        self.ao_chan_off_spin.setRange(0.0, 3600.0)
        self.ao_chan_off_spin.setDecimals(2)
        ao_inspector_box.setMaximumWidth(200)
        ao_inspector.addRow("Selected", self.ao_sel_label)
        ao_inspector.addRow("", self.ao_enabled_chk)
        ao_inspector.addRow("Output", self.ao_output_mode_combo)
        ao_inspector.addRow("Waveform", self.ao_chan_type_combo)
        ao_inspector.addRow("Replay File", self.ao_replay_file_picker)
        ao_inspector.addRow("Freq (Hz)", self.ao_chan_freq_spin)
        ao_inspector.addRow("Amp (V)", self.ao_chan_amp_spin)
        ao_inspector.addRow("Offset (V)", self.ao_chan_offset_spin)
        ao_inspector.addRow("On Time (s)", self.ao_chan_on_spin)
        ao_inspector.addRow("Off Time (s)", self.ao_chan_off_spin)
        ao_editor_layout.addWidget(ao_inspector_box, 2)
        ni_editor_split.addWidget(ao_editor)
        ni_editor_split.setSizes([300, 300])
        self.settings_tabs.addItem(ni_page, "NI Config")

        ipg_page = QtWidgets.QWidget()
        ipg_page.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        ipg_page_layout = QtWidgets.QVBoxLayout(ipg_page)
        ipg_page_layout.setContentsMargins(0, 0, 0, 0)

        self.ipg_group = QtWidgets.QGroupBox("IPG Configuration")
        self.ipg_group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        ipg_layout = QtWidgets.QVBoxLayout(self.ipg_group)
        ipg_page_layout.addWidget(self.ipg_group)

        ipg_editor_split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        ipg_editor_split.setMinimumHeight(360)
        ipg_layout.addWidget(ipg_editor_split, 1)

        rec_editor = QtWidgets.QWidget()
        rec_editor_layout = QtWidgets.QHBoxLayout(rec_editor)
        rec_editor_layout.setContentsMargins(0, 0, 0, 0)
        self.ipg_rec_table = QtWidgets.QTableWidget(MAX_IPG_CHANNELS, 5)
        self.ipg_rec_table.setHorizontalHeaderLabels(["Ch", "Enabled", "Lead", "+Elec", "-Elec"])
        self.ipg_rec_table.verticalHeader().setVisible(False)
        self.ipg_rec_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ipg_rec_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.ipg_rec_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ipg_rec_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.ipg_rec_table.horizontalHeader().setStyleSheet("QHeaderView::section { font-size: 10px; }")
        rec_editor_layout.addWidget(self.ipg_rec_table, 3)

        rec_inspector_box = QtWidgets.QGroupBox("Recording Channel Inspector")
        rec_inspector_scroll_area = QtWidgets.QScrollArea()
        rec_inspector_scroll_area.setWidgetResizable(True)
        rec_inspector_widget = QtWidgets.QWidget()
        rec_inspector = QtWidgets.QFormLayout(rec_inspector_widget)
        
        self.ipg_rec_sel_label = QtWidgets.QLabel("Ch 1")
        self.ipg_rec_enabled_chk = QtWidgets.QCheckBox("Enabled")
        self.ipg_rec_lead_label = QtWidgets.QLabel("Lead 1")
        self.ipg_rec_lead_combo = QtWidgets.QComboBox()
        self.ipg_rec_lead_combo.addItems(["Lead 1", "Lead 2", "Lead 3", "Lead 4"])
        self.ipg_rec_pos_elec_combo = QtWidgets.QComboBox()
        self.ipg_rec_pos_elec_combo.addItems([str(i) for i in range(1, 9)] + ["Case"])
        self.ipg_rec_neg_elec_combo = QtWidgets.QComboBox()
        self.ipg_rec_neg_elec_combo.addItems([str(i) for i in range(1, 9)] + ["Case"])
        
        rec_inspector.addRow("Selected", self.ipg_rec_sel_label)
        rec_inspector.addRow("", self.ipg_rec_enabled_chk)
        rec_inspector.addRow("Lead", self.ipg_rec_lead_label)  # Will be replaced with combo for ch 9-10
        rec_inspector.addRow("+Electrode", self.ipg_rec_pos_elec_combo)
        rec_inspector.addRow("-Electrode", self.ipg_rec_neg_elec_combo)
        
        rec_inspector_scroll_area.setWidget(rec_inspector_widget)
        rec_inspector_box.setLayout(QtWidgets.QVBoxLayout())
        rec_inspector_box.layout().addWidget(rec_inspector_scroll_area)
        rec_inspector_box.layout().setContentsMargins(0, 0, 0, 0)
        rec_editor_layout.addWidget(rec_inspector_box, 2)
        ipg_editor_split.addWidget(rec_editor)

        stim_editor = QtWidgets.QWidget()
        stim_editor_layout = QtWidgets.QHBoxLayout(stim_editor)
        stim_editor_layout.setContentsMargins(0, 0, 0, 0)
        self.ipg_stim_table = QtWidgets.QTableWidget(MAX_STIM_CHANNELS, 5)
        self.ipg_stim_table.setHorizontalHeaderLabels(["Ch", "Enabled", "Lead", "+E", "-E"])
        self.ipg_stim_table.verticalHeader().setVisible(False)
        self.ipg_stim_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ipg_stim_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.ipg_stim_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.ipg_stim_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.ipg_stim_table.horizontalHeader().setStyleSheet("QHeaderView::section { font-size: 10px; }")
        stim_editor_layout.addWidget(self.ipg_stim_table, 3)

        stim_inspector_box = QtWidgets.QGroupBox("Stimulation Channel Inspector")
        stim_inspector_scroll_area = QtWidgets.QScrollArea()
        stim_inspector_scroll_area.setWidgetResizable(True)
        stim_inspector_widget = QtWidgets.QWidget()
        stim_inspector = QtWidgets.QFormLayout(stim_inspector_widget)
        
        self.ipg_stim_sel_label = QtWidgets.QLabel("Ch 1")
        self.ipg_stim_enabled_chk = QtWidgets.QCheckBox("Enabled")
        self.ipg_stim_lead_combo = QtWidgets.QComboBox()
        self.ipg_stim_lead_combo.addItems(["Lead 1", "Lead 2", "Lead 3", "Lead 4"])
        self.ipg_stim_anode_combo = QtWidgets.QComboBox()
        self.ipg_stim_anode_combo.addItems([str(i) for i in range(1, 9)] + ["Case"])
        self.ipg_stim_cathode_combo = QtWidgets.QComboBox()
        self.ipg_stim_cathode_combo.addItems([str(i) for i in range(1, 9)] + ["Case"])
        self.ipg_stim_waveform_combo = QtWidgets.QComboBox()
        self.ipg_stim_waveform_combo.addItems(["square", "sine", "saw", "other"])
        self.ipg_stim_amp_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_amp_spin.setRange(0.0, 50.0)
        self.ipg_stim_amp_spin.setDecimals(2)
        self.ipg_stim_pw_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_pw_spin.setRange(0.01, 100.0)
        self.ipg_stim_pw_spin.setDecimals(2)
        self.ipg_stim_rate_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_rate_spin.setRange(1.0, 10000.0)
        self.ipg_stim_rate_spin.setDecimals(1)
        self.ipg_stim_ratio_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_ratio_spin.setRange(0.0, 100.0)
        self.ipg_stim_ratio_spin.setDecimals(2)
        self.ipg_stim_balance_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_balance_spin.setRange(0.0, 1000.0)
        self.ipg_stim_balance_spin.setDecimals(2)
        self.ipg_stim_biphasic_chk = QtWidgets.QCheckBox("Biphasic")
        self.ipg_stim_burst_on_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_burst_on_spin.setRange(0.0, 3600.0)
        self.ipg_stim_burst_on_spin.setDecimals(2)
        self.ipg_stim_burst_off_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_burst_off_spin.setRange(0.0, 3600.0)
        self.ipg_stim_burst_off_spin.setDecimals(2)
        self.ipg_stim_ramp_up_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_ramp_up_spin.setRange(0.0, 360.0)
        self.ipg_stim_ramp_up_spin.setDecimals(2)
        self.ipg_stim_ramp_down_spin = QtWidgets.QDoubleSpinBox()
        self.ipg_stim_ramp_down_spin.setRange(0.0, 360.0)
        self.ipg_stim_ramp_down_spin.setDecimals(2)
        self.ipg_stim_burst_type_combo = QtWidgets.QComboBox()
        self.ipg_stim_burst_type_combo.addItems(["global", "per_burst"])
        
        stim_inspector.addRow("Selected", self.ipg_stim_sel_label)
        stim_inspector.addRow("", self.ipg_stim_enabled_chk)
        stim_inspector.addRow("Lead", self.ipg_stim_lead_combo)
        stim_inspector.addRow("Anode", self.ipg_stim_anode_combo)
        stim_inspector.addRow("Cathode", self.ipg_stim_cathode_combo)
        stim_inspector.addRow("Waveform", self.ipg_stim_waveform_combo)
        stim_inspector.addRow("Amplitude (mA)", self.ipg_stim_amp_spin)
        stim_inspector.addRow("Pulse Width (ms)", self.ipg_stim_pw_spin)
        stim_inspector.addRow("Frequency (Hz)", self.ipg_stim_rate_spin)
        stim_inspector.addRow("Ratio", self.ipg_stim_ratio_spin)
        stim_inspector.addRow("Balance (ms)", self.ipg_stim_balance_spin)
        stim_inspector.addRow("", self.ipg_stim_biphasic_chk)
        stim_inspector.addRow("Burst On (s)", self.ipg_stim_burst_on_spin)
        stim_inspector.addRow("Burst Off (s)", self.ipg_stim_burst_off_spin)
        stim_inspector.addRow("Burst Ramp Up (s)", self.ipg_stim_ramp_up_spin)
        stim_inspector.addRow("Burst Ramp Down (s)", self.ipg_stim_ramp_down_spin)
        stim_inspector.addRow("Burst Type", self.ipg_stim_burst_type_combo)
        
        stim_inspector_scroll_area.setWidget(stim_inspector_widget)
        stim_inspector_box.setLayout(QtWidgets.QVBoxLayout())
        stim_inspector_box.layout().addWidget(stim_inspector_scroll_area)
        stim_inspector_box.layout().setContentsMargins(0, 0, 0, 0)
        stim_editor_layout.addWidget(stim_inspector_box, 2)
        ipg_editor_split.addWidget(stim_editor)
        ipg_editor_split.setSizes([320, 280])
        self.settings_tabs.addItem(ipg_page, "IPG Config")

        log_page = QtWidgets.QWidget()
        log_page_layout = QtWidgets.QVBoxLayout(log_page)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(2000)
        log_page_layout.addWidget(self.log_view)
        self.settings_tabs.addItem(log_page, "Logs")

        self.exec_group = QtWidgets.QGroupBox("Execution")
        exec_layout = QtWidgets.QGridLayout(self.exec_group)
        self.duration_hours_spin = QtWidgets.QSpinBox()
        self.duration_hours_spin.setRange(0, 999)
        self.duration_minutes_spin = QtWidgets.QSpinBox()
        self.duration_minutes_spin.setRange(0, 59)
        self.duration_seconds_spin = QtWidgets.QSpinBox()
        self.duration_seconds_spin.setRange(0, 59)

        self.start_button = QtWidgets.QPushButton("Start")
        self.start_button.setMinimumHeight(42)
        self.start_button.setMinimumWidth(165)
        self.start_button.setStyleSheet(
            "QPushButton {"
            "background: #10b981;"
            "color: #04170f;"
            "font-weight: 700;"
            "font-size: 14px;"
            "border-radius: 8px;"
            "padding: 6px 12px;"
            "}"
            "QPushButton:hover { background: #22c79a; }"
            "QPushButton:disabled { background: #2a4f43; color: #8cb5a8; }"
        )
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setMinimumHeight(42)
        self.stop_button.setMinimumWidth(95)
        self.stop_button.setStyleSheet(
            "QPushButton {"
            "background: #f97316;"
            "color: #240a00;"
            "font-weight: 700;"
            "font-size: 14px;"
            "border-radius: 8px;"
            "padding: 6px 12px;"
            "}"
            "QPushButton:hover { background: #fb923c; }"
            "QPushButton:disabled { background: #4d2f1c; color: #b79a86; }"
        )
        self.stop_button.setEnabled(False)
        self.pause_between_blocks_button = QtWidgets.QPushButton("Puase Test")
        self.pause_between_blocks_button.setCheckable(True)
        self.pause_between_blocks_button.setMinimumHeight(32)
        self.pause_between_blocks_button.setMinimumWidth(145)
        self.pause_between_blocks_button.setStyleSheet(
            "QPushButton {"
            "background: #eab308;"
            "color: #2a1a00;"
            "font-weight: 700;"
            "font-size: 11px;"
            "border-radius: 8px;"
            "padding: 3px 8px;"
            "}"
            "QPushButton:hover { background: #facc15; }"
            "QPushButton:checked { background: #ca8a04; color: #fff7e6; }"
            "QPushButton:disabled { background: #57534e; color: #b8b0a2; }"
        )
        self.generate_report_button = QtWidgets.QPushButton("Generate Report")
        self.generate_report_button.setMinimumHeight(42)
        self.generate_report_button.setMinimumWidth(165)
        self.generate_report_button.setStyleSheet(
            "QPushButton {"
            "background: #38bdf8;"
            "color: #041624;"
            "font-weight: 700;"
            "font-size: 14px;"
            "border-radius: 8px;"
            "padding: 6px 12px;"
            "}"
            "QPushButton:hover { background: #67cffb; }"
            "QPushButton:disabled { background: #214052; color: #8ea7b4; }"
        )
        self.generate_report_button.setEnabled(False)
        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.status_label.setMinimumHeight(26)
        self.elapsed_label = QtWidgets.QLabel("Elapsed: 00:00:00")
        self.elapsed_label.setAlignment(QtCore.Qt.AlignCenter)

        duration_row = QtWidgets.QHBoxLayout()
        duration_row.setContentsMargins(0, 0, 0, 0)
        duration_row.setSpacing(6)
        duration_row.addWidget(self.duration_hours_spin)
        duration_row.addWidget(QtWidgets.QLabel("h"))
        duration_row.addWidget(self.duration_minutes_spin)
        duration_row.addWidget(QtWidgets.QLabel("m"))
        duration_row.addWidget(self.duration_seconds_spin)
        duration_row.addWidget(QtWidgets.QLabel("s"))
        duration_row.addStretch(1)
        self.duration_controls_widget = QtWidgets.QWidget()
        self.duration_controls_widget.setLayout(duration_row)

        exec_layout.addWidget(QtWidgets.QLabel("Test Duration"), 0, 0)
        exec_layout.addWidget(self.duration_controls_widget, 0, 1, 1, 5)
        exec_layout.addWidget(self.start_button, 1, 0, 1, 2)
        exec_layout.addWidget(self.stop_button, 1, 2)
        exec_layout.addWidget(self.generate_report_button, 1, 3, 1, 3)
        exec_layout.addWidget(QtWidgets.QLabel("Status"), 2, 0)
        exec_layout.addWidget(self.status_label, 2, 1, 1, 2)
        exec_layout.addWidget(self.elapsed_label, 2, 3, 1, 3)
        self.test_progress_label = QtWidgets.QLabel("Test Progress")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_percent_label = QtWidgets.QLabel("0%")
        self.progress_percent_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.progress_percent_label.setMinimumWidth(36)
        exec_layout.addWidget(self.test_progress_label, 3, 0)
        exec_layout.addWidget(self.progress_bar, 3, 1, 1, 2)
        exec_layout.addWidget(self.progress_percent_label, 3, 3)
        exec_layout.addWidget(self.pause_between_blocks_button, 3, 4, 1, 2)
        exec_layout.setColumnStretch(0, 1)
        exec_layout.setColumnStretch(1, 7)
        exec_layout.setColumnStretch(2, 1)
        exec_layout.setColumnStretch(3, 1)
        exec_layout.setColumnStretch(4, 2)
        exec_layout.setColumnStretch(5, 1)
        self.pause_between_blocks_button.setEnabled(False)
        settings_panel_layout.addWidget(self.exec_group, 0)
        self._set_run_status("Ready")

        plots_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        plot_host_layout.addWidget(plots_splitter, 1)

        self.ipg_plot_panel = ChannelPlotPanel(
            "IPG Recording Plots",
            list(range(1, MAX_IPG_CHANNELS + 1)),
            0,
            columns=3,
        )
        self.ni_plot_panel = ChannelPlotPanel(
            "NI AI Plots",
            list(range(MAX_NI_AI_CHANNELS)),
            20,
            columns=3,
        )

        plots_splitter.addWidget(self.ipg_plot_panel)
        plots_splitter.addWidget(self.ni_plot_panel)
        plots_splitter.setSizes([560, 360])

        self.ni_refresh_button.clicked.connect(lambda: self.request_ni_scan.emit())
        self.ipg_connect_button.clicked.connect(self._connect_ipg)
        self.ipg_disconnect_button.clicked.connect(lambda: self.request_ipg_disconnect.emit())
        self.toggle_settings_button.toggled.connect(self._toggle_settings_panel)
        self.bypass_ni_checkbox.toggled.connect(self._update_enable_state)
        self.bypass_ipg_checkbox.toggled.connect(self._update_enable_state)
        self.auto_ipg_testing_checkbox.toggled.connect(self._on_auto_ipg_testing_toggled)
        self.auto_ipg_testing_config_button.clicked.connect(self._open_auto_ipg_testing_config)
        self.save_config_button.clicked.connect(self._save_config_dialog)
        self.load_config_button.clicked.connect(self._load_config_dialog)
        self.start_button.clicked.connect(self._start_test)
        self.stop_button.clicked.connect(self._stop_test)
        self.pause_between_blocks_button.toggled.connect(self._on_pause_between_blocks_toggled)
        self.generate_report_button.clicked.connect(self._generate_report_for_last_session)

        self.ni_ai_table.itemSelectionChanged.connect(self._on_ai_table_selection_changed)
        self.ni_ao_table.itemSelectionChanged.connect(self._on_ao_table_selection_changed)
        self.ipg_rec_table.itemSelectionChanged.connect(self._on_ipg_rec_table_selection_changed)
        self.ipg_stim_table.itemSelectionChanged.connect(self._on_ipg_stim_table_selection_changed)

        self.ai_enabled_chk.toggled.connect(self._apply_ai_inspector)
        self.ai_chan_min_spin.valueChanged.connect(self._apply_ai_inspector)
        self.ai_chan_max_spin.valueChanged.connect(self._apply_ai_inspector)
        self.ao_enabled_chk.toggled.connect(self._apply_ao_inspector)
        self.ao_output_mode_combo.currentTextChanged.connect(self._apply_ao_inspector)
        self.ao_chan_type_combo.currentTextChanged.connect(self._apply_ao_inspector)
        self.ao_replay_file_browse_button.clicked.connect(self._browse_ao_replay_file)
        self.ao_chan_freq_spin.valueChanged.connect(self._apply_ao_inspector)
        self.ao_chan_amp_spin.valueChanged.connect(self._apply_ao_inspector)
        self.ao_chan_offset_spin.valueChanged.connect(self._apply_ao_inspector)
        self.ao_chan_on_spin.valueChanged.connect(self._apply_ao_inspector)
        self.ao_chan_off_spin.valueChanged.connect(self._apply_ao_inspector)

        self.ipg_rec_enabled_chk.toggled.connect(self._apply_ipg_rec_inspector)
        self.ipg_rec_lead_combo.currentTextChanged.connect(self._apply_ipg_rec_inspector)
        self.ipg_rec_pos_elec_combo.currentTextChanged.connect(self._apply_ipg_rec_inspector)
        self.ipg_rec_neg_elec_combo.currentTextChanged.connect(self._apply_ipg_rec_inspector)
        self.ipg_stim_enabled_chk.toggled.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_lead_combo.currentTextChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_anode_combo.currentTextChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_cathode_combo.currentTextChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_waveform_combo.currentTextChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_amp_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_pw_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_rate_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_ratio_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_balance_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_biphasic_chk.toggled.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_burst_on_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_burst_off_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_ramp_up_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_ramp_down_spin.valueChanged.connect(self._apply_ipg_stim_inspector)
        self.ipg_stim_burst_type_combo.currentTextChanged.connect(self._apply_ipg_stim_inspector)

    def _temperature_to_celsius(self, raw_temperature: Any) -> float:
        # Firmware variants encode this field as C, deci-C, or ADC-like values.
        # Prefer a plausible ambient/body range before falling back.
        try:
            raw_value = int(raw_temperature)
        except Exception:
            return 0.0

        signed_byte = raw_value if -128 <= raw_value <= 127 else ((raw_value & 0xFF) - 256)
        unsigned_byte = raw_value & 0xFF
        candidates = [
            float(unsigned_byte) / 10.0,
            float(signed_byte) / 10.0,
            float(raw_value) / 10.0,
            float(unsigned_byte),
            float(signed_byte),
            float(raw_value),
        ]

        # First pass: practical operating range for lab/room/device telemetry.
        for value in candidates:
            if 5.0 <= value <= 50.0:
                return value

        # Fallback pass: broader plausible range.
        for value in candidates:
            if -10.0 <= value <= 60.0:
                return value

        return float(raw_value)

    def _set_table_row(self, table: QtWidgets.QTableWidget, row: int, values: List[str]) -> None:
        for col, value in enumerate(values):
            item = QtWidgets.QTableWidgetItem(value)
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(row, col, item)

    def _browse_ao_replay_file(self) -> None:
        if self._matrix_sync_lock:
            return
        start_dir = MOCK_DATA_ROOT / "replay_v_512Hz"
        if not start_dir.exists():
            start_dir = MOCK_DATA_ROOT
        selected_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Replay File",
            str(start_dir),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not selected_path:
            return
        self.ao_replay_file_edit.setText(selected_path)
        self._apply_ao_inspector()

    def _update_ao_inspector_mode_fields(self, output_mode: str) -> None:
        replay_mode = output_mode == "replay"
        self.ao_chan_type_combo.setEnabled(not replay_mode)
        self.ao_chan_freq_spin.setEnabled(not replay_mode)
        self.ao_chan_amp_spin.setEnabled(not replay_mode)
        self.ao_replay_file_edit.setEnabled(replay_mode)
        self.ao_replay_file_browse_button.setEnabled(replay_mode)

    def _enforce_ao_sample_rate_for_replay(self) -> None:
        has_replay = any(
            bool(row.get("enabled", False)) and str(row.get("output_mode", "waveform")) == "replay"
            for row in self.ni_ao_channel_settings.values()
        )
        self.ao_sample_rate_spin.setEnabled(not has_replay)
        if has_replay and self.ao_sample_rate_spin.value() != REPLAY_FORCED_AO_SAMPLE_RATE_HZ:
            self.ao_sample_rate_spin.setValue(REPLAY_FORCED_AO_SAMPLE_RATE_HZ)

    def _refresh_ni_tables(self) -> None:
        for channel in range(1, MAX_NI_AI_CHANNELS + 1):
            row = channel - 1
            ai = self.ni_ai_channel_settings[channel]
            self._set_table_row(
                self.ni_ai_table,
                row,
                [
                    str(channel - 1),
                    "Yes" if ai["enabled"] else "No",
                    f"{ai['min_v']:.3f}",
                    f"{ai['max_v']:.3f}",
                ],
            )

        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
            row = channel - 1
            ao = self.ni_ao_channel_settings[channel]
            replay_name = Path(str(ao.get("replay_file_path", ""))).name if ao.get("replay_file_path") else "-"
            self._set_table_row(
                self.ni_ao_table,
                row,
                [
                    str(channel - 1),
                    "Yes" if ao["enabled"] else "No",
                    str(ao.get("output_mode", "waveform")),
                    ao["type"],
                    replay_name,
                    f"{ao['frequency_hz']:.2f}",
                    f"{ao['on_time_s']:.2f}",
                    f"{ao['off_time_s']:.2f}",
                ],
            )

        if self.ni_ai_table.currentRow() < 0:
            self.ni_ai_table.selectRow(0)
        if self.ni_ao_table.currentRow() < 0:
            self.ni_ao_table.selectRow(0)

    def _refresh_ipg_tables(self) -> None:
        for channel in range(1, MAX_IPG_CHANNELS + 1):
            row = channel - 1
            rec = self.ipg_rec_channel_settings[channel]
            pos_elec = "Case" if rec["positive_elec"] == "Case" else str(rec["positive_elec"])
            neg_elec = "Case" if rec["negative_elec"] == "Case" else str(rec["negative_elec"])
            self._set_table_row(
                self.ipg_rec_table,
                row,
                [str(channel), "Yes" if rec["enabled"] else "No", rec["lead"], pos_elec, neg_elec],
            )

        for channel in range(1, MAX_STIM_CHANNELS + 1):
            row = channel - 1
            stim = self.ipg_stim_channel_settings[channel]
            anode = "Case" if stim["anode"] == "Case" else str(stim["anode"])
            cathode = "Case" if stim["cathode"] == "Case" else str(stim["cathode"])
            self._set_table_row(
                self.ipg_stim_table,
                row,
                [
                    str(channel),
                    "Yes" if stim["enabled"] else "No",
                    stim["lead"],
                    anode,
                    cathode,
                ],
            )

        if self.ipg_rec_table.currentRow() < 0:
            self.ipg_rec_table.selectRow(0)
        if self.ipg_stim_table.currentRow() < 0:
            self.ipg_stim_table.selectRow(0)

    def _on_ai_table_selection_changed(self) -> None:
        row = self.ni_ai_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        ai = self.ni_ai_channel_settings[channel]
        self._matrix_sync_lock = True
        self.ai_sel_label.setText(f"NI ai{channel - 1} (Differential)")
        self.ai_enabled_chk.setChecked(bool(ai["enabled"]))
        self.ai_chan_min_spin.setValue(float(ai["min_v"]))
        self.ai_chan_max_spin.setValue(float(ai["max_v"]))
        self._matrix_sync_lock = False

    def _on_ao_table_selection_changed(self) -> None:
        row = self.ni_ao_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        ao = self.ni_ao_channel_settings[channel]
        self._matrix_sync_lock = True
        self.ao_sel_label.setText(f"NI ao{channel - 1}")
        self.ao_enabled_chk.setChecked(bool(ao["enabled"]))
        self.ao_output_mode_combo.setCurrentText(str(ao.get("output_mode", "waveform")))
        self.ao_chan_type_combo.setCurrentText(str(ao["type"]))
        self.ao_replay_file_edit.setText(str(ao.get("replay_file_path", "")))
        self.ao_chan_freq_spin.setValue(float(ao["frequency_hz"]))
        self.ao_chan_amp_spin.setValue(float(ao["amplitude_v"]))
        self.ao_chan_offset_spin.setValue(float(ao["offset_v"]))
        self.ao_chan_on_spin.setValue(float(ao.get("on_time_s", 0.0)))
        self.ao_chan_off_spin.setValue(float(ao.get("off_time_s", 0.0)))
        self._update_ao_inspector_mode_fields(str(ao.get("output_mode", "waveform")))
        self._matrix_sync_lock = False

    def _on_ipg_rec_table_selection_changed(self) -> None:
        row = self.ipg_rec_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        rec = self.ipg_rec_channel_settings[channel]
        self._matrix_sync_lock = True
        self.ipg_rec_sel_label.setText(f"Ch {channel}")
        self.ipg_rec_enabled_chk.setChecked(bool(rec["enabled"]))
        if channel <= 8:
            self.ipg_rec_lead_label.setText(str(rec["lead"]))
            self.ipg_rec_lead_combo.hide()
            self.ipg_rec_lead_label.show()
        else:
            self.ipg_rec_lead_combo.setCurrentText(str(rec["lead"]))
            self.ipg_rec_lead_label.hide()
            self.ipg_rec_lead_combo.show()
        pos_elec = str(rec["positive_elec"]) if rec["positive_elec"] != "Case" else "Case"
        neg_elec = str(rec["negative_elec"]) if rec["negative_elec"] != "Case" else "Case"
        self.ipg_rec_pos_elec_combo.setCurrentText(pos_elec)
        self.ipg_rec_neg_elec_combo.setCurrentText(neg_elec)
        self._matrix_sync_lock = False

    def _on_ipg_stim_table_selection_changed(self) -> None:
        row = self.ipg_stim_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        stim = self.ipg_stim_channel_settings[channel]
        self._matrix_sync_lock = True
        self.ipg_stim_sel_label.setText(f"Ch {channel}")
        self.ipg_stim_enabled_chk.setChecked(bool(stim["enabled"]))
        self.ipg_stim_lead_combo.setCurrentText(str(stim["lead"]))
        anode = str(stim["anode"]) if stim["anode"] != "Case" else "Case"
        cathode = str(stim["cathode"]) if stim["cathode"] != "Case" else "Case"
        self.ipg_stim_anode_combo.setCurrentText(anode)
        self.ipg_stim_cathode_combo.setCurrentText(cathode)
        self.ipg_stim_waveform_combo.setCurrentText(str(stim["waveform"]))
        self.ipg_stim_amp_spin.setValue(float(stim["amplitude_ma"]))
        self.ipg_stim_pw_spin.setValue(float(stim["pulse_width_ms"]))
        self.ipg_stim_rate_spin.setValue(float(stim["frequency_hz"]))
        self.ipg_stim_ratio_spin.setValue(float(stim["ratio"]))
        self.ipg_stim_balance_spin.setValue(float(stim["balance_ms"]))
        self.ipg_stim_biphasic_chk.setChecked(bool(stim["biphasic"]))
        self.ipg_stim_burst_on_spin.setValue(float(stim["burst_on_s"]))
        self.ipg_stim_burst_off_spin.setValue(float(stim["burst_off_s"]))
        self.ipg_stim_ramp_up_spin.setValue(float(stim["burst_ramp_up_s"]))
        self.ipg_stim_ramp_down_spin.setValue(float(stim["burst_ramp_down_s"]))
        self.ipg_stim_burst_type_combo.setCurrentText(str(stim["burst_type"]))
        self._matrix_sync_lock = False

    def _apply_ai_inspector(self) -> None:
        if self._matrix_sync_lock:
            return
        row = self.ni_ai_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        self.ni_ai_channel_settings[channel] = {
            "enabled": self.ai_enabled_chk.isChecked(),
            "min_v": self.ai_chan_min_spin.value(),
            "max_v": self.ai_chan_max_spin.value(),
        }
        self._refresh_ni_tables()
        self.ni_ai_table.selectRow(row)

    def _apply_ao_inspector(self) -> None:
        if self._matrix_sync_lock:
            return
        row = self.ni_ao_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        output_mode = self.ao_output_mode_combo.currentText()
        self.ni_ao_channel_settings[channel] = {
            "enabled": self.ao_enabled_chk.isChecked(),
            "output_mode": output_mode,
            "type": self.ao_chan_type_combo.currentText(),
            "frequency_hz": self.ao_chan_freq_spin.value(),
            "amplitude_v": self.ao_chan_amp_spin.value(),
            "offset_v": self.ao_chan_offset_spin.value(),
            "on_time_s": self.ao_chan_on_spin.value(),
            "off_time_s": self.ao_chan_off_spin.value(),
            "replay_file_path": self.ao_replay_file_edit.text().strip(),
        }
        self._update_ao_inspector_mode_fields(output_mode)
        self._enforce_ao_sample_rate_for_replay()
        self._refresh_ni_tables()
        self.ni_ao_table.selectRow(row)

    def _apply_ipg_rec_inspector(self) -> None:
        if self._matrix_sync_lock:
            return
        row = self.ipg_rec_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        pos_elec_str = self.ipg_rec_pos_elec_combo.currentText()
        neg_elec_str = self.ipg_rec_neg_elec_combo.currentText()
        pos_elec = pos_elec_str if pos_elec_str == "Case" else int(pos_elec_str)
        neg_elec = neg_elec_str if neg_elec_str == "Case" else int(neg_elec_str)
        lead = self.ipg_rec_lead_combo.currentText() if channel > 8 else self.ipg_rec_lead_label.text()
        self.ipg_rec_channel_settings[channel] = {
            "enabled": self.ipg_rec_enabled_chk.isChecked(),
            "lead": lead,
            "positive_elec": pos_elec,
            "negative_elec": neg_elec,
        }
        self._refresh_ipg_tables()
        self.ipg_rec_table.selectRow(row)

    def _apply_ipg_stim_inspector(self) -> None:
        if self._matrix_sync_lock:
            return
        row = self.ipg_stim_table.currentRow()
        if row < 0:
            return
        channel = row + 1
        anode_str = self.ipg_stim_anode_combo.currentText()
        cathode_str = self.ipg_stim_cathode_combo.currentText()
        anode = anode_str if anode_str == "Case" else int(anode_str)
        cathode = cathode_str if cathode_str == "Case" else int(cathode_str)
        self.ipg_stim_channel_settings[channel] = {
            "enabled": self.ipg_stim_enabled_chk.isChecked(),
            "lead": self.ipg_stim_lead_combo.currentText(),
            "anode": anode,
            "cathode": cathode,
            "waveform": self.ipg_stim_waveform_combo.currentText(),
            "amplitude_ma": self.ipg_stim_amp_spin.value(),
            "pulse_width_ms": self.ipg_stim_pw_spin.value(),
            "frequency_hz": self.ipg_stim_rate_spin.value(),
            "ratio": self.ipg_stim_ratio_spin.value(),
            "balance_ms": self.ipg_stim_balance_spin.value(),
            "biphasic": self.ipg_stim_biphasic_chk.isChecked(),
            "burst_on_s": self.ipg_stim_burst_on_spin.value(),
            "burst_off_s": self.ipg_stim_burst_off_spin.value(),
            "burst_ramp_up_s": self.ipg_stim_ramp_up_spin.value(),
            "burst_ramp_down_s": self.ipg_stim_ramp_down_spin.value(),
            "burst_type": self.ipg_stim_burst_type_combo.currentText(),
        }
        self._refresh_ipg_tables()
        self.ipg_stim_table.selectRow(row)

    def _toggle_settings_panel(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)
        self.toggle_settings_button.setText("Hide Settings" if checked else "Show Settings")

    def _open_auto_ipg_testing_config(self) -> None:
        dialog = AutoIpgTestingDialog(self, self.auto_ipg_test_config)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self.auto_ipg_test_config = dialog.result_config()
        self.auto_ipg_test_ready = True
        self.auto_ipg_testing_checkbox.setChecked(True)
        self._update_auto_ipg_testing_summary()
        self._append_status("Auto IPG testing configuration updated")

    def _on_auto_ipg_testing_toggled(self, checked: bool) -> None:
        if checked and not self.auto_ipg_test_ready:
            self._open_auto_ipg_testing_config()
            if not self.auto_ipg_test_ready:
                self.auto_ipg_testing_checkbox.blockSignals(True)
                self.auto_ipg_testing_checkbox.setChecked(False)
                self.auto_ipg_testing_checkbox.blockSignals(False)
                self._update_enable_state()
                return

        if checked:
            self.bypass_ni_checkbox.setChecked(False)
            self.bypass_ipg_checkbox.setChecked(False)

        self.duration_controls_widget.setEnabled(not checked)

        self._apply_auto_ipg_testing_channel_locks(checked)
        self._update_auto_ipg_testing_summary()
        self._update_enable_state()

    def _update_auto_ipg_testing_summary(self) -> None:
        if not self.auto_ipg_test_ready:
            self.auto_ipg_testing_summary_label.setText("Auto IPG testing: not configured")
            return

        cfg = self.auto_ipg_test_config
        mode = str(cfg.get("mode", "frequency"))
        mapping = cfg.get("mapping", {})
        ni_ai_reference = cfg.get("ni_ai_reference", {})
        used_ao = [int(channel) for channel, targets in mapping.items() if isinstance(targets, list) and targets]
        used_rec = sorted({int(value) for values in mapping.values() if isinstance(values, list) for value in values})
        ni_ai_ref_enabled = bool(ni_ai_reference.get("enabled", False))
        ni_ai_ref_rows = ni_ai_reference.get("mapping", []) if isinstance(ni_ai_reference, dict) else []
        if ni_ai_ref_enabled:
            used_ni_ai = sorted(
                {
                    int(row.get("ni_ai_channel", 0)) - 1
                    for row in ni_ai_ref_rows
                    if isinstance(row, dict) and int(row.get("ni_ai_channel", 0)) > 0
                }
            )
        else:
            used_ni_ai = sorted(
                [channel - 1 for channel, row in self.ni_ai_channel_settings.items() if bool(row.get("enabled", False))]
            )
        ref_rec = sorted(
            {
                int(value)
                for row in ni_ai_ref_rows
                if isinstance(row, dict)
                for value in row.get("ipg_rec_channels", [])
                if isinstance(value, int) and 1 <= int(value) <= MAX_IPG_CHANNELS
            }
        )
        total_runtime = self._estimate_auto_ipg_runtime_seconds(cfg)
        freq_blocks = cfg.get("frequency_sweep", {}).get("blocks", [])
        amp_blocks = cfg.get("amplitude_sweep", {}).get("blocks", [])
        fixed_amp = cfg.get("frequency_sweep", {}).get("fixed_amplitude_v", 0.0)
        fixed_freq = cfg.get("amplitude_sweep", {}).get("fixed_frequency_hz", 0.0)

        def _format_range(values: List[float], unit: str) -> str:
            if not values:
                return "n/a"
            return f"{min(values):g}-{max(values):g} {unit}"

        freq_values = [float(block.get("frequency_hz", 0.0)) for block in freq_blocks]
        amp_values = [float(block.get("amplitude_v", 0.0)) for block in amp_blocks]
        self.auto_ipg_testing_summary_label.setText(
            "Auto IPG testing configured: "
            f"session={cfg.get('session_name', 'auto_ipg_test')}, "
            f"mode={mode}, "
            f"AO={used_ao or []}, "
            f"IPG Rec={used_rec or []}, "
            f"NI AI ref={'on' if ni_ai_ref_enabled else 'off'}, "
            f"NI AI={used_ni_ai or []}, ref_IPG={ref_rec or []}, "
            f"freq_blocks={len(freq_blocks)}, amp_blocks={len(amp_blocks)}, "
            f"fixed_amp={fixed_amp:g} V, fixed_freq={fixed_freq:g} Hz, "
            f"freq_range={_format_range(freq_values, 'Hz')}, amp_range={_format_range(amp_values, 'V')}, "
            f"est={format_hms(total_runtime)}"
        )

    def _estimate_auto_ipg_runtime_seconds(self, cfg: Dict[str, Any]) -> float:
        mode = str(cfg.get("mode", "frequency"))
        total_seconds = 0.0
        total_blocks = 0
        if mode in {"amplitude", "both"}:
            amp_blocks = cfg.get("amplitude_sweep", {}).get("blocks", [])
            total_seconds += sum(float(block.get("duration_s", 0.0)) for block in amp_blocks)
            total_blocks += len(amp_blocks)
        if mode in {"frequency", "both"}:
            freq_blocks = cfg.get("frequency_sweep", {}).get("blocks", [])
            total_seconds += sum(float(block.get("duration_s", 0.0)) for block in freq_blocks)
            total_blocks += len(freq_blocks)
        if total_blocks > 1:
            total_seconds += float(cfg.get("inter_block_delay_s", 0.0)) * (total_blocks - 1)
        return total_seconds

    def _apply_auto_ipg_testing_channel_locks(self, enabled: bool) -> None:
        if enabled:
            if self._manual_channel_settings_backup is None:
                self._manual_channel_settings_backup = {
                    "ni_ai": json.loads(json.dumps(self.ni_ai_channel_settings)),
                    "ni_ao": json.loads(json.dumps(self.ni_ao_channel_settings)),
                    "ipg_rec": json.loads(json.dumps(self.ipg_rec_channel_settings)),
                    "ipg_stim": json.loads(json.dumps(self.ipg_stim_channel_settings)),
                }

            mapping = self.auto_ipg_test_config.get("mapping", {})
            ni_ai_reference = self.auto_ipg_test_config.get("ni_ai_reference", {})
            ni_ai_reference_enabled = bool(ni_ai_reference.get("enabled", False))
            used_ao = {
                int(channel)
                for channel, targets in mapping.items()
                if isinstance(targets, list) and targets and str(channel).isdigit()
            }
            used_rec = {
                int(target)
                for targets in mapping.values()
                if isinstance(targets, list)
                for target in targets
                if isinstance(target, int) and 1 <= int(target) <= MAX_IPG_CHANNELS
            }

            # Keep NI AI streaming behavior consistent with main GUI:
            # - If NI AI reference is enabled, lock NI AI channels to mapped channels.
            # - If NI AI reference is disabled, preserve the user's manual NI AI selection.
            if ni_ai_reference_enabled:
                used_ni_ai = {
                    int(row.get("ni_ai_channel", 0))
                    for row in ni_ai_reference.get("mapping", [])
                    if isinstance(row, dict) and int(row.get("ni_ai_channel", 0)) > 0
                }
            else:
                used_ni_ai = {
                    int(channel)
                    for channel, row in self.ni_ai_channel_settings.items()
                    if isinstance(row, dict) and bool(row.get("enabled", False))
                }

            for channel in range(1, MAX_NI_AI_CHANNELS + 1):
                self.ni_ai_channel_settings[channel]["enabled"] = channel in used_ni_ai
            for channel in range(1, MAX_NI_AO_CHANNELS + 1):
                self.ni_ao_channel_settings[channel]["enabled"] = channel in used_ao
            for channel in range(1, MAX_IPG_CHANNELS + 1):
                self.ipg_rec_channel_settings[channel]["enabled"] = channel in used_rec
            for channel in range(1, MAX_STIM_CHANNELS + 1):
                self.ipg_stim_channel_settings[channel]["enabled"] = False

            self.ni_group.setEnabled(False)
            self.ipg_group.setEnabled(False)
            self.bypass_ni_checkbox.setEnabled(False)
            self.bypass_ipg_checkbox.setEnabled(False)
            self.settings_tabs.setItemEnabled(1, False)
            self.settings_tabs.setItemEnabled(2, False)
        else:
            if self._manual_channel_settings_backup is not None:
                self.ni_ai_channel_settings = {
                    int(channel): values for channel, values in self._manual_channel_settings_backup["ni_ai"].items()
                }
                self.ni_ao_channel_settings = {
                    int(channel): values for channel, values in self._manual_channel_settings_backup["ni_ao"].items()
                }
                self.ipg_rec_channel_settings = {
                    int(channel): values for channel, values in self._manual_channel_settings_backup["ipg_rec"].items()
                }
                self.ipg_stim_channel_settings = {
                    int(channel): values for channel, values in self._manual_channel_settings_backup["ipg_stim"].items()
                }
                self._manual_channel_settings_backup = None

            self.ni_group.setEnabled(True)
            self.ipg_group.setEnabled(True)
            self.bypass_ni_checkbox.setEnabled(True)
            self.bypass_ipg_checkbox.setEnabled(True)
            self.settings_tabs.setItemEnabled(1, True)
            self.settings_tabs.setItemEnabled(2, True)

        self._refresh_ni_tables()
        self._refresh_ipg_tables()

    def _load_startup_config(self) -> Dict[str, Any]:
        if not DEFAULT_CONFIG_PATH.exists():
            raise FileNotFoundError(f"Missing default config: {DEFAULT_CONFIG_PATH}")
        return load_json(DEFAULT_CONFIG_PATH)

    def _apply_config_to_ui(self, config: Dict[str, Any]) -> None:
        auto_cfg = merge_auto_ipg_test_config(config.get("execution", {}).get("auto_ipg_testing"))
        self.auto_ipg_test_config = auto_cfg
        self.auto_ipg_test_ready = bool(auto_cfg.get("enabled", False))

        self.ipg_device_number_edit.setText(str(config["ipg"]["connection"]["device_number"]))
        self._set_duration_seconds(int(config["app"]["default_test_duration_s"]))
        self.bypass_ni_checkbox.setChecked(bool(config["execution"].get("bypass_ni", False)))
        self.bypass_ipg_checkbox.setChecked(bool(config["execution"].get("bypass_ipg", False)))
        self.auto_ipg_testing_checkbox.setChecked(bool(auto_cfg.get("enabled", False)))

        self.ai_device_name_edit.setText(config["ni"]["devices"].get("ai_device_name", ""))
        self.ao_device_name_edit.setText(config["ni"]["devices"].get("ao_device_name", ""))
        self.ai_sample_rate_spin.setValue(config["ni"]["ai"]["sample_rate_hz"])
        self.ao_sample_rate_spin.setValue(config["ni"]["ao"]["sample_rate_hz"])

        ai_channel_settings = config["ni"]["ai"].get("channel_settings", {})
        ai_active = set(config["ni"]["ai"]["active_channels"])
        default_ai_min = config["ni"]["ai"]["voltage_range"]["min"]
        default_ai_max = config["ni"]["ai"]["voltage_range"]["max"]
        for channel in range(1, MAX_NI_AI_CHANNELS + 1):
            key = str(channel)
            row_cfg = ai_channel_settings.get(key, {})
            self.ni_ai_channel_settings[channel] = {
                "enabled": (channel - 1) in ai_active if "enabled" not in row_cfg else bool(row_cfg.get("enabled")),
                "min_v": float(row_cfg.get("min_v", default_ai_min)),
                "max_v": float(row_cfg.get("max_v", default_ai_max)),
            }

        ao_active = set(config["ni"]["ao"]["active_channels"])
        ao_waveforms = config["ni"]["ao"].get("waveforms", {})
        
        # Ensure AO voltage range is present in config (for later _configure_ao_task calls)
        if "voltage_range" not in config["ni"]["ao"]:
            config["ni"]["ao"]["voltage_range"] = {"min": -10.0, "max": 10.0}
        
        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
            row_cfg = ao_waveforms.get(str(channel - 1), {})
            self.ni_ao_channel_settings[channel] = {
                "enabled": (channel - 1) in ao_active,
                "output_mode": str(row_cfg.get("output_mode", "waveform")),
                "type": str(row_cfg.get("type", "sine")),
                "frequency_hz": float(row_cfg.get("frequency_hz", 10.0)),
                "amplitude_v": float(row_cfg.get("amplitude_v", 0.5)),
                "offset_v": float(row_cfg.get("offset_v", 0.0)),
                "on_time_s": float(row_cfg.get("on_time_s", 0.0)),
                "off_time_s": float(row_cfg.get("off_time_s", 0.0)),
                "replay_file_path": str(row_cfg.get("replay_file_path", "")),
            }

        self._enforce_ao_sample_rate_for_replay()

        rec_active = set(config["ipg"]["recording"].get("enabled_channels", []))
        rec_row_cfg = config["ipg"]["recording"].get("channel_settings", {})
        for channel in range(1, MAX_IPG_CHANNELS + 1):
            cfg_row = rec_row_cfg.get(str(channel), {})
            default_lead = "Lead 1" if channel <= 2 else ("Lead 2" if channel <= 4 else ("Lead 3" if channel <= 6 else "Lead 4"))
            self.ipg_rec_channel_settings[channel] = {
                "enabled": channel in rec_active if "enabled" not in cfg_row else bool(cfg_row.get("enabled")),
                "lead": str(cfg_row.get("lead", default_lead)),
                "positive_elec": cfg_row.get("positive_elec", 1),
                "negative_elec": cfg_row.get("negative_elec", 2),
            }

        stim_active = set(config["ipg"]["stimulation"].get("enabled_channels", []))
        stim_row_cfg = config["ipg"]["stimulation"].get("channel_settings", {})
        for channel in range(1, MAX_STIM_CHANNELS + 1):
            cfg_row = stim_row_cfg.get(str(channel), {})
            self.ipg_stim_channel_settings[channel] = {
                "enabled": channel in stim_active if "enabled" not in cfg_row else bool(cfg_row.get("enabled")),
                "lead": str(cfg_row.get("lead", "Lead 1")),
                "anode": cfg_row.get("anode", 1),
                "cathode": cfg_row.get("cathode", 2),
                "waveform": str(cfg_row.get("waveform", "square")),
                "amplitude_ma": float(cfg_row.get("amplitude_ma", 2.5)),
                "pulse_width_ms": float(cfg_row.get("pulse_width_ms", 0.06)),
                "frequency_hz": float(cfg_row.get("frequency_hz", 130.0)),
                "ratio": float(cfg_row.get("ratio", 1.0)),
                "balance_ms": float(cfg_row.get("balance_ms", 0.0)),
                "biphasic": bool(cfg_row.get("biphasic", True)),
                "burst_on_s": float(cfg_row.get("burst_on_s", 0.0)),
                "burst_off_s": float(cfg_row.get("burst_off_s", 0.0)),
                "burst_ramp_up_s": float(cfg_row.get("burst_ramp_up_s", 0.0)),
                "burst_ramp_down_s": float(cfg_row.get("burst_ramp_down_s", 0.0)),
                "burst_type": str(cfg_row.get("burst_type", "global")),
            }

        self._refresh_ni_tables()
        self._refresh_ipg_tables()
        self.ipg_plot_panel.set_visible_channels(list(range(1, MAX_IPG_CHANNELS + 1)))
        self.ni_plot_panel.set_visible_channels(list(range(MAX_NI_AI_CHANNELS)))
        self._update_auto_ipg_testing_summary()

    def _set_duration_seconds(self, total_seconds: int) -> None:
        hours, minutes, seconds = split_hms(total_seconds)
        self.duration_hours_spin.setValue(hours)
        self.duration_minutes_spin.setValue(minutes)
        self.duration_seconds_spin.setValue(seconds)

    def _get_duration_seconds(self) -> int:
        return (
            int(self.duration_hours_spin.value()) * 3600
            + int(self.duration_minutes_spin.value()) * 60
            + int(self.duration_seconds_spin.value())
        )

    def _collect_ui_config(self) -> Dict[str, Any]:
        config = json.loads(json.dumps(self.config))
        config["ipg"]["connection"]["device_number"] = int(self.ipg_device_number_edit.text() or 0)
        config["execution"]["bypass_ni"] = self.bypass_ni_checkbox.isChecked()
        config["execution"]["bypass_ipg"] = self.bypass_ipg_checkbox.isChecked()
        config["execution"]["auto_ipg_testing"] = merge_auto_ipg_test_config(self.auto_ipg_test_config)
        config["execution"]["auto_ipg_testing"]["enabled"] = bool(self.auto_ipg_testing_checkbox.isChecked())
        config["app"]["default_test_duration_s"] = self._get_duration_seconds()

        effective_ni_ai_channel_settings = {
            int(channel): dict(values)
            for channel, values in self.ni_ai_channel_settings.items()
        }

        auto_cfg = config.get("execution", {}).get("auto_ipg_testing", {})
        if bool(auto_cfg.get("enabled", False)):
            ni_ai_reference = auto_cfg.get("ni_ai_reference", {})
            if isinstance(ni_ai_reference, dict) and bool(ni_ai_reference.get("enabled", False)):
                mapped_channels = {
                    int(row.get("ni_ai_channel", 0))
                    for row in ni_ai_reference.get("mapping", [])
                    if isinstance(row, dict) and 1 <= int(row.get("ni_ai_channel", 0)) <= MAX_NI_AI_CHANNELS
                }
                if mapped_channels:
                    for channel in range(1, MAX_NI_AI_CHANNELS + 1):
                        effective_ni_ai_channel_settings[channel]["enabled"] = channel in mapped_channels

        ai_active_channels = [
            channel - 1
            for channel, row in effective_ni_ai_channel_settings.items()
            if bool(row.get("enabled", False))
        ]
        ai_min_values = [float(row.get("min_v", -10.0)) for row in effective_ni_ai_channel_settings.values()]
        ai_max_values = [float(row.get("max_v", 10.0)) for row in effective_ni_ai_channel_settings.values()]
        config["ni"]["devices"]["ai_device_name"] = self.ai_device_name_edit.text().strip()
        config["ni"]["devices"]["ao_device_name"] = self.ao_device_name_edit.text().strip()
        config["ni"]["ai"]["active_channels"] = ai_active_channels
        config["ni"]["ai"]["sample_rate_hz"] = self.ai_sample_rate_spin.value()
        config["ni"]["ai"]["voltage_range"]["min"] = min(ai_min_values) if ai_min_values else -10.0
        config["ni"]["ai"]["voltage_range"]["max"] = max(ai_max_values) if ai_max_values else 10.0
        config["ni"]["ai"]["channel_settings"] = {
            str(channel): {
                "enabled": bool(row.get("enabled", False)),
                "min_v": float(row.get("min_v", -10.0)),
                "max_v": float(row.get("max_v", 10.0)),
            }
            for channel, row in effective_ni_ai_channel_settings.items()
        }

        ao_active_channels = [channel - 1 for channel, row in self.ni_ao_channel_settings.items() if row["enabled"]]
        config["ni"]["ao"]["active_channels"] = ao_active_channels
        config["ni"]["ao"]["sample_rate_hz"] = self.ao_sample_rate_spin.value()
        
        # Ensure AO voltage range is properly set (preserve from config, default to ±10V)
        config["ni"]["ao"].setdefault("voltage_range", {})
        config["ni"]["ao"]["voltage_range"].setdefault("min", -10.0)
        config["ni"]["ao"]["voltage_range"].setdefault("max", 10.0)
        
        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
            row = self.ni_ao_channel_settings[channel]
            waveform_key = str(channel - 1)
            config["ni"]["ao"].setdefault("waveforms", {})
            config["ni"]["ao"]["waveforms"].setdefault(waveform_key, {})
            config["ni"]["ao"]["waveforms"][waveform_key]["output_mode"] = row.get("output_mode", "waveform")
            config["ni"]["ao"]["waveforms"][waveform_key]["type"] = row["type"]
            config["ni"]["ao"]["waveforms"][waveform_key]["frequency_hz"] = row["frequency_hz"]
            config["ni"]["ao"]["waveforms"][waveform_key]["amplitude_v"] = row["amplitude_v"]
            config["ni"]["ao"]["waveforms"][waveform_key]["offset_v"] = row["offset_v"]
            config["ni"]["ao"]["waveforms"][waveform_key]["on_time_s"] = row["on_time_s"]
            config["ni"]["ao"]["waveforms"][waveform_key]["off_time_s"] = row["off_time_s"]
            config["ni"]["ao"]["waveforms"][waveform_key]["replay_file_path"] = row.get("replay_file_path", "")

        if any(
            bool(row.get("enabled", False)) and str(row.get("output_mode", "waveform")) == "replay"
            for row in self.ni_ao_channel_settings.values()
        ):
            config["ni"]["ao"]["sample_rate_hz"] = REPLAY_FORCED_AO_SAMPLE_RATE_HZ

        config["ipg"]["recording"]["enabled_channels"] = [
            channel for channel, row in self.ipg_rec_channel_settings.items() if row["enabled"]
        ]
        config["ipg"]["recording"]["channel_settings"] = {
            str(channel): {
                "enabled": bool(row["enabled"]),
                "lead": row["lead"],
                "positive_elec": row["positive_elec"],
                "negative_elec": row["negative_elec"],
            }
            for channel, row in self.ipg_rec_channel_settings.items()
        }
        config["ipg"]["stimulation"]["enabled_channels"] = [
            channel for channel, row in self.ipg_stim_channel_settings.items() if row["enabled"]
        ]
        config["ipg"]["stimulation"]["channel_settings"] = {
            str(channel): {
                "enabled": bool(row["enabled"]),
                "lead": row["lead"],
                "anode": row["anode"],
                "cathode": row["cathode"],
                "waveform": row["waveform"],
                "amplitude_ma": float(row["amplitude_ma"]),
                "pulse_width_ms": float(row["pulse_width_ms"]),
                "frequency_hz": float(row["frequency_hz"]),
                "ratio": float(row["ratio"]),
                "balance_ms": float(row["balance_ms"]),
                "biphasic": bool(row["biphasic"]),
                "burst_on_s": float(row["burst_on_s"]),
                "burst_off_s": float(row["burst_off_s"]),
                "burst_ramp_up_s": float(row["burst_ramp_up_s"]),
                "burst_ramp_down_s": float(row["burst_ramp_down_s"]),
                "burst_type": row["burst_type"],
            }
            for channel, row in self.ipg_stim_channel_settings.items()
        }
        return config

    def _save_config_dialog(self) -> None:
        try:
            config = self._collect_ui_config()
        except Exception as exc:
            self._append_status(f"Config collection failed: {exc}")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Config", str(DEFAULT_CONFIG_PATH), "JSON Files (*.json)")
        if not path:
            return
        save_json(Path(path), config)
        self._append_status(f"Saved config to {path}")

    def _load_config_dialog(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load Config", str(DEFAULT_CONFIG_PATH), "JSON Files (*.json)")
        if not path:
            return
        self.config = load_json(Path(path))
        self._apply_config_to_ui(self.config)
        self._refresh_mode_state()
        self._update_safety_interlock()
        self._update_auto_ipg_testing_summary()
        self._append_status(f"Loaded config from {path}")

    def _connect_ipg(self) -> None:
        device_text = self.ipg_device_number_edit.text().strip()
        if not device_text:
            self._append_status("IPG connection failed: enter a device number first")
            return
        try:
            int(device_text)
        except ValueError:
            self._append_status(f"IPG connection failed: '{device_text}' is not a valid device number")
            return
        try:
            config = self._collect_ui_config()
        except Exception as exc:
            self._append_status(f"Invalid configuration: {exc}")
            return
        self.ipg_connect_button.setEnabled(False)
        self.ipg_connect_button.setText("Connecting...")
        self.request_ipg_connect.emit(device_text, config)

    def _on_pause_between_blocks_toggled(self, checked: bool) -> None:
        self.auto_test_pause_between_blocks = checked
        if checked:
            self._append_status("Auto testing will pause between blocks")
        else:
            self._append_status("Auto testing will continue automatically between blocks")

    def _build_auto_test_plan(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        mode = str(cfg.get("mode", "frequency"))
        plan: List[Dict[str, Any]] = []
        mapping_cfg = cfg.get("mapping", {}) if isinstance(cfg, dict) else {}
        mapped_ao_channels = [
            int(channel)
            for channel, targets in mapping_cfg.items()
            if isinstance(targets, list) and targets and str(channel).isdigit()
        ]

        def representative_from_ao_values(ao_values: Dict[str, float], fallback: float) -> float:
            for channel in mapped_ao_channels:
                value = float(ao_values.get(str(channel), 0.0))
                if value != 0.0:
                    return value
            for value in ao_values.values():
                if float(value) != 0.0:
                    return float(value)
            return fallback

        if mode in {"amplitude", "both"}:
            fixed_frequency_hz = float(cfg.get("amplitude_sweep", {}).get("fixed_frequency_hz", 10.0))
            for index, block in enumerate(cfg.get("amplitude_sweep", {}).get("blocks", []), start=1):
                ao_values = {
                    str(channel): float(block.get("ao_values", {}).get(str(channel), block.get("amplitude_v", 0.0)))
                    for channel in range(1, MAX_NI_AO_CHANNELS + 1)
                }
                rep_amplitude_v = representative_from_ao_values(ao_values, float(block.get("amplitude_v", 0.0)))
                plan.append(
                    {
                        "phase": "amplitude",
                        "index": index,
                        "duration_s": float(block.get("duration_s", 0.0)),
                        "frequency_hz": fixed_frequency_hz,
                        "amplitude_v": rep_amplitude_v,
                        "ao_values": ao_values,
                    }
                )
        if mode in {"frequency", "both"}:
            fixed_amplitude_v = float(cfg.get("frequency_sweep", {}).get("fixed_amplitude_v", 2.0))
            for index, block in enumerate(cfg.get("frequency_sweep", {}).get("blocks", []), start=1):
                ao_values = {
                    str(channel): float(block.get("ao_values", {}).get(str(channel), block.get("frequency_hz", 0.0)))
                    for channel in range(1, MAX_NI_AO_CHANNELS + 1)
                }
                rep_frequency_hz = representative_from_ao_values(ao_values, float(block.get("frequency_hz", 0.0)))
                plan.append(
                    {
                        "phase": "frequency",
                        "index": index,
                        "duration_s": float(block.get("duration_s", 0.0)),
                        "frequency_hz": rep_frequency_hz,
                        "amplitude_v": fixed_amplitude_v,
                        "ao_values": ao_values,
                    }
                )
        return plan

    def _apply_auto_block_to_channels(self, block: Dict[str, Any]) -> None:
        mapping = self.auto_ipg_test_config.get("mapping", {})
        used_ao = {
            int(channel)
            for channel, targets in mapping.items()
            if isinstance(targets, list) and targets and str(channel).isdigit()
        }
        used_rec = {
            int(target)
            for targets in mapping.values()
            if isinstance(targets, list)
            for target in targets
            if isinstance(target, int) and 1 <= int(target) <= MAX_IPG_CHANNELS
        }

        frequency_hz = float(block["frequency_hz"])
        amplitude_v = float(block["amplitude_v"])
        ao_values = block.get("ao_values", {})
        for channel in range(1, MAX_NI_AO_CHANNELS + 1):
            row = self.ni_ao_channel_settings[channel]
            row["enabled"] = channel in used_ao
            if row["enabled"]:
                row["output_mode"] = "waveform"
                row["type"] = "sine"
                if block.get("phase") == "frequency":
                    row["frequency_hz"] = float(ao_values.get(str(channel), frequency_hz))
                    row["amplitude_v"] = amplitude_v
                else:
                    row["frequency_hz"] = frequency_hz
                    row["amplitude_v"] = float(ao_values.get(str(channel), amplitude_v))
                row["offset_v"] = 0.0
                row["on_time_s"] = 0.0
                row["off_time_s"] = 0.0
                row["replay_file_path"] = ""

        for channel in range(1, MAX_NI_AI_CHANNELS + 1):
            self.ni_ai_channel_settings[channel]["enabled"] = False
        for channel in range(1, MAX_IPG_CHANNELS + 1):
            self.ipg_rec_channel_settings[channel]["enabled"] = channel in used_rec
        for channel in range(1, MAX_STIM_CHANNELS + 1):
            self.ipg_stim_channel_settings[channel]["enabled"] = False

        self._refresh_ni_tables()
        self._refresh_ipg_tables()

    def _start_auto_test_session(self) -> bool:
        if not self.auto_ipg_test_ready:
            self._append_status("Auto IPG testing is not configured")
            return False

        cfg = merge_auto_ipg_test_config(self.auto_ipg_test_config)
        plan = self._build_auto_test_plan(cfg)
        if not plan:
            self._append_status("Auto IPG testing has no valid blocks")
            return False

        root_dir = ensure_directory(Path(self.config["logging"]["output_directory"]))
        session_stem = timestamped_stem(cfg.get("session_name", "auto_ipg_test"), self.config)
        session_dir = ensure_directory(root_dir / session_stem)
        ensure_directory(session_dir / "report")

        self.auto_test_active = True
        self.auto_test_plan = plan
        self.auto_test_current_index = -1
        self.auto_test_session_dir = session_dir
        self.auto_test_saved_artifacts = []
        self.current_run_output_dir = session_dir

        config_copy = self._collect_ui_config()
        config_copy.setdefault("execution", {})["auto_ipg_testing"] = cfg
        config_copy["execution"]["auto_ipg_testing"]["enabled"] = True
        config_copy["execution"]["auto_ipg_testing"]["session_dir"] = str(session_dir)
        save_json(session_dir / "report" / "auto_test_config.json", config_copy["execution"]["auto_ipg_testing"])
        self.config = config_copy

        self._append_status(f"Auto test session created: {session_dir}")
        return self._start_next_auto_block(announce=True)

    def _start_next_auto_block(self, announce: bool = False) -> bool:
        if not self.auto_test_active:
            return False
        self.auto_test_current_index += 1
        if self.auto_test_current_index >= len(self.auto_test_plan):
            self._finish_auto_test_session(stopped=False)
            return False

        block = self.auto_test_plan[self.auto_test_current_index]
        self._current_auto_block = block
        self._apply_auto_block_to_channels(block)

        duration_s = float(block["duration_s"])
        self._set_duration_seconds(max(1, int(round(duration_s))))

        divider = float(self.auto_ipg_test_config.get("analysis", {}).get("voltage_divider_ratio", 1000.0))
        effective_input = float(block["amplitude_v"]) / divider if divider else 0.0
        ao_values = block.get("ao_values", {})
        ao_summary = ", ".join(f"AO{channel}={float(ao_values.get(str(channel), 0.0)):g}" for channel in range(1, MAX_NI_AO_CHANNELS + 1))
        if announce:
            self._append_status(
                f"Auto block {self.auto_test_current_index + 1}/{len(self.auto_test_plan)}: "
                f"phase={block['phase']} idx={block['index']} f={block['frequency_hz']} Hz "
                f"AO={block['amplitude_v']} V (after divider {effective_input:.6f} V) "
                f"duration={duration_s:.1f} s, settings=({ao_summary})"
            )
        else:
            self._append_status(
                f"Starting next auto block ({self.auto_test_current_index + 1}/{len(self.auto_test_plan)})"
            )

        block_token = f"{self.auto_test_current_index + 1:03d}_{block['phase']}_i{block['index']:02d}_f{float(block['frequency_hz']):g}Hz_a{float(block['amplitude_v']):g}V"
        self._current_run_stem_override = f"run_{datetime.now().strftime(self.config['logging']['timestamp_format'])}_{block_token}"
        self._start_single_run()
        return True

    def _finish_auto_test_session(self, stopped: bool) -> None:
        self.auto_test_active = False
        self._current_auto_block = None
        self._current_run_stem_override = None
        self.pause_between_blocks_button.setEnabled(False)
        self.progress_bar.setValue(100 if not stopped else self.progress_bar.value())
        self.progress_percent_label.setText(f"{self.progress_bar.value()}%")
        if self.auto_test_session_dir is not None:
            summary = {
                "stopped": bool(stopped),
                "total_blocks": len(self.auto_test_plan),
                "completed_blocks": len(self.auto_test_saved_artifacts),
                "artifacts": [
                    {
                        "stem": str(item.get("stem", "")),
                        "ni_path": str(item.get("ni_path", "")),
                        "ipg_path": str(item.get("ipg_path", "")),
                        "battery_path": str(item.get("battery_path", "")),
                        "metadata_path": str(item.get("metadata_path", "")),
                    }
                    for item in self.auto_test_saved_artifacts
                ],
            }
            save_json(self.auto_test_session_dir / "report" / "auto_test_session_summary.json", summary)
            self._append_status(
                f"Auto test summary saved: {self.auto_test_session_dir / 'report' / 'auto_test_session_summary.json'}"
            )
            self._run_auto_test_analysis(self.auto_test_session_dir)
        self._append_status("Auto test session stopped" if stopped else "Auto test session completed")

    def _run_auto_test_analysis(self, session_dir: Path) -> None:
        analyzer_path = Path(__file__).with_name("auto_test_session_analyzer.py")
        summary_path = session_dir / "report" / "auto_test_session_summary.json"
        if not analyzer_path.exists() or not summary_path.exists():
            self._append_status("Auto session analysis skipped: analyzer or summary file missing")
            return

        cmd = [
            sys.executable,
            str(analyzer_path),
            "--session-dir",
            str(session_dir),
            "--summary-json",
            str(summary_path),
        ]

        expected_report_path = session_dir / "report" / "auto_test_report.html"
        legacy_report_path = session_dir / "report" / "auto_test_session_report.md"
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=AUTO_REPORT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            if expected_report_path.exists():
                self._append_status(
                    "Auto session analysis reached timeout but report was produced; opening generated report"
                )
                self._open_report_file(expected_report_path)
                return
            if legacy_report_path.exists():
                self._append_status(
                    "Auto session analysis reached timeout but legacy report was produced; opening generated report"
                )
                self._open_report_file(legacy_report_path)
                return
            self._append_status(
                f"Auto session analysis timed out after {AUTO_REPORT_TIMEOUT_S} s: {exc}"
            )
        except Exception as exc:
            self._append_status(f"Auto session analysis failed: {type(exc).__name__}: {exc}")
            return

        if completed.returncode != 0:
            error_text = (completed.stderr or "").strip() or (completed.stdout or "").strip() or "Unknown error"
            self._append_status(f"Auto session analysis failed: {error_text}")
            return

        payload: Dict[str, Any] = {}
        stdout_text = (completed.stdout or "").strip()
        if stdout_text:
            try:
                payload = json.loads(stdout_text)
            except Exception:
                payload = {"report_path": stdout_text}

        report_path_text = str(payload.get("report_path", "")).strip()
        if report_path_text:
            report_path = Path(report_path_text)
            if report_path.exists():
                self._append_status(f"Auto session report generated: {report_path}")
                self._open_report_file(report_path)

    def _start_test(self) -> None:
        if self.auto_test_active and not self.run_active:
            self._append_status("Resuming auto test sequence")
            self.start_button.setEnabled(False)
            self.pause_between_blocks_button.setEnabled(True)
            self._start_next_auto_block(announce=False)
            return

        try:
            self.config = self._collect_ui_config()
        except Exception as exc:
            self._append_status(f"Invalid configuration: {exc}")
            return

        ni_ai_channels = self.config.get("ni", {}).get("ai", {}).get("active_channels", [])
        self._append_status(f"Run config NI AI active channels: {ni_ai_channels}")

        if self.auto_ipg_testing_checkbox.isChecked():
            if not self.auto_ipg_test_ready:
                self._append_status("Auto IPG testing mode is enabled but not configured")
                return
            if not (self.ni_connected and self.ipg_connected):
                self._append_status("Auto IPG testing mode requires both NI and IPG to be connected")
                return
            if self._start_auto_test_session():
                return
            self._append_status("Auto IPG testing could not start; falling back to single-run")

        self._start_single_run()

    def _start_single_run(self) -> None:
        try:
            self.config = self._collect_ui_config()
        except Exception as exc:
            self._append_status(f"Invalid configuration: {exc}")
            return

        self._update_safety_interlock()
        if not self.start_button.isEnabled():
            return

        self._bypass_ni_this_run = self.bypass_ni_checkbox.isChecked()
        self._bypass_ipg_this_run = self.bypass_ipg_checkbox.isChecked()
        if self._bypass_ni_this_run and self._bypass_ipg_this_run:
            self._append_status("Cannot bypass both NI and IPG at the same time")
            return

        duration = float(self._get_duration_seconds())
        if duration <= 0:
            self._append_status("Test duration must be at least 1 second")
            return
        self._run_duration_s = duration

        self.run_active = True
        self.ni_run_done = self._bypass_ni_this_run
        self.ipg_run_done = self._bypass_ipg_this_run
        self.stop_requested_by_user = False
        self.run_started_monotonic = time.monotonic()
        ni_ai_active_this_run = bool(self.config.get("ni", {}).get("ai", {}).get("active_channels"))
        if not self._bypass_ni_this_run and not ni_ai_active_this_run:
            # NI completion is signaled by the AI loop. If AI is disabled for this
            # run, treat NI as already complete so run finalization cannot deadlock.
            self.ni_run_done = True
            self._append_status("NI AI disabled for this run; NI completion will not gate run finalization")
        if self.auto_test_active and self.auto_test_session_dir is not None:
            self.current_run_output_dir = self.auto_test_session_dir
        else:
            self.current_run_output_dir = ensure_directory(Path(self.config["logging"]["output_directory"]))
        self.logged_ni_rows.clear()
        self._pending_ni_payloads.clear()
        self.logged_ipg_samples.clear()
        self.logged_battery_rows.clear()
        self._ipg_first_chunk_wall_time = None
        self._ipg_last_chunk_wall_time = None

        # Drop any stale queued samples from a previous run so a new run starts
        # with a clean buffer and no backlog-induced lag.
        while not self.ni_data_queue.empty():
            try:
                self.ni_data_queue.get_nowait()
            except Exception:
                break
        while not self.ipg_data_queue.empty():
            try:
                self.ipg_data_queue.get_nowait()
            except Exception:
                break
        for buffer in self.ni_live_buffers.values():
            buffer.clear()
        for buffer in self.ipg_live_buffers.values():
            buffer.clear()

        self._session_pre_status = {}
        self._session_post_status = {}
        self.ni_elapsed_s = 0.0
        self.ipg_elapsed_s = 0.0
        self._set_run_status("Running")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pause_between_blocks_button.setEnabled(bool(self.auto_test_active))
        self.generate_report_button.setEnabled(False)
        self.last_run_artifacts = {}
        self.elapsed_label.setText("Elapsed: 00:00:00")
        self.progress_percent_label.setText("0%")
        self.progress_bar.setValue(0)
        self.elapsed_timer.start()

        if self._bypass_ipg_this_run:
            has_ai = any(row["enabled"] for row in self.ni_ai_channel_settings.values())
            has_ao = any(row["enabled"] for row in self.ni_ao_channel_settings.values())
            if has_ai and has_ao:
                self._current_run_mode = "Simultaneous Rec & Stim"
            elif has_ai:
                self._current_run_mode = "IPG Stim Only"
            elif has_ao:
                self._current_run_mode = "IPG Rec Only"
            else:
                self._append_status("At least one NI AI or AO channel must be enabled for NI-only run")
                self.run_active = False
                self.elapsed_timer.stop()
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                self.pause_between_blocks_button.setEnabled(False)
                self._set_run_status("Ready")
                return
        else:
            has_rec = any(row["enabled"] for row in self.ipg_rec_channel_settings.values())
            has_stim = any(row["enabled"] for row in self.ipg_stim_channel_settings.values())
            if has_rec and has_stim:
                self._current_run_mode = "Simultaneous Rec & Stim"
            elif has_stim:
                self._current_run_mode = "IPG Stim Only"
            else:
                self._current_run_mode = "IPG Rec Only"

        if not self._bypass_ni_this_run:
            self.request_ni_prepare.emit(self.config, duration, self._current_run_mode)
        if not self._bypass_ipg_this_run:
            self.request_ipg_prepare.emit(self.config, duration, self._current_run_mode)

        if self._bypass_ni_this_run:
            self._append_status("Bypassing NI: starting IPG directly")
            if not self._start_ipg_from_settings():
                self.run_active = False
                self.elapsed_timer.stop()
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                self._set_run_status("Ready")
                return
        else:
            self.request_ni_start_ai.emit()
            if self._bypass_ipg_this_run:
                self._append_status("Bypassing IPG: running NI only")
            else:
                self._append_status("Queued run start sequence")

    def _stop_test(self) -> None:
        self.stop_requested_by_user = True
        if self.auto_test_active and not self.run_active:
            self._finish_auto_test_session(stopped=True)
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._set_run_status("Stopped by User")
            return
        if self.auto_test_active:
            self._append_status("Stop requested: current block will stop immediately and be saved as partial")
        if not self._bypass_ni_this_run:
            self.request_ni_stop.emit()
        if not self._bypass_ipg_this_run:
            self.request_ipg_stop.emit()
        self._append_status("Stop requested")
        self._set_run_status("Stopping")
        self.stop_button.setEnabled(False)

    def _handle_ai_started(self) -> None:
        self.request_ni_start_ao.emit()

    def _handle_ao_started(self) -> None:
        if self._bypass_ipg_this_run:
            return
        self._start_ipg_from_settings()

    def _start_ipg_from_settings(self) -> bool:
        recording_channels = [channel for channel, row in self.ipg_rec_channel_settings.items() if row["enabled"]]
        stim_channels = [channel for channel, row in self.ipg_stim_channel_settings.items() if row["enabled"]]
        mode = self._current_run_mode
        if mode in {"IPG Rec Only", "Simultaneous Rec & Stim"} and not recording_channels:
            self._append_status("At least one IPG recording channel must be enabled")
            return False
        if mode in {"IPG Stim Only", "Simultaneous Rec & Stim"} and not stim_channels:
            self._append_status("At least one stimulation channel must be enabled")
            return False
        recording_mask_hex = channels_to_hex_mask(recording_channels)
        stim_mask_hex = channels_to_hex_mask(stim_channels)
        self._append_status(
            f"IPG masks prepared from enabled channels: rec={recording_channels or [0]} -> 0x{recording_mask_hex or '0'}, stim={stim_channels or [0]} -> 0x{stim_mask_hex or '0'}"
        )
        self.request_ipg_start.emit(recording_mask_hex or "0", stim_mask_hex or "0")
        return True

    def _on_ni_status(self, status: Dict[str, Any]) -> None:
        self.ni_connected = bool(status.get("NI-9222")) and bool(status.get("NI-9263"))

        ai_name = status.get("ai_device_name", "")
        ao_name = status.get("ao_device_name", "")
        self.ni_9222_indicator.setText(f"NI-9222 ({ai_name})" if ai_name else "NI-9222")
        self.ni_9263_indicator.setText(f"NI-9263 ({ao_name})" if ao_name else "NI-9263")
        self._set_indicator(self.ni_9222_indicator, bool(status.get("NI-9222")))
        self._set_indicator(self.ni_9263_indicator, bool(status.get("NI-9263")))
        self.ni_status_details.setPlainText("\n".join(status.get("details", [])))

        # Auto-populate device name fields from detected modules
        if ai_name:
            self.ai_device_name_edit.setText(ai_name)
            self.config["ni"]["devices"]["ai_device_name"] = ai_name
        if ao_name:
            self.ao_device_name_edit.setText(ao_name)
            self.config["ni"]["devices"]["ao_device_name"] = ao_name

        self._update_enable_state()

    def _on_ipg_connection_changed(self, connected: bool, address: str) -> None:
        self.ipg_connected = connected
        self.ipg_connect_button.setText("Connect")
        self.ipg_connect_button.setEnabled(not connected)
        self.ipg_disconnect_button.setEnabled(connected)
        self.ipg_indicator.setText("IPG: Connected" if connected else "IPG: Not Connected")
        self._set_indicator(self.ipg_indicator, connected)
        if connected:
            self.ipg_address_label.setText(f"BLE: {address}")
            self._append_status(f"Connected to IPG at {address}")
        else:
            self.ipg_address_label.setText("")
            self._append_status("IPG disconnected")
        self._update_enable_state()

    def _on_one_time_status(self, payload: Dict[str, Any]) -> None:
        self.battery_label.setText(f"Battery: {payload['battery_voltage_mv']} mV")
        temp_c = float(payload.get("temperature_c", self._temperature_to_celsius(payload.get("temperature_raw", 0))))
        self.temperature_label.setText(f"Temperature: {temp_c:.1f} C")

    def _on_battery_status(self, payload: Dict[str, Any]) -> None:
        """Direct slot — called before run_finished so post-test status is always captured."""
        temp_c = float(payload.get("temperature_c", self._temperature_to_celsius(payload.get("temperature_raw", 0))))
        self.battery_label.setText(f"Battery: {payload['battery_voltage_mv']} mV")
        self.temperature_label.setText(f"Temperature: {temp_c:.1f} C")
        phase = str(payload.get("snapshot_phase", ""))
        if phase == "pre-test":
            self._session_pre_status = payload
        elif phase == "post-test":
            self._session_post_status = payload
        timestamp = time.monotonic() - self.run_started_monotonic if self.run_started_monotonic else 0.0
        self.logged_battery_rows.append([
            timestamp,
            payload["battery_voltage_mv"],
            temp_c,
            payload["charge_level"],
            payload["charging_voltage_mv"],
            payload["is_charging"],
            phase,
        ])

    def _on_ni_run_finished(self) -> None:
        self.ni_run_done = True
        self._maybe_finalize_run()

    def _on_ipg_run_finished(self) -> None:
        self.ipg_run_done = True
        if not self._bypass_ipg_this_run:
            self.request_ipg_stop.emit()
        if not self._bypass_ni_this_run:
            self.request_ni_stop.emit()
        self._maybe_finalize_run()

    def _maybe_finalize_run(self) -> None:
        if not self.run_active:
            return
        mode = self._current_run_mode
        if mode == "IPG Rec Only" and not self.ipg_run_done:
            return
        if mode == "IPG Stim Only" and (not self.ipg_run_done or not self.ni_run_done):
            return
        if mode == "Simultaneous Rec & Stim" and (not self.ipg_run_done or not self.ni_run_done):
            return

        self.run_active = False
        self.elapsed_timer.stop()
        self.stop_button.setEnabled(False)
        self._set_run_status("Stopped by User" if self.stop_requested_by_user else "Completed")
        self._save_run_artifacts(partial=bool(self.stop_requested_by_user and self.auto_test_active))
        self._append_status("Test stopped" if self.stop_requested_by_user else "Test completed")

        if self.auto_test_active:
            if self.last_run_artifacts:
                self.auto_test_saved_artifacts.append(dict(self.last_run_artifacts))

            if (not self.stop_requested_by_user) and (not self._check_auto_block_quality_and_confirm_continue()):
                self.stop_requested_by_user = True
                self.start_button.setEnabled(True)
                self.pause_between_blocks_button.setEnabled(False)
                self._finish_auto_test_session(stopped=True)
                return

            if self.stop_requested_by_user:
                self.start_button.setEnabled(True)
                self.pause_between_blocks_button.setEnabled(False)
                self._finish_auto_test_session(stopped=True)
                return

            if self.auto_test_pause_between_blocks and self.auto_test_current_index < (len(self.auto_test_plan) - 1):
                self.start_button.setEnabled(True)
                self.pause_between_blocks_button.setEnabled(True)
                self._append_status("Paused between blocks. Click Start to continue auto sequence.")
                return

            delay_s = float(self.auto_ipg_test_config.get("inter_block_delay_s", 0.0))
            self.start_button.setEnabled(False)
            if delay_s > 0.0 and self.auto_test_current_index < (len(self.auto_test_plan) - 1):
                QtCore.QTimer.singleShot(int(delay_s * 1000), lambda: self._start_next_auto_block(announce=False))
            else:
                self._start_next_auto_block(announce=False)
            return

        self.start_button.setEnabled(True)
        self.pause_between_blocks_button.setEnabled(False)

    def _check_auto_block_quality_and_confirm_continue(self) -> bool:
        if not self.auto_test_active or not self.last_run_artifacts:
            return True

        ipg_path = Path(str(self.last_run_artifacts.get("ipg_path", "")))
        if not ipg_path.exists():
            return True

        try:
            with ipg_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                channel_names = header[1:]
                totals = [0 for _ in channel_names]
                nan_counts = [0 for _ in channel_names]
                for row in reader:
                    if not row:
                        continue
                    for idx in range(len(channel_names)):
                        totals[idx] += 1
                        text = row[idx + 1].strip() if idx + 1 < len(row) else ""
                        if not text:
                            nan_counts[idx] += 1
                            continue
                        try:
                            value = float(text)
                            if not math.isfinite(value):
                                nan_counts[idx] += 1
                        except Exception:
                            nan_counts[idx] += 1
        except Exception as exc:
            self._append_status(f"Auto block quality check failed ({type(exc).__name__}): {exc}")
            return True

        warnings: List[str] = []
        for idx, channel_name in enumerate(channel_names):
            total = totals[idx]
            if total <= 0:
                warnings.append(f"{channel_name}: no data recorded")
                continue
            ratio = nan_counts[idx] / float(total)
            if ratio >= 0.1:
                warnings.append(f"{channel_name}: {ratio * 100.0:.1f}% NaN/empty samples")

        if not warnings:
            return True

        message = (
            "Significant data quality issue detected in current block.\n\n"
            + "\n".join(f"- {line}" for line in warnings)
            + "\n\nContinue with next block?"
        )
        reply = QtWidgets.QMessageBox.warning(
            self,
            "Auto Test Data Quality Warning",
            message,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return reply == QtWidgets.QMessageBox.Yes

    def _set_run_status(self, state: str) -> None:
        self.status_label.setText(state)
        status_styles = {
            "Ready": ("#eef2ff", "#3730a3", "#c7d2fe"),
            "Running": ("#ecfeff", "#0e7490", "#99f6e4"),
            "Stopping": ("#fffbeb", "#92400e", "#fde68a"),
            "Completed": ("#ecfdf5", "#166534", "#86efac"),
            "Stopped by User": ("#fff7ed", "#9a3412", "#fdba74"),
        }
        bg, fg, border = status_styles.get(state, ("#eef2ff", "#3730a3", "#c7d2fe"))
        self.status_label.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {border}; "
            "padding: 1px 8px; border-radius: 4px; font-weight: 600;"
        )

    def _log_session_status_summary(self) -> None:
        if not self._session_pre_status and not self._session_post_status:
            return
        pre_batt = self._session_pre_status.get("battery_voltage_mv", "--")
        pre_temp_text = (
            f"{float(self._session_pre_status.get('temperature_c', 0.0)):.1f} C"
            if self._session_pre_status
            else "--"
        )
        post_batt = self._session_post_status.get("battery_voltage_mv", "--")
        post_temp_text = (
            f"{float(self._session_post_status.get('temperature_c', 0.0)):.1f} C"
            if self._session_post_status
            else "--"
        )
        self._append_status(
            "Session status summary: "
            f"pre[batt={pre_batt} mV, temp={pre_temp_text}] "
            f"post[batt={post_batt} mV, temp={post_temp_text}]"
        )

    def _drain_plot_queues(self, force: bool = False) -> None:
        start = time.perf_counter()
        budget_s = None if force else 0.025
        dirty_ni_channels = set()

        if force and self._pending_ni_payloads:
            for pending_payload in self._pending_ni_payloads:
                self._append_logged_ni_rows_from_payload(pending_payload)
            self._pending_ni_payloads.clear()

        while not self.ni_data_queue.empty():
            if budget_s is not None and (time.perf_counter() - start > budget_s):
                break
            payload = self.ni_data_queue.get()
            for channel, samples in payload["channels"].items():
                display_channel = int(channel)
                buffer = self.ni_live_buffers[display_channel]
                buffer.extend(samples)
                dirty_ni_channels.add(display_channel)
            if force:
                self._append_logged_ni_rows_from_payload(payload)
            else:
                self._pending_ni_payloads.append(payload)
        for channel in sorted(dirty_ni_channels):
            self.ni_plot_panel.update_channel(channel, list(self.ni_live_buffers[channel]))

        dirty_ipg_channels = set()
        while not self.ipg_data_queue.empty():
            if budget_s is not None and (time.perf_counter() - start > budget_s):
                break
            payload = self.ipg_data_queue.get()
            samples_per_chunk = max((len(samples) for samples in payload["channels"].values()), default=0)
            for channel, samples in payload["channels"].items():
                buffer = self.ipg_live_buffers[channel]
                buffer.extend(samples)
                dirty_ipg_channels.add(channel)
            # Record IPG samples first; timestamps are reconstructed once per run
            # from wall-clock start/end and total sample count to stay stable
            # across different channel-count throughput modes.
            chunk_wall_time = payload.get("chunk_wall_time")
            if chunk_wall_time is not None:
                current_wall = float(chunk_wall_time)
                if self._ipg_first_chunk_wall_time is None:
                    self._ipg_first_chunk_wall_time = current_wall
                self._ipg_last_chunk_wall_time = current_wall

            for sample_index in range(samples_per_chunk):
                row = []
                for channel in sorted(payload["channels"]):
                    channel_samples = payload["channels"][channel]
                    row.append(channel_samples[sample_index] if sample_index < len(channel_samples) else "")
                self.logged_ipg_samples.append(row)
            self.ipg_elapsed_s += samples_per_chunk / payload["sample_rate_hz"] if samples_per_chunk else 0.0
        for channel in sorted(dirty_ipg_channels):
            self.ipg_plot_panel.update_channel(channel, list(self.ipg_live_buffers[channel]))

    def _append_logged_ni_rows_from_payload(self, payload: Dict[str, Any]) -> None:
        timestamps = payload.get("timestamps_s", [])
        channels = payload.get("channels", {})
        if not isinstance(timestamps, list) or not isinstance(channels, dict):
            return
        ordered_channels = sorted(channels)
        for sample_index in range(len(timestamps)):
            row: List[Any] = [timestamps[sample_index]]
            for channel in ordered_channels:
                channel_samples = channels.get(channel, [])
                row.append(channel_samples[sample_index] if sample_index < len(channel_samples) else "")
            self.logged_ni_rows.append(row)



    def _save_run_artifacts(self, partial: bool = False) -> None:
        if self.current_run_output_dir is None:
            return

        # Flush any remaining queued chunks before persisting CSV/metadata so
        # sample counts and timing stats reflect the full run.
        self._drain_plot_queues(force=True)

        stem = self._current_run_stem_override or timestamped_stem("run", self.config)
        self._current_run_stem_override = None
        ni_path = self.current_run_output_dir / f"{stem}_{self.config['logging']['ni_csv_prefix']}.csv"
        ipg_path = self.current_run_output_dir / f"{stem}_{self.config['logging']['ipg_csv_prefix']}.csv"
        battery_path = self.current_run_output_dir / f"{stem}_battery_temp.csv"
        metadata_path = self.current_run_output_dir / f"{stem}_{self.config['logging']['metadata_prefix']}.json"

        self._write_csv(
            ni_path,
            ["Time (s)"] + [f"Channel_{channel}" for channel in self.config["ni"]["ai"]["active_channels"]],
            self.logged_ni_rows,
        )
        ipg_rows_for_csv: List[List[float]] = []
        if self.logged_ipg_samples:
            sample_count = len(self.logged_ipg_samples)
            if (
                self._ipg_first_chunk_wall_time is not None
                and self._ipg_last_chunk_wall_time is not None
                and self._ipg_last_chunk_wall_time > self._ipg_first_chunk_wall_time
                and sample_count > 1
            ):
                effective_duration_s = self._ipg_last_chunk_wall_time - self._ipg_first_chunk_wall_time
                dt = effective_duration_s / (sample_count - 1)
                for sample_index, values in enumerate(self.logged_ipg_samples):
                    ipg_rows_for_csv.append([sample_index * dt] + values)
            else:
                fs_nominal = float(IPG_SAMPLE_RATE_HZ)
                for sample_index, values in enumerate(self.logged_ipg_samples):
                    ipg_rows_for_csv.append([sample_index / fs_nominal] + values)

        self._write_csv(
            ipg_path,
            ["Time (s)"] + [f"Channel_{channel}" for channel in [
                channel for channel, row in self.ipg_rec_channel_settings.items() if row["enabled"]
            ]],
            ipg_rows_for_csv,
        )
        self._write_csv(
            battery_path,
            ["Time (s)", "Battery_mV", "Temperature_C", "ChargeLevel", "ChargingVoltage_mV", "IsCharging", "Phase"],
            self.logged_battery_rows,
        )
        metadata_payload = json.loads(json.dumps(self.config))
        ipg_recording_meta = metadata_payload.setdefault("ipg", {}).setdefault("recording", {})
        ipg_recording_meta["samples_unit"] = "uV"
        ipg_recording_meta["adc_lsb_uV"] = IPG_ADC_LSB_UV
        ipg_recording_meta["samples_converted_to_uV"] = True

        ipg_sample_count = len(self.logged_ipg_samples)
        ipg_time_span_s = None
        ipg_effective_rate_hz = None
        if (
            self._ipg_first_chunk_wall_time is not None
            and self._ipg_last_chunk_wall_time is not None
            and self._ipg_last_chunk_wall_time > self._ipg_first_chunk_wall_time
            and ipg_sample_count > 1
        ):
            ipg_time_span_s = float(self._ipg_last_chunk_wall_time - self._ipg_first_chunk_wall_time)
            if ipg_time_span_s > 0.0:
                ipg_effective_rate_hz = float((ipg_sample_count - 1) / ipg_time_span_s)

        ipg_recording_meta["nominal_sample_rate_hz"] = float(IPG_SAMPLE_RATE_HZ)
        ipg_recording_meta["measured_sample_count"] = int(ipg_sample_count)
        ipg_recording_meta["measured_time_span_s"] = ipg_time_span_s
        ipg_recording_meta["measured_effective_sample_rate_hz"] = ipg_effective_rate_hz
        ipg_recording_meta["measured_effective_rate_method"] = "(total_samples-1)/(last_chunk_wall_time-first_chunk_wall_time)"
        ipg_recording_meta["queue_flush_before_save"] = True

        if ipg_effective_rate_hz is not None:
            deviation_pct = 100.0 * (ipg_effective_rate_hz - float(IPG_SAMPLE_RATE_HZ)) / float(IPG_SAMPLE_RATE_HZ)
            self._append_status(
                "IPG effective sampling (post-run): "
                f"{ipg_effective_rate_hz:.2f} Hz from {ipg_sample_count} samples over {ipg_time_span_s:.3f} s "
                f"({deviation_pct:+.2f}% vs nominal {IPG_SAMPLE_RATE_HZ} Hz)"
            )
        if self.auto_test_active and self._current_auto_block is not None:
            metadata_payload.setdefault("execution", {})["auto_block"] = {
                "index": int(self.auto_test_current_index + 1),
                "phase": str(self._current_auto_block.get("phase", "")),
                "phase_index": int(self._current_auto_block.get("index", 0)),
                "duration_s": float(self._current_auto_block.get("duration_s", 0.0)),
                "frequency_hz": float(self._current_auto_block.get("frequency_hz", 0.0)),
                "amplitude_v": float(self._current_auto_block.get("amplitude_v", 0.0)),
                "ao_values": {
                    str(channel): float(self._current_auto_block.get("ao_values", {}).get(str(channel), 0.0))
                    for channel in range(1, MAX_NI_AO_CHANNELS + 1)
                },
                "partial": bool(partial),
            }
        save_json(metadata_path, metadata_payload)
        self.last_run_artifacts = {
            "stem": Path(stem),
            "ni_path": ni_path,
            "ipg_path": ipg_path,
            "battery_path": battery_path,
            "metadata_path": metadata_path,
            "output_dir": self.current_run_output_dir,
            "partial": partial,
        }
        
        # Log NI AI reference status if enabled
        ni_ai_ref_enabled = bool(self.config.get("execution", {}).get("auto_ipg_testing", {}).get("ni_ai_reference", {}).get("enabled", False))
        if ni_ai_ref_enabled:
            ni_ai_ref_mapping = self.config.get("execution", {}).get("auto_ipg_testing", {}).get("ni_ai_reference", {}).get("mapping", [])
            ni_csv_exists = ni_path.exists()
            self._append_status(
                f"NI AI reference: enabled={ni_ai_ref_enabled}, CSV saved={ni_csv_exists}, "
                f"mappings={len(ni_ai_ref_mapping)}, NI CSV: {ni_path if ni_csv_exists else '[NOT FOUND]'}"
            )
        
        self.generate_report_button.setEnabled(True)


    def _generate_report_for_last_session(self) -> None:
        if not self.last_run_artifacts:
            self._append_status("No completed session available for report generation")
            return

        analyzer_path = Path(__file__).with_name("run_report_analyzer.py")
        if not analyzer_path.exists():
            self._append_status("Report analyzer script not found: run_report_analyzer.py")
            return

        cmd = [
            sys.executable,
            str(analyzer_path),
            "--ni-csv",
            str(self.last_run_artifacts["ni_path"]),
            "--ipg-csv",
            str(self.last_run_artifacts["ipg_path"]),
            "--battery-csv",
            str(self.last_run_artifacts["battery_path"]),
            "--metadata-json",
            str(self.last_run_artifacts["metadata_path"]),
            "--output-root",
            str(self.last_run_artifacts["output_dir"]),
            "--stem",
            str(self.last_run_artifacts["stem"]),
        ]

        expected_report_path = Path(self.last_run_artifacts["output_dir"]) / "reports" / str(self.last_run_artifacts["stem"]) / "analysis_report.md"
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=MANUAL_REPORT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            if expected_report_path.exists():
                self._append_status(
                    "Report generation reached timeout but report file exists; opening generated report"
                )
                self._open_report_file(expected_report_path)
                return
            self._append_status(
                f"Report generation timed out after {MANUAL_REPORT_TIMEOUT_S} s: {exc}"
            )
        except Exception as exc:
            self._append_status(f"Report generation failed: {type(exc).__name__}: {exc}")
            return

        if completed.returncode != 0:
            stderr_text = (completed.stderr or "").strip() or "Unknown error"
            self._append_status(f"Report generation failed: {stderr_text}")
            return

        stdout_text = (completed.stdout or "").strip()
        report_info: Dict[str, Any] = {}
        if stdout_text:
            try:
                report_info = json.loads(stdout_text)
            except json.JSONDecodeError:
                report_info = {"report_path": stdout_text}

        report_path = str(report_info.get("report_path", ""))
        report_dir = str(report_info.get("report_dir", ""))
        if report_path:
            self._append_status(f"Report generated: {report_path}")
            self._open_report_file(Path(report_path))

    def _open_report_file(self, report_path: Path) -> None:
        if not report_path.exists():
            self._append_status(f"Cannot open report: file not found ({report_path})")
            return

        opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(report_path.resolve())))
        if opened:
            return

        try:
            if os.name == "nt":
                os.startfile(str(report_path.resolve()))  # type: ignore[attr-defined]
                return
        except Exception as exc:
            self._append_status(f"Failed to auto-open report: {type(exc).__name__}: {exc}")
            return

        self._append_status("Failed to auto-open report with default markdown application")

    def _write_csv(self, path: Path, header: List[str], rows: List[List[Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)

    def _refresh_mode_state(self) -> None:
        self.ipg_plot_panel.set_visible_channels(list(range(1, MAX_IPG_CHANNELS + 1)))
        self.ni_plot_panel.set_visible_channels(list(range(MAX_NI_AI_CHANNELS)))

    def _update_safety_interlock(self) -> None:
        self._update_enable_state()

    def _update_enable_state(self) -> None:
        bypass_ni = self.bypass_ni_checkbox.isChecked()
        bypass_ipg = self.bypass_ipg_checkbox.isChecked()
        auto_enabled = self.auto_ipg_testing_checkbox.isChecked()
        auto_ready = self.auto_ipg_test_ready and self.ni_connected and self.ipg_connected
        self._enforce_ao_sample_rate_for_replay()
        self.start_button.setEnabled((not self.run_active) and not (bypass_ni and bypass_ipg) and (not auto_enabled or auto_ready))
        self.generate_report_button.setEnabled((not self.run_active) and bool(self.last_run_artifacts))
        self.auto_ipg_testing_config_button.setEnabled(not self.run_active)
        if not self.run_active and not self.auto_test_active:
            self.pause_between_blocks_button.setEnabled(False)

    def _update_elapsed_label(self) -> None:
        if self.run_active:
            elapsed = time.monotonic() - self.run_started_monotonic
            if self._run_duration_s > 0:
                elapsed = min(elapsed, self._run_duration_s)
            self.elapsed_label.setText(f"Elapsed: {format_hms(elapsed)}")

        if self.auto_test_active and self.auto_test_plan:
            total = self._estimate_auto_ipg_runtime_seconds(self.auto_ipg_test_config)
            completed = 0.0
            for index, block in enumerate(self.auto_test_plan):
                block_duration = float(block.get("duration_s", 0.0))
                if index < self.auto_test_current_index:
                    completed += block_duration
                    if index < len(self.auto_test_plan) - 1:
                        completed += float(self.auto_ipg_test_config.get("inter_block_delay_s", 0.0))
                elif index == self.auto_test_current_index and self.run_active:
                    completed += min(time.monotonic() - self.run_started_monotonic, block_duration)
            progress = int(max(0.0, min(100.0, (completed / total * 100.0) if total > 0 else 0.0)))
            self.progress_bar.setValue(progress)
            self.progress_percent_label.setText(f"{progress}%")
        elif not self.run_active:
            self.progress_bar.setValue(0)
            self.progress_percent_label.setText("0%")

    def _append_status(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return

        lower = text.lower()
        error_markers = ("error", "failed", "exception", "timeout", "warning", "nack")
        if any(marker in lower for marker in error_markers):
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_view.appendPlainText(f"[{timestamp}] {text}")
            return

        noisy_prefixes = (
            "IPG streaming:",
            "IPG chunk:",
            "Waiting for pre-test status...",
            "Waiting for post-test status...",
            "Still waiting for pre-test status...",
            "Still waiting for post-test status...",
            "IPG pre-test status:",
            "IPG post-test status:",
            "IPG status snapshot",
            "NI AO channel applied:",
            "NI AO applied:",
            "IPG start command:",
            "IPG masks prepared from enabled channels:",
            "IPG stimulation started with mask",
            "NI AI task started",
            "Queued run start sequence",
            "IPG test stopped",
            "IPG disconnected",
            "Session status summary:",
            "IPG status snapshot",
            "IPG recording duration reached; stopping recording",
            "IPG start command:",
            "IPG masks prepared from enabled channels:",
        )
        if text.startswith(noisy_prefixes):
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {text}")

    def _set_indicator(self, label: QtWidgets.QLabel, ok: bool) -> None:
        color = "#177245" if ok else "#b00020"
        label.setStyleSheet(f"background: {color}; color: white; padding: 4px 8px; border-radius: 6px;")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.request_ipg_stop.emit()
        self.request_ni_stop.emit()
        self.request_ipg_disconnect.emit()
        self.request_ni_close.emit()
        self.ni_thread.quit()
        self.ni_thread.wait(2000)
        self.ipg_thread.quit()
        self.ipg_thread.wait(2000)
        super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Bahnschrift", 9))
    pg.setConfigOptions(antialias=False, background="#232136", foreground="#cfd2ff")
    window = MainWindow()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())