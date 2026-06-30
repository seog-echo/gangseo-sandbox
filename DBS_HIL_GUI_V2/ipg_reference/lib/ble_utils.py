import asyncio
import sys
import threading
from bleak import BleakScanner

FW_NEW = "new"
FW_LEGACY = "legacy"

SIPG_SERVICE_UUIDS = [
	"0000fc88-0000-1000-8000-00805f9b34fb",
	"0d7fccc5-d8cd-4a29-a87d-bf5a94f63faf",
]

SERVICE_UUID_TO_FIRMWARE = {
	"0000fc88-0000-1000-8000-00805f9b34fb": FW_NEW,
	"0d7fccc5-d8cd-4a29-a87d-bf5a94f63faf": FW_LEGACY,
}


def get_device_address_by_name(device_name, timeout=10.0, ensure_loop=True,
                               service_uuids=None):
	"""
	Synchronous helper to resolve a BLE device address by name.
	Falls back to a service-UUID-filtered scan on macOS when the name
	isn't advertised (common CoreBluetooth quirk).
	"""
	if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
		raise RuntimeError("BLE scan must run on the main thread on macOS.")

	if service_uuids is None:
		service_uuids = SIPG_SERVICE_UUIDS

	loop = None
	if ensure_loop:
		try:
			loop = asyncio.get_event_loop()
		except RuntimeError:
			loop = asyncio.new_event_loop()
			asyncio.set_event_loop(loop)

	async def _find():
		# 1) Try matching by advertised name
		print(f"Scanning for device with name '{device_name}'...")
		target_device = await BleakScanner.find_device_by_filter(
			lambda d, ad: (d.name == device_name)
			or (getattr(ad, "local_name", None) == device_name),
			timeout=timeout,
		)

		if target_device:
			print(f"Found device: {target_device.name}")
			print(f"UUID (Address): {target_device.address}")
			return str(target_device.address)

		# 2) Full unfiltered scan
		print(f"Device '{device_name}' not found by name.")
		print("Performing full scan...")
		devices = await BleakScanner.discover(timeout=timeout)
		for d in devices:
			if d.name == device_name:
				print(f"Found {d.name} on full scan")
				print(f"UUID (Address): {d.address}")
				return str(d.address)

		named = [d for d in devices if d.name]
		if named:
			print("Named devices found:")
			for d in named:
				print(f"  - {d.name} ({d.address})")

		# 3) macOS fallback: scan with service UUID filter
		if service_uuids:
			print(f"Trying service-UUID-filtered scan {service_uuids}...")
			matches = []

			def _on_detect(device, adv_data):
				matches.append((device, adv_data))

			async with BleakScanner(
				detection_callback=_on_detect,
				service_uuids=service_uuids,
			):
				await asyncio.sleep(timeout)

			if len(matches) == 1:
				dev, ad = matches[0]
				name = dev.name or getattr(ad, "local_name", None) or "(no name)"
				print(f"Found 1 device via service UUID: {name}")
				print(f"UUID (Address): {dev.address}")
				return str(dev.address)
			elif len(matches) > 1:
				print(f"Found {len(matches)} devices with matching service UUID:")
				for dev, ad in matches:
					name = dev.name or getattr(ad, "local_name", None) or "(no name)"
					rssi = ad.rssi if ad else "N/A"
					print(f"  - {name} ({dev.address}) RSSI: {rssi}")
				print("Cannot auto-select. Pass the address directly.")
			else:
				print("No devices found with service UUID filter either.")

		return None

	if loop is None:
		loop = asyncio.new_event_loop()
		try:
			return loop.run_until_complete(_find())
		finally:
			loop.close()

	return loop.run_until_complete(_find())
