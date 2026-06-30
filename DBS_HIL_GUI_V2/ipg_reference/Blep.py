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

	_CHAR_UUIDS = {
		"new": {
			"order": '0000012d-0000-1000-8000-00805f9b34fb',
			"notify": '0000da7a-0000-1000-8000-00805f9b34fb',
		},
		"legacy": {
			"order": '0d7f012d-d8cd-4a29-a87d-bf5a94f63faf',
			"notify": '0d7fda7a-d8cd-4a29-a87d-bf5a94f63faf',
		},
	}
	_ORDER_CHAR_CANDIDATES = [v["order"] for v in _CHAR_UUIDS.values()]
	_NOTIF_CHAR_CANDIDATES = [v["notify"] for v in _CHAR_UUIDS.values()]

	def __init__(self, address: str):

		# Bleak interface
		self.address = address
		self.firmware = None
		self._client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
		self._async_thread = RunAsyncThread(asyncio.get_event_loop())
		self._recv_queue = asyncio.queues.Queue()

		# GATT characteristic UUIDs (resolved on connect)
		self._order_char_uuid = None
		self._notif_char_uuid = None

		# Packet and message processing
		self._packet_process_thread = LoopingThread(target=self._background_process_packets)
		self._current_sequence_number = 0
		self._received_data = bytearray()
		self.__listener_lock = threading.Lock()
		self.__message_listeners = list()  # callback list

		# Start all threads
		self._async_thread.start()
		self._packet_process_thread.start()

	@staticmethod
	def _resolve_char_uuid(candidates, services):
		"""Find the first candidate UUID that exists in the discovered services."""
		all_char_uuids = set()
		for service in services:
			for char in service.characteristics:
				all_char_uuids.add(char.uuid.lower())

		for uuid in candidates:
			if uuid.lower() in all_char_uuids:
				return uuid

		raise RuntimeError(
			f"None of the expected characteristics {candidates} "
			f"found in device services. Available: {sorted(all_char_uuids)}"
		)

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

	def _start_ble_notify(self):
		services = self._client.services
		if not services:
			raise RuntimeError("No GATT services discovered; device may have disconnected.")
		self._order_char_uuid = self._resolve_char_uuid(self._ORDER_CHAR_CANDIDATES, services)
		self._notif_char_uuid = self._resolve_char_uuid(self._NOTIF_CHAR_CANDIDATES, services)

		for fw_name, uuids in self._CHAR_UUIDS.items():
			if self._order_char_uuid == uuids["order"]:
				self.firmware = fw_name
				break

		print(f"Using order={self._order_char_uuid}, notify={self._notif_char_uuid} (firmware={self.firmware})")
		self._async_thread.run_coro(self._client.start_notify(self._notif_char_uuid, self._on_data_read))

	def _stop_ble_notify(self):
		self._async_thread.run_coro(self._client.stop_notify(self._notif_char_uuid))

	# endregion

	# region Send

	def _write(self, data: bytes):
		return self._async_thread.run_coro(self._client.write_gatt_char(self._order_char_uuid, data))

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
				break
			except Exception as e:
				print(f"Found exception while connecting: {e}")
				pass
		else:
			raise Exception("Timeout Reached"
							)
		self._start_ble_notify()

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


if __name__ == '__main__':

	message_received = False

	def on_message_received(message_type: BlepMessageType, channel: int, transaction_id: int, error_code:int, payload: bytes):
		lines = (
			f'Received message. Type {BlepMessageType(message_type).name} ({message_type:04X}), channel {channel}, ',
			f'transaction id {transaction_id}, data length {len(payload)}',
			f'error code 0x{error_code:04X}',
			f'Payload: {payload.hex().upper()}'
		)
		print('\n'.join(lines))
		global message_received
		message_received = True

	address = 'e9:82:1c:80:cd:09'

	with Blep(address) as blep:
		blep.connect()
		transaction_id = 0
		blep._add_message_listener(on_message_received)
		blep._send_message(BlepMessageType.REQUEST, 0, transaction_id, bytes.fromhex('03FC'))
		while not message_received:
			pass
