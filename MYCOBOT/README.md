# MYCOBOT — myCobot 280 Pi control + test GUI

A small Tkinter GUI to test-drive a **myCobot 280 Pi** robot arm, plus a serial
diagnostic script. This README is also a **handoff doc** so the work can be
continued on another machine (e.g. a Windows laptop) — including by Claude.

---

## TL;DR — what this robot is and how to control it

- The robot is a **myCobot 280 Pi**: a 6-axis arm with a **Raspberry Pi 4 inside the base**.
- **It is NOT a USB serial peripheral.** Plugging USB into a PC/Mac shows **no serial port** —
  that path does not exist for this model. (We learned this the hard way; see History below.)
- The arm's Atom controller is wired to the **onboard Pi** over GPIO serial: **`/dev/ttyAMA0` @ `1000000`**.

There are two ways to control it:

| Mode | Where the GUI runs | Connection | Needs |
|------|--------------------|------------|-------|
| **Serial**  | ON the Pi (via VNC/SSH/monitor) | `MyCobot280("/dev/ttyAMA0", 1000000)` | nothing extra |
| **Network** | On your laptop | `MyCobot280Socket(pi_ip, 9000)` | `Server_280.py` running on the Pi |

The GUI ([robot_tester_gui.py](robot_tester_gui.py)) supports **both** via a Serial/Network toggle.

---

## Files

- **robot_tester_gui.py** — the test GUI. Serial/Network mode toggle; buttons for Stand / Sleep /
  Walk; STOP and Release Servos. Safe to run anywhere (it only talks to hardware on Connect).
- **diagnose.py** — SERIAL communication test. Run it **on the Pi** to confirm the arm responds.
- **.gitignore** — excludes venvs and the cloned `pymycobot` source (recreate locally).

---

## Continuing on a Windows laptop (current plan)

Goal: laptop ↔ robot over an **Ethernet cable**, control the arm from the laptop (Network mode).

### 1. Install Python deps on Windows
Windows Python includes tkinter already. Just add pymycobot:
```cmd
pip install pymycobot
```

### 2. Connect the robot to the laptop via Ethernet
A **direct** cable (no router) means there's no DHCP server, so both ends self-assign a
link-local address (`169.254.x.x`). Two ways to reach the Pi:

- **Easiest:** plug both the Pi and the laptop into a **router/switch** instead — the Pi gets a
  normal DHCP IP. Find it from your router's device list.
- **Direct cable:** install **Bonjour** (ships with iTunes) so mDNS works, then try the Pi's
  hostname, e.g. `ping er-desktop.local`. Or assign static IPs to both NICs
  (e.g. laptop `192.168.50.1`, and set the Pi to `192.168.50.2`).

To find the Pi from Windows once it's linked:
```cmd
arp -a            REM look for the Pi's MAC/IP on your Ethernet adapter
ping er-desktop.local
```

### 3. SSH into the Pi (Windows 10+ has built-in `ssh`)
Default Elephant Robotics login is usually **`er` / `Elephant`**:
```cmd
ssh er@<pi-ip>
```

### 4. On the Pi: start the network control server
pymycobot ships demo servers. Find and run the 280 one:
```bash
find / -name "Server_280.py" 2>/dev/null      # locate it
python3 /path/to/pymycobot/demo/Server_280.py
```
It prints `ip: <pi-ip> port: 9000` and relays commands to the arm. Leave it running.
(`Server_280.py` reads the Pi's `wlan0` IP and listens on TCP **9000**; it needs `RPi.GPIO`, so
it only runs on the Pi. If the Pi is on Ethernet not Wi-Fi, edit the `ifname = "wlan0"` line near
the bottom to `"eth0"`.)

### 5. On the laptop: run the GUI in Network mode
```cmd
python robot_tester_gui.py
```
- Select **Network (from Mac/laptop)**
- Enter the Pi's IP, port `9000`
- Click **Connect**, then test **Stand / Sleep / Walk**

### Pre-flight check (laptop → Pi over network)
```cmd
python -c "from pymycobot import MyCobot280Socket; mc=MyCobot280Socket('PI_IP',9000); print(mc.get_angles())"
```
Six numbers printed = you're controlling the arm from the laptop. ✅

---

## Alternative: Serial mode (run the GUI on the Pi itself)
VNC or SSH (with X forwarding) into the Pi, then:
```bash
python3 robot_tester_gui.py     # Serial mode, /dev/ttyAMA0 @ 1000000
# or sanity-check first:
python3 diagnose.py
```

---

## Troubleshooting cheat sheet

- **No serial port appears when plugged into a PC/Mac** → expected. The 280 Pi is not a USB serial
  device. Use Network mode, or run on the Pi.
- **`get_angles()` returns `-1`** → no real comms. On the Pi: wrong baud (use 1000000), arm not
  powered, or firmware issue. Over network: server not running / wrong IP / firewall.
- **Connects but doesn't move** → check the arm's **power adapter** is on (USB/logic power ≠ servo
  power), and that servos aren't released.
- **Can't find the Pi's IP** → router device list; `arp -a`; or `hostname -I` when on the Pi.
- **Server_280.py errors on `wlan0`** → the Pi is on Ethernet; change `ifname` to `eth0`.

---

## History (why the setup is the way it is)

1. Started by trying to control the robot via USB from a **Mac**. The GUI "connected" but the arm
   never moved.
2. `diagnose.py` showed all reads returning `-1` — i.e. no real two-way communication.
3. Found the "connected" port `/dev/cu.usbmodemRFCW10MKCPN2` was actually the user's **Samsung
   phone**, not the robot (confirmed via `ioreg` USB vendor = SAMSUNG).
4. No USB-serial chip ever enumerated on the Mac for the robot, even at the raw USB level → a
   physical-layer dead end.
5. Identified the robot as a **myCobot 280 Pi** (Raspberry Pi version, no M5 base screen). That
   model is its own computer; USB-to-PC is simply not the control path. The original script
   defaults (`/dev/ttyAMA0`, `1000000`) were the Pi's settings all along.
6. Conclusion: control either **on the Pi** (serial) or **from a laptop over the network** (socket
   via `Server_280.py`). GUI updated to support both.

---

## Local environments (not committed)
- `.venv/` — full pymycobot dev install (created on the Mac; **no tkinter** because pyenv Python
  3.13 was built without Tk).
- `.venv-gui/` — Mac venv from system Python 3.9 that **does** have tkinter; used for offline GUI
  testing on the Mac. Not needed on Windows (system Python has tkinter; just `pip install pymycobot`).
- `pymycobot/` — cloned upstream source. Recreate with
  `git clone https://github.com/elephantrobotics/pymycobot.git`.
