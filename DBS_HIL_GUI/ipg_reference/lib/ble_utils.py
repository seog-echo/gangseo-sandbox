import asyncio
import sys
import threading
from bleak import BleakScanner


def get_device_address_by_name(device_name, timeout=10.0, ensure_loop=True):
	"""
	Synchronous helper to resolve a BLE device address by name.
	If ensure_loop is True, ensures a main-thread event loop exists on macOS.
	"""
	if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
		raise RuntimeError("BLE scan must run on the main thread on macOS.")

	loop = None
	if ensure_loop:
		try:
			loop = asyncio.get_event_loop()
		except RuntimeError:
			loop = asyncio.new_event_loop()
			asyncio.set_event_loop(loop)

	async def _find():
		print(f"Scanning for device with name '{device_name}'...")
		target_device = await BleakScanner.find_device_by_filter(
			lambda d, ad: (d.name == device_name)
			or (getattr(ad, "local_name", None) == device_name),
			timeout=timeout,
		)

		if not target_device:
			print(f"Device '{device_name}' not found.")
			print("Performing full scan, devices list:")
			devices = await BleakScanner.discover(timeout=timeout)
			for d in devices:
				if d.name == device_name:
					target_device = d
					break
			for d in devices:
				if d.name:
					print(f"- {d.name} ({d.address})")
			if target_device:
				print(f"Found {target_device.name} this time")
				print(f"UUID (Address): {target_device.address}")
				return str(target_device.address)
			return None

		print(f"Found device: {target_device.name}")
		print(f"UUID (Address): {target_device.address}")
		return str(target_device.address)

	if loop is None:
		loop = asyncio.new_event_loop()
		try:
			return loop.run_until_complete(_find())
		finally:
			loop.close()

	return loop.run_until_complete(_find())
