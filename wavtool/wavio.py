"""Strict, streaming-friendly PCM WAV reader/writer.

Only local files are used. No audio servers, no cloud APIs, no external
services. All samples are exchanged with the DSP layer as Python floats
normalized to [-1.0, 1.0]; quantization with explicit saturation happens
only at write time.
"""

from __future__ import annotations

import array
import struct
import sys
from dataclasses import dataclass


class WavFormatError(Exception):
    """Raised when a WAV file fails strict header/data validation."""


SUPPORTED_BITS = (8, 16, 24, 32)
MAX_CHANNELS = 8


@dataclass
class WavInfo:
    channels: int
    sample_rate: int
    bits_per_sample: int
    num_frames: int
    data_size: int
    data_offset: int

    @property
    def block_align(self) -> int:
        return self.channels * (self.bits_per_sample // 8)

    @property
    def duration(self) -> float:
        return self.num_frames / self.sample_rate


def parse_header(f) -> WavInfo:
    """Parse and strictly validate a RIFF/WAVE header from file object `f`.

    Raises WavFormatError on any inconsistency. On success the file is
    positioned at the start of the PCM data chunk.
    """
    riff = f.read(12)
    if len(riff) < 12:
        raise WavFormatError("file too short for RIFF header")
    if riff[0:4] != b"RIFF":
        raise WavFormatError("missing RIFF chunk id")
    (riff_size,) = struct.unpack("<I", riff[4:8])
    if riff[8:12] != b"WAVE":
        raise WavFormatError("missing WAVE form type")

    pos = f.tell()
    f.seek(0, 2)
    file_size = f.tell()
    f.seek(pos)
    if riff_size + 8 > file_size:
        raise WavFormatError(
            f"RIFF size {riff_size + 8} exceeds actual file size {file_size}"
        )

    fmt = None
    data_size = None
    data_offset = None
    # Walk chunks until both fmt and data are found.
    while f.tell() + 8 <= min(file_size, riff_size + 8):
        hdr = f.read(8)
        if len(hdr) < 8:
            raise WavFormatError("truncated chunk header")
        chunk_id = hdr[0:4]
        (chunk_size,) = struct.unpack("<I", hdr[4:8])
        payload_pos = f.tell()
        if payload_pos + chunk_size > file_size:
            raise WavFormatError(
                f"chunk {chunk_id!r} size {chunk_size} exceeds file size"
            )
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise WavFormatError("fmt chunk too small")
            fmt = struct.unpack("<HHIIHH", f.read(16))
        elif chunk_id == b"data":
            data_size = chunk_size
            data_offset = payload_pos
        # Chunks are word-aligned: skip payload plus optional pad byte.
        f.seek(payload_pos + chunk_size + (chunk_size & 1))
        if fmt is not None and data_size is not None:
            break

    if fmt is None:
        raise WavFormatError("missing fmt chunk")
    if data_size is None:
        raise WavFormatError("missing data chunk")

    audio_format, channels, sample_rate, byte_rate, block_align, bits = fmt
    if audio_format != 1:
        raise WavFormatError(
            f"unsupported audio format {audio_format} (only integer PCM=1)"
        )
    if not 1 <= channels <= MAX_CHANNELS:
        raise WavFormatError(f"unsupported channel count {channels}")
    if sample_rate <= 0:
        raise WavFormatError(f"invalid sample rate {sample_rate}")
    if bits not in SUPPORTED_BITS:
        raise WavFormatError(f"unsupported bit depth {bits}")
    expected_align = channels * (bits // 8)
    if block_align != expected_align:
        raise WavFormatError(
            f"block_align {block_align} != channels*bytes_per_sample {expected_align}"
        )
    if byte_rate != sample_rate * block_align:
        raise WavFormatError(
            f"byte_rate {byte_rate} != sample_rate*block_align "
            f"{sample_rate * block_align}"
        )
    if data_size % block_align != 0:
        raise WavFormatError(
            f"data size {data_size} not a multiple of block_align {block_align}"
        )

    num_frames = data_size // block_align
    f.seek(data_offset)
    return WavInfo(
        channels=channels,
        sample_rate=sample_rate,
        bits_per_sample=bits,
        num_frames=num_frames,
        data_size=data_size,
        data_offset=data_offset,
    )


def _decode_block(raw: bytes, info: WavInfo) -> list[list[float]]:
    """Decode raw PCM bytes into a list of frames (lists of floats)."""
    ch = info.channels
    bits = info.bits_per_sample
    frames = []
    if bits == 16:
        a = array.array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        scale = 1.0 / 32768.0
        for i in range(0, len(a), ch):
            frames.append([a[i + c] * scale for c in range(ch)])
    elif bits == 8:
        for i in range(0, len(raw), ch):
            frames.append([(raw[i + c] - 128) / 128.0 for c in range(ch)])
    elif bits == 32:
        a = array.array("i")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        scale = 1.0 / 2147483648.0
        for i in range(0, len(a), ch):
            frames.append([a[i + c] * scale for c in range(ch)])
    elif bits == 24:
        n = len(raw)
        idx = 0
        while idx < n:
            frame = []
            for _ in range(ch):
                v = raw[idx] | (raw[idx + 1] << 8) | (raw[idx + 2] << 16)
                if v & 0x800000:
                    v -= 1 << 24
                frame.append(v / 8388608.0)
                idx += 3
            frames.append(frame)
    return frames


class WavReader:
    """Streaming reader: yields blocks of frames without loading the file."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "rb")
        self.info = parse_header(self._f)
        self.frames_read = 0

    def __enter__(self) -> "WavReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._f.close()

    def read_block(self, num_frames: int) -> list[list[float]]:
        """Read up to `num_frames` frames; returns [] at end of stream."""
        remaining = self.info.num_frames - self.frames_read
        if remaining <= 0:
            return []
        n = min(num_frames, remaining)
        raw = self._f.read(n * self.info.block_align)
        got = len(raw) // self.info.block_align
        if got < n:
            raise WavFormatError(
                f"unexpected end of data: wanted {n} frames, got {got}"
            )
        self.frames_read += got
        return _decode_block(raw, self.info)


def clip_unit(x: float) -> float:
    """Saturate a float sample to the [-1.0, 1.0] range."""
    if x > 1.0:
        return 1.0
    if x < -1.0:
        return -1.0
    return x


def quantize(x: float, bits: int) -> int:
    """Float [-1,1] -> integer PCM with explicit clipping/saturation."""
    x = clip_unit(x)
    # Scale by 2^(bits-1) so that -1.0 saturates to the most negative
    # code (e.g. -32768 for 16-bit); positive full scale clips at 2^(n-1)-1.
    if bits == 16:
        q = int(round(x * 32768.0))
        return max(-32768, min(32767, q))
    if bits == 8:
        q = int(round(x * 128.0))
        return max(-128, min(127, q))
    if bits == 24:
        q = int(round(x * 8388608.0))
        return max(-8388608, min(8388607, q))
    if bits == 32:
        q = int(round(x * 2147483648.0))
        return max(-2147483648, min(2147483647, q))
    raise ValueError(f"unsupported bit depth {bits}")


def _encode_block(frames: list[list[float]], bits: int) -> bytes:
    if bits == 16:
        a = array.array("h", (quantize(x, 16) for f in frames for x in f))
        if sys.byteorder == "big":
            a.byteswap()
        return a.tobytes()
    if bits == 32:
        a = array.array("i", (quantize(x, 32) for f in frames for x in f))
        if sys.byteorder == "big":
            a.byteswap()
        return a.tobytes()
    if bits == 8:
        return bytes((quantize(x, 8) + 128) for f in frames for x in f)
    if bits == 24:
        out = bytearray()
        for f in frames:
            for x in f:
                q = quantize(x, 24) & 0xFFFFFF
                out += bytes((q & 0xFF, (q >> 8) & 0xFF, (q >> 16) & 0xFF))
        return bytes(out)
    raise ValueError(f"unsupported bit depth {bits}")


class WavWriter:
    """Streaming writer; header sizes are patched on close()."""

    def __init__(self, path: str, channels: int, sample_rate: int, bits: int = 16):
        if bits not in SUPPORTED_BITS:
            raise ValueError(f"unsupported bit depth {bits}")
        self.path = path
        self.channels = channels
        self.sample_rate = sample_rate
        self.bits = bits
        self.frames_written = 0
        self._f = open(path, "wb")
        self._write_header(0)  # placeholder, patched on close

    def __enter__(self) -> "WavWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def block_align(self) -> int:
        return self.channels * (self.bits // 8)

    def _write_header(self, data_size: int) -> None:
        byte_rate = self.sample_rate * self.block_align
        self._f.seek(0)
        self._f.write(b"RIFF")
        self._f.write(struct.pack("<I", 36 + data_size))
        self._f.write(b"WAVE")
        self._f.write(b"fmt ")
        self._f.write(struct.pack("<I", 16))
        self._f.write(
            struct.pack(
                "<HHIIHH",
                1,  # PCM
                self.channels,
                self.sample_rate,
                byte_rate,
                self.block_align,
                self.bits,
            )
        )
        self._f.write(b"data")
        self._f.write(struct.pack("<I", data_size))

    def write_block(self, frames: list[list[float]]) -> None:
        if not frames:
            return
        for f in frames:
            if len(f) != self.channels:
                raise ValueError(
                    f"frame has {len(f)} channels, writer expects {self.channels}"
                )
        data = _encode_block(frames, self.bits)
        self._f.seek(0, 2)
        self._f.write(data)
        self.frames_written += len(frames)

    def close(self) -> None:
        if self._f.closed:
            return
        data_size = self.frames_written * self.block_align
        self._write_header(data_size)
        self._f.close()
