# NODES (Unified DBS Simulator) — Model Walkthrough

This document explains the NODES model from baseline mock data generation to stimulation effects, behavioral-state modulation, bipolar conversion, and GUI integration.

## 1) What this simulator is

The unified simulator models a bilateral DBS programming workflow with:

- 32 monopolar channels total
  - Left depth: 8 contacts
  - Right depth: 8 contacts
  - Left paddle: 8 contacts
  - Right paddle: 8 contacts
- Optional stimulation command per hemisphere (depth-contact targeted)
- Separate bipolar derivation from already-generated monopolar channels
- Real-time display in Qt/pyqtgraph

Main code entry points:

- [simulator/model.py](simulator/model.py)
- [simulator/baseline.py](simulator/baseline.py)
- [simulator/config.py](simulator/config.py)
- [simulator/geometry.py](simulator/geometry.py)
- [simulator/bipolar_converter.py](simulator/bipolar_converter.py)
- [nodes.py](nodes.py)

---

## 2) Geometry and channel map

Geometry is defined in [simulator/geometry.py](simulator/geometry.py):

- `build_depth_positions()` creates 8 depth contacts spaced 2 mm apart on a 1D axis.
- `build_paddle_positions()` creates a 2×4 paddle grid.
- `distance_to_hotspots()` returns nearest distance from a contact to lead hotspot(s).
- `spatial_weight()` applies exponential distance decay with floor:

$$
\text{weight}(d)=f + (1-f)e^{-d/\lambda}
$$

where $d$ is distance, $\lambda$ is `decay_mm`, and $f$ is floor.

Current hotspot definitions/config live in [simulator/config.py](simulator/config.py):

- Depth hotspot: contact index 3 (channel 4)
- Paddle hotspots: contact indices 2 and 6 (channels 3 and 7)

This weighting is used both for baseline feature strength and stimulation spread.

---

## 3) Baseline mock data generation (per contact)

Baseline generation is in [simulator/baseline.py](simulator/baseline.py), function `generate_contact_baseline()`.

For each channel, the baseline is synthesized as:

1. **Colored 1/f background** (`_colored_noise`) with power-law exponent `alpha` — the continuous aperiodic floor
2. **Additive beta peak** (sits above the floor):
   - Band-limited modulator in beta band
   - Multi-tone center structure around `beta_hz` (wider peak than single-tone)
   - Contact-dependent spatial scaling (`beta_weight`)
3. **Additive finely-tuned gamma bump** (sits above the floor):
   - Smooth Gaussian spectral bump centered ~75 Hz (≈60–90 Hz), tapering to the 1/f floor at the edges — so scaling raises/lowers a natural hump, not a flat shelf
   - Contact-dependent spatial scaling (`gamma_weight`)

The model is an **aperiodic 1/f floor plus additive oscillatory peaks**. The
floor is continuous across all bands and is *never* scaled; the peaks are added
on top, and only the peaks are scaled by state scalars / stim suppression. So
reducing a band lowers its peak toward the floor (a smaller hill) instead of
carving a hole below the 1/f — matching real spectra, where e.g. a desynchronized
beta still leaves the 1/f intact.

$$
\text{signal} = \underbrace{\text{colored}\cdot A_{\text{rms}}\cdot w_{\text{base}}}_{\text{1/f floor (unscaled)}} + s_\beta\!\cdot\!\text{beta\_peak} + s_\gamma\!\cdot\!\text{gamma\_activity} + (\text{sleep})\,\text{slow wave}
$$

The function returns a `ContactBaseline`:

- `background` — the **full** broadband 1/f (× RMS × spatial weight), continuous across every band. The aperiodic floor; never scaled.
- `beta_peak` — additive beta peak (carrier + narrowband around `beta_hz`) sitting *above* the floor. Scaled by `beta_scalar` and stim suppression; carries beta coherence.
- `gamma_activity` — additive **finely-tuned gamma bump** (smooth Gaussian envelope centered ~75 Hz, ≈60–90 Hz) sitting above the floor; modest at rest, prominent in movement. Scaled by `gamma_scalar`; carries gamma coherence. Built via `_spectral_bump` so it blends into the floor at the edges rather than scaling as a flat block.
- `slow_indep` — unit-variance delta/theta slow wave (0.5–~7 Hz), scaled at runtime by `slow_amp = sleep_slow_wave_gain × RMS`. `sleep_slow_wave_gain` is a per-lead `LeadProfile` field, set higher on cortex/paddle than depth/STN so cortical slow waves dominate in Sleep. Added (not re-colored) to avoid infra-slow blow-up.
- scalars `slow_amp`, `beta_amp`, `gamma_amp` (peak amplitudes used to scale the shared oscillators) and `gradient` (`0.6 + 0.4·hotspot_weight`, the across-contact weight for shared injection so coherence partly survives bipolar derivation).

Because the 1/f floor is independent of the (partly shared) peaks, band-averaged coherence is naturally capped by peak prominence — a desynchronized band falls toward the incoherent floor in both power *and* coherence, as in real data.

---

## 4) Configuration model (profiles and lead behavior)

Lead-specific parameters are defined in [simulator/config.py](simulator/config.py):

- `LeadProfile` controls spectral and stimulation behavior for each lead type/side.
- `SimulatorConfig` controls global runtime settings.

Key profile parameters:

- Baseline spectrum: `alpha`, `beta_hz`, `beta_low_hz`, `beta_high_hz`, `beta_uv`, `gamma_activity_uv`
- Entrainment dynamics: `amax_uv`, `a50_ma`, `hill_n`, `tau_on_s`, `tau_off_s`
- Spatial spread: `stim_decay_mm`
- Artifact terms: `artifact_scale_uv_per_ma`, `artifact_2f_ratio`
- Beta suppression curve: start/end/strength fields

Current tuning emphasizes:

- clearer hotspot-centric baseline gradients
- reduced broad 40 Hz hump
- stronger distance fall-off of stimulation effect

---

## 5) Runtime model flow (`DBSArrayModel`)

Core class is `DBSArrayModel` in [simulator/model.py](simulator/model.py).

### 5.1 Initialization

At initialization:

- Builds all 32 channel `ContactBaseline`s (`_contacts`)
- Generates the shared coherence oscillators: one global delta (`_shared_delta`) and per-hemisphere beta and gamma (`_shared_beta`, `_shared_gamma`)
- Creates per-channel dynamic state (`ContactState`)
- Initializes smoothed behavioral-state scalars per lead kind (`_state_current`) and coherence coefficients (`_coherence_current`) to the Rest values

### 5.2 Chunk simulation

`simulate_chunk(state=...)` first advances the smoothed behavioral-state scalars **and** coherence coefficients once (one-pole ramps), pulls one shared-oscillator chunk per band for the whole array, then for each channel:

1. Pull the channel's component chunks from circular buffers
2. Compute stimulation spatial scale `stim_scale`
3. Apply beta suppression (suppression strength × state `suppression_scalar`) — to the beta *peak* only
4. Build each peak by **energy-preserving mixing** of the independent peak with the shared oscillator (`peak = √(1−s)·indep + √s·shared·amp`, shared fraction `s = r·gradient²`), then add the scaled peaks **on top of the unscaled `background` 1/f floor**: `signal = background + beta_scalar·beta_factor·beta_peak + gamma_scalar·gamma_activity + sleep_blend·slow_wave`
5. Compute entrainment envelope target using Hill nonlinearity (midpoint `a50 × a50_scalar`) and frequency gain
6. Update envelope with exponential on/off dynamics
7. Update jitter with exact OU-discrete update
8. Add entrained `f/2` term (+ optional subharmonics)
9. Add stimulation artifacts at `f` and `2f`
10. Add observation noise

### 5.3 Beta suppression mechanism

If stimulation amplitude is above threshold:

- suppression ramps from start to end amplitude
- suppression is spatially weighted by `stim_scale`
- only the additive **beta peak** is attenuated; the 1/f floor underneath is untouched

So even at high amplitude the beta band bottoms out *at* the 1/f floor (a flattened peak), never below it — no sub-1/f "hole." M1/paddle uses a gentler `beta_suppression_strength` (0.60) than the STN here.

### 5.4 Gamma entrainment mechanism

Entrainment target is:

$$
\text{target}_{env}=A_{max}\cdot \text{Hill}(I)\cdot G\left(\frac{f_{stim}}{2}\right)
$$

with:

- Hill term in amplitude $I$
- Gaussian frequency gain around profile center/sigma
- per-channel spatial scaling before envelope update

Envelope dynamics:

$$
E_{t+\Delta t}=E_t+\alpha\left(E_{target}-E_t\right),\quad
\alpha=1-e^{-\Delta t/\tau}
$$

### 5.5 Jitter model

Frequency jitter is OU:

$$
x_{t+\Delta t}=x_t e^{-\Delta t/\tau}+\eta,\quad
\eta\sim\mathcal N(0,\sigma_{\Delta t}^2)
$$

with exact discrete-time variance term.

This is stable across chunk sizes.

### 5.6 Behavioral state modulation

Three mutually exclusive states — **Rest**, **Movement**, **Sleep** — apply scalar multipliers defined per `(state, lead_kind)` in `STATE_MODIFIERS` ([simulator/config.py](simulator/config.py)). Each `StateModifiers` carries:

- Baseline: `beta_scalar`, `gamma_scalar`, `sleep_blend` (0 → no slow waves, 1 → full delta/theta slow-wave term)
- Stimulation: `a50_scalar` (entrainment resistance), `suppression_scalar` (max beta-suppression strength)

Targets are not applied instantly — they are smoothed each chunk with a one-pole ramp (`state_transition_tau_s`) so transitions glide instead of stepping.

`beta_scalar` and `gamma_scalar` are amplitude multipliers, so band **power** scales ≈ scalar². Values are tuned to land in physiologically reasonable ranges (verified by band-power measurement); approximate power ratios vs Rest are noted below.

Physiological intent:

- **Rest** — scalar identity (all 1.0, `sleep_blend` 0); band *powers* match the pre-upgrade model, but the signals now carry resting STN–M1 beta coherence (see §5.7), so Rest is no longer bit-for-bit identical.
- **Movement** — beta desynchronization (ERD) in *both* STN (`beta_scalar` 0.5 → ~0.25× beta power) and M1 (`beta_scalar` 0.5, classic sensorimotor ERD); a finely-tuned gamma bump that emerges with movement (STN `gamma_scalar` 1.5, M1 1.95 → ~1.7× power) but is kept *below* the beta peak — gamma is a smaller mountain than beta in every state; cortex harder to entrain (`a50_scalar` 1.5); STN beta suppression held above a floor (`suppression_scalar` 0.6).
- **Sleep** — added delta/theta slow waves via `sleep_blend` 1.0, cortex-dominant per the per-lead `sleep_slow_wave_gain` (paddle 0.60 → delta ~9×, amplitude ~1.9×; depth 0.30 → delta ~6×, amplitude ~1.6×), steepening the apparent 1/f; beta reduced *more deeply than movement* in both STN (`beta_scalar` 0.4 → ~0.17×) and M1 (`beta_scalar` 0.45, NREM nearly abolishes beta); M1 high-gamma reduction (`gamma_scalar` 0.7 → ~0.5×); strong rejection of HF cortical entrainment (`a50_scalar` 2.5 on paddle).

Resting beta is calibrated so STN beta is a clear but not overwhelming peak (~37% of 1–100 Hz power), receding to ~13% (Movement) and ~3% (Sleep).

### 5.7 Network coherence

Inter-channel coherence is created by mixing a **shared** oscillator with each channel's **independent** band component, energy-preservingly so band power is unchanged:

$$
\text{band} = \sqrt{1-s}\cdot\text{indep} + \sqrt{s}\cdot\text{shared}\cdot A,\qquad s = r\cdot \text{gradient}^2
$$

Two channels driven by the same shared oscillator then have magnitude-squared coherence **MSC ≈ r²** (so `r = √(target MSC)`), tapered per contact by `gradient` so a fraction survives bipolar (A−B) derivation. Pairings are fixed by *which* shared oscillator a channel uses:

- **delta** — one global oscillator (`_shared_delta`) shared by all four leads → global slow-wave coherence (only audible while `sleep_blend` > 0).
- **hemisphere beta / gamma** — per-hemisphere oscillators (`_shared_beta`, `_shared_gamma`) shared by STN + M1 on the same side → intra-hemisphere cortico-subthalamic coupling.
- **bilateral beta** — one oscillator (`_shared_beta_bilat`) shared by the left + right STN → the (weaker) inter-hemisphere STN–STN beta coupling seen in PD. Depth (STN) beta is a 3-way mix (independent + hemisphere + bilateral, via `_mix3`); M1 beta is a 2-way mix (independent + hemisphere). Asymmetric loadings (`beta_depth`, `beta_paddle`, `beta_bilateral`) let STN–M1 stay strong while STN–STN stays modest within the depth channel's variance budget.

Coefficients are defined per state in `STATE_COHERENCE` ([simulator/config.py](simulator/config.py)) and smoothed each chunk like the other state parameters. Measured MSC at the leads' hotspot contacts:

- **Rest** — STN–M1 beta ≈ 0.5 (PD resting hallmark) > STN–STN beta ≈ 0.3 (modest bilateral coupling) > M1–M1 ≈ 0.1; gamma/delta low.
- **Movement** — STN–M1 gamma ≈ 0.3 (motor coupling; modest because the finely-tuned gamma bump is deliberately kept small/below beta); both beta couplings break down (STN–M1 ≈ 0.2, STN–STN ≈ 0.18).
- **Sleep** — delta ≈ 0.65 across *all four* leads (inter- and intra-hemisphere global slow wave); beta couplings low.

---

## 6) Spatial rules (baseline and stimulation)

### Baseline

Baseline beta/gamma/background strength is strongest at configured hotspots and decays with distance using lead geometry + `hotspot_decay_mm` + `baseline_floor`.

### Stimulation

- Depth channels on stimulated side: strongest at stimulated depth contact, decays with contact distance
- Paddle channels on stimulated side: product of:
  - how strongly depth hotspot is driven by stim contact
  - local paddle hotspot proximity
- Opposite hemisphere receives no direct stimulation in this model

---

## 7) Bipolar derivation

Bipolar is intentionally separate from simulation generation.

In [simulator/bipolar_converter.py](simulator/bipolar_converter.py):

- `convert_bipolar()` computes `A - B`
- optional normalization by $\sqrt{2}$
- returns metadata including inter-contact distance

This keeps monopolar generation clean and lets GUI/users switch bipolar pairs on demand.

---

## 8) GUI pipeline and controls

Qt app is in [nodes.py](nodes.py).

### Data buffering

- 5 s rolling buffers for raw/PSD views
- 15 s rolling buffers for spectrogram views
- Separate monopolar and bipolar buffers

### Views

- 4 monopolar group panels (L/R × depth/paddle)
- 4 bipolar panels (one per lead/side block)
- each panel supports Raw, PSD, Spectrogram modes
- a **Network Coherence (MSC)** matrix in the control rail: 4 channel pairs × δ/β/γ, color-coded, computed with `scipy.signal.coherence` on the 15 s buffer at each lead's hotspot contact, updated on heavy ticks

### Behavioral State control

- A `Behavioral State` radio group (Rest / Movement / Sleep) in the control rail
- Selection sets `self.state`, passed into `simulate_chunk(state=...)` each tick
- Effects (state scalars *and* coherence) ramp in smoothly via the model's one-pole smoothing

### Stimulation controls

- Single `Stimulation` section
- Per-side controls:
  - enable
  - contact
  - amplitude
  - independent stimulation frequency sliders (`L Hz`, `R Hz`)

### Performance strategy

- Real-time timer updates at 100 ms chunk cadence
- Heavy spectral redraws decimated in time
- Lightweight phased redraw in between to reduce UI lag

---

## 9) Known modeling notes

- Beta and gamma are additive oscillatory peaks on top of a continuous 1/f floor; state scalars and stim suppression scale the peaks only, so a reduced band falls to the floor (a smaller hill), never below it.
- Entrainment and artifact terms are stimulation-linked and spatially weighted.
- Spectrogram brightness can vary naturally with auto-levels in streaming mode.

---

## 10) How to extend safely

Recommended extension points:

1. **Spectral character**: adjust profile terms in [simulator/config.py](simulator/config.py)
2. **Spatial spread**: tune `hotspot_decay_mm`, `baseline_floor`, `stim_decay_mm`
3. **Dynamics**: tune `tau_on_s`, `tau_off_s`, `a50_ma`, `hill_n`
4. **Artifacts**: tune `artifact_scale_uv_per_ma`, `artifact_2f_ratio`
5. **Behavioral states**: edit `STATE_MODIFIERS` (power) and `STATE_COHERENCE` (coherence `r = √MSC`) in [simulator/config.py](simulator/config.py)
6. **GUI behavior**: adjust redraw cadence and panel layouts in [nodes.py](nodes.py)

---

## 11) Minimal developer trace (one tick)

1. GUI builds left/right stimulation commands and reads the selected behavioral state
2. `DBSArrayModel.simulate_chunk(state=...)` advances state scalars + coherence, mixes shared/independent components, then generates a new monopolar chunk for all 32 channels
3. GUI appends to rolling buffers
4. Optional bipolar conversions are computed from fresh monopolar chunk
5. Panels redraw according to mode and update cadence; the coherence matrix updates on heavy ticks

This separation (generation → conversion → rendering) is the core design of this unified simulator.
