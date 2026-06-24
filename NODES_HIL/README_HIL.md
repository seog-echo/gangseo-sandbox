# NODES_HIL — Hardware-in-the-Loop DBS Simulator

A self-contained copy of NODES wrapped in a **partially closed loop** with NI-DAQ
hardware. The simulator runs exactly as in stock NODES, except stimulation is no
longer set by hand — it is driven by an external signal measured in real time.

```
  selected NODES channel ──(uV→V × gain)──► NI-9263 AO ──► oscilloscope / future IPG
  external mock stimulation ──► NI-9222 AI ──► amplitude + frequency
        ──► NODES stim params (LEFT side, contact 4) ──► reshapes ALL channels
```

This folder is **independent** of `../NODES` and `../DBS_HIL_GUI`; nothing here
imports from them.

## Run

```bash
pip install -r requirements.txt      # numpy, scipy, PySide6, pyqtgraph, pyarrow, nidaqmx
python nodes_hil.py
```

The main NODES window opens with an extra **HIL toolbar** on top:

| Button | Action |
|--------|--------|
| **Check NI Devices** | Scans for NI-9222 (AI) and NI-9263 (AO); reports hardware vs. simulation. |
| **Start HIL** | Begins the closed loop, opens the Loop Monitor, and disables the manual Stimulation controls. Auto-starts the simulator stream if idle. |
| **Stop HIL** | Ends the loop and re-enables manual stimulation. |
| **Show Monitor** | (Re)opens the Loop Monitor window. |

While HIL is running:
- The **Stimulation** controls are disabled (stim is input-driven).
- The **Behavioral State** radio buttons stay fully live — change Rest/Movement/Sleep
  any time and watch the effect propagate.

## Loop Monitor window

- **Output** plot — the selected NODES channel being streamed to NI-9263 (raw µV).
- **Input** plot — the mock-stimulation waveform sampled on NI-9222 (volts).
- **Readouts** — measured input amplitude/frequency, the resulting stim mA/Hz,
  the AO peak voltage, the stim target, and the I/O mode.
- **AO channel** selector — pick which of the 32 NODES channels goes to the scope.
- **AO gain** — multiplier on the volt-valued neural signal (default `10000`:
  ~50 µV → ~0.5 V).
- **Simulated input** controls — visible only when running without hardware;
  set the synthetic input amplitude/frequency to exercise the whole loop.

## Robotic arm (optional — myCobot 280)

NODES_HIL can drive a physical **myCobot 280** so the behavioral state has a
bodily embodiment. It is **entirely optional and isolated**: if `pymycobot` is
missing or the arm is unreachable, the feature is disabled and nothing else in
NODES_HIL is affected.

- On startup, a **background probe** tries to reach the arm over the network
  (the Pi's `Server_280.py`, default `192.168.137.33:9000`) — non-blocking, with a
  short timeout. The toolbar **Robot** group shows the result and an editable
  address + **Reconnect Robot** button.
- The **Enable Arm** checkbox is greyed out until a connection succeeds.
- **Enable Arm ON** → powers servos and the arm **mirrors the behavioral state**:
  - `Rest` → Stand pose `[0, 0, 90, 0, 0, 0]` (held)
  - `Sleep` → Sleep pose `[0, 0, 90, 0, 90, 0]` (held)
  - `Movement` → continuous bouncy "head bob" (J3/J4, head kept level)
  - These are the exact poses/motion from the MYCOBOT tester.
- Changing the state radio moves the arm **immediately** (a worker thread driven
  by an event; non-blocking `send_angles`, no `sync_*` stalls).
- **Enable Arm OFF** → **releases the servos** (note: the arm goes limp and may
  sag under gravity).
- The arm follows the state **whenever the toggle is on**, independent of whether
  a HIL run is active. On app close it releases the servos and disconnects.

Implemented in [`robot_arm.py`](robot_arm.py) (`RobotArmController`). Requires
`pymycobot` in the venv (in `requirements.txt`) and the Pi's `mycobot-server`
service running (see [../MYCOBOT/README.md](../MYCOBOT/README.md)).

## Mapping (configured defaults)

Defined in [`hil_mapping.py`](hil_mapping.py) (`HilMapping`):

| Parameter | Default | Meaning |
|-----------|---------|---------|
| Target | LEFT, contact index 3 (contact 4, STN hotspot) | Which NODES stim site the input drives. |
| Amplitude | `1.0 mA/V`, clamp 0–4 mA | **4 V peak → 4 mA.** |
| Frequency | gain 1.0, offset 0, clamp 10–200 Hz | **Direct pass-through.** |
| Deadband | 0.02 V | Below this input, stim is off. |

## Module map

| File | Role |
|------|------|
| `nodes_hil.py` | Entry point. `NodesHilWindow` subclasses `UnifiedDBSWindow`, adds the HIL toolbar, the generation thread, the display-only tick override, and the monitor. |
| `ni_io.py` | `NiHilIO` — AI reader thread + AO output (queued or externally-paced); NI-9222/NI-9263 detection; simulation fallback. |
| `signal_metrics.py` | `measure_signal()` — rolling-window peak amplitude + FFT dominant frequency. |
| `hil_mapping.py` | `HilMapping`/`resolve_drive()`/`drive_to_commands()` — measurement → `StimulationCommand`. |
| `hil_monitor.py` | `HilMonitorWindow` — the traceability plot/readout window (Raw/PSD/Spectrogram). |
| `hil_check_input.py` | Standalone read-only NI-9222 input check (verify the generator chain). |
| `robot_arm.py` | `RobotArmController` — optional myCobot 280 arm that mirrors the behavioral state (Rest/Sleep poses, Movement bob). Connection-probed, event-driven worker, fully isolated. |
| `nodes.py`, `simulator/`, `load_recording.py` | Copy of stock NODES. `nodes.py` has one added line (`self._last_out`) so the HIL subclass can route a chunk to AO without re-running the tick. |

## Real-time architecture (why the analog output is glitch-free)

During a HIL run the simulation does **not** run on the GUI timer. A dedicated
**generation thread** owns the model and loops:

> measure AI → resolve stim drive → `simulate_chunk(one block)` → **write the
> selected channel to NI-9263 (blocking)** → stash all 32 channels for display

The blocking AO write paces the generator to the hardware's 1024 Hz clock and lets
it run ahead to keep the DAQ buffer full. Because the generator is off the GUI
thread, redraw load (PSD/spectrogram/coherence) cannot starve the output — there is
no software queue to underrun, so no 0 V dropouts or flat holds. The GUI timer is a
pure **display consumer**: it drains the stashed chunks, updates the rolling buffers,
and redraws at its own (jittery) pace. The model is generated in exactly one place at
a time — the generation thread during HIL, the GUI tick in manual mode — so state is
never double-advanced. Stopping HIL joins the thread before manual generation resumes.

While HIL runs, the manual transport buttons (Start/Pause/End) are locked too; use
**Stop HIL** to end the run (the manual NODES stream then continues).

## Without hardware (simulation mode)

If `nidaqmx` is missing, no NI devices are found, or task setup fails, the loop runs
in **simulation mode**: `write_ao_block` paces by sleeping (nothing is driven) and the
AI reader synthesises a sine (amplitude/frequency adjustable in the Monitor). This lets
the entire pipeline — measurement, mapping, stim drive, and all 32 channels reacting —
be demonstrated on any machine.

## Output reconstruction (for the IPG, which has no anti-aliasing filter)

The NI-9263 is a zero-order-hold DAC: at 1024 Hz it emits a staircase whose
spectral images sit around multiples of 1024 Hz. The IPG samples at ~1024 Hz with
**no anti-aliasing filter**, so those images would fold into its baseband and
corrupt the bandpower it computes. Two defences:

1. **AO oversampling (software, on by default ×8).** The selected channel is
   interpolated to `1024 × N` Hz before output (continuous piecewise-linear,
   carried across blocks — no boundary glitch). This shrinks the staircase and
   pushes/attenuates the images: at ×8 the near-1024 Hz images are ~**-67 dB**.
   Set the factor in the Monitor ("AO oversample ×", applies on next Start HIL).
2. **Analog reconstruction low-pass (hardware, recommended when the IPG is wired
   in).** Place a low-pass between NI-9263 `ao0` and the IPG input with a corner
   **~200-300 Hz** (above NODES' content incl. the 2×stim artifact ≤ 400 Hz... so
   use ~300-400 Hz, below the 512 Hz IPG Nyquist). A 2nd-4th order active filter is
   ideal; even a single RC (e.g. 10 kΩ + 100 nF ≈ 160 Hz, or 4.7 kΩ + 100 nF ≈
   340 Hz) markedly cleans the output. This is the definitive guarantee that no
   image energy reaches the no-AAF IPG.

Validate empirically once the IPG is available: compare NODES bandpower on the
source channel against the IPG-recorded bandpower and tune gain/oversample/filter
until they match.

## Timing

- Generation block: ~50 ms (`_gen_block`), paced by the AO write.
- Stim-parameter update: every block (~20 Hz); NODES ramps stim internally.
- AO: streamed at the NODES-native **1024 Hz**, regeneration disabled (true streaming),
  ~0.5 s DAQ buffer kept full by the generation thread.
- AI: **20 kHz**, 1 channel, ~0.3 s measurement window.
- GUI redraw: ~100 ms, fully decoupled from AO.
