import threading
import asyncio
from bleak import BleakClient
from typing import Coroutine, Optional, Callable
import ctypes
import crcmod
import enum
import time

crc32 = crcmod.predefined.mkCrcFun('crc-32')

# region Auxiliary Classes


class RunAsyncThread(threading.Thread):
	"""
	Runs the event loop in a background thread, to avoid using async in the main thread

	The run_coro method allows the main thread to execute coroutines in this thread's event loop,
	blocking until they're complete, in a thread-safe manner.
	"""

	def __init__(self, loop: asyncio.AbstractEventLoop):
		super(RunAsyncThread, self).__init__()
		self.loop = loop
		self.finish_event = asyncio.Event()

	def run(self) -> None:
		asyncio.set_event_loop(self.loop)
		self.loop.run_until_complete(self.finish_event.wait())

	async def _set_finish_event(self):
		self.finish_event.set()

	def join(self, timeout: Optional[float] = None) -> None:
		self.run_coro(self._set_finish_event())
		super(RunAsyncThread, self).join(timeout)

	def run_coro(self, coroutine: Coroutine):
		return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()


class LoopingThread(threading.Thread):

	def __init__(self, target: Callable, args: tuple = tuple(), **kwargs):
		super(LoopingThread, self).__init__(target=target, args=args, **kwargs)
		self.finish = False
		self._target = target

	def run(self) -> None:
		while not self.finish:
			if self._target:
				self._target()

	def join(self, timeout: Optional[float] = None) -> None:
		self.finish = True
		super(LoopingThread, self).join(timeout)

# endregion

# region Structures


INVALID_TRANSACTION_ID = 0xFFFF
INVALID_CHANNEL = 0xFF


class BlepMessageType(enum.IntEnum):
	REQUEST 		= 0x00
	ANSWER 			= 0x06
	NOTIFICATION 	= 0x07
	ERROR 			= 0x0E
	NACK 			= 0x0F
	BG_PARTIAL 		= 0xB5
	BG_COMPLETE 	= 0xB6
	BG_ERROR 		= 0xBE
	BG_NACK 		= 0xBF


class BlepHeader(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('first_packet', ctypes.c_uint8, 1),
		('last_packet', ctypes.c_uint8, 1),
		('error', ctypes.c_uint8, 1),
		('reserved', ctypes.c_uint8, 1),
		('sequence', ctypes.c_uint8, 4),
	]


class BlepTrailer(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('type', ctypes.c_uint8),
		('reserved1', ctypes.c_uint8),
		('channel', ctypes.c_uint8),
		('reserved2', ctypes.c_uint8),
		('transaction_id', ctypes.c_uint16),
		('error_code', ctypes.c_uint16),
		('reserved', ctypes.c_uint8 * 2),
	]

# endregion


class Blep:
	# Original UUIDs
	_service_uuid = '0d7fccc5-d8cd-4a29-a87d-bf5a94f63faf'
	_order_char_uuid = '0d7f012d-d8cd-4a29-a87d-bf5a94f63faf'
	_notif_char_uuid = '0d7fda7a-d8cd-4a29-a87d-bf5a94f63faf'

	# New UUIDs
	_new_service_uuid = '0000ccc5-0000-1000-8000-00805F9B34FB'
	_new_order_char_uuid = '0000012d-0000-1000-8000-00805F9B34FB'
	_new_notif_char_uuid = '0000da7a-0000-1000-8000-00805F9B34FB'

	def __init__(self, address: str):

		# Bleak interface
		self.address = address
		self._client = BleakClient(
			self.address,
			disconnected_callback=self._on_disconnect,
			services=[self._service_uuid, self._new_service_uuid],
			pair=True,
			winrt={"use_cached_services": False},
		)
		self._active_order_char_uuid = self._order_char_uuid
		self._active_notif_char_uuid = self._notif_char_uuid
		self._async_thread = RunAsyncThread(asyncio.get_event_loop())
		self._recv_queue = asyncio.queues.Queue()

		# Packet and message processing
		self._packet_process_thread = LoopingThread(target=self._background_process_packets)
		self._current_sequence_number = 0
		self._received_data = bytearray()
		self.__listener_lock = threading.Lock()
		self.__message_listeners = list()  # callback list

		# Start all threads
		self._async_thread.start()
		self._packet_process_thread.start()

	# region Private Functions

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.disconnect()
		self._packet_process_thread.join()
		self._async_thread.join()

	# region BLE

	def _on_disconnect(self, client):
		print("Disconnected!")
		# raise Exception("Disconnected")

	def _start_ble_notify(self, retry_count: int = 0, raise_on_failure: bool = True):
		"""Start BLE notify subscription. 
		
		Args:
			retry_count: For retries after session role setup
			raise_on_failure: If False, warn but don't raise if characteristics not found (allow deferred setup)
		"""
		# Compatibility patch for bleak versions:
		# - bleak 0.20-0.22: has get_services() method
		# - bleak 2.x: removed get_services(), uses client.services property
		if hasattr(self._client, 'get_services'):
			# Old bleak API (0.20-0.22)
			srv = self._async_thread.run_coro(self._client.get_services(use_cached=False))
		elif retry_count > 0:
			# For bleak 2.x, clear cached services by resetting the descriptor attribute
			# This forces a fresh discovery on next access
			if hasattr(self._client, '_services'):
				print("Clearing cached BLE services for fresh discovery...")
				self._client._services = None
		
		# Try UUID variants in order: legacy, new 16-bit alias, then auto-discover.
		uuid_variants = [
			(self._notif_char_uuid, self._order_char_uuid, "legacy 128-bit"),
			(self._new_notif_char_uuid, self._new_order_char_uuid, "new 16-bit alias"),
		]
		
		last_error = None
		for notif_uuid, order_uuid, variant_name in uuid_variants:
			try:
				self._async_thread.run_coro(self._client.start_notify(notif_uuid, self._on_data_read))
				self._active_notif_char_uuid = notif_uuid
				self._active_order_char_uuid = order_uuid
				print(f"Successfully using {variant_name} UUIDs (notify={notif_uuid})")
				return
			except Exception as exc:
				last_error = exc
				print(f"UUID variant {variant_name} failed: {exc}")
				continue
		
		# Known UUIDs failed; try auto-discovery
		print("Known UUIDs not found. Attempting auto-discovery of characteristics...")
		try:
			self._auto_discover_characteristics()
			return
		except Exception as disc_error:
			print(f"Auto-discovery failed: {disc_error}")
			if raise_on_failure:
				raise last_error if last_error else disc_error
			else:
				print("(Deferring characteristic setup until after session role initialization)")

	def retry_characteristic_discovery(self):
		"""For devices that expose characteristics AFTER session role is set.
		Call this method after o_set_session_role() completes to refresh service cache and retry.
		This version WILL raise if it fails since at this point device should be ready."""
		print("Retrying characteristic discovery after session role setup...")
		try:
			self._start_ble_notify(retry_count=1, raise_on_failure=True)
		except Exception as e:
			print(f"Retry discovery failed: {e}")
			raise


	def _auto_discover_characteristics(self) -> None:
		"""Auto-discover write and notify characteristics when known UUIDs don't exist."""
		print("Starting auto-discovery of BLE characteristics...")
		
		# In bleak 2.x, after connection, services are available via client.services
		# We need to ensure services are loaded
		try:
			if hasattr(self._client, 'services'):
				services = self._client.services
				print(f"Services collection obtained")
			else:
				print("WARNING: client.services not available, attempting to get_services()")
				services = self._async_thread.run_coro(self._client.get_services())
				print(f"Services obtained via get_services()")
		except Exception as e:
			print(f"Error accessing services: {e}")
			raise RuntimeError(f"Cannot discover characteristics: {e}")
		
		notify_char = None
		write_char = None
		
		# Prefer scanning the known IPG service first, then custom services, then everything else.
		services_list = list(services)
		known_service_uuids = {self._service_uuid.lower(), self._new_service_uuid.lower()}
		standard_service_uuids = {
			'00001800-0000-1000-8000-00805f9b34fb',
			'00001801-0000-1000-8000-00805f9b34fb',
		}
		known_services = [s for s in services_list if str(s.uuid).lower() in known_service_uuids]
		custom_services = [s for s in services_list if str(s.uuid).lower() not in standard_service_uuids and str(s.uuid).lower() not in known_service_uuids]
		fallback_services = [s for s in services_list if str(s.uuid).lower() in standard_service_uuids]
		scan_services = known_services + custom_services + fallback_services
		
		# Scan services for notify/indicate and write characteristics.
		service_count = 0
		for service in scan_services:
			service_count += 1
			print(f"Service {service_count}: {service.uuid}")
			for char in service.characteristics:
				char_uuid = str(char.uuid)
				char_props_raw = char.properties if hasattr(char, 'properties') else []
				char_props = {str(prop).lower() for prop in char_props_raw}
				print(f"  Characteristic: {char_uuid}, properties: {list(char_props)}")
				
				if notify_char is None and ('notify' in char_props or 'indicate' in char_props):
					notify_char = char_uuid
					print(f"  -> Selected as notify characteristic")
				
				if write_char is None and 'write-without-response' in char_props:
					write_char = char_uuid
					print(f"  -> Selected as write characteristic (write-without-response)")
				elif write_char is None and 'write' in char_props:
					write_char = char_uuid
					print(f"  -> Selected as write characteristic (write)")
				
				if notify_char and write_char:
					break
			if notify_char and write_char:
				break
		
		print(f"Scanned {service_count} services. Found notify={notify_char}, write={write_char}")
		
		if not notify_char or not write_char:
			raise RuntimeError(f"Could not auto-discover: notify={notify_char}, write={write_char}")
		
		# Start notify and set active UUIDs
		try:
			self._async_thread.run_coro(self._client.start_notify(notify_char, self._on_data_read))
			self._active_notif_char_uuid = notify_char
			self._active_order_char_uuid = write_char
			print(f"Auto-discovery successful: notify={notify_char}, write={write_char}")
		except Exception as e:
			print(f"Error starting notify on discovered characteristic: {e}")
			raise

	def _stop_ble_notify(self):
		self._async_thread.run_coro(self._client.stop_notify(self._active_notif_char_uuid))

	# endregion

	# region Send

	def _write(self, data: bytes):
		return self._async_thread.run_coro(self._client.write_gatt_char(self._active_order_char_uuid, data))

	def _fragment_and_send(self, data: bytes):
		max_packet_size = self._client.mtu_size - 3  # no se por que aun
		max_payload_size = max_packet_size - ctypes.sizeof(BlepHeader)
		number_packets = len(data) // max_payload_size
		last_payload_size = len(data) % max_payload_size
		if last_payload_size:
			number_packets += 1
		else:
			last_payload_size = max_payload_size
		# Send fragmented data
		for i in range(number_packets):
			is_last_packet = (i == number_packets - 1)
			is_first_packet = (i == 0)
			header = BlepHeader(first_packet=is_first_packet, last_packet=is_last_packet, error=False, sequence=i)
			current_payload_size = last_payload_size if is_last_packet else max_payload_size
			start_idx = i * max_payload_size
			end_idx = start_idx + current_payload_size
			current_payload = data[start_idx:end_idx]
			self._write(bytes(header) + current_payload)

	def _send_message(self, message_type: BlepMessageType, channel: int, transaction_id: int, payload: bytes):
		metadata = BlepTrailer(type=message_type.value, channel=channel, transaction_id=transaction_id)
		data = payload + bytes(metadata)
		crc = crc32(data)
		data = data + crc.to_bytes(4, byteorder='little')
		self._fragment_and_send(data)

	# endregion

	# region Reception
	async def _on_data_read(self, sender: int, data: bytearray):
		# print(f'Notification {sender}: {data}')
		# print(f'Notification thread {threading.get_ident()}')
		await self._recv_queue.put(data)

	async def _is_recv_queue_empty(self):
		return self._recv_queue.qsize() == 0

	def _background_process_packets(self):

		if self._async_thread.run_coro(self._is_recv_queue_empty()): #todo event en vez de pollig?
			return

		data: bytearray = self._async_thread.run_coro(self._recv_queue.get())

		# print(f"BLEP Packet: {data.hex()}")

		if len(data) < ctypes.sizeof(BlepHeader):
			print('_background_process_packets - error: packet received too short for BLEP header')
			return

		header = BlepHeader.from_buffer_copy(data)

		# print(f"BLEP Header: First={header.first_packet}, Last={header.last_packet}. Data={data[1:].hex().upper()}")

		if self._received_data:
			# reception has already started
			if header.first_packet:
				print('_background_process_packets - warning: previous packet discarded')
				self._received_data.clear()
			else:
				# check sequence
				expected_seq = (self._current_sequence_number + 1) % 16
				if header.sequence != expected_seq:
					print(f'_background_process_packets - error: wrong sequence number, expected {expected_seq} and got {header.sequence}.')
					return
		else:
			# No data received yet, check first packet received
			if not header.first_packet:
				print('_background_process_packets - error: first packet missing!')
				return

		# Header checks passed
		self._current_sequence_number = header.sequence
		self._received_data.extend(data[1:])

		# If it's the last packet, finish reception
		if header.last_packet:
			message = self._received_data
			self._received_data = bytearray()
			self._on_message_complete(message)

	def _on_message_complete(self, data: bytearray):

		if len(data) < ctypes.sizeof(BlepTrailer) + 4:
			print(f'_on_message_complete - error: message too short for trailer metadata and crc ({len(data)}).')
			return

		crc_offset = -4
		metadata_offset = crc_offset - ctypes.sizeof(BlepTrailer)
		metadata = BlepTrailer.from_buffer_copy(data[metadata_offset:])
		crc_recv = int.from_bytes(data[crc_offset:], byteorder='little')
		crc = crc32(data[:-4])

		if crc != crc_recv:
			print(f'_on_message_complete - error: bad message crc, received {crc_recv} expected {crc}.')
			return

		self._dispatch_message(metadata, data[:metadata_offset])

	def _dispatch_message(self, metadata: BlepTrailer, payload: bytes):
		try:
			message_type = BlepMessageType(metadata.type)
		except ValueError:
			print(f'_dispatch_message - error: unknown BLEP message type {metadata.type}')
			return
		self.__listener_lock.acquire()
		for callback in self.__message_listeners:
			try:
				callback(message_type, metadata.channel, metadata.transaction_id, metadata.error_code, payload)
			except Exception as e:
				print(	f'_dispatch_message - error: exception in callback {callback}'
						f' for channel {metadata.channel} trxid {metadata.transaction_id}. Error was {str(e)}')
		self.__listener_lock.release()

	def _add_message_listener(self, callback: Callable[[BlepMessageType, int, int, int, bytes], None]):
		"""
		:param callback: should be callback(message_type: BlepMessageType, channel: int, transaction_id: int, error_code:int, payload: bytes)
		:return:
		"""
		self.__listener_lock.acquire()
		self.__message_listeners.append(callback)
		self.__listener_lock.release()

	def _remove_message_listener(self, callback: Callable[[BlepMessageType, int, int, int, bytes], None]):
		self.__listener_lock.acquire()
		self.__message_listeners.remove(callback)
		self.__listener_lock.release()

	# endregion

	# endregion

	# region Exported BLE functions

	def connect(self, timeout=60):
		t0 = time.time()
		while time.time() - t0 < timeout:
			try:
				self._async_thread.run_coro(self._client.connect(timeout=timeout))
				if hasattr(self._client, 'pair'):
					try:
						pair_result = self._async_thread.run_coro(self._client.pair())
						print(f"Pairing attempt result: {pair_result}")
					except Exception as pair_error:
						print(f"Pairing attempt failed or not required: {pair_error}")
				break
			except Exception as e:
				print(f"Found exception while connecting: {e}")
				pass
		else:
			raise Exception("Timeout Reached"
							)
		# Initial notify setup with soft failure - device may need session role before exposing characteristics
		self._start_ble_notify(raise_on_failure=False)

	def disconnect(self):
		self._async_thread.run_coro(self._client.disconnect())

	def is_connected(self):
		return self._client.is_connected

	def reconnect(self):
		if self.is_connected():
			self.disconnect()
		self.connect()

	# endregion

	def __test(self):
		pass

