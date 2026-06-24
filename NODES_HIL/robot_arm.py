#!/usr/bin/env python3
"""Optional myCobot 280 arm control for NODES_HIL.

Mirrors the GUI behavioral state onto a physical myCobot 280, using the exact
motions from the MYCOBOT tester:

    Rest     -> Stand pose  [0, 0, 90, 0, 0, 0]   (held)
    Sleep    -> Sleep pose  [0, 0, 90, 0, 90, 0]   (held)
    Movement -> continuous bouncy "head bob" (J3/J4, head kept level)

The arm is reached over the network via the Pi's Server_280.py
(MyCobot280Socket, default 192.168.137.33:9000).

Design goals:
* **Optional / isolated** -- if pymycobot is missing or the arm is unreachable,
  nothing here raises into the rest of NODES_HIL. Probe returns False and the
  caller simply disables the feature.
* **Instant state transitions** -- a worker thread driven by a threading.Event:
  for Rest/Sleep it sends the pose once (non-blocking send_angles) then waits on
  the event; for Movement it streams the bob, checking the event each step. A
  state change sets the event, so the worker reacts within one ~60 ms step --
  far faster than the blocking sync_send_angles used in the initial test.
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


# Static poses (degrees, [J1..J6]) -- identical to the MYCOBOT tester.
POSES = {
    "Rest": [0, 0, 90, 0, 0, 0],
    "Sleep": [0, 0, 90, 0, 90, 0],
}
POSE_SPEED = 80  # non-blocking send_angles speed for static poses

# Movement "head bob" -- identical parameters to robot_tester_gui.py state_walk.
BOB_D = 7            # amplitude (deg)
BOB_T = 1.0          # cycle seconds
BOB_RISE_FRAC = 0.62  # >0.5 => slower rise / faster fall (bouncy)
BOB_DT = 0.06        # streaming step (s)
BOB_SPEED = 90


class RobotArmController:
    """Owns the optional arm connection and a worker thread that mirrors state.

    All public methods are safe to call from the GUI thread; the worker does the
    blocking socket I/O. Every robot call is guarded so a mid-session drop can't
    propagate into NODES_HIL.
    """

    def __init__(self, ip: str = "192.168.137.33", port: int = 9000) -> None:
        self.ip = ip
        self.port = port
        self.available = MyCobot280Socket is not None
        self.connected = False
        self.enabled = False

        self._mc = None
        self._state = "Rest"
        self._lock = threading.Lock()
        self._wake = threading.Event()   # state changed / wake the worker
        self._stop = threading.Event()   # stop the worker (disable / shutdown)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- connection
    def probe(self, timeout: float = 2.0) -> bool:
        """Quick, safe connectivity check. Opens a persistent connection on
        success (held for later enable). Returns True iff the arm responds."""
        self.connected = False
        if not self.available:
            return False
        # Fast TCP pre-check so we never hang on an absent/unreachable host.
        try:
            with socket.create_connection((self.ip, self.port), timeout=timeout):
                pass
        except Exception:
            return False
        try:
            mc = MyCobot280Socket(self.ip, self.port)
            time.sleep(0.3)
            angles = mc.get_angles()
            if isinstance(angles, (list, tuple)) and len(angles) == 6:
                self._mc = mc
                self.connected = True
                return True
            self._safe_close(mc)
        except Exception:
            pass
        return False

    # --------------------------------------------------------- enable/disable
    def enable(self, state: str) -> bool:
        """Power on and start mirroring ``state`` immediately."""
        if not self.connected or self._mc is None:
            return False
        with self._lock:
            self._state = state
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
        """Stop mirroring and RELEASE the servos immediately (per spec)."""
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

    def set_state(self, state: str) -> None:
        """Update the target state; the worker reacts on its next step."""
        with self._lock:
            self._state = state
        self._wake.set()

    def shutdown(self) -> None:
        """Disable (release) and close the connection -- for app exit."""
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
        # Fall back to closing the underlying socket if exposed.
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
                self._run_bob()          # loops until wake/stop
            else:
                self._send_pose(POSES.get(state, POSES["Rest"]))
                self._wake.wait()        # idle until state changes or stop

    def _send_pose(self, pose) -> None:
        try:
            self._mc.send_angles(list(pose), POSE_SPEED)
        except Exception:
            pass

    def _run_bob(self) -> None:
        # Pre-settle: move to the bob base (J5=0) at high speed and wait for J5 to
        # arrive BEFORE bobbing -- otherwise, coming from Sleep (J5=90), J5 keeps
        # rotating across the first bob steps. get_angles() returns [] while the
        # arm is moving, so we just poll until J5 is near 0 (or a 2 s timeout).
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

        steps = max(1, int(round(BOB_T / BOB_DT)))
        while not self._stop.is_set() and not self._wake.is_set():
            for i in range(steps):
                if self._stop.is_set() or self._wake.is_set():
                    return
                phase = i / steps
                if phase < BOB_RISE_FRAC:
                    tau = phase / BOB_RISE_FRAC               # slow rise
                    h = -math.cos(math.pi * tau)
                else:
                    tau = (phase - BOB_RISE_FRAC) / (1.0 - BOB_RISE_FRAC)  # faster fall
                    h = math.cos(math.pi * tau)
                j3 = 90.0 - BOB_D * h
                j4 = BOB_D * h                                # keeps J3 + J4 = 90 (head level)
                try:
                    self._mc.send_angles([0, 0, j3, j4, 0, 0], BOB_SPEED)
                except Exception:
                    return
                time.sleep(BOB_DT)
