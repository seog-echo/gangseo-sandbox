import Blep
from typing import Union, Type, Callable, Hashable, List
import ctypes
import threading
from collections import defaultdict
import enum
import random


# region Aux


def str_struct(obj, indent=0):
	strings = []
	for fields in obj._fields_:
		fname, ftype = fields[:2]
		fvalue = getattr(obj, fname)
		if isinstance(fvalue, ctypes.Array):
			# This means that arrays of structs are NOT pretty printed.
			strings.append("{}{} = [{}]".format(' ' * indent, fname, ", ".join(str(x) for x in fvalue)))
		elif hasattr(ftype, '_fields_'):
			strings.append(fname)
			strings.append(str_struct(fvalue, indent + 4))
		else:
			strings.append(('{}{} = {}'.format(' ' * indent, fname, fvalue)))
	return "\n".join(strings)


def eq_struct(obj1, obj2):
	for fields in obj1._fields_:
		field_name, field_type = fields[:2]  # ignore bit-fields...
		field_value1 = getattr(obj1, field_name)
		field_value2 = getattr(obj2, field_name)
		if isinstance(field_value1, ctypes.Array):
			# Compare all entries
			if not all(eq_struct(entry1, entry2) for entry1, entry2 in zip(field_value1, field_value2)):
				return False
		elif isinstance(field_value1, ctypes.Structure):
			# Compare each field of the nested struct
			if not eq_struct(field_value1, field_value2):
				return False
		else:
			if not field_value1 == field_value2:
				return False

	return True

# endregion

# region Exceptions


class AlcpException(Exception):
	def __init__(self, message, payload=None, *args):
		self.payload = payload
		self.args = args
		super(AlcpException, self).__init__(message)


class AlcpError(AlcpException):
	def __init__(self, error_code: int, error_name: str, payload: bytes, message: str = None, override_message: bool = False, *args):
		self.error_code = error_code
		self.error_name = error_name
		message_base = f'IPG answered error 0x{error_code:04X} ({error_name}).'
		if message:
			if not override_message:
				message = message_base + message
		else:
			message = message_base
		super(AlcpError, self).__init__(message, payload, *args)


class AlcpNack(AlcpException):
	def __init__(self, nack_code: int, nack_name: str, payload: bytes, message: str = None, override_message: bool = False, *args):
		self.nack_code = nack_code
		self.nack_name = nack_name
		message_base = f'IPG answered NACK 0x{nack_code:04X} ({nack_name}).'
		if message:
			if not override_message:
				message = message_base + message
		else:
			message = message_base
		super(AlcpNack, self).__init__(message, payload, *args)


# endregion


class AlcpBase(Blep.Blep):
	nack_codes: Type[enum.IntEnum] = None
	error_codes: Type[enum.IntEnum] = None

	def __init__(self, address: str, transaction_id_seed: int = None):

		random.seed(transaction_id_seed)

		super(AlcpBase, self).__init__(address)

		# Order-answer processing
		self._received_answer: Union[None, bytes, Exception] = None
		self._answer_complete = threading.Event()

		# These are mappings from channels and transaction ids to dictionaries of the type handle -> callback
		self.__transaction_id_listeners = defaultdict(dict)
		self.__transaction_id_listeners_lock = threading.Lock()

		self.__channel_listeners = defaultdict(dict)
		self.__channel_listeners_lock = threading.Lock()

		# Register a single callback for new raw messages
		self._add_message_listener(self._on_new_message)

	@staticmethod
	def _new_transaction_id():
		return random.randint(0, 2**16-1)

	@staticmethod
	def _send_message_to_listeners(
			lock: threading.Lock, listeners: dict,
			message_type: Blep.BlepMessageType, channel: int, transaction_id: int, error_code: int, payload: bytes
	):
		lock.acquire()
		for _, callback in listeners.items():
			try:
				callback(message_type, channel, transaction_id, error_code, payload)
			except Exception as e:
				print(f'_send_message_to_listeners - error: callback {callback} failed with error {str(e)}')
		lock.release()

	def _on_new_message(self, message_type: Blep.BlepMessageType, channel: int, transaction_id: int, error_code: int,
						payload: bytes):
		self._send_message_to_listeners(self.__transaction_id_listeners_lock, self.__transaction_id_listeners[transaction_id], message_type, channel, transaction_id, error_code, payload)
		self._send_message_to_listeners(self.__channel_listeners_lock, self.__channel_listeners[channel], message_type, channel, transaction_id, error_code, payload)

	# region Order-Answer processing

	def _add_notification_listener_for_transaction_id(
			self,
			transaction_id: int,
			callback: Callable[[Blep.BlepMessageType, int, int, int, bytes], None],
			handle: Hashable = None,
	):
		"""
		:param transaction_id:
		:param callback: should be callback(message_type: BlepMessageType, channel: int, transaction_id: int, error_code: int, payload: bytes)
		:param handle: an identifier to erase the callback afterwards. If none is supplied, the callback itself is used
		:return:
		"""
		self.__transaction_id_listeners_lock.acquire()
		if handle is None:
			handle = callback
		self.__transaction_id_listeners[transaction_id][handle] = callback
		self.__transaction_id_listeners_lock.release()

	def _remove_notification_listener_for_transaction_id(self, transaction_id: int, handle: Hashable):
		self.__transaction_id_listeners_lock.acquire()
		self.__transaction_id_listeners[transaction_id].pop(handle)
		self.__transaction_id_listeners_lock.release()

	def _on_order_answer_received(self, message_type: Blep.BlepMessageType, channel: int, transaction_id: int,
								  error_code: int, payload: bytes):
		if message_type == Blep.BlepMessageType.ANSWER:
			self._received_answer = payload
		elif message_type == Blep.BlepMessageType.ERROR:
			self._received_answer = self.process_error_message(error_code, payload)
		elif message_type == Blep.BlepMessageType.NACK:
			self._received_answer = self.process_nack_message(error_code, payload)
		else:
			raise ValueError(f"_on_order_answer_received: Unexpected message type {message_type}")
		self._answer_complete.set()

	def _send_alcp_order_receive_ans(
			self,
			order_code: int,
			order_payload: Union[bytes, ctypes.Structure] = None,
			response_struct: Type[ctypes.Structure] = None,
			timeout: Union[float, int, None] = 10,
	) -> Union[bytes, ctypes.Structure]:

		# Build payload
		if order_payload is None:
			order_payload = bytes()
		elif isinstance(order_payload, ctypes.Structure):
			order_payload = bytes(order_payload)
		payload = bytes((order_code,)) + order_payload + bytes((0xFF - order_code,))

		# Set transaction id and callback.
		transaction_id = self._new_transaction_id()
		self._answer_complete.clear()
		self._received_answer = None
		self._add_notification_listener_for_transaction_id(transaction_id, self._on_order_answer_received)

		# Send message
		self._send_message(Blep.BlepMessageType.REQUEST, Blep.INVALID_CHANNEL, transaction_id, payload)

		# Wait for answer. _on_order_answer_received sets the event
		try:
			if not self._answer_complete.wait(timeout):
				raise TimeoutError()
		finally:
			# todo aca hay una race condition donde podria entrar el siguiente mensaje antes de borrar el callback
			self._remove_notification_listener_for_transaction_id(transaction_id, self._on_order_answer_received)

		# Process received data
		if isinstance(self._received_answer, AlcpException):
			raise self._received_answer
		elif response_struct is None:
			return self._received_answer
		else:
			return response_struct.from_buffer_copy(self._received_answer)

	@classmethod
	def process_error_message(cls, error_code: int, payload: bytes, error_name: str = None, message: str = None,
							override_msg: bool = False, *args, **kwargs):
		if not error_name:
			try:
				error_name = cls.error_codes(error_code).name
			except:
				error_name = "Unknown Error"
		return AlcpError(error_code, error_name, payload, message, override_msg, *args)

	@classmethod
	def process_nack_message(cls, error_code: int, payload: bytes, nack_name: str = None, message: str = None,
							 override_msg: bool = True, *args, **kwargs):
		if not nack_name:
			try:
				nack_name = cls.nack_codes(error_code).name
			except:
				nack_name = "Unknown NACK"
		return AlcpNack(error_code, nack_name, payload, message, override_msg, *args)

	# endregion


	# region Notifications

	def _add_notification_listener_for_channel(
			self,
			channel: int,
			callback: Callable[[Blep.BlepMessageType, int, int, int, bytes], None],
			handle: Hashable = None,
	):
		"""
		:param channel:
		:param callback: should be callback(message_type: BlepMessageType, channel: int, transaction_id: int, error_code: int, payload: bytes)
		:param handle: an identifier to erase the callback afterwards. If none is supplied, the callback itself is used
		:return:
		"""
		self.__channel_listeners_lock.acquire()
		if handle is None:
			handle = callback
		self.__channel_listeners[channel][handle] = callback
		self.__channel_listeners_lock.release()

	def _remove_notification_listener_for_channel(self, channel: int, handle: Hashable):
		self.__channel_listeners_lock.acquire()
		self.__channel_listeners[channel].pop(handle)
		self.__channel_listeners_lock.release()

	def _subscribe_to_channel_notifications(
			self,
			channel: int,
			callback: Callable,
			answer_struct_cls: Type[ctypes.Structure] = None,
			send_order: bool = True,
			only_struct_in_callback: bool = False,
	):
		"""
		Sends a subscribe order to the IPG (if send_order is True) for the desired channel and stores the passed
		callback, which will be called if notifications arrive for that channel.

		If only_struct_in_callback is True, then answer_struct_cls must be a subclass of ctype.Structure. The received
		data will be parsed into an answer_struct_cls instance and passed to the callback, discarding other message
		information. This is useful for RSSI or Status messages. The callback signature must be:

			def callback(struct: answer_struct_cls):

		If only_struct_in_callback is False, then answer_struct_cls may be a subclass of ctype.Structure or None.
		The received data will be parsed into an answer_struct_cls if it's supplied and passed to the callback, along
		with other message information as kwargs. The callback signature must be:

			def callback(**kwargs):

		The kwargs dict will contain
			'message_type': Blep.BlepMessageType
			'channel': int
			'transaction_id': int
			'error_code': int
			'struct': answer_struct_cls if supplied, None otherwise
			'payload': the raw payload received

		"""
		if callback:

			if only_struct_in_callback and answer_struct_cls is None:
				raise ValueError("A struct must be set in only struct mode")

			# Dynamically create a new function to handle this callback. This function only parses the received data and
			# calls the original callback
			def notif_listener_callback(message_type: Blep.BlepMessageType, channel: int, transaction_id: int,
										error_code: int, payload: bytes):

				if only_struct_in_callback:
					struct = answer_struct_cls.from_buffer_copy(payload)
					callback(struct)
				else:
					params = {
						'message_type': message_type,
						'channel': channel,
						'transaction_id': transaction_id,
						'error_code': error_code,
						'payload': payload,
					}
					if answer_struct_cls is not None:
						params['struct'] = answer_struct_cls.from_buffer_copy(payload)
					callback(**params)

			self._add_notification_listener_for_channel(channel, notif_listener_callback, callback)

		if send_order:
			return self.o_subscribe_to_channel(channel)

	def _unsubscribe_from_channel_notifications(self, channel: int, callback: Callable = None, send_order: bool = True):

		if callback:
			self._remove_notification_listener_for_channel(channel, callback)

		if send_order:
			self.o_unsubscribe_from_channel(channel)

	# endregion

	def o_subscribe_to_channel(self, channel: int):
		raise NotImplementedError()

	def o_unsubscribe_from_channel(self, channel: int):
		raise NotImplementedError()

	def o_reset(self, *args, **kwargs):
		raise NotImplementedError()

	def reset(self, *args, **kwargs):
		self.o_reset(*args, **kwargs)
		self.reconnect()


