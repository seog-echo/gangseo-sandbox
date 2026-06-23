"""Diagnose MyCobot 280 SERIAL communication.

This is for the 280 Pi when run ON the Pi (port /dev/ttyAMA0), or any
serial-attached MyCobot. Opening a serial port "succeeds" even if nothing
is listening at the chosen baud rate, so this checks REAL two-way
communication: it reads firmware version and joint angles, which only
return sane values when the baud rate and wiring are correct.

Run (on the Pi):
    python3 diagnose.py
    python3 diagnose.py /dev/ttyAMA0          # explicit port

NOTE: For controlling the 280 Pi FROM a separate laptop over the network,
this serial test does not apply -- use the socket one-liner in README.md.
"""

import sys
import time

from pymycobot import MyCobot280

# Defaults target the 280 Pi's onboard GPIO serial.
PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyAMA0"
BAUDS = [1000000, 115200]


def probe(port, baud):
    print(f"\n=== Trying {port} @ {baud} ===")
    try:
        mc = MyCobot280(port, baud)
    except Exception as e:
        print(f"  open failed: {e}")
        return None

    time.sleep(0.5)
    ok = False
    try:
        ver = mc.get_system_version()
        print(f"  get_system_version() -> {ver}")
        # A valid version is a positive number; None/0/-1 means no real reply.
        if isinstance(ver, (int, float)) and ver > 0:
            ok = True
    except Exception as e:
        print(f"  get_system_version() error: {e}")

    try:
        angles = mc.get_angles()
        print(f"  get_angles()          -> {angles}")
        # Valid only when it's an actual list of 6 joint values. A bare -1
        # (int) is the failure sentinel, NOT a successful read.
        if isinstance(angles, (list, tuple)) and len(angles) == 6:
            ok = True
    except Exception as e:
        print(f"  get_angles() error: {e}")

    try:
        print(f"  is_power_on()         -> {mc.is_power_on()}")
        print(f"  is_all_servo_enable() -> {mc.is_all_servo_enable()}")
        print(f"  get_error_information -> {mc.get_error_information()}")
    except Exception as e:
        print(f"  status read error: {e}")

    if ok:
        print(f"  ==> COMMUNICATION OK at baud {baud}")
    else:
        print(f"  ==> No valid reply at baud {baud} (likely wrong baud)")
    return mc if ok else None


def main():
    print(f"Probing robot on port: {PORT}")
    working = None
    working_baud = None
    for baud in BAUDS:
        mc = probe(PORT, baud)
        if mc is not None:
            working, working_baud = mc, baud
            break

    if not working:
        print("\nNo baud rate produced a valid reply. Check:")
        print("  - Are you running this ON the Pi? (the 280 Pi is not a USB serial device)")
        print("  - Is the robot's power adapter plugged in and switched on?")
        print("  - Is the Atom firmware OK on the top module?")
        print("  - Is another process holding /dev/ttyAMA0?")
        print("  - Confirm the port exists: `ls -l /dev/ttyAMA0`")
        return

    print(f"\nGood comms at baud {working_baud}. Attempting a small, safe move...")
    working.power_on()
    time.sleep(1)
    print("  powered on; sending angles [0, 0, 0, 0, 0, 0] at speed 30")
    working.send_angles([0, 0, 0, 0, 0, 0], 30)
    time.sleep(3)
    print("  angles after move:", working.get_angles())
    print(f"\nDONE. Use baud {working_baud} in the GUI's Baud field.")


if __name__ == "__main__":
    main()
