"""Automated tests for the local WAV tool.

All signals (sine, impulse, silence) are generated in code. Every test
prints numeric metrics to the terminal; nothing plays audio and nothing
opens a GUI. Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import unittest

from wavtool import dsp
from wavtool.stream import Pipeline, process_file
from wavtool.wavio import WavError, read_wav, read_wav_header, write_wav


def gen_sine(freq, rate, seconds, amplitude=0.5):
    n = int(round(rate * seconds))
    return [amplitude * math.sin(2.0 * math.pi * freq * i / rate)
            for i in range(n)]


def gen_impulse(rate, seconds, amplitude=1.0):
    n = int(round(rate * seconds))
    out = [0.0] * n
    out[0] = amplitude
    return out


def gen_silence(rate, seconds):
    return [0.0] * int(round(rate * seconds))


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def measure_frequency(samples, rate):
    """Estimate frequency from upward zero crossings.

    Uses the time between the first and last crossing (with linear
    interpolation of the crossing instant) to avoid boundary bias.
    """
    times = []
    for i in range(1, len(samples)):
        if samples[i - 1] < 0.0 <= samples[i]:
            frac = -samples[i - 1] / (samples[i] - samples[i - 1])
            times.append((i - 1 + frac) / rate)
    if len(times) < 2:
        return 0.0
    return (len(times) - 1) / (times[-1] - times[0])


def db(ratio):
    return 20.0 * math.log10(ratio) if ratio > 0 else -300.0


class WavToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp, name)

    def write_float_wav(self, name, rate, channels, floats):
        path = self.path(name)
        write_wav(path, rate, channels, dsp.float_to_int16(floats))
        return path


def make_wav_blob(rate=8000, channels=1, bits=16, data=b"",
                  magic=b"RIFF", form=b"WAVE", fmt_first=True,
                  byte_rate=None, block_align=None, riff_size=None,
                  data_size=None):
    """Build a raw WAV byte string, with knobs to create broken files."""
    align = block_align if block_align is not None else channels * 2
    brate = byte_rate if byte_rate is not None else rate * align
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate,
                      brate, align, bits)
    dsize = len(data) if data_size is None else data_size
    dat = struct.pack("<4sI", b"data", dsize) + data
    body = (fmt + dat) if fmt_first else (dat + fmt)
    rsize = (4 + len(body)) if riff_size is None else riff_size
    return magic + struct.pack("<I", rsize) + form + body


class TestHeader(WavToolTestCase):
    def test_roundtrip_header_fields(self):
        floats = gen_sine(440.0, 8000, 0.1)
        path = self.write_float_wav("sine.wav", 8000, 1, floats)
        info, samples = read_wav(path)
        print(f"\n[header] rate={info.sample_rate} ch={info.channels} "
              f"bits={info.bits_per_sample} frames={info.num_frames} "
              f"data_size={info.data_size} duration={info.duration:.4f}s")
        self.assertEqual(info.sample_rate, 8000)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.bits_per_sample, 16)
        self.assertEqual(info.num_frames, 800)
        self.assertEqual(info.data_size, 1600)
        self.assertEqual(len(samples), 800)

    def test_roundtrip_sample_values(self):
        floats = gen_sine(440.0, 8000, 0.1, amplitude=0.9)
        path = self.write_float_wav("sine.wav", 8000, 1, floats)
        _, samples = read_wav(path)
        back = dsp.int16_to_float(samples)
        max_err = max(abs(a - b) for a, b in zip(floats, back))
        print(f"\n[roundtrip] max sample error = {max_err:.8f} "
              f"(1 LSB = {1 / 32768:.8f})")
        self.assertLessEqual(max_err, 1.0 / 32768.0 + 1e-12)

    def _expect_corrupt(self, name, blob):
        path = self.path(name)
        with open(path, "wb") as fh:
            fh.write(blob)
        with self.assertRaises(WavError):
            with open(path, "rb") as fh:
                read_wav_header(fh)
        print(f"\n[corrupt] {name}: correctly rejected "
              f"({len(blob)} bytes)")

    def test_corrupt_files(self):
        good_data = b"\x00\x00" * 100
        self._expect_corrupt("bad_magic.wav",
                             make_wav_blob(magic=b"RIFX", data=good_data))
        self._expect_corrupt("bad_form.wav",
                             make_wav_blob(form=b"AVI ", data=good_data))
        self._expect_corrupt("bits8.wav",
                             make_wav_blob(bits=8, data=b"\x00" * 100))
        self._expect_corrupt("bits24.wav",
                             make_wav_blob(bits=24, data=b"\x00" * 300))
        self._expect_corrupt("ch3.wav",
                             make_wav_blob(channels=3,
                                           data=b"\x00" * 600))
        self._expect_corrupt("odd_data.wav",
                             make_wav_blob(data=b"\x00" * 101))
        self._expect_corrupt("data_before_fmt.wav",
                             make_wav_blob(fmt_first=False,
                                           data=good_data))
        self._expect_corrupt("bad_byte_rate.wav",
                             make_wav_blob(byte_rate=12345,
                                           data=good_data))
        self._expect_corrupt("bad_align.wav",
                             make_wav_blob(block_align=4,
                                           data=good_data))
        self._expect_corrupt("riff_too_big.wav",
                             make_wav_blob(riff_size=10 ** 9,
                                           data=good_data))
        self._expect_corrupt("data_overrun.wav",
                             make_wav_blob(data=good_data,
                                           data_size=len(good_data) + 100))
        # truncated file: header promises data that is not there
        blob = make_wav_blob(data=good_data)
        self._expect_corrupt("truncated.wav", blob[:len(blob) - 50])
        # file shorter than the RIFF header
        self._expect_corrupt("tiny.wav", b"RIFF")


class TestGain(WavToolTestCase):
    def test_gain_db(self):
        rate = 8000
        floats = gen_sine(440.0, rate, 0.2, amplitude=0.4)
        before = rms(floats)
        gained = dsp.apply_gain(list(floats), dsp.db_to_linear(6.0))
        after = rms(gained)
        measured_db = db(after / before)
        print(f"\n[gain] rms {before:.6f} -> {after:.6f} "
              f"({measured_db:+.3f} dB, expected +6.000 dB)")
        self.assertAlmostEqual(measured_db, 6.0, places=3)

    def test_gain_through_file(self):
        rate = 8000
        src = self.write_float_wav("in.wav", rate, 1,
                                   gen_sine(440.0, rate, 0.2, 0.4))
        dst = self.path("out.wav")
        stats = process_file(src, dst, Pipeline(gain_db=-6.0))
        info, samples = read_wav(dst)
        out_rms = rms(dsp.int16_to_float(samples))
        expected = 0.4 / math.sqrt(2.0) * dsp.db_to_linear(-6.0)
        print(f"\n[gain-file] out rms={out_rms:.6f} "
              f"expected~{expected:.6f} clipped={stats['clipped_samples']}")
        self.assertAlmostEqual(out_rms, expected, delta=0.002)
        self.assertEqual(stats["clipped_samples"], 0)


class TestChannelMix(WavToolTestCase):
    def test_stereo_to_mono(self):
        rate = 8000
        left = gen_sine(440.0, rate, 0.2, amplitude=0.6)
        right = gen_silence(rate, 0.2)
        stereo = [v for pair in zip(left, right) for v in pair]
        mono = dsp.mix_channels(stereo, 2, 1)
        mono_rms = rms(mono)
        expected = 0.5 * rms(left)
        print(f"\n[mix] stereo(L=0.6 sine, R=0) -> mono rms={mono_rms:.6f} "
              f"expected={expected:.6f}")
        self.assertAlmostEqual(mono_rms, expected, places=6)
        self.assertEqual(len(mono), len(left))

    def test_mono_to_stereo(self):
        rate = 8000
        mono = gen_sine(440.0, rate, 0.1, amplitude=0.3)
        stereo = dsp.mix_channels(mono, 1, 2)
        self.assertEqual(len(stereo), 2 * len(mono))
        max_diff = max(abs(stereo[2 * i] - stereo[2 * i + 1])
                       for i in range(len(mono)))
        print(f"\n[mix] mono->stereo max |L-R| = {max_diff:.9f}")
        self.assertEqual(max_diff, 0.0)


class TestTrimFade(WavToolTestCase):
    def test_trim(self):
        rate = 8000
        floats = gen_sine(440.0, rate, 1.0, amplitude=0.5)
        cut = dsp.trim(floats, 1, rate, 0.25, 0.75)
        print(f"\n[trim] 1.00s -> [{0.25}, {0.75})s : "
              f"{len(floats)} -> {len(cut)} samples")
        self.assertEqual(len(cut), 4000)
        # boundary alignment: first kept sample equals input sample 2000
        self.assertAlmostEqual(cut[0], floats[2000], places=12)

    def test_fade(self):
        rate = 8000
        floats = [0.8] * 8000
        faded = dsp.apply_fade(list(floats), 1, rate, 0.1, 0.1)
        n = int(0.1 * rate)
        mid_ok = all(abs(v - 0.8) < 1e-12 for v in faded[n:-n])
        print(f"\n[fade] first={faded[0]:.6f} mid={faded[n]:.6f} "
              f"last={faded[-1]:.6f} ramp_len={n} mid_flat={mid_ok}")
        self.assertEqual(faded[0], 0.0)
        self.assertEqual(faded[-1], 0.0)
        self.assertAlmostEqual(faded[n], 0.8, places=12)
        self.assertTrue(mid_ok)
        # linear ramp check
        self.assertAlmostEqual(faded[n // 2], 0.8 * 0.5, places=3)


class TestResample(WavToolTestCase):
    def test_upsample_length_and_frequency(self):
        rate_in, rate_out = 8000, 16000
        floats = gen_sine(440.0, rate_in, 0.25, amplitude=0.5)
        out = dsp.resample_linear(floats, 1, rate_in, rate_out)
        expected_len = dsp.resample_length(len(floats), rate_in, rate_out)
        freq = measure_frequency(out, rate_out)
        print(f"\n[resample] 8000->16000 Hz: {len(floats)} -> {len(out)} "
              f"samples (expected {expected_len}), "
              f"measured freq={freq:.2f} Hz (expected 440)")
        self.assertEqual(len(out), expected_len)
        self.assertAlmostEqual(freq, 440.0, delta=2.0)

    def test_downsample_length_and_frequency(self):
        rate_in, rate_out = 16000, 8000
        floats = gen_sine(300.0, rate_in, 0.25, amplitude=0.5)
        out = dsp.resample_linear(floats, 1, rate_in, rate_out)
        expected_len = dsp.resample_length(len(floats), rate_in, rate_out)
        freq = measure_frequency(out, rate_out)
        print(f"\n[resample] 16000->8000 Hz: {len(floats)} -> {len(out)} "
              f"samples (expected {expected_len}), "
              f"measured freq={freq:.2f} Hz (expected 300)")
        self.assertEqual(len(out), expected_len)
        self.assertAlmostEqual(freq, 300.0, delta=2.0)

    def test_non_integer_ratio_length(self):
        rate_in, rate_out = 8000, 11025
        floats = gen_sine(200.0, rate_in, 0.3, amplitude=0.5)
        out = dsp.resample_linear(floats, 1, rate_in, rate_out)
        expected_len = dsp.resample_length(len(floats), rate_in, rate_out)
        freq = measure_frequency(out, rate_out)
        print(f"\n[resample] 8000->11025 Hz: {len(floats)} -> {len(out)} "
              f"samples (expected {expected_len}), "
              f"measured freq={freq:.2f} Hz (expected 200)")
        self.assertEqual(len(out), expected_len)
        self.assertAlmostEqual(freq, 200.0, delta=3.0)

    def test_boundary_no_overrun_and_endpoint(self):
        floats = gen_sine(440.0, 8000, 0.1, amplitude=0.5)
        out = dsp.resample_linear(floats, 1, 8000, 16000)
        # last output sample must be clamped to the last input sample
        print(f"\n[resample] boundary: out[-1]={out[-1]:.8f} "
              f"in[-1]={floats[-1]:.8f}")
        self.assertAlmostEqual(out[-1], floats[-1], places=12)


class TestLowpass(WavToolTestCase):
    def test_cutoff_design(self):
        rate = 16000
        coeffs = dsp.design_lowpass(1000.0, rate, 101)
        dc_gain = sum(coeffs)
        print(f"\n[lowpass] taps={len(coeffs)} dc_gain={dc_gain:.9f}")
        self.assertAlmostEqual(dc_gain, 1.0, places=9)
        low = gen_sine(200.0, rate, 0.25, amplitude=0.5)
        high = gen_sine(5000.0, rate, 0.25, amplitude=0.5)
        low_out = dsp.fir_filter(low, 1, coeffs)
        high_out = dsp.fir_filter(high, 1, coeffs)
        # skip the filter warm-up transient
        skip = len(coeffs)
        low_ratio = rms(low_out[skip:]) / rms(low[skip:])
        high_ratio = rms(high_out[skip:]) / rms(high[skip:])
        print(f"[lowpass] 200 Hz gain={db(low_ratio):+.2f} dB, "
              f"5000 Hz gain={db(high_ratio):+.2f} dB "
              f"(cutoff=1000 Hz)")
        self.assertGreater(db(low_ratio), -1.0)
        self.assertLess(db(high_ratio), -30.0)

    def test_custom_coefficients_impulse(self):
        rate = 8000
        coeffs = [0.25, 0.5, 0.25]
        impulse = gen_impulse(rate, 0.01)
        out = dsp.fir_filter(impulse, 1, coeffs)
        got = out[:len(coeffs)]
        max_err = max(abs(a - b) for a, b in zip(got, coeffs))
        print(f"\n[fir] impulse response={['%.4f' % g for g in got]} "
              f"max err vs coeffs={max_err:.2e}")
        self.assertLessEqual(max_err, 1e-12)
        # identity filter
        ident = dsp.fir_filter(impulse, 1, [1.0])
        self.assertEqual(ident[:4], [1.0, 0.0, 0.0, 0.0])

    def test_invalid_cutoff_rejected(self):
        with self.assertRaises(ValueError):
            dsp.design_lowpass(9000.0, 16000)   # above Nyquist
        with self.assertRaises(ValueError):
            dsp.design_lowpass(0.0, 16000)


class TestClipping(WavToolTestCase):
    def test_saturation_no_wraparound(self):
        rate = 8000
        # 0.9 amplitude sine boosted by +12 dB (~x3.98) -> heavy clipping
        floats = gen_sine(440.0, rate, 0.2, amplitude=0.9)
        boosted = dsp.apply_gain(list(floats), dsp.db_to_linear(12.0))
        ints = dsp.float_to_int16(boosted)
        peak_pos = max(ints)
        peak_neg = min(ints)
        n_clipped = sum(1 for v in boosted
                        if v * 32768.0 > 32767.5 or v * 32768.0 < -32768.5)
        n_at_max = sum(1 for v in ints if v == 32767)
        n_at_min = sum(1 for v in ints if v == -32768)
        print(f"\n[clip] +12 dB on 0.9 sine: peak+={peak_pos} "
              f"peak-={peak_neg} clipped={n_clipped} "
              f"at_max={n_at_max} at_min={n_at_min}")
        self.assertEqual(peak_pos, 32767)
        self.assertEqual(peak_neg, -32768)
        self.assertGreater(n_clipped, 0)
        # every over-range positive sample saturated to +32767 (no wrap)
        for f, i in zip(boosted, ints):
            if f * 32768.0 > 32767.5:
                self.assertEqual(i, 32767)
            elif f * 32768.0 < -32768.5:
                self.assertEqual(i, -32768)

    def test_silence_stays_silent(self):
        rate = 8000
        silence = gen_silence(rate, 0.1)
        out = dsp.apply_gain(list(silence), 1000.0)
        ints = dsp.float_to_int16(out)
        peak = max(abs(v) for v in ints)
        print(f"\n[silence] gain x1000 on silence: peak={peak}")
        self.assertEqual(peak, 0)


class TestStreaming(WavToolTestCase):
    def _offline_reference(self, floats, channels, rate, pipe):
        """Run the same pipeline with the whole-file dsp functions."""
        data = dsp.trim(list(floats), channels, rate,
                        pipe.trim_start, pipe.trim_end)
        ch = pipe.out_channels or channels
        data = dsp.mix_channels(data, channels, ch)
        coeffs = pipe.lowpass_coeffs
        if coeffs is None and pipe.lowpass_cutoff is not None:
            coeffs = dsp.design_lowpass(pipe.lowpass_cutoff, rate,
                                        pipe.lowpass_taps)
        if coeffs:
            data = dsp.fir_filter(data, ch, coeffs)
        if pipe.gain_db is not None:
            data = dsp.apply_gain(data, dsp.db_to_linear(pipe.gain_db))
        data = dsp.apply_fade(data, ch, rate, pipe.fade_in, pipe.fade_out)
        out_rate = pipe.out_rate or rate
        if out_rate != rate:
            data = dsp.resample_linear(data, ch, rate, out_rate)
        return dsp.float_to_int16(data), ch, out_rate

    def test_streaming_matches_offline(self):
        rate = 8000
        seconds = 0.35
        left = gen_sine(440.0, rate, seconds, amplitude=0.5)
        right = gen_sine(220.0, rate, seconds, amplitude=0.25)
        stereo = [v for pair in zip(left, right) for v in pair]
        src = self.write_float_wav("stereo.wav", rate, 2, stereo)

        pipe = Pipeline(
            gain_db=-3.0,
            out_channels=1,
            trim_start=0.02,
            trim_end=0.30,
            fade_in=0.01,
            fade_out=0.02,
            lowpass_cutoff=1500.0,
            lowpass_taps=63,
            out_rate=16000,
        )
        dst = self.path("streamed.wav")
        # deliberately small, odd block size to stress block boundaries
        stats = process_file(src, dst, pipe, block_frames=257)

        ref_ints, ref_ch, ref_rate = self._offline_reference(
            stereo, 2, rate, pipe)
        info, got = read_wav(dst)
        max_diff = max((abs(a - b) for a, b in zip(ref_ints, got)),
                       default=0)
        print(f"\n[stream] frames_in={stats['frames_in']} "
              f"frames_out={stats['frames_out']} "
              f"(expected {len(ref_ints) // ref_ch}) "
              f"max|stream-offline|={max_diff} LSB "
              f"peak={stats['peak_float']:.4f}")
        self.assertEqual(info.sample_rate, ref_rate)
        self.assertEqual(info.channels, ref_ch)
        self.assertEqual(len(got), len(ref_ints))
        self.assertEqual(stats["frames_out"], len(ref_ints) // ref_ch)
        self.assertLessEqual(max_diff, 1)

    def test_streaming_large_file_block_boundaries(self):
        # gain-only pass on a longer file with tiny blocks
        rate = 8000
        floats = gen_sine(100.0, rate, 1.0, amplitude=0.7)
        src = self.write_float_wav("long.wav", rate, 1, floats)
        dst = self.path("long_out.wav")
        stats = process_file(src, dst, Pipeline(gain_db=-20.0),
                             block_frames=100)
        _, got = read_wav(dst)
        ref = dsp.float_to_int16(
            dsp.apply_gain(list(floats), dsp.db_to_linear(-20.0)))
        max_diff = max(abs(a - b) for a, b in zip(ref, got))
        print(f"\n[stream-blocks] 8000 frames, block=100: "
              f"frames_out={stats['frames_out']} max diff={max_diff} LSB")
        self.assertEqual(len(got), len(ref))
        self.assertLessEqual(max_diff, 1)


if __name__ == "__main__":
    unittest.main()
