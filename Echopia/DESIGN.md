# Echopia — Design Sketch

A cozy, cute, low-poly game that simulates the daily life of a Parkinson's
disease patient ("Mr. Echo") with a DBS system, to demonstrate the NODES
neural engine and the concept of continuous vs. adaptive stimulation in a
fun, intuitive way.

Status: **DESIGN / SKETCH PHASE — no game code yet.**

---

## Decisions locked so far

| Topic            | Decision |
|------------------|----------|
| Name             | Echopia (avatar: "Mr. Echo") |
| Front-end        | Web, Three.js (top-down tilted 3D, 2D movement) |
| Back-end         | Python, WebSocket server wrapping NODES `DBSArrayModel` |
| NODES            | Reused untouched; backend imports `simulate_chunk(state, stim)` |
| Aesthetic        | Cozy, cute, low-poly — NOT realistic/clinical; warm & lived-in |
| Adaptive stim    | Switchable: simple state-lookup first, closed-loop-on-beta later |
| Hardware         | Pure software first; architect a sink so NODES_HIL → NI-9263 → IPG can subscribe later |
| Folder           | `Echopia/` (this folder), NODES untouched |

---

## State mapping (avatar behavior -> NODES `state`)

| Avatar action          | NODES state | Notes |
|------------------------|-------------|-------|
| Walking (right-click move) | Movement | beta desync, gamma up |
| Sitting (sofa/chair)   | Rest        | beta elevated |
| Lying on bed           | Rest        | |
| Sleeping on bed        | Sleep       | delta/theta slow waves |

Streaming display: 1 hotspot monopolar channel per lead = 4 plots
(Paddle L/R, Depth L/R). Stimulation applied on the two **depth (STN)** leads only.

---

## Stimulation modes

- Demo **starts with stim OFF**.
- **Continuous:** fixed amplitude, user-controllable 1–3 mA via phone slider.
- **Adaptive:** slider disabled, amplitude set automatically.
  - *State-lookup* (ship first): sleep 1 mA / rest 2 mA / move 3 mA.
  - *Closed-loop* (showcase later): amplitude driven by measured beta power.
- Symptom relief ladder: **OFF (worst) -> Continuous -> Adaptive (best)**.

---

## Symptom model (stim efficacy x state x location)

| Symptom       | Trigger / behavior | Relief by stim |
|---------------|--------------------|----------------|
| Tremor        | rest tremor, gentle avatar shake | yes |
| Bradykinesia  | slowed walk speed | yes |
| FoG           | at the **doorway** & turns, during movement; avatar freezes | yes |
| Fall          | probabilistic, often after FoG; avatar tips over | yes |
| Dyskinesia    | (optional) from OVER-stim at high continuous amp — shows adaptive's value | n/a |

Adverse events (FoG, fall) -> phone auto-alarms + sound -> "Report this episode?"
-> logged in an auto-report list.

---

## Screen layout (16:9)

```
+-----------------------------------------------------------------------------+
|  ECHOPIA          state: Rest      stim: OFF        IPG 78%        music    | top bar
+----------------------------------------------+------------------------------+
|                                              |  NODES SIGNALS    [raw v]    |
|        +-------------+   +-------------+      |  Paddle L  ~~~~~~~~~~~~~~~~   |
|        |  BEDROOM    |   | LIVING ROOM |      |  Paddle R  ~~~~~~~~~~~~~~~~   |
|        |  bed chair  | || |  sofa table |      |  Depth  L  ~~~~~~~~~~~~~~~~   | 4 plots
|        |             | || |             |      |  Depth  R  ~~~~~~~~~~~~~~~~   |
|        |     Mr.Echo (top-down)         |      |                              |
|        +-------------+   +-------------+      |  Stim amp  L  0.0 mA          |
|              ^ doorway = FoG hotspot          |            R  0.0 mA          |
|                                              |  beta ||||||  (when on)       |
|     right-click = move | click phone = app   |                              |
+----------------------------------------------+------------------------------+
                 GAME VIEWPORT (~65%)                  SIDE PANEL (~35%)
```

Tapping the phone slides a phone-shaped panel in over the lower side panel;
signal plots stay visible above it.

---

## House floor plan (top-down) — furnished & decorated

```
   +=================================+=================================+
   |  BEDROOM                        |  LIVING ROOM                    |
   |  [pic][pic]      .-clock-.       |     [shelf/books][TV stand+TV]  |
   |   +-------+   +------+           |    +--------+    +----------+   |
   |   |  BED  |   |wardrobe|         |    |  SOFA  |    | bookshelf|   |
   |   | +pillow|  +------+           |    +--------+    +----------+   |
   |   +-------+                      +--+   [coffee]                   |
   |   [night    (lamp)              DOORWAY  table     [floor lamp]    |
   |    stand]   [rug]               +--+   +------+                    |
   |   [slippers]      o<-chair      |      | TABLE |  [potted plant]   |
   |   [laundry basket]              |      +------+   [armchair]       |
   |                                 |   [rug]      [window+curtains]   |
   |   [potted plant]  [wall art]    |   [side table+vase] [picture]    |
   +=================================+=================================+
        warm wood floor                   warm wood floor

   phone starts in Mr.Echo's pocket (icon follows the avatar).
```

The doorway is the single narrow chokepoint between rooms => natural FoG trigger.

---

## Decorative props (cozy, lived-in, low-poly)

Shared / structural:
- warm wood floors, soft wall color, baseboards
- area rugs in each room, warm ambient + lamp glow lighting

Bedroom:
- bed with pillows + blanket, nightstand with small lamp, wardrobe
- slippers by the bed, laundry basket, framed family photos on wall
- potted plant, wall art, small clock

Living room:
- sofa + armchair, coffee table (mug, remote), side table with vase/flowers
- TV on a stand, bookshelf with books + trinkets
- floor lamp, big potted plant, window with curtains, framed pictures
- small wall clock, maybe a rug-side cat bed for charm

These are static decor (no interaction) EXCEPT the interactable set below;
they exist purely to make the home feel warm and not empty.

---

## Phone app screens

```
   HOME                  ALERT (FoG/Fall)       AFTER REPORT
  +-----------+         +-----------+          +-----------+
  |  Echopia  |         |  ! FoG     |          | logged    |
  | STIM [OFF]|         | detected  |          | Reports:3 |
  | Mode:     |         | Report?   |          | FoG 10:42 |
  |  Continuous|        | [Yes][No] |          | Fall 9:15 |
  |  Adaptive |         +-----------+          | FoG 8:50  |
  | Amp 2.0mA |       (auto-pops + sound        | [Back]    |
  | IPG 78%   |        on FoG / fall)           +-----------+
  +-----------+
```

Amplitude slider enabled only in Continuous mode (greys out in Adaptive).

---

## Object interaction

When the avatar is close enough, the object glows/outlines. Click it -> popup.
Interactable objects: **bed, sofa, armchair, chair** (decor is non-interactive).

```
   +--------------+        +--------------+
   |  Bed         |        |  Sofa / Chair|
   |  > Lie down  |        |  > Sit       |
   |  > Sleep     |        +--------------+
   +--------------+
```

---

## Data contract (WebSocket JSON, ~20 Hz)

Frontend -> backend:
```json
{ "state": "Rest|Movement|Sleep",
  "stim": { "mode": "off|continuous|adaptive",
            "adaptive_kind": "state|closed_loop",
            "amplitude_ma": 2.0,
            "left":  { "contact": 3 },
            "right": { "contact": 3 } } }
```

Backend -> frontend:
```json
{ "t": 12.34,
  "channels": { "paddleL": [], "paddleR": [], "depthL": [], "depthR": [] },
  "beta":     { "depthL": 0.0, "depthR": 0.0 },
  "stim_applied": { "left": 2.0, "right": 2.0 },
  "battery": 0.78 }
```

Backend owns *applied* amplitude so all adaptive logic lives in one place.

---

## Build phases (after sketch is approved)

0. Backend seam: WS server wrapping NODES, streams 4 hotspot channels.
1. Cozy world: 2-room house + doorway + decor, camera, avatar, right-click move, furniture interact.
2. Signal panel: 4 live plots, raw/psd/spectrogram toggle, stim amp readout.
3. Phone app: pocket phone, stim on/off, mode, amp slider, battery. Starts OFF.
4. Symptoms + adverse events: state machine, doorway FoG, falls, alarms, reports.
5. Adaptive: state-lookup, then switchable closed-loop-on-beta.
6. Polish: BGM, animations, report history.
7. Later: HIL output hook (NODES_HIL subscribes to the live state/stim stream).
```
