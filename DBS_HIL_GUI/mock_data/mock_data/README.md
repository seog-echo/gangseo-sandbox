# Mock Data Package

This folder contains synthetic neural datasets generated from the local simulator for bench testing and signal-chain validation.

## Folder structure

- `baseline/`: raw simulator baseline signals (original units: **µV**)
- `ao_ready/`: AO-ready replay files resampled to **1024 Hz**
- `ao_ready_512Hz/`: AO-ready replay files resampled to **512 Hz**
- `replay_v_512Hz/`: simplified replay files in **volts** (`signal_v`) for direct AO playback at **512 Hz**

## 1) Baseline datasets (`baseline/`)

Files (2 datasets per lead):

- `baseline_depth_lfp_dataset1.csv`
- `baseline_depth_lfp_dataset2.csv`
- `baseline_cortex_ecog_dataset1.csv`
- `baseline_cortex_ecog_dataset2.csv`

Columns:

- `sample_idx`: sample index
- `time_s`: time in seconds
- `signal_uv`: synthetic neural signal in microvolts (µV)
- `beta_component_uv`: extracted beta-band component used in model generation

Generation settings used:

- Source sampling rate: **1000 Hz**
- Duration per file: **120 s**

## 2) AO-ready replay datasets (`ao_ready/`, `ao_ready_512Hz/`)

These files are prepared for NI analog output replay.

Columns:

- `sample_idx`
- `time_s`
- `ao_v`: voltage to output from NI AO channel (V)
- `ipg_est_mv`: estimated IPG input after divider (mV)
- `source_signal_uv`: resampled source neural signal (µV)

Scaling model:

- `ao_v = signal_uv × 1e-6 × gain`
- `ipg_est_mv = (ao_v / divider_ratio) × 1e3`

Current prepared sets use:

- `gain = 10000`
- `divider_ratio = 1000` (1/1000 divider)

### Volts-scale replay set (`replay_v_512Hz/`)

If you want files explicitly in voltage units for NI AO playback, use `replay_v_512Hz/`.

Columns:

- `sample_idx`
- `time_s`
- `signal_v`: AO output voltage command (V)
- `ipg_est_mv`: estimated IPG input after 1/1000 divider (mV)

These are derived from `ao_ready_512Hz/` with `signal_v = ao_v`.

Observed peaks in current AO-ready files are roughly:

- cortex: ~0.48 to 0.55 mV at IPG input estimate
- depth: ~0.75 to 0.77 mV at IPG input estimate

## Recommendation for NI 9263 AO settings

### Preferred sample rate

Use one of:

1. **512 S/s** (recommended if DBS recorder is ~512 Hz)
2. **1024 S/s** (optional 2× oversampling)

Use matching AO-ready folder:

- 512 S/s -> `ao_ready_512Hz/`
- 1024 S/s -> `ao_ready/`

For direct volts-scale playback at 512 S/s, prefer:

- 512 S/s -> `replay_v_512Hz/` (`signal_v` column)

### Task configuration (typical)

- Output mode: **Continuous Samples**
- Regeneration:
  - **Enabled** for looping one file
  - **Disabled** if streaming new chunks in real time
- AO range: start with **±1 V** (expand if needed)
- Buffer size: at least **5–10 s** worth of data
  - 512 S/s -> 2560 to 5120 samples minimum
  - 1024 S/s -> 5120 to 10240 samples minimum

### Wiring and scaling checks

- Apply the 1/1000 divider between AO output and IPG input.
- Verify signal levels with scope/DAQ before connecting to the IPG.
- Ensure common reference/ground is correct for your test setup.
- Confirm no clipping at AO stage or downstream front-end.

## How to regenerate AO-ready datasets

From project root, run:

- `python3 prepare_ao_replay_data.py --target-fs 512 --gain 10000 --divider 1000 --output-dir mock_data/ao_ready_512Hz`
- `python3 prepare_ao_replay_data.py --target-fs 1024 --gain 10000 --divider 1000 --output-dir mock_data/ao_ready`

You can adjust `--gain` and `--divider` to target a different effective mV range at the IPG input.

## Notes

- These are synthetic signals for engineering validation only.
- Not for clinical use or physiological claims without separate validation.
