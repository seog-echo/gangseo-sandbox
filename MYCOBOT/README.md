# MYCOBOT — myCobot 280 Pi control + test GUI

A small Tkinter GUI to test-drive a **myCobot 280 Pi** robot arm, plus a serial
diagnostic script. This README is also a **handoff doc** so the work can be
continued on another machine — including by Claude.

---

## TL;DR — what this robot is and how to control it

- The robot is a **myCobot 280 Pi**: a 6-axis arm with a **Raspberry Pi 4 inside the base**.
- **It is NOT a USB serial peripheral.** Plugging USB into a PC/Mac shows **no serial port** —
  that path does not exist for this model. (We learned this the hard way; see History below.)
- The arm's Atom controller is wired to the **onboard Pi** over GPIO serial: **`/dev/ttyAMA0` @ `1000000`**.

There are two ways to control it:

| Mode | Where the GUI runs | Connection | Needs |
|------|--------------------|------------|-------|
| **Serial**  | ON the Pi (via VNC/SSH/monitor) | `MyCobot("/dev/ttyAMA0", 1000000)` | nothing extra |
| **Network** | On your laptop | `MyCobot280Socket(pi_ip, 9000)` | `Server_280.py` running on the Pi (auto-starts, see below) |

The GUI ([robot_tester_gui.py](robot_tester_gui.py)) supports **both** via a Serial/Network toggle.

---

## This robot's specifics (discovered)

- Pi OS: **Ubuntu 20.04**, hostname **`er`**, user **`er`**. Boot FAT is labelled
  `system-boot` (mounts at `/boot/firmware`); root ext4 is labelled `writable`.
- On the **Pi**: **pymycobot 3.1.2** — old API (`MyCobot`, *not* `MyCobot280`).
- On the **laptop**: **pymycobot 4.0.5** (`MyCobot280Socket`) under **Python 3.14** (`py -3.14`).
- Control is **joint-space only**: Cartesian `send_coords` does **not** execute on this
  arm's firmware (the sync variant polls forever and freezes the GUI). Use `send_angles`.
- Laptop Ethernet is an **Intel I226-V**; the laptop feeds the Pi an IP via ICS.

---

## Files

- **robot_tester_gui.py** — the test GUI. Serial/Network toggle; Stand / Sleep / Walk;
  STOP and Release Servos. Default Network IP is `192.168.137.33`.
- **diagnose.py** — SERIAL communication test. Run it **on the Pi** to confirm the arm responds.
- **.gitignore** — excludes venvs and the cloned `pymycobot` source (recreate locally).

---

## Windows laptop control — WORKING SETUP (verified)

Direct Ethernet cable, laptop → Pi. Steps in order:

### 1. Laptop Python
Use global **Python 3.14** (`py -3.14`; has tkinter). Install pymycobot once:
```cmd
py -3.14 -m pip install pymycobot
```
`py -3.14` sidesteps any activated project venv (which won't have pymycobot).

### 2. Share laptop internet → Ethernet (ICS = DHCP for the Pi)
A direct cable has no DHCP, so make the laptop the DHCP server:
- `ncpa.cpl` → right-click **Wi-Fi** → **Properties → Sharing** → check
  "Allow other network users to connect…", and target **Ethernet**.
- Ethernet becomes **192.168.137.1** and serves DHCP. The Pi gets **192.168.137.33**
  (and internet via NAT, which is how we install things on it).

### 3. If Ethernet shows "Disconnected" despite link lights (Intel I226-V quirk)
Disable Energy-Efficient Ethernet and reset the NIC (**elevated** PowerShell):
```powershell
Set-NetAdapterAdvancedProperty -Name "Ethernet" -DisplayName "Energy Efficient Ethernet" -DisplayValue "Off"
Restart-NetAdapter -Name "Ethernet"
```
After any robot power-cycle, you may need `Restart-NetAdapter -Name "Ethernet"` again
(the I226-V often won't re-acquire the link until reset).

### 4. Find the Pi / SSH in
```powershell
arp -a | findstr 192.168.137      # Pi MAC starts D8-3A-DD- (Raspberry Pi)
ssh er@192.168.137.33             # password: mycobot123 (we reset it; see recovery note)
```
A passwordless SSH **key for this laptop** is installed in the Pi's
`~/.ssh/authorized_keys`, so SSH/scp from this laptop need no password.

### 5. The control server auto-starts on the Pi
`Server_280.py` runs as a **systemd service** (`mycobot-server.service`), bound to
`0.0.0.0:9000`, started on every boot, restarts on crash. It's a raw TCP↔serial bridge
from the cloned repo at `~/pymycobot_src` (patched to bind `0.0.0.0`). Manage it:
```bash
sudo systemctl status mycobot-server
sudo systemctl restart mycobot-server
```

### 6. Run the GUI (Network mode)
```cmd
py -3.14 robot_tester_gui.py
```
- Mode **Network**, IP **192.168.137.33** (default), Port **9000** → **Connect**
- **Stand / Sleep / Walk**; **STOP** and **Release Servos** for safety.

### Pre-flight check (laptop → Pi)
```cmd
py -3.14 -c "from pymycobot import MyCobot280Socket; mc=MyCobot280Socket('192.168.137.33',9000); print(mc.get_angles())"
```
Six numbers = you're controlling the arm. ✅

---

## Poses / motions (joint-space, degrees [J1..J6])

- **Stand:** `[0, 0, 90, 0, 0, 0]` — J2 straight up, J3 forearm out at 90; balanced/stable.
- **Sleep:** `[0, 0, 90, 0, 90, 0]` — same, J5=90.
- **Walk:** a continuous, bouncy **head bob**. J1/J2 fixed at 0; J3 oscillates a few
  degrees around 90 with **J4 = -(J3-90)** so the head (accelerometer on top) stays
  **level**. It's **streamed** as many small setpoints along a slow-rise / fast-fall
  curve (mimics gait), not two end poses — so it's smooth, not stop-and-go. Tunables at
  the top of `state_walk`: `d` (amplitude), `T` (cycle time), `rise_frac` (bounce
  asymmetry), `speed`, `cycles`.

---

## Alternative: Serial mode (run the GUI on the Pi itself)
Note the GUI imports `MyCobot280`, which the Pi's pymycobot 3.1.2 lacks. On the Pi use the
`MyCobot` class directly, e.g.:
```bash
python3 -c "from pymycobot import MyCobot; import time; mc=MyCobot('/dev/ttyAMA0',1000000); time.sleep(1); print(mc.get_angles())"
```

---

## Recovery: lost the Pi password (how we got back in)
We had no password and no monitor on the Pi. Recovery, from this laptop:
1. Got the link up (ICS + the I226-V/EEE fix above) and found the Pi via mDNS (`er.local`).
2. SSH was reachable but the password was unknown → reset it headlessly via the **microSD**:
   pulled the card, and on the laptop wrote a one-shot `firstrun.sh` to the boot FAT plus a
   `systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot
   systemd.unit=kernel-command-line.target` hook in `cmdline.txt`. On next boot that script
   (as root) reset the uid-1000 user's password (`mycobot123`), ensured SSH, and installed
   our SSH public key — then cleaned the hook from `cmdline.txt` and rebooted.
3. A backup of the original boot partition is at `Desktop\mycobot_boot_backup`.

---

## Troubleshooting cheat sheet

- **No serial port on a PC/Mac** → expected; the 280 Pi is not a USB serial device. Use
  Network mode, or run on the Pi.
- **`get_angles()` returns `-1` / `[]`** → `-1` = no comms (on Pi: baud must be 1000000, arm
  powered, Atom OK). A bare `[]` is normal *during motion* (serial busy) — not a failure.
- **Walk freezes the GUI / arm won't do Cartesian moves** → `send_coords`/`sync_send_coords`
  don't work on this firmware (sync polls forever). Use joint-space `send_angles` only.
- **GUI "connected party did not respond / timeout"** → almost always the wrong IP in the
  field; it must be **192.168.137.33** (not the old `192.168.1.42`).
- **Ethernet "Disconnected" but link lights are on** → Intel I226-V: disable EEE +
  `Restart-NetAdapter` (see step 3). Needed again after each robot power-cycle.
- **Pi has no IPv4 / can't reach it on a direct cable** → enable ICS so the laptop hands it
  `192.168.137.33`; bounce the cable to trigger the Pi's DHCP if needed.
- **Can connect, can't move** → check servo power (logic power ≠ servo power) and that servos
  aren't released.
- **Server not listening after a Pi reboot** → `sudo systemctl status mycobot-server`
  (should auto-start; `restart` it if not).

---

## History (why the setup is the way it is)

1. Started by trying to control the robot via USB from a **Mac**. The GUI "connected" but the arm
   never moved.
2. `diagnose.py` showed all reads returning `-1` — no real two-way communication.
3. The "connected" port was actually the user's **Samsung phone**, not the robot.
4. No USB-serial chip ever enumerated for the robot → a physical-layer dead end.
5. Identified it as a **myCobot 280 Pi** — its own computer; USB-to-PC is not the control path.
6. Moved to a **Windows laptop**, direct Ethernet. Worked through: I226-V/EEE link issue →
   ICS to give the Pi an IP → mDNS discovery → SSH password recovery via the microSD →
   installed `Server_280.py` as a systemd service → drove the arm from the laptop GUI. Found
   Cartesian moves unusable on this firmware, so all states/Walk use joint-space angles.

---

## Local environments (not committed)
- `pymycobot/` (laptop) and `~/pymycobot_src` (Pi) — cloned upstream source. Recreate with
  `git clone https://github.com/elephantrobotics/pymycobot.git`.
- Mac venvs (`.venv`, `.venv-gui`) from the original Mac attempt — not needed on Windows.
</content>
