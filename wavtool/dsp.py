"""DSP primitives operating on interleaved float sample lists.

All internal processing is done in float64 (Python floats) to avoid
integer overflow. Conversion back to int16 PCM always goes through
explicit saturation (clipping to [-32768, 32767]).
"""

from __future__ import annotations

import math
from array import array

INT16_MIN = -32768
INT16_MAX = 32767
_SCALE = 32768.0


def int16_to_float(samples: array) -> list[float]:
    """int16 interleaved -> float interleaved in [-1.0, 1.0)."""
    inv = 1.0 / _SCALE
    return [s * inv for s in samples]


def clip_int16(value: float) -> int:
    """Round and saturate a float sample to the int16 range."""
    ivalue = int(round(value * _SCALE))
    if ivalue > INT16_MAX:
        return INT16_MAX
    if ivalue < INT16_MIN:
        return INT16_MIN
    return ivalue


def float_to_int16(samples) -> array:
    """Float interleaved -> int16 interleaved with hard saturation."""
    out = array("h", bytes(2 * len(samples)))
    for i, v in enumerate(samples):
        out[i] = clip_int16(v)
    return out


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def apply_gain(samples: list, gain: float) -> list:
    """Multiply every sample by a linear gain factor (in place)."""
    for i in range(len(samples)):
        samples[i] *= gain
    return samples


def mix_channels(samples: list, ch_in: int, ch_out: int) -> list:
    """Convert between mono and stereo.

    mono   -> stereo : duplicate
    stereo -> mono   : 0.5 * (L + R)
    """
    if ch_in == ch_out:
        return samples
    if ch_in == 1 and ch_out == 2:
        out = []
        append = out.append
        for s in samples:
            append(s)
            append(s)
        return out
    if ch_in == 2 and ch_out == 1:
        return [0.5 * (samples[i] + samples[i + 1])
                for i in range(0, len(samples), 2)]
    raise ValueError(f"unsupported channel conversion {ch_in} -> {ch_out}")


def trim(samples: list, channels: int, sample_rate: int,
         start_sec: float = 0.0, end_sec=None) -> list:
    """Keep only the [start_sec, end_sec) time interval."""
    total_frames = len(samples) // channels
    start = max(0, min(total_frames, int(round(start_sec * sample_rate))))
    if end_sec is None:
        end = total_frames
    else:
        end = max(start, min(total_frames, int(round(end_sec * sample_rate))))
    return samples[start * channels:end * channels]


def apply_fade(samples: list, channels: int, sample_rate: int,
               fade_in_sec: float = 0.0, fade_out_sec: float = 0.0) -> list:
    """Apply linear fade-in / fade-out ramps (in place)."""
    total_frames = len(samples) // channels
    n_in = min(total_frames, int(round(fade_in_sec * sample_rate)))
    n_out = min(total_frames, int(round(fade_out_sec * sample_rate)))
    for frame in range(n_in):
        factor = frame / n_in
        base = frame * channels
        for c in range(channels):
            samples[base + c] *= factor
    for k in range(n_out):
        frame = total_frames - 1 - k
        factor = k / n_out
        base = frame * channels
        for c in range(channels):
            samples[base + c] *= factor
    return samples


def resample_length(num_frames_in: int, rate_in: int, rate_out: int) -> int:
    """Exact output frame count used by the resampler."""
    return int(round(num_frames_in * rate_out / rate_in))


def resample_linear(samples: list, channels: int, rate_in: int,
                    rate_out: int) -> list:
    """Linear-interpolation sample-rate conversion.

    Output length is round(n_in * rate_out / rate_in). Positions are
    clamped at the end so the boundary never reads past the last frame.
    """
    if rate_in <= 0 or rate_out <= 0:
        raise ValueError("sample rates must be positive")
    n_in = len(samples) // channels
    if n_in == 0 or rate_in == rate_out:
        return list(samples)
    n_out = resample_length(n_in, rate_in, rate_out)
    step = rate_in / rate_out
    out = [0.0] * (n_out * channels)
    last = n_in - 1
    for i in range(n_out):
        pos = i * step
        idx = int(pos)
        if idx >= last:
            base = last * channels
            obase = i * channels
            for c in range(channels):
                out[obase + c] = samples[base + c]
        else:
            frac = pos - idx
            base = idx * channels
            obase = i * channels
            for c in range(channels):
                a = samples[base + c]
                out[obase + c] = a + frac * (samples[base + channels + c] - a)
    return out


def design_lowpass(cutoff_hz: float, sample_rate: int,
                   num_taps: int = 101) -> list:
    """Windowed-sinc low-pass FIR design (Hamming window).

    Coefficients are normalized so the DC gain is exactly 1.
    """
    if not 0.0 < cutoff_hz < sample_rate / 2.0:
        raise ValueError(
            f"cutoff {cutoff_hz} Hz must be in (0, {sample_rate / 2}) Hz")
    if num_taps < 3:
        raise ValueError("num_taps must be >= 3")
    if num_taps % 2 == 0:
        num_taps += 1  # keep the filter symmetric with an integer delay
    fc = cutoff_hz / sample_rate  # cycles per sample
    mid = (num_taps - 1) / 2.0
    coeffs = []
    for n in range(num_taps):
        x = n - mid
        if x == 0.0:
            sinc = 2.0 * fc
        else:
            sinc = math.sin(2.0 * math.pi * fc * x) / (math.pi * x)
        window = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / (num_taps - 1))
        coeffs.append(sinc * window)
    norm = 1.0 / sum(coeffs)
    return [c * norm for c in coeffs]


def fir_filter(samples: list, channels: int, coefficients: list) -> list:
    """Causal FIR convolution, output length equals input length.

    y[i] = sum_k h[k] * x[i - k]   (zero padding before the signal,
    the tail beyond the last input frame is truncated).
    """
    if not coefficients:
        raise ValueError("empty coefficient list")
    n_frames = len(samples) // channels
    n_taps = len(coefficients)
    out = [0.0] * len(samples)
    for c in range(channels):
        chan = samples[c::channels]
        ochan = [0.0] * n_frames
        for i in range(n_frames):
            acc = 0.0
            kmax = n_taps if i + 1 >= n_taps else i + 1
            for k in range(kmax):
                acc += coefficients[k] * chan[i - k]
            ochan[i] = acc
        out[c::channels] = ochan
    return out
