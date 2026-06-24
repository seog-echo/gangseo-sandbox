# Echopia — Phase 7: Hardware-in-the-Loop (HIL) Plan

**Status: PLANNING ONLY — not built. Pick this up when the NI-DAQ bench is ready.**

Goal: let Echopia drive a **real IPG** through the existing NODES_HIL hardware
path — NODES (the simulated brain) streams its neural signal out through the
**NI-9263 (AO)** to the IPG's sense input, and the IPG's real stimulation comes
back in through the **NI-9222 (AI)** and closes the loop by driving NODES'
stimulation. The cozy game becomes the front-end and the "body/brain"; the
implant under test does the actual sensing and stimulating.

This document is grounded in the real code in `../NODES_HIL/`. File/function
names below are exact so you can start wiring immediately.

---

## 1. The two signals on the wire (don't conflate them)

There are **two distinct analog signals**, flowing opposite directions:

```
   NODES neural signal (the "brain")                IPG stimulation output
   one hotspot channel, µV-scale                    the device's real pulses
            │                                                  ▲
            ▼                                                  │
   ×oversample, ×1e-6, ×AO_GAIN                       measure_signal() →
            │  (µV → volts)                            amplitude_v, frequency_hz
            ▼                                                  │
   NI-9263 AO  ──►  [analog reconstruction LP ~200–400 Hz]  ──►  IPG SENSE in
                                                                     │
   IPG runs its own (a)DBS algorithm on what it senses ─────────────┘
                                                                     │
   IPG STIM out  ──►  [attenuator/safety]  ──►  NI-9222 AI  ─────────┘
            │
            ▼
   measure_signal() → resolve_drive() → drive_to_commands()
            │
            ▼
   DBSArrayModel.simulate_chunk(stim_commands, state)  ← state from the GAME
            │
            ▼  (beta suppressed by the IPG's stim → changed neural signal)
   back to the top — the IPG is now closing the loop on a simulated brain
```

The IPG senses NODES' signal, decides how to stimulate, and that stimulation
(measured on AI) feeds back into NODES — which suppresses beta accordingly. The
**game state (Rest/Movement/Sleep)** continuously reshapes the brain the IPG
is sensing.

---

## 2. Reusable pieces already in `../NODES_HIL/` (verified)

| File | What to reuse | Qt-coupled? |
|------|---------------|-------------|
| `hil_mapping.py` | `HilMapping`, `resolve_drive(amplitude_v, frequency_hz, mapping) -> StimDrive`, `drive_to_commands(drive) -> {side: StimulationCommand}` | **No** — pure, reuse as-is |
| `signal_metrics.py` | `measure_signal(samples, fs) -> SignalMeasurement(amplitude_v, frequency_hz, rms_v, valid)` | **No** — pure, reuse as-is |
| `ni_io.py` | `NiHilIO` (AI reader + AO writer threads, sim fallback), `AiConfig`, `AoConfig`, `check_devices()`, `start(..., external_ao=True)`, `write_ao_block(volts)` [blocking, paces the loop], `get_ai_window() -> (window, fs)` | **Yes** — uses `PySide6.QtCore.Signal` |
| `nodes_hil.py` | `_gen_loop()` pattern, `_upsample_for_ao()`, AO scaling, constants `DEFAULT_AO_GAIN=10000`, `DEFAULT_AO_OVERSAMPLE=8` | reference (Qt GUI) |

Key hardware facts baked into that code:
- **AO (NI-9263):** NODES native `fs = 1024 Hz`, interpolated ×8 (`_upsample_for_ao`,
  linear, carried across block boundaries), then `µV × 1e-6 × AO_GAIN(=10000)` →
  volts. `external_ao=True` makes `write_ao_block()` block on the DAQ buffer, which
  **paces the generation loop to the hardware clock** (glitch-free, no regen).
- **AI (NI-9222):** 20 kHz, 1 channel, 0.3 s rolling window; `get_ai_window()` →
  `measure_signal()` → peak amplitude + dominant frequency.
- **Mapping default:** `amp_gain_ma_per_v=1.0` (4 V → 4 mA), `amp_deadband_v=0.02`,
  `freq` pass-through, target **left depth contact 3** (STN hotspot), `idle_frequency_hz=130`.
- The IPG has **no anti-aliasing filter** → keep the ×8 oversample AND add an
  **analog reconstruction low-pass (~200–400 Hz)** between NI-9263 and the IPG.

---

## 3. The one refactor to do first

`NiHilIO` is a `QtCore.QObject` and emits `ai_chunk_ready` / `status_text` /
`error` as Qt signals. Echopia's backend is headless asyncio — we don't want a
Qt dependency or event loop there.

**Decision: make `NiHilIO` transport-agnostic.** Replace the three `QtCore.Signal`
members with plain optional callbacks (`on_ai_chunk`, `on_status`, `on_error`),
and drop the `QtCore.QObject` base. The NODES_HIL GUI then passes Qt-signal
emitters as those callbacks (one-line adapters); Echopia passes plain functions.
Net: **one** NI I/O implementation shared by both. `hil_mapping.py` and
`signal_metrics.py` need no changes.

(Alternative if you don't want to touch NODES_HIL: copy `ni_io.py` into
`Echopia/backend/ni_io.py` and strip the Qt bits. Costs a divergent copy.)

---

## 4. Echopia backend changes

Today: `backend/engine.py` (`EchopiaEngine`) is ticked at 20 Hz by the asyncio
broadcaster in `backend/server.py`, and stim comes from the phone control.

Introduce a **driver abstraction** so the WebSocket layer doesn't care whether
the model is ticked in software or paced by hardware:

```
backend/
  engine.py        # unchanged core: wraps DBSArrayModel, beta, battery, payload build
  drivers/
    base.py        # ModelDriver: set_state(s), set_stim_control(c), latest_payload(), start(), stop()
    software.py    # SoftwareDriver: asyncio 20 Hz tick (today's behavior) — stim from phone
    hil.py         # HilDriver: real-time thread; AO out + AI in; stim from IPG
  ni_io.py         # (after refactor) Qt-free NI I/O, or import from ../NODES_HIL
  server.py        # picks driver by config/flag; broadcaster reads latest_payload()
```

### `HilDriver` (the heart) — mirrors `nodes_hil._gen_loop`
A dedicated OS thread (NOT the asyncio loop), because `write_ao_block()` blocks:

```
start():
  io = NiHilIO()                       # refactored, Qt-free
  io.start(AiConfig(device=<9222>, channel=…),
           AoConfig(device=<9263>, channel=…, sample_rate_hz=1024*OVERSAMPLE),
           external_ao=True)
  spawn thread → _gen_loop()

_gen_loop():                            # ~1024-Hz-paced
  while running:
    window, fs = io.get_ai_window()
    meas  = measure_signal(window, fs)            # IPG stim → amplitude/freq
    drive = resolve_drive(meas.amplitude_v, meas.frequency_hz, mapping)
    cmds  = drive_to_commands(drive)              # → {side: StimulationCommand}
    out   = model.simulate_chunk(cmds, n_samples=BLOCK, state=self.state)  # state from GAME
    sig   = upsample_x8(out[ao_channel]) * 1e-6 * AO_GAIN
    io.write_ao_block(sig)              # BLOCKS → paces this loop to hardware
    # stash latest channels + measured drive + beta for the broadcaster (locked)
```

The broadcaster in `server.py` stays at ~20 Hz and just reads
`driver.latest_payload()` (thread-safe snapshot) → sends to the browser. The
browser still sends `{state}`; in HIL mode the backend applies `state` to the
gen loop and **ignores phone stim** (the IPG is the stim authority).

### Engine reuse
`EchopiaEngine` already computes the 4 display channels, beta biomarker, and
payload shape. Factor those helpers out so both drivers call them; `HilDriver`
feeds them the `out` dict from the gen loop and the measured drive instead of
the software-resolved amplitude.

---

## 5. Stim authority: who decides stimulation in HIL?

Two modes — build **HIL-A** as the headline; **HIL-B** is a simpler stepping stone.

- **HIL-A — IPG-driven (the real story).** Stim command = whatever the IPG is
  actually doing, measured on AI via `resolve_drive`. The phone shows on/off only
  (maps to enabling the IPG / its channels on the bench). Symptom `protection`
  in the game should be derived from the **measured** amplitude, so the avatar's
  symptoms reflect the real device's behavior. This demonstrates a real implant
  controlling symptoms in a simulated patient.
- **HIL-B — game-driven (visualization only).** Phone/adaptive logic still sets
  stim (today's software path); AO just streams the resulting neural signal out
  to a scope/IPG-sense for show; AI is optional/monitoring. Easier first light-up.

---

## 6. Data-contract additions (backend → browser)

Add to the per-tick payload so the UI can show the hardware loop:
```
source: "sim" | "hil",
measured: { amplitude_v, frequency_hz, valid },   // from AI / measure_signal
ao: { underruns, oversample, gain },               // health/diagnostics
device: { ai: "<name|sim>", ao: "<name|sim>", simulation: bool }
```
Front-end: a small "HIL" badge + a measured-input readout in the side panel; the
phone's mode controls grey out in HIL-A (with a note: "stimulation controlled by
implant"). The stimulation readout shows the measured/applied current as today.

---

## 7. Build checklist (when you start)

1. [ ] Refactor `NiHilIO` to be Qt-free (callbacks); update NODES_HIL GUI adapters.
2. [ ] Add `websockets`-safe imports: backend imports `../NODES_HIL` for
       `hil_mapping`, `signal_metrics`, `ni_io` (path insert like NODES today).
3. [ ] `backend/drivers/base.py` + refactor current logic into `software.py`.
4. [ ] `backend/drivers/hil.py` — port `_gen_loop` + `_upsample_for_ao`; thread;
       thread-safe `latest_payload()`.
5. [ ] `server.py` — `--driver sim|hil` flag (env or CLI); broadcaster reads driver.
6. [ ] Payload additions (§6) + front-end HIL badge / measured readout.
7. [ ] HIL-A: derive symptom `protection` from measured amplitude (in `main.js`,
       use `payload.measured`/`stim_applied` instead of phone-derived value).
8. [ ] Bench bring-up in **simulation mode first** (`force_simulation=True`,
       `nidaqmx` absent) — verifies the whole loop with no hardware.
9. [ ] Hardware bring-up + calibration (§8).

---

## 8. Calibration & safety (bench)

- **AO sense scaling (`AO_GAIN`, oversample):** start at `AO_GAIN=10000`, ×8.
  Confirm on a scope that the NI-9263 output (after the reconstruction LP) is in
  the IPG's expected sense range; adjust gain so µV-scale NODES content maps to a
  clean, in-range sense voltage.
- **AI stim mapping (`HilMapping`):** measure the IPG stim as it appears on the
  NI-9222 (after any attenuator), then set `amp_gain_ma_per_v` so measured volts
  → correct mA, and `amp_deadband_v` above the noise floor. `freq` is pass-through.
- **Reconstruction filter:** ~200–400 Hz passive LP between NI-9263 and IPG sense
  (IPG has no AAF). Keep the ×8 oversample regardless.
- **Safety:** protect the NI-9222 input from IPG stim voltages/currents
  (attenuation, current limiting, isolation as appropriate). Verify ranges in
  `AiConfig.voltage_min/max` before connecting the implant.
- **Battery:** real IPG has its own battery; make Echopia's battery cosmetic or
  hide it in HIL mode.

---

## 9. Risks / gotchas

- **Timing:** the gen loop must run off the asyncio thread; `write_ao_block`
  blocks. Don't await it. Use a plain thread + locked snapshot for the broadcaster.
- **Underruns:** `AoConfig.target_latency_s` (default 0.3 s) cushions producer
  jitter; surface `ao.underruns` in the payload so you can tune it.
- **Loop latency:** AI 0.3 s window + measurement + sim block adds delay before
  the IPG's stim affects NODES. Fine for a demo; note it when explaining.
- **Two FFTs:** `signal_metrics.measure_signal` (AI) is separate from the panel's
  display PSD (`plots.js`). Keep them separate.
- **State vs stim:** in HIL-A the game still owns **state**; the IPG owns **stim**.
  Don't let the phone also push stim in that mode (gate it in the driver).

---

## 10. One-paragraph summary for future-me

Reuse `hil_mapping.py` + `signal_metrics.py` as-is. Make `NiHilIO` Qt-free.
Add a `HilDriver` that runs NODES_HIL's `_gen_loop` in a thread: read NI-9222 →
`measure_signal` → `resolve_drive` → `simulate_chunk(state=<from game>)` →
upsample ×8 → ×1e-6×10000 → `write_ao_block` (blocks/paces). The WebSocket
broadcaster just publishes the gen thread's latest snapshot; the browser keeps
sending game state. Headline mode (HIL-A): the IPG, sensing NODES over the
NI-9263 and stimulating back through the NI-9222, closes the loop and controls
Mr. Echo's symptoms. Bring it up in simulation mode first, then calibrate.
```
