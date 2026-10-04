"""Streaming DSP stages.

A "block" is a list of frames; a frame is a list of floats (one per
channel) normalized to [-1.0, 1.0]. Every stage implements:

    process(frames) -> frames
    flush()         -> frames   (tail emitted after end of input)

All arithmetic is done in float64, so intermediate stages cannot overflow
16-bit integer ranges; saturation happens only at WAV quantization time.
"""

from __future__ import annotations

import math


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


class GainStage:
    """Constant gain (linear or dB)."""

    def __init__(self, gain_db: float = 0.0, linear: float | None = None):
        self.g = linear if linear is not None else db_to_linear(gain_db)

    def process(self, frames):
        g = self.g
        return [[x * g for x in f] for f in frames]

    def flush(self):
        return []


class MixStage:
    """Channel mixing: 'mono' (average downmix) or 'stereo' (mono dup)."""

    def __init__(self, mode: str):
        if mode not in ("mono", "stereo"):
            raise ValueError(f"unknown mix mode {mode!r}")
        self.mode = mode

    def out_channels(self, in_channels: int) -> int:
        if self.mode == "mono":
            return 1
        return 2 if in_channels == 1 else in_channels

    def process(self, frames):
        if self.mode == "mono":
            return [[sum(f) / len(f)] for f in frames]
        # to stereo: duplicate mono, pass everything else through
        return [[f[0], f[0]] if len(f) == 1 else list(f) for f in frames]

    def flush(self):
        return []


class TrimStage:
    """Keep frames in [start_frame, end_frame)."""

    def __init__(self, start_frame: int, end_frame: int):
        self.start = max(0, start_frame)
        self.end = max(self.start, end_frame)
        self.pos = 0

    def process(self, frames):
        out = []
        start, end, pos = self.start, self.end, self.pos
        for f in frames:
            if start <= pos < end:
                out.append(f)
            pos += 1
        self.pos = pos
        return out

    def flush(self):
        return []


class FadeStage:
    """Linear fade-in / fade-out over a stream of known total length."""

    def __init__(self, fade_in_frames: int, fade_out_frames: int, total_frames: int):
        self.fi = max(0, fade_in_frames)
        self.fo = max(0, fade_out_frames)
        self.total = max(0, total_frames)
        self.pos = 0

    def _gain(self, idx: int) -> float:
        g = 1.0
        if self.fi > 0 and idx < self.fi:
            g = idx / self.fi
        if self.fo > 0:
            remaining = self.total - idx
            if remaining < self.fo:
                g = min(g, remaining / self.fo)
        return g

    def process(self, frames):
        out = []
        pos = self.pos
        for f in frames:
            g = self._gain(pos)
            pos += 1
            out.append([x * g for x in f])
        self.pos = pos
        return out

    def flush(self):
        return []


class LinearResampler:
    """Streaming linear-interpolation sample-rate converter.

    Emits exactly round(total_in_frames * dst_rate / src_rate) frames;
    the final output sample is clamped to the last input sample so the
    stream length is deterministic regardless of block boundaries.
    """

    def __init__(self, src_rate: int, dst_rate: int, channels: int,
                 total_in_frames: int):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.step = src_rate / dst_rate
        self.channels = channels
        self.total_out = int(round(total_in_frames * dst_rate / src_rate))
        self.emitted = 0
        self.pos = 0.0  # position of next output sample, in input frames
        self.buf: list[list[float]] = []

    def process(self, frames):
        self.buf.extend(frames)
        out = []
        ch = self.channels
        while self.emitted < self.total_out:
            i = int(self.pos)
            if i + 1 >= len(self.buf):
                break
            frac = self.pos - i
            a, b = self.buf[i], self.buf[i + 1]
            out.append([a[c] + (b[c] - a[c]) * frac for c in range(ch)])
            self.emitted += 1
            self.pos += self.step
        consumed = int(self.pos)
        if consumed:
            del self.buf[:consumed]
            self.pos -= consumed
        return out

    def flush(self):
        out = []
        while self.emitted < self.total_out and self.buf:
            i = int(self.pos)
            if i >= len(self.buf):
                i = len(self.buf) - 1
            out.append(list(self.buf[i]))
            self.emitted += 1
            self.pos += self.step
        self.buf = []
        return out


def design_lowpass(cutoff_hz: float, sample_rate: int, num_taps: int) -> list[float]:
    """Windowed-sinc FIR low-pass design (Hamming window, unity DC gain)."""
    nyquist = sample_rate / 2.0
    if not 0.0 < cutoff_hz < nyquist:
        raise ValueError(
            f"cutoff {cutoff_hz} Hz outside (0, {nyquist}) Hz"
        )
    if num_taps < 3:
        raise ValueError("num_taps must be >= 3")
    fc = cutoff_hz / sample_rate
    m = num_taps - 1
    mid = m / 2.0
    h = []
    for n in range(num_taps):
        x = n - mid
        if x == 0.0:
            sinc = 2.0 * fc
        else:
            sinc = math.sin(2.0 * math.pi * fc * x) / (math.pi * x)
        window = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / m)  # Hamming
        h.append(sinc * window)
    s = sum(h)
    return [c / s for c in h]


class FirStage:
    """Streaming FIR filter with per-channel delay state."""

    def __init__(self, coeffs, channels: int):
        if not coeffs:
            raise ValueError("empty coefficient list")
        self.coeffs = [float(c) for c in coeffs]
        self.m = len(self.coeffs)
        self.state = [[0.0] * (self.m - 1) for _ in range(channels)]

    def process(self, frames):
        out = []
        m = self.m
        coeffs = self.coeffs
        for f in frames:
            oframe = []
            for c in range(len(f)):
                st = self.state[c]
                acc = coeffs[0] * f[c]
                for k in range(1, m):
                    acc += coeffs[k] * st[k - 1]
                # shift delay line: newest sample in, oldest out
                st.pop()
                st.insert(0, f[c])
                oframe.append(acc)
            out.append(oframe)
        return out

    def flush(self):
        return []
