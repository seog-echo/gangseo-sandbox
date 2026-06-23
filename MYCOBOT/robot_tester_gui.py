"""Simple Tkinter GUI to test controlling a MyCobot 280.

Two connection modes:
  - Serial : run this GUI ON the robot's Pi  (port /dev/ttyAMA0, baud 1000000)
  - Network: run this GUI on your Mac/laptop; the robot's Pi must be running
             Server_280.py. Connects via MyCobot280Socket(ip, 9000).

Run:
    # on the Pi (serial mode):
    python3 robot_tester_gui.py
    # on a Mac (network mode), using the Tk-capable venv:
    .venv-gui/bin/python robot_tester_gui.py
"""

import tkinter as tk
from tkinter import messagebox
import threading
import time

from pymycobot import MyCobot280, MyCobot280Socket


class RobotTesterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MyCobot 280 - Motion Tester")
        self.root.geometry("420x430")

        self.mc = None
        self.is_connected = False
        # Set when the user presses STOP so a running movement loop can bail out.
        self.stop_requested = threading.Event()

        self.create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        # --- Connection Setup ---
        conn_frame = tk.LabelFrame(self.root, text="Connection Setup", padx=10, pady=10)
        conn_frame.pack(fill="x", padx=10, pady=5)

        # Mode toggle: Serial (run on the Pi) vs Network (run on the Mac).
        self.mode_var = tk.StringVar(value="serial")
        mode_row = tk.Frame(conn_frame)
        mode_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        tk.Label(mode_row, text="Mode:").pack(side="left")
        tk.Radiobutton(mode_row, text="Serial (on Pi)", variable=self.mode_var,
                       value="serial", command=self.update_mode_fields).pack(side="left")
        tk.Radiobutton(mode_row, text="Network (from Mac)", variable=self.mode_var,
                       value="network", command=self.update_mode_fields).pack(side="left")

        # Serial fields: port + baud
        self.lbl_port = tk.Label(conn_frame, text="Port:")
        self.port_entry = tk.Entry(conn_frame, width=24)
        # 280 Pi: arm is wired to the onboard Pi over GPIO serial (= PI_PORT).
        self.port_entry.insert(0, "/dev/ttyAMA0")

        self.lbl_baud = tk.Label(conn_frame, text="Baud:")
        self.baud_entry = tk.Entry(conn_frame, width=24)
        # 1000000 for the 280 Pi GPIO serial (= PI_BAUD). M5 USB version uses 115200.
        self.baud_entry.insert(0, "1000000")

        # Network fields: Pi IP + TCP port (Server_280.py listens on 9000)
        self.lbl_ip = tk.Label(conn_frame, text="Pi IP:")
        self.ip_entry = tk.Entry(conn_frame, width=24)
        self.ip_entry.insert(0, "192.168.1.42")

        self.lbl_netport = tk.Label(conn_frame, text="TCP Port:")
        self.netport_entry = tk.Entry(conn_frame, width=24)
        self.netport_entry.insert(0, "9000")

        self.btn_connect = tk.Button(conn_frame, text="Connect", command=self.connect_robot, bg="lightgreen")
        self.btn_connect.grid(row=1, column=2, rowspan=2, padx=5, sticky="ns")

        self.update_mode_fields()  # lay out fields for the default (serial) mode

        # --- Robot Controls ---
        ctrl_frame = tk.LabelFrame(self.root, text="Test States", padx=10, pady=10)
        ctrl_frame.pack(fill="x", padx=10, pady=5)

        self.btn_stand = tk.Button(ctrl_frame, text="1. Stand (Resting)", width=20,
                                   command=lambda: self.run_in_thread(self.state_stand))
        self.btn_stand.pack(pady=5)

        self.btn_sleep = tk.Button(ctrl_frame, text="2. Sleep (Flat)", width=20,
                                   command=lambda: self.run_in_thread(self.state_sleep))
        self.btn_sleep.pack(pady=5)

        self.btn_walk = tk.Button(ctrl_frame, text="3. Walk (Bobbing)", width=20,
                                  command=lambda: self.run_in_thread(self.state_walk))
        self.btn_walk.pack(pady=5)

        # --- Safety & Power ---
        safety_frame = tk.Frame(self.root)
        safety_frame.pack(fill="x", padx=10, pady=10)

        self.btn_stop = tk.Button(safety_frame, text="STOP", bg="red", fg="white",
                                  font=("Arial", 10, "bold"), command=self.stop_robot)
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=5)

        self.btn_release = tk.Button(safety_frame, text="Release Servos", command=self.release_servos)
        self.btn_release.pack(side="right", expand=True, fill="x", padx=5)

        # --- Status Bar ---
        self.status_var = tk.StringVar()
        self.status_var.set("Status: Disconnected")
        status_label = tk.Label(self.root, textvariable=self.status_var, bd=1, relief="sunken", anchor="w")
        status_label.pack(side="bottom", fill="x")

    # --- Thread-safe GUI helpers ---
    # Tkinter widgets must only be touched from the main thread, so worker
    # threads schedule updates with root.after instead of calling directly.
    def set_status(self, text):
        self.root.after(0, lambda: self.status_var.set(text))

    def toggle_buttons(self, state):
        def _apply():
            self.btn_stand.config(state=state)
            self.btn_sleep.config(state=state)
            self.btn_walk.config(state=state)
        self.root.after(0, _apply)

    def update_mode_fields(self):
        """Show serial fields or network fields depending on the selected mode."""
        # Clear both field sets from the grid, then re-place the active one.
        for w in (self.lbl_port, self.port_entry, self.lbl_baud, self.baud_entry,
                  self.lbl_ip, self.ip_entry, self.lbl_netport, self.netport_entry):
            w.grid_forget()

        if self.mode_var.get() == "serial":
            self.lbl_port.grid(row=1, column=0, sticky="w")
            self.port_entry.grid(row=1, column=1, padx=5)
            self.lbl_baud.grid(row=2, column=0, sticky="w")
            self.baud_entry.grid(row=2, column=1, padx=5)
        else:
            self.lbl_ip.grid(row=1, column=0, sticky="w")
            self.ip_entry.grid(row=1, column=1, padx=5)
            self.lbl_netport.grid(row=2, column=0, sticky="w")
            self.netport_entry.grid(row=2, column=1, padx=5)

    # --- Hardware Connection ---
    def connect_robot(self):
        mode = self.mode_var.get()
        try:
            if mode == "serial":
                port = self.port_entry.get().strip()
                try:
                    baud = int(self.baud_entry.get().strip())
                except ValueError:
                    messagebox.showerror("Connection Error", "Baud rate must be a number (e.g. 1000000).")
                    return
                target = f"{port} @ {baud}"
                self.mc = MyCobot280(port, baud)
            else:
                ip = self.ip_entry.get().strip()
                try:
                    netport = int(self.netport_entry.get().strip())
                except ValueError:
                    messagebox.showerror("Connection Error", "TCP port must be a number (e.g. 9000).")
                    return
                target = f"{ip}:{netport}"
                self.mc = MyCobot280Socket(ip, netport)

            self.mc.power_on()
            time.sleep(1)
            self.is_connected = True
            self.status_var.set(f"Status: Connected ({mode}) — {target}")
            self.btn_connect.config(state="disabled", text="Connected")
        except Exception as e:
            self.mc = None
            messagebox.showerror("Connection Error", f"Could not connect.\nError: {e}")

    # --- Threading Wrapper ---
    def run_in_thread(self, target_function):
        if not self.is_connected:
            messagebox.showwarning("Not Connected", "Please connect to the robot first.")
            return

        # Clear any previous stop request and disable buttons while moving.
        self.stop_requested.clear()
        self.toggle_buttons("disabled")

        thread = threading.Thread(target=self._thread_runner, args=(target_function,), daemon=True)
        thread.start()

    def _thread_runner(self, target_function):
        try:
            target_function()
        except Exception as e:
            print(f"Error during execution: {e}")
            self.set_status("Status: Error during movement!")
        finally:
            self.toggle_buttons("normal")

    # --- Robot States ---
    def state_stand(self):
        self.set_status("Status: Moving to Stand State...")
        self.mc.sync_send_angles([0, 90, 0, 0, 90, 0], 40)
        self.set_status("Status: Standing State Reached")

    def state_sleep(self):
        self.set_status("Status: Moving to Sleep State...")
        self.mc.sync_send_angles([0, 90, 0, 0, 0, 0], 40)
        self.set_status("Status: Sleep State Reached")

    def state_walk(self):
        self.set_status("Status: Initializing Walk...")
        # Get to baseline first
        self.mc.sync_send_angles([0, 90, 0, 0, 90, 0], 40)
        time.sleep(1)

        base_coords = self.mc.get_coords()
        if not base_coords or len(base_coords) < 6:
            self.set_status("Status: Error reading coordinates")
            return

        base_x, base_y, base_z, rx, ry, rz = base_coords
        z_bounce, y_sway, speed = 20, 10, 60
        steps = 5

        self.set_status("Status: Walking...")
        for _ in range(steps):
            # Bail out immediately if the user hit STOP.
            if self.stop_requested.is_set():
                break

            self.mc.sync_send_coords([base_x, base_y + y_sway, base_z + z_bounce, rx, ry, rz], speed, 1)
            self.mc.sync_send_coords([base_x, base_y, base_z, rx, ry, rz], speed, 1)
            self.mc.sync_send_coords([base_x, base_y - y_sway, base_z + z_bounce, rx, ry, rz], speed, 1)
            self.mc.sync_send_coords([base_x, base_y, base_z, rx, ry, rz], speed, 1)

        if self.stop_requested.is_set():
            self.set_status("Status: Walk Stopped.")
        else:
            self.set_status("Status: Walk Finished.")

    # --- Safety Features ---
    def stop_robot(self):
        # Signal any running movement loop to stop, then halt current motion.
        self.stop_requested.set()
        if self.mc:
            self.mc.stop()
            self.set_status("Status: STOPPED")

    def release_servos(self):
        if self.mc:
            self.mc.release_all_servos()
            self.set_status("Status: Servos Released (Freely moving)")

    def on_close(self):
        # Try to leave the robot in a safe, powered-down state.
        if self.mc:
            try:
                self.mc.stop()
                self.mc.release_all_servos()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = RobotTesterGUI(root)
    root.mainloop()
