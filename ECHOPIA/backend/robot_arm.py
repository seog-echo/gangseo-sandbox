"""Optional myCobot 280 arm control for Echopia (backend side).

Mirrors Mr. Echo's behavioral state onto a physical myCobot 280 over the network
(the Pi's Server_280.py, default 192.168.137.33:9000), using the exact motions
from the MYCOBOT tester:

    Rest      -> Stand pose  [0, 0, 90, 0, 0, 0]   (held)
    Sleep     -> Sleep pose  [0, 0, 90, 0, 90, 0]   (held)
    Movement  -> continuous bouncy "head bob" whose CADENCE tracks the avatar's
                 (symptom-scaled) walking speed -- slower/bradykinetic gait bobs
                 slower, faster gait bobs faster.

Entirely optional and isolated: if pymycobot is missing or the arm is
unreachable, nothing here raises into the Echopia server; status just reports
"unavailable"/"offline" and the browser toggle stays disabled.

The websockets server is async; all blocking pymycobot socket I/O runs on this
controller's own worker thread. The server only calls the cheap, thread-safe
methods (handle_control / status_dict) from the event loop.
"""

from __future__ import annotations

import math
import socket
import threading
import time

try:
    from pymycobot import MyCobot280Socket
except Exception:  # pragma: no cover - pymycobot optional
    MyCobot280Socket = None

STATES = ("Rest", "Movement", "Sleep")

# Static poses (degrees, [J1..J6]) -- identical to the MYCOBOT tester.
POSES = {
    "Rest": [0, 0, 90, 0, 0, 0],
    "Sleep": [0, 0, 90, 0, 90, 0],
}
POSE_SPEED = 80

# "head bob" geometry (identical to the tester); period is dynamic (see set_speed).
BOB_D = 7              # amplitude (deg)
BOB_RISE_FRAC = 0.62   # >0.5 => slower rise / faster fall (bouncy)
BOB_DT = 0.06          # streaming step (s)
BOB_SPEED = 90
BOB_T_FAST = 0.7       # cycle seconds at full gait speed (walk == 1.0)
BOB_T_SLOW = 1.6       # cycle seconds at slowest gait (walk == 0.0)


class RobotArmController:
    def __init__(self, ip: str = "192.168.137.33", port: int = 9000) -> None:
        self.ip = ip
        self.port = port
        self.available = MyCobot280Socket is not None
        self.connected = False
        self.enabled = False
        self.status = "unavailable" if MyCobot280Socket is None else "offline"

        self._mc = None
        self._state = "Rest"
        self._bob_T = BOB_T_FAST
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probing = False

    # ----------------------------------------------------------- status / API
    def status_dict(self) -> dict:
        return {"status": self.status, "enabled": self.enabled, "addr": f"{self.ip}:{self.port}"}

    def handle_control(self, msg: dict) -> None:
        """Cheap, non-blocking. Called from the async server on each control msg."""
        if not isinstance(msg, dict):
            return
        # Prefer the dedicated arm posture (FoG/Fall -> Rest, lie -> Sleep, ...);
        # fall back to the neural state if the front-end didn't send arm_state.
        st = msg.get("arm_state", msg.get("state"))
        if st in STATES:
            self.set_state(st)
        if "walk" in msg:
            try:
                self.set_speed(float(msg["walk"]))
            except (TypeError, ValueError):
                pass
        if msg.get("arm_reconnect") and not self.connected:
            self.start_probe()
        if "arm_enabled" in msg and self.connected:
            want = bool(msg["arm_enabled"])
            if want and not self.enabled:
                self.enable()
            elif not want and self.enabled:
                self.disable()

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
        self._wake.set()

    def set_speed(self, walk: float) -> None:
        """walk in [0,1] (avatar gait speed) -> bob cycle period (inverse)."""
        w = max(0.0, min(1.0, walk))
        with self._lock:
            self._bob_T = BOB_T_FAST + (BOB_T_SLOW - BOB_T_FAST) * (1.0 - w)

    # ------------------------------------------------------------- connection
    def start_probe(self) -> None:
        """Probe in a background thread (never blocks the caller / event loop)."""
        if self._probing or not self.available:
            return
        self._probing = True
        self.status = "connecting"
        threading.Thread(target=self._probe, daemon=True).start()

    def _probe(self, timeout: float = 2.0) -> None:
        ok = False
        try:
            if self.available:
                try:
                    with socket.create_connection((self.ip, self.port), timeout=timeout):
                        pass
                    mc = MyCobot280Socket(self.ip, self.port)
                    time.sleep(0.3)
                    angles = mc.get_angles()
                    if isinstance(angles, (list, tuple)) and len(angles) == 6:
                        self._mc = mc
                        ok = True
                    else:
                        self._safe_close(mc)
                except Exception:
                    ok = False
        finally:
            self.connected = ok
            self.status = "connected" if ok else "offline"
            self._probing = False

    # --------------------------------------------------------- enable/disable
    def enable(self) -> bool:
        if not self.connected or self._mc is None:
            return False
        self.enabled = True
        self._stop.clear()
        try:
            self._mc.power_on()
        except Exception:
            pass
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
        self._wake.set()
        return True

    def disable(self) -> None:
        self.enabled = False
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        try:
            if self._mc is not None:
                self._mc.release_all_servos()
        except Exception:
            pass

    def shutdown(self) -> None:
        if self.enabled:
            self.disable()
        self._safe_close(self._mc)
        self._mc = None
        self.connected = False

    # -------------------------------------------------------------- internals
    @staticmethod
    def _safe_close(mc) -> None:
        if mc is None:
            return
        for attr in ("close", "disconnect"):
            try:
                getattr(mc, attr)()
                return
            except Exception:
                pass
        for attr in ("sock", "_sock", "client"):
            s = getattr(mc, attr, None)
            if s is not None:
                try:
                    s.close()
                    return
                except Exception:
                    pass

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                state = self._state
            if state == "Movement":
                self._run_bob()
            else:
                self._send_pose(POSES.get(state, POSES["Rest"]))
                self._wake.wait()

    def _send_pose(self, pose) -> None:
        try:
            self._mc.send_angles(list(pose), POSE_SPEED)
        except Exception:
            pass

    def _run_bob(self) -> None:
        # Pre-settle J5 to 0 before bobbing (coming from Sleep, J5=90, it would
        # otherwise keep rotating across the first bob steps). get_angles() returns
        # [] while moving, so poll until J5 is near 0 (or a 2 s timeout).
        try:
            self._mc.send_angles([0, 0, 90, 0, 0, 0], 100)
        except Exception:
            return
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self._stop.is_set() or self._wake.is_set():
                return
            try:
                a = self._mc.get_angles()
            except Exception:
                a = None
            if isinstance(a, (list, tuple)) and len(a) == 6 and abs(a[4]) <= 6.0:
                break
            time.sleep(0.08)

        while not self._stop.is_set() and not self._wake.is_set():
            with self._lock:
                period = self._bob_T
            steps = max(1, int(round(period / BOB_DT)))
            for i in range(steps):
                if self._stop.is_set() or self._wake.is_set():
                    return
                phase = i / steps
                if phase < BOB_RISE_FRAC:
                    tau = phase / BOB_RISE_FRAC
                    h = -math.cos(math.pi * tau)
                else:
                    tau = (phase - BOB_RISE_FRAC) / (1.0 - BOB_RISE_FRAC)
                    h = math.cos(math.pi * tau)
                j3 = 90.0 - BOB_D * h
                j4 = BOB_D * h
                try:
                    self._mc.send_angles([0, 0, j3, j4, 0, 0], BOB_SPEED)
                except Exception:
                    return
                time.sleep(BOB_DT)
