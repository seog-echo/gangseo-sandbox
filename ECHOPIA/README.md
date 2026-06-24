# Echopia

A cozy, low-poly game that simulates the daily life of a Parkinson's patient
("Mr. Echo") with an implanted DBS system, to demonstrate the **NODES** neural
engine and the concept of continuous vs. adaptive stimulation in an intuitive,
fun way.

See [DESIGN.md](DESIGN.md) for the full concept, wireframes, and build plan.

## Architecture

```
Browser front-end (Three.js game + signal plots + phone app)
        |  WebSocket JSON, ~20 Hz  (state + stim  <->  4 channels + beta + battery)
Python backend (backend/) -- wraps NODES DBSArrayModel, owns adaptive logic
        |
NODES (../NODES, untouched) -- generates the bilateral DBS signals
```

The browser never touches NODES directly; it sends control messages to the
backend and receives a compact per-tick payload. A future hardware path
(NODES_HIL -> NI-9263 -> IPG) can subscribe to the same state/stim stream.

## Run

Requires Python 3.10+ and an internet connection (the front-end loads Three.js
from a CDN). Keep this folder next to the `NODES/` folder (the backend imports
the NODES simulator from `../NODES`).

**macOS / Linux**
```bash
cd ECHOPIA
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/python -m backend.server
# open http://localhost:8765/
```

**Windows** (PowerShell or Command Prompt)
```bat
cd ECHOPIA
py -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements.txt
.venv\Scripts\python -m backend.server
:: open http://localhost:8765/ in Edge/Chrome/Firefox
```

The server takes a few seconds to start (NODES builds its signal baselines),
then serves the front-end and the WebSocket on the same port (8765). Open the
URL in any modern browser (WebGL required).

### Engine self-test (no browser)

```bash
.venv/bin/python -m backend.engine      # Windows: .venv\Scripts\python -m backend.engine
```

Prints beta biomarker + applied amplitude across states/modes. Expected:
Rest+OFF has high STN beta; Movement desynchronizes it; stimulation suppresses
it; adaptive modes regulate it.

## Status

- [x] **Phase 0** — backend seam: WS server wrapping NODES, 4 hotspot channels,
      adaptive logic (off / continuous / adaptive-state / adaptive-closed-loop),
      beta biomarker, battery. Test page at `/` (`test_client.html`).
- [x] **Phase 1** — cozy 3D world (decorated 2-room house + doorway), cute
      capsule avatar (Mr. Echo), right-click omni-movement with wall sliding,
      furniture highlight + interaction (sit / lie / sleep) wired to backend state.
- [x] **Phase 2** — live signal panel: 4 hotspot channels with Raw / PSD /
      Spectrogram views (self-contained FFT, beta-band shading), plus stim
      amplitude readout and STN beta biomarker bars. Layout split into game
      viewport + side panel.
- [x] **Phase 3** — patient phone app: floating phone icon above Mr. Echo
      (tap to open), slide-in phone UI with stim on/off, continuous/adaptive
      mode, 1–3 mA amplitude slider (continuous only), and IPG battery. Starts
      OFF; turning it on visibly suppresses the STN beta in the panel.
- [x] **Phase 4** — symptom model (tremor, bradykinesia, doorway FoG, falls)
      driven by stim efficacy × state × location; adverse events (FoG/fall)
      trigger a phone alarm + "Report this episode?" flow with an event-report log.
- [x] **Phase 5** — adaptive showcase: phone toggle between Schedule
      (state-lookup: sleep 1 / rest 2 / move 3 mA) and Closed-loop (amplitude
      driven by measured STN beta). Closed-loop regulates beta with less total
      current than continuous.
- [x] **Phase 6** — polish: generative ambient background music (WebAudio,
      toggle in top bar), animated avatar (swinging arms/legs, sit/freeze/fall
      poses), cozy lamp flicker, camera ease-in, and report-count in the phone.
- [ ] **Phase 7 (later)** — hardware-in-the-loop: NODES → NI-9263 → real IPG,
      and IPG stim → NI-9222 → closes the loop. Full architecture & build plan
      in [PHASE7_HIL_PLAN.md](PHASE7_HIL_PLAN.md) (grounded in the existing
      `../NODES_HIL/` code, ready to start when the bench is available).

## Layout

```
Echopia/
  DESIGN.md            concept + wireframes + plan
  README.md            this file
  test_client.html     Phase-0 throwaway stream/plot test page
  backend/
    engine.py          EchopiaEngine: wraps NODES, state/stim -> signals + beta
    server.py          WebSocket + static file server
    requirements.txt
```
