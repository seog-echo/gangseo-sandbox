# NODES — Unified DBS Simulator

A standalone, simplified simulator for multi-contact DBS clinician-programming workflows.

## Goals
- Generate 32 monopolar channels per chunk:
  - 8 depth contacts on the left hemisphere
  - 8 paddle contacts on the left hemisphere
  - 8 depth contacts on the right hemisphere
  - 8 paddle contacts on the right hemisphere
- Keep the current gamma-model mechanism as the core stimulation engine.
- Support optional, hemisphere-local stimulation on depth contacts only.
- Derive bipolar channels as a separate, on-demand conversion step.

## Design Summary
- **Baseline generation**: per-contact signals with realistic beta/gamma weighting by contact location.
- **Stimulation**: soft spatial decay from the selected depth contact, with beta suppression, gamma entrainment at `f/2`, and stim artifacts at `f` and harmonics.
- **Behavioral states**: Rest / Movement / Sleep apply smoothed scalar multipliers to both baseline features (beta, gamma floor, 1/f slope) and stimulation dynamics (entrainment threshold, suppression strength) for state-dependent physiology.
- **Network coherence**: state-dependent inter-channel phase locking via shared oscillators — resting intra-hemisphere STN–M1 beta coherence (plus modest bilateral STN–STN beta), movement STN–M1 high-gamma coupling (with beta decoupling), and global inter-hemisphere slow-wave (delta) coherence in sleep. Shown live as a per-band magnitude-squared-coherence (MSC) matrix.
- **Bipolar conversion**: separate utility that subtracts two monopolar channels from the same lead and hemisphere.
- **GUI direction**: grouped views by hemisphere and lead, with a per-panel mode switch (`raw`, `psd`, `spectrogram`).

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python nodes.py
```

Highlights:
- 5s rolling window for all 32 monopolar channels
- Grouped views (left/right × paddle/depth)
- Per-group mode switch: Raw / PSD / Spectrogram
- Behavioral State selector: Rest / Movement / Sleep (smoothed transitions)
- Interactive stimulation controls for left/right depth leads
- Optional bipolar panels (up to 1 per lead per side)
- Raw-data recording to Parquet (32 monopolar channels + per-sample timestamps, stim params, behavioral state), written from a background thread so the GUI never stalls; manual or fixed-duration capture, saved to `recordings/`

## Recording
While streaming, use the recording bar (below the right monopolar column): set an optional auto-stop duration (0 = manual), press **● REC** to start/stop. A clock shows elapsed time and a red dot indicates active recording; state and stimulation can be changed freely during capture. On stop (manual, duration, or End-stream) the file is finalized to `recordings/nodes_<timestamp>.parquet` — natively 1024 Hz, zstd-compressed, readable from Python (pandas/pyarrow), R, Julia, MATLAB, etc.

## Behavioral States
- **Rest** — identity; output matches the pre-upgrade model.
- **Movement** — STN (depth) beta desynchronization with raised broadband/gamma variance; strong M1 (paddle) gamma; cortex harder to entrain (higher `a50`); beta suppression held above a physiological floor.
- **Sleep** — added delta/theta slow waves (steepens the apparent 1/f) via a band-limited slow-wave term, cortex-dominant (paddle/M1 slow waves larger than depth/STN); reduced STN beta; M1 high-gamma reduced; strong rejection of high-frequency cortical entrainment (large `a50`).

State magnitudes live in `STATE_MODIFIERS` in [simulator/config.py](simulator/config.py) and are applied with smoothed (one-pole) transitions inside `DBSArrayModel.simulate_chunk()`.

## Network Coherence
Each behavioral state also sets inter-channel coherence, modeled by mixing a **shared** oscillator with each channel's **independent** band component (energy-preserving, so band power is unchanged):

- **Rest** — elevated intra-hemisphere STN–M1 **beta** coherence (PD resting hallmark; MSC ≈ 0.5), plus modest inter-hemisphere STN–STN beta coupling (MSC ≈ 0.3).
- **Movement** — intra-hemisphere STN–M1 **high-gamma** coherence appears (MSC ≈ 0.3) while resting beta coherence breaks down.
- **Sleep** — strong **delta** coherence shared globally across all four leads (inter- and intra-hemisphere; MSC ≈ 0.65).

Targets live in `STATE_COHERENCE` in [simulator/config.py](simulator/config.py) as `r = √(MSC)` shared-power fractions. A shared component varies slightly across a lead's contacts (`gradient`) so coherence partly survives bipolar (A−B) derivation. The GUI shows a live per-band MSC matrix for the key channel pairs (computed on the 15 s buffer).
