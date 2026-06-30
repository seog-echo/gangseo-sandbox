"""
Utilities for loading and creating .mips programming word files.

.mips format (hex-encoded ASCII, no line breaks):
    [1B model] [3B dummy] [1B alcp_ver_hi] [1B alcp_ver_lo]
    [NB programming_word]
    [2B crc16_ccitt LE]
"""

MIPS_HEADER_SIZE = 6
MIPS_CRC_SIZE = 2


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16-CCITT matching the C# CCC.Utility.CRC_ITT() extension method."""
    msb = (init >> 8) & 0xFF
    lsb = init & 0xFF
    for c in data:
        x = c ^ msb
        x ^= (x >> 4)
        msb = (lsb ^ (x >> 3) ^ (x << 4)) & 0xFF
        lsb = (x ^ (x << 5)) & 0xFF
    return (msb << 8) + lsb


def parse_mips_file(filepath: str) -> dict:
    """
    Parse a .mips file, validate its CRC, and return the header fields
    alongside the programming word bytes.

    Raises ValueError on files that are too small or have a CRC mismatch.
    """
    with open(filepath, 'r') as f:
        hex_str = f.read().strip()

    raw = bytes.fromhex(hex_str)

    if len(raw) < MIPS_HEADER_SIZE + MIPS_CRC_SIZE:
        raise ValueError(f"File too small: {len(raw)} bytes")

    stored_crc = int.from_bytes(raw[-MIPS_CRC_SIZE:], 'little')
    computed_crc = crc16_ccitt(raw[:-MIPS_CRC_SIZE])

    if stored_crc != computed_crc:
        raise ValueError(
            f"CRC mismatch: stored=0x{stored_crc:04X}, computed=0x{computed_crc:04X}"
        )

    return {
        'model': raw[0],
        'alcp_version_high': raw[4],
        'alcp_version_low': raw[5],
        'programming_word': raw[MIPS_HEADER_SIZE:-MIPS_CRC_SIZE],
        'crc': stored_crc,
    }


def load_mips_file(filepath: str) -> bytes:
    """
    Load a .mips file and return the raw programming word bytes.
    Validates the file CRC before returning.
    """
    return parse_mips_file(filepath)['programming_word']


def save_mips_file(filepath: str, programming_word: bytes,
                   model: int, alcp_version_high: int,
                   alcp_version_low: int):
    """
    Save a programming word to a .mips file with the standard header and CRC.
    """
    header = bytes((model, 0x00, 0x00, 0x00, alcp_version_high, alcp_version_low))
    data = header + programming_word
    crc = crc16_ccitt(data)
    data += crc.to_bytes(2, 'little')
    with open(filepath, 'w') as f:
        f.write(data.hex().upper())
