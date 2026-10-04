"""Strict PCM WAV reading/writing using only the standard library.

Supported subset:
  * RIFF/WAVE container (little-endian)
  * audio format 1 (uncompressed PCM)
  * 1 (mono) or 2 (stereo) channels
  * 16-bit signed samples

Header and data lengths are validated strictly; any inconsistency raises
WavError. Samples are stored in ``array('h')`` (interleaved int16).
"""

from __future__ import annotations

import os
import struct
from array import array
from dataclasses import dataclass


class WavError(Exception):
    """Raised when a WAV file is malformed or uses an unsupported feature."""


PCM_FORMAT = 1
SUPPORTED_BITS = 16
SUPPORTED_CHANNELS = (1, 2)
MAX_CHANNELS = 2


@dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    num_frames: int          # frames = samples per channel
    data_offset: int         # byte offset of the data chunk payload
    data_size: int           # byte length of the data chunk payload

    @property
    def block_align(self) -> int:
        return self.channels * (self.bits_per_sample // 8)

    @property
    def duration(self) -> float:
        return self.num_frames / self.sample_rate


def _read_exact(fh, n: int, what: str) -> bytes:
    buf = fh.read(n)
    if len(buf) != n:
        raise WavError(f"unexpected end of file while reading {what} "
                       f"(wanted {n} bytes, got {len(buf)})")
    return buf


def read_wav_header(fh, file_size: int | None = None) -> WavInfo:
    """Parse and strictly validate a WAV header from a binary file object.

    On success the file object is positioned at the start of the data
    chunk payload. Raises WavError on any inconsistency.
    """
    if file_size is None:
        try:
            file_size = os.fstat(fh.fileno()).st_size
        except (OSError, AttributeError):
            pos = fh.tell()
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            fh.seek(pos)

    riff = _read_exact(fh, 12, "RIFF header")
    riff_id, riff_size, wave_id = struct.unpack("<4sI4s", riff)
    if riff_id != b"RIFF":
        raise WavError(f"not a RIFF file (magic={riff_id!r})")
    if wave_id != b"WAVE":
        raise WavError(f"not a WAVE file (form type={wave_id!r})")
    if riff_size < 4:
        raise WavError(f"invalid RIFF size {riff_size}")
    if riff_size + 8 > file_size:
        raise WavError(
            f"RIFF size {riff_size} exceeds file size {file_size} - 8")

    fmt_seen = False
    sample_rate = channels = bits = block_align = 0
    data_offset = data_size = -1
    riff_end = 8 + riff_size

    while fh.tell() + 8 <= riff_end:
        chunk_header = _read_exact(fh, 8, "chunk header")
        chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
        payload_offset = fh.tell()
        chunk_end = payload_offset + chunk_size
        if chunk_end > riff_end or chunk_end > file_size:
            raise WavError(
                f"chunk {chunk_id!r} size {chunk_size} overruns file")

        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise WavError(f"fmt chunk too small ({chunk_size} bytes)")
            fmt = _read_exact(fh, 16, "fmt chunk")
            (audio_format, channels, sample_rate, byte_rate,
             block_align, bits) = struct.unpack("<HHIIHH", fmt)
            if audio_format != PCM_FORMAT:
                raise WavError(
                    f"unsupported audio format {audio_format} "
                    f"(only uncompressed PCM=1 is supported)")
            if channels not in SUPPORTED_CHANNELS:
                raise WavError(
                    f"unsupported channel count {channels} "
                    f"(only mono/stereo are supported)")
            if bits != SUPPORTED_BITS:
                raise WavError(
                    f"unsupported bit depth {bits} (only 16-bit PCM)")
            expected_align = channels * (bits // 8)
            if block_align != expected_align:
                raise WavError(
                    f"block align {block_align} != {expected_align} "
                    f"(channels * bytes_per_sample)")
            if sample_rate <= 0:
                raise WavError(f"invalid sample rate {sample_rate}")
            if byte_rate != sample_rate * block_align:
                raise WavError(
                    f"byte rate {byte_rate} != "
                    f"{sample_rate * block_align} (rate * block_align)")
            fmt_seen = True
        elif chunk_id == b"data":
            if not fmt_seen:
                raise WavError("data chunk appears before fmt chunk")
            data_offset = payload_offset
            data_size = chunk_size
            # Data is the last chunk we care about; stop scanning.
            fh.seek(payload_offset)
            break

        # Chunks are padded to even sizes.
        fh.seek(chunk_end + (chunk_size & 1))
    else:
        raise WavError("no data chunk found")

    if not fmt_seen:
        raise WavError("no fmt chunk found")
    if data_size % block_align != 0:
        raise WavError(
            f"data size {data_size} is not a multiple of "
            f"block align {block_align}")
    if data_offset + data_size > file_size:
        raise WavError(
            f"data chunk ({data_size} bytes at offset {data_offset}) "
            f"extends past end of file ({file_size} bytes)")

    return WavInfo(
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        num_frames=data_size // block_align,
        data_offset=data_offset,
        data_size=data_size,
    )


def read_wav(path: str) -> tuple[WavInfo, array]:
    """Read an entire WAV file into an interleaved int16 array."""
    with open(path, "rb") as fh:
        info = read_wav_header(fh)
        raw = _read_exact(fh, info.data_size, "sample data")
    samples = array("h")
    samples.frombytes(raw)
    if struct.unpack("=H", b"\x01\x00")[0] != 1:  # big-endian host
        samples.byteswap()
    return info, samples


class WavWriter:
    """Streaming WAV writer; header sizes are patched on close().

    Only one in-progress file is kept open at a time, so arbitrarily
    large outputs can be produced block by block.
    """

    def __init__(self, path: str, sample_rate: int, channels: int):
        if channels not in SUPPORTED_CHANNELS:
            raise WavError(f"unsupported channel count {channels}")
        if sample_rate <= 0:
            raise WavError(f"invalid sample rate {sample_rate}")
        self._path = path
        self._sample_rate = sample_rate
        self._channels = channels
        self._data_size = 0
        self._closed = False
        self._fh = open(path, "wb")
        block_align = channels * 2
        self._fh.write(struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 0,                 # patched on close
            b"WAVE",
            b"fmt ", 16,
            PCM_FORMAT, channels, sample_rate,
            sample_rate * block_align, block_align, SUPPORTED_BITS,
            b"data", 0,                 # patched on close
        ))

    def write(self, samples: array) -> None:
        """Append interleaved int16 samples (array('h'))."""
        if self._closed:
            raise WavError("write on closed WavWriter")
        if samples.typecode != "h":
            raise TypeError("samples must be array('h')")
        if len(samples) % self._channels != 0:
            raise WavError("sample count is not a multiple of channels")
        buf = samples.tobytes()
        if struct.unpack("=H", b"\x01\x00")[0] != 1:
            tmp = array("h", samples)
            tmp.byteswap()
            buf = tmp.tobytes()
        self._fh.write(buf)
        self._data_size += len(buf)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fh.seek(4)
        self._fh.write(struct.pack("<I", 36 + self._data_size))
        self._fh.seek(40)
        self._fh.write(struct.pack("<I", self._data_size))
        self._fh.close()

    def __enter__(self) -> "WavWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_wav(path: str, sample_rate: int, channels: int,
              samples: array) -> None:
    """Write a complete interleaved int16 sample array to a WAV file."""
    with WavWriter(path, sample_rate, channels) as writer:
        writer.write(samples)
