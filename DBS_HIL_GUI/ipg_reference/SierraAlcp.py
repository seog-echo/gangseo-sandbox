##### Received from Integer Jan 2026 for Sep 2024 firmware delivery #####

import AlcpBase
import enum
import ctypes
import time
from typing import Callable, Type

# region Order Codes and Structures


class Channels(enum.IntEnum):
	BOOTLOADER_PROGRESS			= 0x00
	BOOTLOADER_RESULT 			= 0x01
	STATUS 						= 0x02
	RSSI 						= 0x07
	LOG							= 0x0A
	CHARGE 						= 0x0B
	RECORDING                   = 0x0C
	EXT_MEM_PROGRESS 			= 0x0E
	EXT_MEM_RESULT				= 0x0F



class AlcpOrderCodes(enum.IntEnum):
	O_NO_ALCP_ORDER = 0x00
	O_TRIGGER_STATUS_UPDATE = 0x01
	O_GET_ALCP_INFO = 0x02
	O_INTERROGATE = 0x03
	O_PROGRAM = 0x04
	O_RESET = 0x05
	O_GET_TIME = 0x06
	O_SET_TIME = 0x07
	O_GET_PATIENT_INFO = 0x08
	O_SET_PATIENT_INFO = 0x09
	O_GET_LOG_INFO = 0x0A
	O_GET_LOG = 0x0B
	O_CLEAR_LOG = 0x0C
	O_STOP_LOG = 0x1A
	O_START_THERAPY = 0x0D
	O_STOP_THERAPY = 0x0E
	O_CHANGE_AMPLITUDE = 0x0F
	O_GET_TEMPERATURE = 0x10
	O_TURN_OFF = 0x11
	O_START_IMPEDANCE_MEAS = 0x12
	O_GET_IMPEDANCE = 0x13
	O_EP_RECORDING = 0x14
	O_START_RECORDING = 0x15
	O_GET_ACCEL_MEASURE = 0x16
	O_STOP_RECORDING = 0x17
	O_START_RSSI = 0x18
	O_STOP_RSSI = 0x19
	O_SET_SESSION_ROLE = 0x1B
	O_DUMP_MEMORY = 0x30
	O_SET_TEST_CASE = 0x31
	O_SET_PRODUCTION_MODE = 0x40
	O_CLEAR_PRODUCTION_MODE = 0x41
	O_GET_PRODUCTION_INFO = 0x42
	O_LAB_CHECK_SERIAL_MEMORY = 0x43
	O_LAB_GET_SERIAL_MEMORY_RESULT = 0x44
	O_LAB_TURN_ON_ACCEL = 0x45
	O_LAB_TURN_OFF_ACCEL = 0x46
	O_LAB_CUSTOM_IMPEDANCE_MEAS = 0x47

	O_LAB_GET_CHARGE_INFORMATION = 0x55
	O_SUBSCRIBE_TO_CHANNEL = 0xD5
	O_UNSUBSCRIBE_FROM_CHANNEL = 0xD6
	O_CHARGE_COMMUNICATION = 0xE4
	O_GET_CCC_DEVICE_INFO = 0xE8
	O_GET_DEVICE_ID = 0xEF
	O_GET_BOOTLOADER_INFO = 0xF0
	O_BOOTLD_START = 0xF1
	O_BOOTLD_ERASE = 0xF2
	O_BOOTLD_WRITE = 0xF3
	O_BOOTLD_READ = 0xF4
	O_BOOTLD_FINISH = 0xF5
	O_BOOTLD_HASHED_FINISH = 0xF6


class ChargeCommunicationCodes(enum.IntEnum):
	CHARGE_GET_STATUS               = 0x00
	CHARGE_SET_ADAPTIVE_CHARGE		= 0x01
	CHARGE_SET_CHARGER_ID			= 0x02
	CHARGE_SET_COUPLING				= 0x03
	CHARGE_DO_MEASURE				= 0x04
	CHARGE_END_SESSION				= 0x05
	GET_CHARGE_RAW_MEASUREMENTS		= 0x06


class ErrorCodes(enum.IntEnum):
	COMM_MSG_TOO_SHORT_FOR_CRC = 0x10
	COMM_MSG_TOO_SHORT_FOR_METADATA = 0x11
	COMM_MSG_TOO_SHORT_FOR_ORDER_CODE = 0x12
	COMM_MSG_TOO_LONG_FOR_IO_BUFFER = 0x13
	COMM_PARAMS_TOO_SHORT = 0x14
	COMM_PARAMS_TOO_LONG = 0x15
	COMM_PARAMS_INVALID_FORMAT = 0x16
	COMM_CONFIRMATION_INCORRECT = 0x17
	COMM_CRC_INCORRECT = 0x18
	COMM_ORDER_CODE_UNKNOWN = 0x19
	COMM_SPI_READY_TIMEOUT = 0x1A
	COMM_SPI_NOT_READY_TIMEOUT = 0x1B
	COMM_SPI_PACKET_TOO_LONG = 0x1C
	COMM_SPI_PACKET_CRC_INCORRECT = 0x1D
	COMM_RESPONSE_TIMEOUT = 0x1E
	COMM_RESPONSE_WRONG_SIZE = 0x1F
	COMM_RESPONSE_UNSUCCESSFUL = 0x20
	COMM_RECEIVE_ALREADY_DISCARDED = 0x21
	COMM_RECEIVE_DISCARDED_MID_RECEPTION = 0x22
	COMM_RECEIVE_TIMEOUT = 0x23
	COMM_CONNECTION_HANDLE_INVALID = 0x24
	COMM_CHANNEL_INVALID = 0x25
	COMM_ABORT = 0x26
	COMM_UNEXPECTED_MESSAGE_TYPE = 0x27
	COMM_NO_ERROR = 0xFF


class NackCodes(enum.IntEnum):
	SAFE_MODE = 0x0000
	BATTERY_EOS = 0x0001
	BATTERY_ERI = 0x0002
	NOT_IN_PROD_MODE = 0x0003
	INVALID_PARAMETERS = 0x0004
	FRAM_WRITE_FAILED = 0x0005
	WRONG_STATE = 0x0006
	LOG_ALREADY_STARTED = 0x0007
	INVALID_PROG_IDX = 0x0008
	PROGRAM_DISABLED = 0x0009
	INVALID_ELECTRODE = 0x000A
	IMPEDANCE_UNAVAILABLE = 0x000B
	IMPEDANCE_OUT_OF_RANGE = 0x000C
	SLAVE_SPI_FAIL = 0x000D
	SLAVE_NACK = 0x000E
	ABORTED_BY_ORDER = 0x000F
	ABORTED_LOG_WRITTEN = 0x0010
	INVALID_SIGNATURE = 0x0017
	SESSION_ROLE_UNSET = 0x0018
	SESSION_ROLE_MAX_REACHED = 0x0019
	PROGRAM_LIMIT_NOT_MEASURED = 0x001A
	NO_ERROR = 0xFFFF

class SafeModeCode(enum.IntEnum):
	NO_ERROR = 0x0000
	ERROR1 = 0x0001

class SessionRole(enum.IntEnum):
	UNSET = 0
	WAND = 1
	PROGRAMMER = 2
	CHARGER = 3
	RDP = 4

class AlcpInfo(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Version', ctypes.c_uint8),
		('Subversion', ctypes.c_uint8),
		('WandALCPVersion', ctypes.c_uint8),
		('WandALCPSubVersion', ctypes.c_uint8),
		('ChargerALCPVersion', ctypes.c_uint8),
		('ChargerALCPSubVersion', ctypes.c_uint8),
	]


class AsicStimUnit(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('unit', ctypes.c_uint32 * 15),
	]

class AsicProgWord(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('stim_comm', ctypes.c_uint32 * 7),
		('stim_unit_0', ctypes.c_uint32 * 15 ),
		('stim_unit_1', ctypes.c_uint32 * 15 ),
		('stim_unit_2', ctypes.c_uint32 * 15 ),
		('stim_unit_3', ctypes.c_uint32 * 15 ),
		('stim_unit_4', ctypes.c_uint32 * 15 ),
		('stim_unit_5', ctypes.c_uint32 * 15 ),
		('stim_unit_6', ctypes.c_uint32 * 15 ),
		('stim_unit_7', ctypes.c_uint32 * 15 ),
		('stim_ampl', ctypes.c_uint32 * 26),
		('stim_afe', ctypes.c_uint32 * 12),
		('stim_llen', ctypes.c_uint32 * 12),
		('stim_dsp', ctypes.c_uint32 * 36),
	]


class ProgrammingWord(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('AsicProg', AsicProgWord),
	]


class DeviceId(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Model', ctypes.c_char * 16),
		('ModelCrc', ctypes.c_uint16),
		('Serial', ctypes.c_char * 18),
	]


class Timestamp(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('RtcTS', ctypes.c_uint32),
		('RtcID', ctypes.c_uint32),
	]

class Calendar(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Seconds', ctypes.c_uint8),
		('Minutes', ctypes.c_uint8),
		('Hours', ctypes.c_uint8),
		('Day', ctypes.c_uint8),
		('Month', ctypes.c_uint8),
		('Year', ctypes.c_uint8),
	]

class GetTimeResponse(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Timestamp', Timestamp),
		('Calendar', Calendar),
	]

class HardwareInfo(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('HwConfig', ctypes.c_uint8),
		('BootloaderVersion', ctypes.c_uint8),
		('filler', ctypes.c_byte * 8),
	]


class IpgStatus(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ALCPVersion', ctypes.c_uint8),
		('ALCPSubVersion', ctypes.c_uint8),
		('DeviceID', DeviceId),
		('TelemetFwVersion', ctypes.c_char * 32),
		('NRFFwVersion', ctypes.c_char * 32),
		('AsicRevision', ctypes.c_uint32),
		('BoardVersion', ctypes.c_uint8),
		('BootloaderVersion', ctypes.c_uint8),


		('BatteryState', ctypes.c_uint16, 2),
		('ProductionMode', ctypes.c_uint16, 1),
		('IsCharging', ctypes.c_uint16, 1),
		('Reserved', ctypes.c_uint16, 12),

		('Temperature', ctypes.c_uint16),
		('BatteryVoltage', ctypes.c_uint16),
		('BatteryChargeLevel', ctypes.c_uint8),
		('ChargingVoltage', ctypes.c_uint16),

		('ErrorStatus', ctypes.c_uint16),
		('ErrorTime', Timestamp),
		('CurrentTime', Timestamp),
	]



class ChargeStatus(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ChargerALCPVersion', ctypes.c_uint8),
		('ChargerALCPSubVersion', ctypes.c_uint8),
		('IpgDownSuggestedAction', ctypes.c_uint8),
		('ErrorStatus', ctypes.c_uint8),
		('BatteryLevel', ctypes.c_uint8),
		('ChargeComplete', ctypes.c_bool),
		('TemperatureHigh', ctypes.c_bool),
		('CanDetectCharge', ctypes.c_bool),
		('ChargerMute', ctypes.c_bool),
		('AdaptationStatus', ctypes.c_uint8),
		('AdaptationStepSize', ctypes.c_uint8),
		('CurrentAdaptiveStatus', ctypes.c_uint8),
		('Time', ctypes.c_uint32),
	]


class LabChargeInformation(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Charging', ctypes.c_bool),
		('I_Charge_AD', ctypes.c_uint16),
		('V_Charge_AD', ctypes.c_uint16),
		('I_Charge_mA', ctypes.c_uint16),
		('V_Charge_mV', ctypes.c_uint16),
		('BatteryLevel', ctypes.c_uint8),
		('AdaptationStatus', ctypes.c_uint8),
		('AdaptationStepSize', ctypes.c_uint8),
		('ChargeDuration', ctypes.c_uint32),
		('AccumulatedBatteryCapacity', ctypes.c_uint32),
		('IpgDownSuggestedAction', ctypes.c_uint8),
		('TemperatureHigh', ctypes.c_bool),
		('ChargeComplete', ctypes.c_bool),
		('InstabilityOfST1', ctypes.c_bool),
		('StatusPins', ctypes.c_uint8),
	]



ChargeNotification = LabChargeInformation


class InterrogationResponse(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Status', IpgStatus),
		('ProgrammingWord', ProgrammingWord),
	]


class BootloaderInfo(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("InBootloader", ctypes.c_bool),
		("BootloaderVersion", ctypes.c_uint8),
		("EOL", ctypes.c_bool),
		("InSafe", ctypes.c_bool),
		("Battery", ctypes.c_uint16),
		("Count", ctypes.c_uint16),
		("DateTime", ctypes.c_uint32),
		("FirmwareVersions", ctypes.c_uint8 * 8),
		("StatusCodes", ctypes.c_uint8 * 8),
		("Hash", ctypes.c_uint8 * 32),
		("Crc", ctypes.c_uint32),
	]

class BootloaderStartParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Hash', ctypes.c_uint8 * 32),
	]

class BootloaderFinishParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('FwLenForHash', ctypes.c_uint32),
		('RandombNumber', ctypes.c_uint32),
	]

class BootloaderMemoryAccess(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Code', ctypes.c_uint8),
		('Start', ctypes.c_uint32),
		('Stop', ctypes.c_uint32),
	]

class LogInfo(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('InPointer', ctypes.c_uint32),
		('OutPointer', ctypes.c_uint32),
		('LogSize', ctypes.c_uint32),
		('LogSequenceNumber', ctypes.c_uint32),
	]

class LogSpace(enum.IntEnum):
	GENERAL = 0
	RAW_MEMORY = 1


class LogChunkNotificationHeader(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('LogSpace', ctypes.c_uint8),
		('LogPointer', ctypes.c_uint32),
		('CurrentChunk', ctypes.c_uint16),
		('TotalChunks', ctypes.c_uint16),
		('CurrentChunkSize', ctypes.c_uint16),
	]

class RecordingChunkNotificationHeader(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ChunkType', ctypes.c_uint8),
		('IsLastChunk', ctypes.c_bool),
		('ChunkSize', ctypes.c_uint16),
		('ChunkNumber', ctypes.c_uint32),
		('Timestamp', ctypes.c_uint32),
		('BufferAddress', ctypes.c_uint32),
	]



class ChargeRawMeasurements(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('BatteryAd', ctypes.c_uint16),
		('TemperatureAd', ctypes.c_uint16),
		('ChargeVoltageAd', ctypes.c_uint16),
		('ChargeCurrentAd', ctypes.c_uint16),
	]

class AccelMeasurement(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('x', ctypes.c_int16),
		('y', ctypes.c_int16),
		('z', ctypes.c_int16),
	]

class TemperatureMeasurement(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('TemperatureAD', ctypes.c_uint16),
		('Degrees', ctypes.c_int8),
	]

class SerialMemoryResult(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Result', ctypes.c_int8),
	]

class LabCustomImpedMeasureResult(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ResMeasure', ctypes.c_int16),
		('CapMeasure', ctypes.c_int16),
	]

class ImpedanceMeasureReadResult(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Status', ctypes.c_int16),
		('ResMeasure', ctypes.c_int16 * 32),
		('CapMeasure', ctypes.c_int16 * 32),
	]


class BootloaderReadAnswer(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('data', ctypes.c_uint8 * 32),
	]


class BleGapAddr(ctypes.Structure):
	LENGTH = 6
	_pack_ = 1
	_fields_ = [
		('AddrIdPeer', ctypes.c_uint8, 1),
		('AddrType', ctypes.c_uint8, 7),
		('Address',  ctypes.c_uint8 * LENGTH),
	]


class BondedDeviceData(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Rank', ctypes.c_uint32),
		('Address',  BleGapAddr),
	]


class BondedDeviceList(ctypes.Structure):
	MAX = 30
	_pack_ = 1
	_fields_ = [
		('TopIdx', ctypes.c_uint8),
		('DeviceData', BondedDeviceData * MAX),
	]

# region Notifications


class RssiNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Rssi_dBm', ctypes.c_int8),
		('ChIndex', ctypes.c_uint8),
	]



class BootloaderProgressNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("ImageType", ctypes.c_uint8),
		("Progress", ctypes.c_uint8),
	]


class BootloaderErrorNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("ImageType", ctypes.c_uint8),
		("DestintationAddress", ctypes.c_uint32),
		("CurrentByte", ctypes.c_uint32),
		("TotalBytes", ctypes.c_uint32),
		("PseErrorCode", ctypes.c_uint8),
	]




# endregion

# endregion

# region Auxiliary

def print_bl_info(blinfo: BootloaderInfo):
	signature = int.from_bytes(ctypes.string_at(blinfo.Signature, 4), 'little')
	lines = [
		'Bootloader Info:',
		f'InBootloader: {blinfo.InBootloader}',
		f'FirmwareVersions: {blinfo.FirmwareVersions:16X}',
		f'FirmwareVersions: {ctypes.string_at(blinfo.FirmwareVersions)}',
		f'StatusCodes: {blinfo.StatusCodes:16X}',
		f'Count: {blinfo.Count}',
		f"Hash: {blinfo.Hash:64X}",
	]
	print('\n\t'.join(lines))


# endregion


class SierraAlcp(AlcpBase.AlcpBase):

	nack_codes = NackCodes
	error_codes = ErrorCodes

	def __init__(self, address: str, api_id: int = 0x00):
		super(SierraAlcp, self).__init__(address, api_id)

	# region Error processing

	@classmethod
	def process_nack_message(cls, error_code: int, payload: bytes, nack_name: str = None, message: str = None, *args, **kwargs):
		try:
			last_response = payload[-1]
		except:
			last_response = 0xFF
		last_message = f" Last response code 0x{last_response:02X}" if error_code == NackCodes.SLAVE_NACK else None
		if message and last_message:
			message = last_message + message
		elif last_message:
			message = last_message
		return super(SierraAlcp, cls).process_nack_message(error_code, payload, nack_name, message, False, last_response)

	# endregion

	# region Orders

	# def o_reset(self, full_reset: bool = True):
		# # full reset erases the Programming Word.
		# return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_RESET, full_reset.to_bytes(1, "little"))

	def o_reset(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_RESET)

	def o_set_session_role(self, role: SessionRole = SessionRole.PROGRAMMER):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_SESSION_ROLE, role.to_bytes(1, "little"))

	def o_get_alcp_info(self) -> AlcpInfo:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_ALCP_INFO, response_struct=AlcpInfo)

	def o_get_time(self) -> GetTimeResponse:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_TIME, response_struct=GetTimeResponse)
	def o_start_therapy(self, stim_units: int):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_THERAPY, stim_units.to_bytes(1, byteorder='little'))
	def o_stop_therapy(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_THERAPY)

	def o_set_time(self, seconds: int, minutes: int, hours: int, day: int, month: int, year: int):
		cal = Calendar(Seconds=seconds, Minutes=minutes, Hours=hours, Day=day, Month=month, Year=year)
		self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_TIME, cal)

	def o_set_test_case(self, test_case: int, param1: int = 0, param2: int = 0, param3: int = 0):
		class Payload(ctypes.Structure):
			_pack_ = 1
			_fields_ = [
				("test_case", ctypes.c_uint8),
				("param1", ctypes.c_uint16),
				("param2", ctypes.c_uint16),
				("param3", ctypes.c_uint16),
			]
		payload = Payload(test_case=test_case, param1=param1, param2=param2, param3=param3)
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_TEST_CASE, payload)


	def o_interrogate(self) -> InterrogationResponse:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_INTERROGATE, response_struct=InterrogationResponse)

	def o_trigger_status_update(self):
		"""Request the IPG to push an IpgStatus notification on the STATUS channel (only has effect when subscribed)."""
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_TRIGGER_STATUS_UPDATE)

	def o_program(self, programming_word: ProgrammingWord):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_PROGRAM, programming_word)


	def o_start_rssi(self, threshold_dbm: int, skip_count: int):
		payload = threshold_dbm.to_bytes(1, 'little') + skip_count.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_RSSI, payload)

	def o_stop_rssi(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_RSSI)

	def o_get_lab_charge_info(self) -> LabChargeInformation:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_GET_CHARGE_INFORMATION, response_struct=LabChargeInformation)

	def o_subscribe_to_channel(self, channel: Channels):
		return self._send_alcp_order_receive_ans(
			AlcpOrderCodes.O_SUBSCRIBE_TO_CHANNEL,
			channel.to_bytes(1, byteorder='little')
		)

	def o_unsubscribe_from_channel(self, channel: Channels):
		return self._send_alcp_order_receive_ans(
			AlcpOrderCodes.O_UNSUBSCRIBE_FROM_CHANNEL,
			channel.to_bytes(1, byteorder='little')
		)

	def o_get_log_info(self, log_space: LogSpace) -> LogInfo:
		payload = log_space.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_LOG_INFO, payload, response_struct=LogInfo)

	def o_get_log(self, log_space: LogSpace, start_address: int, size: int):
		payload = log_space.to_bytes(1, 'little') + start_address.to_bytes(4, 'little') + size.to_bytes(4, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_LOG, payload)

	def o_stop_log(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_LOG)

	def o_clear_log(self, log_space: LogSpace):
		payload = log_space.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_CLEAR_LOG, payload)     

	def o_ep_recording(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_EP_RECORDING)

	def o_start_recording(self, seconds: int, source: int, loopRecChannelMask: int, discard_buffer: bool):
		payload = seconds.to_bytes(2, 'little') + source.to_bytes(1, 'little') + loopRecChannelMask.to_bytes(2, 'little') + discard_buffer.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_RECORDING, payload)

	def o_stop_recording(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_RECORDING)

	def o_get_temperature(self) -> TemperatureMeasurement:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_TEMPERATURE, response_struct=TemperatureMeasurement)

	def o_get_accel_measure(self) -> AccelMeasurement:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_ACCEL_MEASURE, response_struct=AccelMeasurement)


	def o_set_production_mode(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_PRODUCTION_MODE)
	def o_clear_production_mode(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_CLEAR_PRODUCTION_MODE)


	def o_lab_check_serial_memory(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_CHECK_SERIAL_MEMORY)
	def o_lab_get_serial_memory_result(self) -> SerialMemoryResult:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_GET_SERIAL_MEMORY_RESULT, response_struct=SerialMemoryResult)
	def o_lab_turn_on_accel(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_TURN_ON_ACCEL)
	def o_lab_turn_off_accel(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_TURN_OFF_ACCEL)
	def o_lab_custom_impedance_measure(self, negPulse: int, posPulse: int, negMeas: int, posMeas: int):
		payload = negPulse.to_bytes(1, 'little') + posPulse.to_bytes(1, 'little') + negMeas.to_bytes(1, 'little') + posMeas.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_LAB_CUSTOM_IMPEDANCE_MEAS, payload, response_struct=LabCustomImpedMeasureResult )


	def o_get_bl_info(self) -> BootloaderInfo:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_BOOTLOADER_INFO, response_struct=BootloaderInfo)

	def o_bl_start(self, blHash: bytes):
		#payload = blParams.to_bytes(32, 'little')
		print(f'start bl hash: {blHash.hex()}, len: {len(blHash)} ')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLD_START, blHash)     

	def o_bl_erase(self, target: int, start: int, stop: int) -> BootloaderMemoryAccess:
		payload = target.to_bytes(1, 'little') + start.to_bytes(4, 'little') + stop.to_bytes(4, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLD_ERASE, payload, response_struct=BootloaderMemoryAccess)     

	def o_bl_write(self, target: int, start: int, stop: int, data: bytes) -> BootloaderMemoryAccess:
		padding = int(0)
		payload = target.to_bytes(1, 'little') + start.to_bytes(4, 'little') + stop.to_bytes(4, 'little') + padding.to_bytes(6, 'little') + data
		print(f'bl write payload: {payload.hex()}')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLD_WRITE, payload, response_struct=BootloaderMemoryAccess)     

	def o_bl_read(self, target: int, start: int, stop: int) -> BootloaderReadAnswer:
		payload = target.to_bytes(1, 'little') + start.to_bytes(4, 'little') + stop.to_bytes(4, 'little')
		print(f'bl read payload: {payload.hex()}')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLD_READ, payload, response_struct=BootloaderReadAnswer)     

	def o_bl_finish(self, length: int, randomNumber: int):
		payload = length.to_bytes(4, 'little') + randomNumber.to_bytes(4, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLD_FINISH, payload)     

	def o_start_impedance(self, electrodes: int):
		payload = electrodes.to_bytes(4, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_IMPEDANCE_MEAS, payload)

	def o_get_impedance(self) -> ImpedanceMeasureReadResult:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_IMPEDANCE, response_struct=ImpedanceMeasureReadResult)


	# endregion

	# region Notifications

	def subscribe_to_status(self, callback: Callable[[IpgStatus], None], send_order: bool = True):
		return self._subscribe_to_channel_notifications(Channels.STATUS, callback, IpgStatus, send_order, True)

	def unsubscribe_from_status(self, callback: Callable, send_order: bool = True):
		return self._unsubscribe_from_channel_notifications(Channels.STATUS, callback, send_order)

	def subscribe_to_charge_notifications(self, callback: Callable[[ChargeNotification], None], send_order: bool = True):
		return self._subscribe_to_channel_notifications(Channels.CHARGE, callback, ChargeNotification, send_order, True)

	def unsubscribe_from_charge_notifications(self, callback: Callable, send_order: bool = True):
		return self._unsubscribe_from_channel_notifications(Channels.CHARGE, callback, send_order)

	def start_rssi(
			self, threshold_dbm: int, skip_count: int,
			callback: Callable[[RssiNotification], None] = None,
			send_order: bool = True
	):

		self._subscribe_to_channel_notifications(Channels.RSSI, callback, RssiNotification, send_order, True)

		if send_order:
			# first subscribe, otherwise IPG stops RSSI if notifications are ready without subscriptors
			self.o_start_rssi(threshold_dbm, skip_count)

	def stop_rssi(self, callback: Callable = None, send_order: bool = True):

		self._unsubscribe_from_channel_notifications(Channels.RSSI, callback, send_order)

		if send_order:
			self.o_stop_rssi()

	def start_get_log(self, log_space: LogSpace, callback: Callable, send_order: bool = True, test_chunks=None):

		if callback:
			self._subscribe_to_channel_notifications(Channels.LOG, callback, None, send_order, False)  # todo revisar a todos estos tengo q pasar send_order True o no tiene sentido

		if send_order:
			log_info = self.o_get_log_info(log_space)
			if test_chunks:
				start_address = 0
				size = 2048 * test_chunks
			else:
				start_address = log_info.OutPointer
				size = log_info.InPointer - log_info.OutPointer if log_info.InPointer > log_info.OutPointer else log_info.LogSize

			self.o_get_log(log_space, start_address, size)
			print(f'LogInfo: LogSpace = {log_space}, StartAddress = {start_address}, Length = {size}, InPointer = {log_info.InPointer}, OutPointer = {log_info.OutPointer}, LogSize = {log_info.LogSize}, Sequence = {log_info.LogSequenceNumber}')

	def start_get_raw_log(self, log_space: LogSpace, start_address: int, size: int, callback: Callable, send_order: bool = True, test_chunks=None):

		if callback:
			self._subscribe_to_channel_notifications(Channels.LOG, callback, None, send_order, False)

		if send_order:
			if test_chunks:
				start_address = 0
				size = 2048 * test_chunks

			self.o_get_log(log_space, start_address, size)
			print(f'RAW_MEMORY: StartAddress = {start_address}, Length = {size} ')

	def start_recording(self, seconds: int, source: int, loopRecChannelMask: int, discard_buffer: bool, callback: Callable, send_order: bool = True, test_chunks=None):

		if callback:
			self._subscribe_to_channel_notifications(Channels.RECORDING, callback, None, send_order, False)

		self.o_start_recording(seconds, source, loopRecChannelMask, discard_buffer)
            
	# endregion

	def __test(self):
		pass


if __name__ == '__main__':

	from address import address

	with SierraAlcp(address) as ipg:
		ipg.connect()
		pass
