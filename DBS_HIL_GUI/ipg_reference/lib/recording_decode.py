import os
import re


BUFFER_SIZE = 128 * 1024


def enabled_channels_from_mask(channel_bit):
    return [i + 1 for i in range(32) if (channel_bit >> i) & 1]


def parse_channel_mask_from_name(path):
    match = re.search(r"chMask_([0-9A-Fa-f]+)", os.path.basename(path))
    if not match:
        return None
    return int(match.group(1), 16)


def bigram_to_sample(bigram):
    max_value = 2**14  # 14-bit ADC
    mask_neg_bit = 0x2000  # 14th bit is the sign bit
    # Keep only the lower 6 bits of the first byte (drop top 2 bits).
    a = ((bigram[0] & 0x3F) << 8) | bigram[1]
    # Two's-complement sign conversion for 14-bit values.
    if a & mask_neg_bit:
        return a - max_value
    return a


def chunk_to_samples(chunk_data):
    """
        Each 4 bytes become 2 samples (older sample first).
        Raw bytes:
        | 01 02 | 03 04 | 05 06 | 07 08 | 09 0A | 0B 0C |

        Group by 4 bytes:
        [01 02 03 04] -> samples: (03 04), (01 02)
        [05 06 07 08] -> samples: (07 08), (05 06)
        [09 0A 0B 0C] -> samples: (0B 0C), (09 0A)

        Sample stream:
        A=(03 04), B=(01 02), C=(07 08), D=(05 06), E=(0B 0C), F=(09 0A)
    """
    samples = []
    usable_len = len(chunk_data) - (len(chunk_data) % 4)
    for i in range(0, usable_len, 4):
        # Process older sample, then newer sample
        s1 = bigram_to_sample(chunk_data[i + 2 : i + 4])
        s2 = bigram_to_sample(chunk_data[i : i + 2])
        samples.append(s1)
        samples.append(s2)
    return samples


class RecordingDecoder:
    def __init__(self, num_channels, buffer_size=BUFFER_SIZE):
        self.num_channels = num_channels
        self.buffer_size = buffer_size
        self.first_chunk_address = None
        self.last_buffer_address = None
        self.wrap_offset = 0
        self.odd_start_dropped = False
        self.phase_offset = 0

    def process_chunk(self, address, chunk_data, pad_samples=0, pad_front=False):
        """ 
            The first chunk has to start with an even address. 
            Drop the first sample if the first chunk address is odd.
            Then the data stream (after chunk_to_samples()) is de-interleaved into channels.
            The start channel rotates based on start_channel_idx. 
            Example below assumes start_channel_idx == 0 for a three channel recording with Ch1, Ch2, Ch3.
            Samples:  A   B   C   D   E   F
            Ch1:      A           D
            Ch2:          B           E
            Ch3:              C           F
        """
        samples = chunk_to_samples(chunk_data)
        if pad_samples > 0:
            if pad_front:
                samples = ([0] * pad_samples) + samples
            else:
                samples.extend([0] * pad_samples)
        current_address = address

        if (
            self.last_buffer_address is not None
            and current_address < self.last_buffer_address
        ):
            self.wrap_offset += self.buffer_size
        self.last_buffer_address = current_address
        virtual_address = current_address + self.wrap_offset

        if self.first_chunk_address is None:
            if (virtual_address % 2) == 1 and not self.odd_start_dropped:
                if samples:
                    samples = samples[1:]
                    virtual_address += 1
                    self.odd_start_dropped = True
            self.first_chunk_address = virtual_address

        start_channel_idx = (
            virtual_address - self.first_chunk_address + self.phase_offset
        ) % self.num_channels
        channel_lists = [[] for _ in range(self.num_channels)]
        for i, s in enumerate(samples):
            ch_idx = (start_channel_idx + i) % self.num_channels
            channel_lists[ch_idx].append(s)

        return channel_lists

    def apply_skip_samples(self, skipped_samples):
        if skipped_samples <= 0:
            return
        self.phase_offset = (self.phase_offset + skipped_samples) % self.num_channels
