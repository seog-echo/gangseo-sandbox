import time

import AlcpBase
import enum
import ctypes
from typing import Callable, Type
from lib.mips import crc16_ccitt

# region Order Codes and Structures


class StimulationEndCause(enum.IntEnum):
	STOP_BY_ORDER			= 0x00
	STOP_BY_MAGNET			= 0x01
	STOP_BY_LOW_BATTERY		= 0x02
	STOP_BY_DOWN			= 0x03
	STOP_BY_RESET			= 0x04
	STOP_BY_TURNOFF			= 0x05
	STOP_BY_BUTTON			= 0x06
	STOP_BY_CHARGE_PULSE	= 0x07
	STOP_BY_CHARGE_SESSION 	= 0x08


class Channels(enum.IntEnum):
	BOOTLOADER_PROGRESS			= 0x00
	BOOTLOADER_RESULT 			= 0x01
	STATUS 						= 0x02
	IMPEDANCE_PROGRESS 			= 0x03
	IMPEDANCE_RESULT 			= 0x04
	THERAPY_STOP_PROGRESS		= 0x05
	THERAPY_STOP_COMPLETE 		= 0x06
	RSSI 						= 0x07
	THERAPY_STATUS				= 0x08
	LOG							= 0x0A
	CHARGE 						= 0x0B
	RECORDING                   = 0x0C
	EXT_MEM_PROGRESS 			= 0x0E
	EXT_MEM_RESULT				= 0x0F


class PseSmState(enum.IntEnum):
	IDLE					= 0
	ACTIVE					= 1
	STOPPING				= 2
	IDLE_SCHEDULED			= 3
	ACTIVE_SCHEDULED		= 4
	STOPPING_SCHEDULED		= 5
	SAFE					= 6
	MEAS_IMP				= 7
	SMCU_UPDATE_PENDING		= 8
	SMCU_UPDATING			= 9
	WAITING_BL				= 10
	WAITING_FOR_CHARGE_OFF	= 11


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
	O_START_LOOP_REC = 0x13
	O_STOP_LOOP_REC = 0x14
	O_START_RECORDING = 0x15
	O_GET_ACCEL_MEASURE = 0x16
	O_STOP_RECORDING = 0x17
	O_START_RSSI = 0x18
	O_STOP_RSSI = 0x19
	O_SET_SESSION_ROLE = 0x1B
	O_SET_DISPLAY_MODE = 0X2C

	O_GET_THERAPY_STATUS = 0x1E
	O_GET_LOOP_RECORDER_METADATA = 0x1F
	O_TURN_ON_THERAPY = 0x20
	O_TURN_OFF_THERAPY = 0x21
	O_INCREASE_THERAPY_AMPLITUDE = 0x22
	O_DECREASE_THERAPY_AMPLITUDE = 0x23
	O_ACTIVATE_PROGRAM = 0x24
	O_ENABLE_STIMULATION = 0x25
	O_DISABLE_STIMULATION = 0x26
	O_ENABLE_ADAPTATIVE_MODE = 0x27
	O_DISABLE_ADAPTATIVE_MODE = 0x28
	O_GET_PARAMS = 0x29

	O_DUMP_MEMORY = 0x30
	O_SET_TEST_CASE = 0x31
	O_LOAD_PROGRAMMING_BUFFER = 0x32
	O_FLUSH_LOOP_RECORDER = 0x33
	O_SET_PRODUCTION_MODE = 0x40
	O_CLEAR_PRODUCTION_MODE = 0x41
	O_GET_PRODUCTION_INFO = 0x42
	O_CLEAR_BURN_IN_COUNTER = 0x43


	O_LAB_GET_CHARGE_INFORMATION = 0x55
	O_SUBSCRIBE_TO_CHANNEL = 0xD5
	O_UNSUBSCRIBE_FROM_CHANNEL = 0xD6
	O_CHARGE_COMMUNICATION = 0xE4
	O_GET_NRF_VERSION = 0xE5,
	O_GET_MAC_ADDRESS = 0xE6
	O_GET_CCC_DEVICE_INFO = 0xE8
	O_GET_DEVICE_ID = 0xEF
	O_GET_BOOTLOADER_INFO = 0xF0
	O_BOOTLOADER_START = 0xF1


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
	START_THERAPY_FAILED = 0x0012
	ASIC_COMM_FAILED = 0x0013
	ORDER_NOT_ALLOWED = 0x0014
	INVALID_SIGNATURE = 0x0017
	SESSION_ROLE_UNSET = 0x0018
	SESSION_ROLE_MAX_REACHED = 0x0019
	PROGRAM_LIMIT_NOT_MEASURED = 0x001A
	NO_ERROR = 0xFFFF

class SafeModeCode(enum.IntEnum):
	NO_ERROR = 0x0000
	ERROR1 = 0x0001


class StimulationStrategy(enum.IntEnum):
	LF = 0
	ULF = 1


class TherapySchedule(enum.IntEnum):
	SCH_DISABLED = 0
	SCH_5s = 1
	SCH_10s = 2
	SCH_30s = 3
	SCH_1min = 4
	SCH_2min = 5
	SCH_5min = 6
	SCH_10min = 7
	SCH_20min = 8
	SCH_30min = 9
	SCH_1h = 10
	SCH_2h = 11
	SCH_3h = 12
	SCH_6h = 13
	SCH_12h = 14
	SCH_18h = 15
	SCH_1day = 16
	SCH_2day = 17
	SCH_5day = 18


class ExternalMemoryTestResult(enum.IntEnum):
	NOT_DONE = 0
	FAIL = 1
	SUCCESS = 2


class SessionRole(enum.IntEnum):
	UNSET = 0
	WAND = 1
	PROGRAMMER = 2
	CHARGER = 3
	RDP = 4

class DisplayMode(enum.IntEnum):
	TRANSPARENT = 0
	BLINDED = 1

class CurrentMeasurementGain(enum.IntEnum):
	MICROAMPERES = 0
	MILLIAMPERES = 1
	AUTOMATIC = 2


class BootloaderWriteSource(enum.IntEnum):
	ACTIVE = 0x00
	RESTORE = 0x01
	A = 0x02
	B = 0x03

class VersionStruct(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Major', ctypes.c_uint8),
		('Middle', ctypes.c_uint8),
		('Minor', ctypes.c_uint8),
		('Branch', ctypes.c_uint8),
	]

	@classmethod
	def from_string(cls, string: str):
		version_list = string.split('.')
		version = cls(
			Major=int(version_list[0]),
			Middle=int(version_list[1]),
			Minor=int(version_list[2]),
			Branch=int(version_list[3]),
		)
		return version

	def __str__(self, indent=0):
		return self.indent_str(indent) + f"{self.Major}.{self.Middle}.{self.Minor}.{self.Branch}"


class FullVersionStruct(ctypes.Structure):
	DESCRIPTION_LENGTH = 32
	_pack_ = 1
	_fields_ = [
		('Numeric', VersionStruct),
		('Description', DESCRIPTION_LENGTH * ctypes.c_char),
	]

	def __str__(self, indent=0):
		return self.indent_str(indent) + f"{self.Numeric}.{bytes(self.Description).decode('ascii')}"




class GetLoopRecorderMetadataResponse(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Feed', ctypes.c_uint8),
		('Wptr', ctypes.c_uint32),
	]



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


class LeadsConfig(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('LeftRightReversed', ctypes.c_uint8, 1),
		('LeftBottomUp', ctypes.c_uint8, 1),
		('RightBottomUp', ctypes.c_uint8, 1),
	]


class TherapyProgram(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Description', ctypes.c_char * 32),
		('ProgramEnable', ctypes.c_uint8, 1),
		('IsUlfAmplitudeLimitMeasured', ctypes.c_uint8, 1),
		('RemoteEnable', ctypes.c_uint8, 1),
		('Reserved', ctypes.c_uint8, 5),
		('Strategy', ctypes.c_uint8),
		('Cathode', ctypes.c_uint8),
		('Anode', ctypes.c_uint8),
		('OnTime', ctypes.c_uint8),
		('OffTime', ctypes.c_uint8),
		# LF
		('LfSoftStartTime', ctypes.c_uint16),
		('LfStartAmplitude', ctypes.c_uint16),
		('LfMaxAmplitude', ctypes.c_uint16),
		('LfPulseInterval', ctypes.c_uint32),
		('LfFrequency', ctypes.c_uint16),
		('LfPulseWidth', ctypes.c_uint32),
		('LfRecoveryPWRatio', ctypes.c_uint16),
		# ULF
		('UlfMeasuredAmplitudeLimit', ctypes.c_uint16),
		('UlfStartAmplitude', ctypes.c_uint16),
		('UlfMaxAmplitude', ctypes.c_uint16),
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


PROGRAMMING_BUFFER_CHUNK_SIZE = 1024


class ProgrammingBufferParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('LoadIndex', ctypes.c_uint16),
		('LoadSize', ctypes.c_uint16),
		('Buffer', ctypes.c_uint8 * PROGRAMMING_BUFFER_CHUNK_SIZE),
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


_IPG_STATUS_FIELDS_BEFORE_BOOTLOADER = [
	('ALCPVersion', ctypes.c_uint8),
	('ALCPSubVersion', ctypes.c_uint8),
	('DeviceID', DeviceId),
	('TelemetFwVersion', ctypes.c_char * 32),
	('NRFFwVersion', ctypes.c_char * 32),
	('AsicRevision', ctypes.c_uint32),
	('BoardVersion', ctypes.c_uint8),
]

_IPG_STATUS_FIELDS_AFTER_BOOTLOADER = [
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


class IpgStatusLegacy(ctypes.Structure):
	"""IpgStatus for legacy firmware (135 bytes). BootloaderVersion is a single byte."""
	_pack_ = 1
	_fields_ = (
		_IPG_STATUS_FIELDS_BEFORE_BOOTLOADER
		+ [('BootloaderVersion', ctypes.c_uint8)]
		+ _IPG_STATUS_FIELDS_AFTER_BOOTLOADER
	)


class IpgStatus(ctypes.Structure):
	"""IpgStatus for new firmware (138 bytes). BootloaderVersion is a 4-byte VersionStruct."""
	_pack_ = 1
	_fields_ = (
		_IPG_STATUS_FIELDS_BEFORE_BOOTLOADER
		+ [('BootloaderVersion', VersionStruct)]
		+ _IPG_STATUS_FIELDS_AFTER_BOOTLOADER
	)


class PatientInfoReadOnly(ctypes.Structure):
	PATIENT_INFO_CHARGER_ID_NUM = 2
	PATIENT_INFO_REMOTE_ID_NUM = 2
	PATIENT_INFO_REMOTE_ID_SIZE = 16
	_pack_ = 1
	_fields_ = [
		('DeviceId', DeviceId),
		('ChargerIds', DeviceId * PATIENT_INFO_CHARGER_ID_NUM),
		('RemoteIds', (ctypes.c_char * PATIENT_INFO_REMOTE_ID_SIZE) * PATIENT_INFO_REMOTE_ID_NUM),
	]


class PatientInfoReadWriteIpgParsable(ctypes.Structure):
	PATIENT_ID_SIZE = 32
	_pack_ = 1
	_fields_ = [
		('PatientId', ctypes.c_char * PATIENT_ID_SIZE),
	]


PATIENT_INFO_SIZE = 1500


class PatientInfoReadWrite(ctypes.Structure):
	BUFFER_SIZE = PATIENT_INFO_SIZE - ctypes.sizeof(PatientInfoReadWriteIpgParsable) - ctypes.sizeof(PatientInfoReadOnly)
	_pack_ = 1
	_fields_ = [
		('IpgParsableFields', PatientInfoReadWriteIpgParsable),
		('Buffer', ctypes.c_byte * BUFFER_SIZE),
	]


class PatientInfo(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ReadOnly', PatientInfoReadOnly),
		('ReadWrite', PatientInfoReadWrite),
	]


assert ctypes.sizeof(PatientInfo) == PATIENT_INFO_SIZE, "Wrong patient info size!"


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


class PseStatus(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('State', ctypes.c_uint8),
		('Amplitude', ctypes.c_uint16),
		('PercentComplete', ctypes.c_uint8),
	]


FittingSessionStatusNotification = PseStatus


OLD_PROGRAMMING_WORD_SIZE = ctypes.sizeof(ProgrammingWord)
NEW_PROGRAMMING_WORD_SIZE = 3136

_LEGACY_STATUS_SIZE = ctypes.sizeof(IpgStatusLegacy)  # 135
_NEW_STATUS_SIZE = ctypes.sizeof(IpgStatus)            # 138
_OLD_RESPONSE_SIZE = _LEGACY_STATUS_SIZE + OLD_PROGRAMMING_WORD_SIZE  # 987
_NEW_RESPONSE_SIZE = _NEW_STATUS_SIZE + NEW_PROGRAMMING_WORD_SIZE     # 3274


class InterrogationResponse:
	"""
	Flexible interrogation response that handles both firmware formats.

	Old firmware: IpgStatusLegacy (135 bytes) + AsicProgWord (852 bytes) = 987 bytes
	New firmware: IpgStatus (138 bytes) + full ProgrammingWord (3136 bytes) = 3274 bytes

	Format is detected automatically by response size.
	Both Status structs expose the same field names (ALCPVersion, ErrorStatus, etc.).
	"""

	def __init__(self, raw_bytes: bytes):
		if len(raw_bytes) < _LEGACY_STATUS_SIZE:
			raise ValueError(
				f"Response too small: {len(raw_bytes)} bytes, "
				f"need at least {_LEGACY_STATUS_SIZE} for IpgStatus"
			)

		if len(raw_bytes) <= _OLD_RESPONSE_SIZE:
			status_size = _LEGACY_STATUS_SIZE
			self.Status = IpgStatusLegacy.from_buffer_copy(raw_bytes[:status_size])
		else:
			status_size = _NEW_STATUS_SIZE
			self.Status = IpgStatus.from_buffer_copy(raw_bytes[:status_size])

		self.programming_word_bytes = raw_bytes[status_size:]

	@property
	def is_new_format(self) -> bool:
		return len(self.programming_word_bytes) == NEW_PROGRAMMING_WORD_SIZE

	@property
	def ProgrammingWord(self) -> ProgrammingWord:
		"""Backward-compatible access to the old-format ProgrammingWord struct (852 bytes).
		For new firmware, this parses the first 852 bytes of the 3136-byte programming word."""
		if len(self.programming_word_bytes) >= OLD_PROGRAMMING_WORD_SIZE:
			return ProgrammingWord.from_buffer_copy(
				self.programming_word_bytes[:OLD_PROGRAMMING_WORD_SIZE]
			)
		raise ValueError(
			f"Programming word too small: {len(self.programming_word_bytes)} bytes, "
			f"need at least {OLD_PROGRAMMING_WORD_SIZE}"
		)


LEADS_NUMBER = 16

IMPEDANCE_NUMBER = 17


class TherapySuspendConfig(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('EnableMagnet', ctypes.c_uint8, 1),
		('EnableChargerPulseTherapySuspension', ctypes.c_uint8, 1),
	]


class ImpedanceMeasurementResults(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ImpedanceResult', ctypes.c_uint32 * IMPEDANCE_NUMBER),
		('LeadsConfig', LeadsConfig)
	]


class ImageType(enum.IntEnum):
	TELEMET = 0
	PSE = 1


class BinImgSwapStatus(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("RestoreImgSetIsA", ctypes.c_uint8),
		("Reserved", ctypes.c_uint8*29),
		("Crc", ctypes.c_uint16),
	]


class PseImageMetadata(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		# PSE Specific fields
		("MmcuAddress", ctypes.c_uint32),
		("MmcuSize", ctypes.c_uint32),
		("SmcuLoadAddress", ctypes.c_uint32),
		("SmcuRunAddress", ctypes.c_uint32),
		("SmcuSize", ctypes.c_uint32),
		("PseCrc", ctypes.c_uint32),
	]


class BinImageRange(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("StartAddr", ctypes.c_uint32),
		("Size", ctypes.c_uint32),
	]


class BinaryImageMemoryMetadata(ctypes.Structure):
	MAX_RANGES = 10
	DESC_STR_MAX_SIZE = 128
	_pack_ = 1
	_fields_ = [
		# General Image Fields
		("Description", ctypes.c_char * DESC_STR_MAX_SIZE),
		("ImageCrc", ctypes.c_uint32),
		("NumRanges", ctypes.c_uint8),
		("Ranges", BinImageRange * MAX_RANGES),
		# PSE Specific fields
		("PseImageMetadata", PseImageMetadata),
	]


class BinaryImageMetadata(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		# General Image Fields
		("Version", ctypes.c_uint8),
		("ValidImageStored", ctypes.c_uint8),
		("Signature", ctypes.c_uint8 * 4),
		("Memory", BinaryImageMemoryMetadata),
		("Crc", ctypes.c_uint16),
	]


# TYPEDEFS
TIME_T = ctypes.c_uint32

class VERSION(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Major", ctypes.c_uint8),
        ("Middle", ctypes.c_uint8),
        ("Minor", ctypes.c_uint8),
        ("Branch", ctypes.c_uint8),
    ]


class BootloaderInfo(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("InBootloader", ctypes.c_uint8),               # offset 0, 1 byte
        ("BootloaderVersion", VERSION),                  # offset 1, 4 bytes
        ("EOL", ctypes.c_uint8),                        # offset 5, 1 byte
        ("InSafe", ctypes.c_uint8),                     # offset 6, 1 byte
        ("Mode", ctypes.c_uint8),                       # offset 7, 1 byte
        ("Battery", ctypes.c_uint16),                   # offset 8, 2 bytes
        ("Count", ctypes.c_uint16),                     # offset 10, 2 bytes
        ("DateTime", TIME_T),                           # offset 12, 4 bytes
        ("FirmwareVersions", VERSION * 8),              # offset 16, 32 bytes
        ("StatusCodes", ctypes.c_uint8 * 8),            # offset 48, 8 bytes
        ("Hash", ctypes.c_uint8 * 32),                  # offset 56, 32 bytes
        ("NrfFirmwareVersion", VERSION),                # offset 88, 4 bytes
    ]


class MacAddress(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("AddrIdPeer", ctypes.c_uint8, 1),   # 1 bit
        ("AddrType", ctypes.c_uint8, 7),     # 7 bits
        ("Addr", ctypes.c_uint8 * 6),
    ]

class VERSION_EXTENDED(ctypes.Structure):
    _fields_ = [
        ("Version", VERSION),
        ("Description", ctypes.c_uint8 * 32),
    ]

# GetNrfVersionAnswer
class NrfVersion(ctypes.Structure):
    _fields_ = [
        ("version", VERSION_EXTENDED),
    ]

class BootloaderStartParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("Hash", ctypes.c_uint8 * 32),
		("NrfFirmwareVersions", VersionStruct),
	]

class BtldProgressNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Progress', ctypes.c_uint8),
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


class TherapyStopProgressNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("Progress", ctypes.c_uint8),
	]


class ImpedanceProgressNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Progress', ctypes.c_uint8),
	]


class ImpedanceResultNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Progress', ctypes.c_uint8),
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


class SmcuUpdateNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("Progress", ctypes.c_uint8),
	]


class ExternalMemoryTestProgressNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("Progress", ctypes.c_uint8),
	]


class ExternalMemoryTestErrorNotification(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		("Address", ctypes.c_uint32),
	]


# region Wand Therapy Structures (matching iOS patient app protocol)

MAX_THERAPIES = 16
MAX_PROFILES_PER_SCHEDULE = 2
SCHEDULE_SLOTS_PER_DAY = 24
PROGRAM_COUNT = 6
SENSING_CHANNEL_COUNT = 10


class TherapyStatusEntry(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('ProgramId', ctypes.c_uint8),
		('TherapyId', ctypes.c_uint8),
		('IsOn', ctypes.c_uint8),
		('Amplitude', ctypes.c_uint16),
	]

	@property
	def is_on(self) -> bool:
		return (self.IsOn & 0x01) == 0x01

	@property
	def program_name(self) -> str:
		names = {0: 'None', 1: 'A', 2: 'B', 3: 'C', 4: 'D', 5: 'E', 6: 'F'}
		return names.get(self.ProgramId, f'0x{self.ProgramId:02X}')


class ProfileStatusEntry(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Id', ctypes.c_uint8),
		('CurrentProgram', ctypes.c_uint8),
	]

	@property
	def program_name(self) -> str:
		names = {0: 'None', 1: 'A', 2: 'B', 3: 'C', 4: 'D', 5: 'E', 6: 'F'}
		return names.get(self.CurrentProgram, f'0x{self.CurrentProgram:02X}')


class ScheduleStatus(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('IsAdaptativeActive', ctypes.c_uint8),
		('IsStimulationActive', ctypes.c_uint8),
		('CurrentProfile', ctypes.c_uint8),
	]

	@property
	def is_adaptative_active(self) -> bool:
		return (self.IsAdaptativeActive & 0x01) == 0x01

	@property
	def is_stimulation_active(self) -> bool:
		return (self.IsStimulationActive & 0x01) == 0x01


class GlobalTherapyStatus(ctypes.Structure):
	"""Response to O_GET_THERAPY_STATUS (0x1E) — 87 bytes."""
	_pack_ = 1
	_fields_ = [
		('Schedule', ScheduleStatus),
		('Profiles', ProfileStatusEntry * MAX_PROFILES_PER_SCHEDULE),
		('Therapies', TherapyStatusEntry * MAX_THERAPIES),
	]


class TherapyParamsEntry(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('IsEnabled', ctypes.c_uint8),
		('PatientCanEnable', ctypes.c_uint8),
		('PatientCanChangeAmplitude', ctypes.c_uint8),
		('AmplitudeLowerLimit', ctypes.c_uint16),
		('AmplitudeUpperLimit', ctypes.c_uint16),
	]

	@property
	def is_enabled(self) -> bool:
		return (self.IsEnabled & 0x01) == 0x01

	@property
	def patient_can_enable(self) -> bool:
		return (self.PatientCanEnable & 0x01) == 0x01

	@property
	def patient_can_change_amplitude(self) -> bool:
		return (self.PatientCanChangeAmplitude & 0x01) == 0x01


class ProfileParamsEntry(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('IsEnabled', ctypes.c_uint8),
		('Adaptative', ctypes.c_uint8),
	]


class ProgramParamsEntry(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('IsEnabled', ctypes.c_uint8),
		('ProfileId', ctypes.c_uint8),
		('IsProgramEligible', ctypes.c_uint8),
	]


class SensingChannelParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('Cathode', ctypes.c_uint8),
		('Anode', ctypes.c_uint8),
		('Gain', ctypes.c_uint8),
		('Enabled', ctypes.c_uint8),
	]

	@property
	def is_enabled(self) -> bool:
		return (self.Enabled & 0x01) == 0x01


class SensingParams(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('SamplingRate', ctypes.c_uint16),
		('Channels', SensingChannelParams * SENSING_CHANNEL_COUNT),
	]


class WandParams(ctypes.Structure):
	"""Response to O_GET_PARAMS (0x29)."""
	_pack_ = 1
	_fields_ = [
		('PatientCanChangeAdaptative', ctypes.c_uint8),
		('Slots', ctypes.c_uint8 * SCHEDULE_SLOTS_PER_DAY),
		('Profiles', ProfileParamsEntry * MAX_PROFILES_PER_SCHEDULE),
		('Programs', ProgramParamsEntry * PROGRAM_COUNT),
		('Therapies', TherapyParamsEntry * MAX_THERAPIES),
		('Sensing', SensingParams),
	]


class UpdateTherapyStatePayload(ctypes.Structure):
	_pack_ = 1
	_fields_ = [
		('TherapyId', ctypes.c_uint8),
		('ProgramId', ctypes.c_uint8),
	]

# endregion

# endregion

# endregion

# region Auxiliary


def print_bl_info(blinfo: BootloaderInfo):
	bl_ver = blinfo.BootloaderVersion
	telemet_ver = blinfo.FirmwareVersions[0]
	stim_ver = blinfo.FirmwareVersions[1]
	nrf_ver = blinfo.NrfFirmwareVersion
	lines = [
		'Bootloader Info:',
		f'InBootloader: {blinfo.InBootloader}',
		f'BootloaderVersion: {bl_ver.Major}.{bl_ver.Middle}.{bl_ver.Minor}.{bl_ver.Branch}',
		f'EOL: {blinfo.EOL}',
		f'InSafe: {blinfo.InSafe}',
		f'Mode: {blinfo.Mode}',
		f'Battery: {blinfo.Battery} mV',
		f'Count: {blinfo.Count}',
		f'TelemetFwVersion: {telemet_ver.Major}.{telemet_ver.Middle}.{telemet_ver.Minor}.{telemet_ver.Branch}',
		f'StimFwVersion: {stim_ver.Major}.{stim_ver.Middle}.{stim_ver.Minor}.{stim_ver.Branch}',
		f'NrfFwVersion: {nrf_ver.Major}.{nrf_ver.Middle}.{nrf_ver.Minor}.{nrf_ver.Branch}',
		f'Hash: {bytes(blinfo.Hash).hex()}',
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
			last_pse_response = payload[-1]
		except:
			last_pse_response = 0xFF
		pse_message = f" Last PSE response code 0x{last_pse_response:02X}" if error_code == NackCodes.SLAVE_NACK else None
		if message and pse_message:
			message = pse_message + message
		elif pse_message:
			message = pse_message
		return super(SierraAlcp, cls).process_nack_message(error_code, payload, nack_name, message, False, last_pse_response)

	# endregion

	# region Orders

	# def o_reset(self, full_reset: bool = True):
		# # full reset erases the Programming Word.
		# return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_RESET, full_reset.to_bytes(1, "little"))

	def o_reset(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_RESET)

	def o_set_session_role(self, role: SessionRole = SessionRole.PROGRAMMER) -> None:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_SESSION_ROLE, role.to_bytes(1, "little"))

	def o_set_display_mode(self, mode: DisplayMode = DisplayMode.BLINDED) -> None:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_SET_DISPLAY_MODE, mode.to_bytes(1, "little"))

	def o_get_loop_recorder_metadata(self) -> GetLoopRecorderMetadataResponse:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_LOOP_RECORDER_METADATA, response_struct=GetLoopRecorderMetadataResponse)

	def o_get_alcp_info(self) -> AlcpInfo:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_ALCP_INFO, response_struct=AlcpInfo)

	def o_get_time(self) -> GetTimeResponse:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_TIME, response_struct=GetTimeResponse)
	def o_start_therapy(self, stim_units: int):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_THERAPY, stim_units.to_bytes(1, byteorder='little'))
	def o_stop_therapy(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_THERAPY)

	def o_get_therapy_status(self) -> GlobalTherapyStatus:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_THERAPY_STATUS, response_struct=GlobalTherapyStatus)

	def o_get_params(self) -> WandParams:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_PARAMS, response_struct=WandParams)

	def o_turn_on_therapy(self, therapy_id: int, program_id: int):
		payload = UpdateTherapyStatePayload(TherapyId=therapy_id, ProgramId=program_id)
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_TURN_ON_THERAPY, payload)

	def o_turn_off_therapy(self, therapy_id: int, program_id: int):
		payload = UpdateTherapyStatePayload(TherapyId=therapy_id, ProgramId=program_id)
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_TURN_OFF_THERAPY, payload)

	def o_increase_therapy_amplitude(self, therapy_id: int, program_id: int):
		payload = UpdateTherapyStatePayload(TherapyId=therapy_id, ProgramId=program_id)
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_INCREASE_THERAPY_AMPLITUDE, payload)

	def o_decrease_therapy_amplitude(self, therapy_id: int, program_id: int):
		payload = UpdateTherapyStatePayload(TherapyId=therapy_id, ProgramId=program_id)
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_DECREASE_THERAPY_AMPLITUDE, payload)

	def o_activate_program(self, program_id: int):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_ACTIVATE_PROGRAM, program_id.to_bytes(1, 'little'))

	def o_enable_stimulation(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_ENABLE_STIMULATION)

	def o_disable_stimulation(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_DISABLE_STIMULATION)

	def o_enable_adaptative_mode(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_ENABLE_ADAPTATIVE_MODE)

	def o_disable_adaptative_mode(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_DISABLE_ADAPTATIVE_MODE)

	def o_turn_off(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_TURN_OFF)

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
		raw = self._send_alcp_order_receive_ans(AlcpOrderCodes.O_INTERROGATE)
		return InterrogationResponse(raw)

	def o_trigger_status_update(self):
		"""Request the IPG to push an IpgStatus notification on the STATUS channel (only has effect when subscribed)."""
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_TRIGGER_STATUS_UPDATE)

	def o_program(self, programming_word: ProgrammingWord):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_PROGRAM, programming_word)

	def o_load_programming_buffer(self, params: ProgrammingBufferParams):
		"""Send a single LoadProgrammingBuffer chunk to the device."""
		return self._send_alcp_order_receive_ans(
			AlcpOrderCodes.O_LOAD_PROGRAMMING_BUFFER,
			params,
			timeout=10,
		)

	def o_program_with_crc(self, crc: int):
		"""
		Send the Programming order with only a CRC (new-style).
		Used after LoadProgrammingBuffer chunks have been sent.
		The device distinguishes old vs new flow by payload size
		(852 bytes = old ProgrammingWord, 2 bytes = new CRC).
		"""
		payload = crc.to_bytes(2, 'little')
		return self._send_alcp_order_receive_ans(
			AlcpOrderCodes.O_PROGRAM,
			payload,
			timeout=30,
		)

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

	def o_start_looprec(self, loopRecChannelMask: int):
		payload = loopRecChannelMask.to_bytes(2, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_LOOP_REC, payload)

	def o_stop_looprec(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_LOOP_REC)

	def o_start_recording(self, seconds: int, source: int, loopRecChannelMask: int, discard_buffer: bool):
		payload = seconds.to_bytes(2, 'little') + source.to_bytes(1, 'little') + loopRecChannelMask.to_bytes(2, 'little') + discard_buffer.to_bytes(1, 'little')
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_START_RECORDING, payload)

	def o_stop_recording(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_STOP_RECORDING)

	def o_get_temperature(self) -> TemperatureMeasurement:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_TEMPERATURE, response_struct=TemperatureMeasurement)

	def o_get_accel_measure(self) -> AccelMeasurement:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_ACCEL_MEASURE, response_struct=AccelMeasurement)

	def o_get_bl_info(self) -> BootloaderInfo:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_BOOTLOADER_INFO, response_struct=BootloaderInfo)

	def o_get_mac_address(self) -> MacAddress:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_MAC_ADDRESS, response_struct=MacAddress)

	def o_get_nrf_version(self) -> NrfVersion:
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_GET_NRF_VERSION, response_struct=NrfVersion)

	def o_start_bootloader(self, fw_hash: bytes, nrf_fw_version: VersionStruct):
		payload = bytes(BootloaderStartParams((ctypes.c_uint8 * 32)(*fw_hash), nrf_fw_version))
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_BOOTLOADER_START, order_payload=payload)

	def o_flush_loop_recorder(self):
		return self._send_alcp_order_receive_ans(AlcpOrderCodes.O_FLUSH_LOOP_RECORDER)

	def program_from_buffer(self, programming_word: bytes, on_progress=None):
		"""
		Program the device using the LoadProgrammingBuffer + Programming sequence.
		Replicates the C# SierraModel.Programming() flow:
		  1. Chunk the programming word into 1024-byte segments
		  2. Send each chunk via LoadProgrammingBuffer (0x32)
		  3. Send Programming (0x04) with CRC-16-CCITT of the full buffer

		Args:
			programming_word: Raw programming word bytes to send.
			on_progress: Optional callback(chunk_num, total_chunks, load_index, load_size)
			             called after each chunk is acknowledged.
		"""
		chunk_size = PROGRAMMING_BUFFER_CHUNK_SIZE
		total_chunks = (len(programming_word) + chunk_size - 1) // chunk_size
		offset = 0

		for chunk_num in range(total_chunks):
			load_size = min(chunk_size, len(programming_word) - offset)

			chunk_data = bytearray(chunk_size)
			chunk_data[:load_size] = programming_word[offset:offset + load_size]

			params = ProgrammingBufferParams(
				LoadIndex=offset,
				LoadSize=load_size,
				Buffer=(ctypes.c_uint8 * chunk_size)(*chunk_data),
			)

			self.o_load_programming_buffer(params)

			if on_progress:
				on_progress(chunk_num + 1, total_chunks, offset, load_size)

			offset += chunk_size

		crc = crc16_ccitt(programming_word)
		self.o_program_with_crc(crc)

	# endregion

	# region Notifications

	def subscribe_to_status(self, callback: Callable, send_order: bool = True):
		status_cls = IpgStatusLegacy if self.firmware == "legacy" else IpgStatus
		return self._subscribe_to_channel_notifications(Channels.STATUS, callback, status_cls, send_order, True)

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

	#o_start_bootloader
	#o_btld_verify_finish
	#search_and_connect
	#reconnect
	#clear_notification_callbacks

	def __test(self):
		pass


if __name__ == '__main__':

	from address import address

	with SierraAlcp(address) as ipg:
		ipg.connect()
		pass

