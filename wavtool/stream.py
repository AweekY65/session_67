"""Chunked (streaming) WAV processing pipeline.

Processes a WAV file block by block so that arbitrarily large files can
be handled with bounded memory: at most one block, the FIR tail and the
resampler carry are kept in memory. No complete copy of the input or
output is ever held in RAM.

Pipeline order (all in float64 to avoid integer overflow):
    read block -> trim window -> channel mix -> FIR low-pass
    -> gain -> fade in/out -> linear resample -> saturate -> write
"""

from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass

from . import dsp
from .wavio import WavError, WavWriter, read_wav_header

DEFAULT_BLOCK_FRAMES = 65536


@dataclass
class Pipeline:
    """Configuration for one streaming processing run."""
    gain_db: float | None = None
    out_channels: int | None = None        # 1 or 2; None keeps input
    trim_start: float = 0.0                # seconds
    trim_end: float | None = None          # seconds; None = to EOF
    fade_in: float = 0.0                   # seconds
    fade_out: float = 0.0                  # seconds
    lowpass_cutoff: float | None = None    # Hz; designs windowed-sinc
    lowpass_coeffs: list | None = None     # explicit FIR coefficients
    lowpass_taps: int = 101                # taps when designing
    out_rate: int | None = None            # output sample rate; None = same


class _StreamingFIR:
    """Causal FIR identical to dsp.fir_filter, evaluated block by block."""

    def __init__(self, coeffs, channels):
        self._coeffs = coeffs
        self._channels = channels
        self._n_taps = len(coeffs)
        # Per-channel history of the last n_taps-1 input frames.
        self._tail = [[0.0] * (self._n_taps - 1)
                      for _ in range(channels)]

    def process(self, block: list) -> list:
        n_frames = len(block) // self._channels
        out = [0.0] * len(block)
        for c in range(self._channels):
            hist = self._tail[c] + block[c::self._channels]
            ochan = [0.0] * n_frames
            offset = self._n_taps - 1
            for i in range(n_frames):
                acc = 0.0
                hi = i + offset
                for k in range(self._n_taps):
                    acc += self._coeffs[k] * hist[hi - k]
                ochan[i] = acc
            out[c::self._channels] = ochan
            self._tail[c] = hist[-(self._n_taps - 1):] if self._n_taps > 1 \
                else []
        return out


class _StreamingResampler:
    """Linear-interpolation resampler identical to dsp.resample_linear."""

    def __init__(self, rate_in: int, rate_out: int, channels: int):
        self._step = rate_in / rate_out
        self._channels = channels
        self._pos = 0.0          # global input position of next output
        self._consumed = 0       # global index of the next input frame
        self._carry: list = []   # last frame of the previous block
        self._emitted = 0

    def _emit_from(self, buf: list, g0: int, last_avail: int,
                   out: list) -> None:
        """Emit every output whose source position is computable from
        buf. ``last_avail`` is the global index of the last frame in
        buf. A position strictly inside is interpolated; a position
        exactly on the last frame is emitted as-is; anything beyond
        waits for the next block (or flush())."""
        ch = self._channels
        while True:
            idx = int(self._pos)
            if idx > last_avail:
                break
            frac = self._pos - idx
            rel = idx - g0
            if rel < 0:
                rel = 0
                idx = g0
                frac = 0.0
            if frac == 0.0:
                base = rel * ch
                for c in range(ch):
                    out.append(buf[base + c])
            elif idx < last_avail:
                base = rel * ch
                for c in range(ch):
                    a = buf[base + c]
                    out.append(a + frac * (buf[base + ch + c] - a))
            else:
                break  # needs the next input frame to interpolate
            self._pos += self._step
            self._emitted += 1

    def process(self, block: list) -> list:
        n_frames = len(block) // self._channels
        buf = self._carry + block
        g0 = self._consumed - len(self._carry) // self._channels
        out: list = []
        # Interpolate only where both neighbours are inside buf.
        self._emit_from(buf, g0, g0 + len(buf) // self._channels - 1, out)
        self._carry = buf[-self._channels:] if self._channels else []
        self._consumed += n_frames
        return out

    def flush(self, total_in_frames: int) -> list:
        """Emit the remaining outputs so the total count matches
        dsp.resample_length(total_in_frames, ...)."""
        rate_ratio = 1.0 / self._step
        total_out = int(round(total_in_frames * rate_ratio))
        remaining = total_out - self._emitted
        out: list = []
        if remaining > 0:
            last = self._carry
            for _ in range(remaining):
                idx = int(self._pos)
                if idx >= total_in_frames - 1:
                    out.extend(last)
                else:
                    # Should not happen after process(), but stay safe.
                    out.extend(last)
                self._pos += self._step
                self._emitted += 1
        return out


def _frames_to_float_array(raw: bytes) -> list:
    ints = array("h")
    ints.frombytes(raw)
    if struct.unpack("=H", b"\x01\x00")[0] != 1:
        ints.byteswap()
    return dsp.int16_to_float(ints)


def process_file(src_path: str, dst_path: str, pipeline: Pipeline,
                 block_frames: int = DEFAULT_BLOCK_FRAMES) -> dict:
    """Run the pipeline from src_path to dst_path in blocks.

    Returns a dict of numeric statistics (frame counts, peak level,
    number of saturated samples).
    """
    with open(src_path, "rb") as fh:
        info = read_wav_header(fh)
        rate_in = info.sample_rate
        ch_in = info.channels

        # --- trim window in input frames ------------------------------
        start_frame = max(0, min(info.num_frames,
                                 int(round(pipeline.trim_start * rate_in))))
        if pipeline.trim_end is None:
            end_frame = info.num_frames
        else:
            end_frame = max(start_frame, min(
                info.num_frames,
                int(round(pipeline.trim_end * rate_in))))
        total_frames = end_frame - start_frame

        ch_work = pipeline.out_channels or ch_in
        if ch_work not in (1, 2):
            raise WavError(f"unsupported output channels {ch_work}")

        # --- stage setup ----------------------------------------------
        coeffs = pipeline.lowpass_coeffs
        if coeffs is None and pipeline.lowpass_cutoff is not None:
            coeffs = dsp.design_lowpass(pipeline.lowpass_cutoff, rate_in,
                                        pipeline.lowpass_taps)
        fir = _StreamingFIR(coeffs, ch_work) if coeffs else None

        gain = (dsp.db_to_linear(pipeline.gain_db)
                if pipeline.gain_db is not None else None)

        fade_in_frames = min(total_frames,
                             int(round(pipeline.fade_in * rate_in)))
        fade_out_frames = min(total_frames,
                              int(round(pipeline.fade_out * rate_in)))

        rate_out = pipeline.out_rate or rate_in
        resampler = (_StreamingResampler(rate_in, rate_out, ch_work)
                     if rate_out != rate_in else None)

        stats = {
            "sample_rate_in": rate_in,
            "sample_rate_out": rate_out,
            "channels_in": ch_in,
            "channels_out": ch_work,
            "frames_in": total_frames,
            "frames_out": 0,
            "peak_float": 0.0,
            "clipped_samples": 0,
        }

        fh.seek(info.data_offset + start_frame * info.block_align)
        frames_left = total_frames
        frame_index = 0

        with WavWriter(dst_path, rate_out, ch_work) as writer:
            while frames_left > 0:
                n = min(block_frames, frames_left)
                raw = fh.read(n * info.block_align)
                if len(raw) != n * info.block_align:
                    raise WavError("unexpected end of data chunk")
                frames_left -= n

                block = _frames_to_float_array(raw)

                if ch_work != ch_in:
                    block = dsp.mix_channels(block, ch_in, ch_work)
                if fir is not None:
                    block = fir.process(block)
                if gain is not None:
                    block = dsp.apply_gain(block, gain)

                # fades need the global frame index inside the trim window
                if fade_in_frames or fade_out_frames:
                    for local in range(n):
                        g = frame_index + local
                        factor = 1.0
                        if g < fade_in_frames:
                            factor *= g / fade_in_frames
                        out_from_end = total_frames - 1 - g
                        if out_from_end < fade_out_frames:
                            factor *= out_from_end / fade_out_frames
                        if factor != 1.0:
                            base = local * ch_work
                            for c in range(ch_work):
                                block[base + c] *= factor
                frame_index += n

                if resampler is not None:
                    block = resampler.process(block)

                _emit_block(writer, block, stats, ch_work)

            if resampler is not None:
                tail = resampler.flush(total_frames)
                _emit_block(writer, tail, stats, ch_work)

    return stats


def _emit_block(writer: WavWriter, block: list, stats: dict,
                channels: int) -> None:
    if not block:
        return
    ints = dsp.float_to_int16(block)
    peak = stats["peak_float"]
    clipped = stats["clipped_samples"]
    for v in block:
        av = abs(v)
        if av > peak:
            peak = av
        if v * dsp._SCALE > dsp.INT16_MAX + 0.5 or \
                v * dsp._SCALE < dsp.INT16_MIN - 0.5:
            clipped += 1
    stats["peak_float"] = peak
    stats["clipped_samples"] = clipped
    stats["frames_out"] += len(block) // channels
    writer.write(ints)
