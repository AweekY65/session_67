"""Automated tests for wavtool.

All signals (sine, impulse, silence) are generated in code; every check
prints numeric metrics to the terminal. No audio playback, no GUI.
"""

import math
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wavtool import dsp
from wavtool.pipeline import run_pipeline
from wavtool.wavio import (WavFormatError, WavReader, WavWriter, parse_header)

SR = 44100


def sine(frames, freq, sr=SR, amp=0.5, channels=1):
    out = []
    for n in range(frames):
        v = amp * math.sin(2.0 * math.pi * freq * n / sr)
        out.append([v] * channels)
    return out


def silence(frames, channels=1):
    return [[0.0] * channels for _ in range(frames)]


def impulse(frames, amp=1.0):
    out = silence(frames)
    out[0] = [amp]
    return out


def rms(frames):
    vals = [x for f in frames for x in f]
    return math.sqrt(sum(x * x for x in vals) / max(1, len(vals)))


def goertzel_amp(frames, freq, sr=SR, channel=0):
    """Single-frequency amplitude estimate (Goertzel)."""
    xs = [f[channel] for f in frames]
    w = 2.0 * math.pi * freq / sr
    cw = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in xs:
        s0 = x + cw * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - cw * s1 * s2
    return 2.0 * math.sqrt(max(0.0, power)) / max(1, len(xs))


def zero_cross_freq(frames, sr):
    xs = [f[0] for f in frames]
    crossings = 0
    for i in range(1, len(xs)):
        if xs[i - 1] < 0.0 <= xs[i]:
            crossings += 1
    dur = len(xs) / sr
    return crossings / dur


def write_wav(path, frames, sr=SR, bits=16):
    ch = len(frames[0]) if frames else 1
    with WavWriter(path, ch, sr, bits) as w:
        w.write_block(frames)
    return ch


def read_wav(path):
    with WavReader(path) as r:
        frames = []
        while True:
            b = r.read_block(1024)
            if not b:
                break
            frames.extend(b)
        return r.info, frames


def run_file(in_path, out_path, stages, out_ch, out_rate, bits=16, block=4096):
    with WavReader(in_path) as r, WavWriter(out_path, out_ch, out_rate, bits) as w:
        return run_pipeline(r, w, stages, block)


class WavToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return os.path.join(self.dir, name)

    # ---- header parsing -------------------------------------------------
    def test_header_parse(self):
        path = self.p("sine.wav")
        write_wav(path, sine(4410, 440.0, channels=2))
        with open(path, "rb") as f:
            info = parse_header(f)
        print(f"\n[header] ch={info.channels} sr={info.sample_rate} "
              f"bits={info.bits_per_sample} frames={info.num_frames} "
              f"data_size={info.data_size}")
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.sample_rate, SR)
        self.assertEqual(info.bits_per_sample, 16)
        self.assertEqual(info.num_frames, 4410)
        self.assertEqual(info.data_size, 4410 * 4)

    def test_corrupted_files(self):
        path = self.p("ok.wav")
        write_wav(path, sine(1000, 440.0))
        with open(path, "rb") as f:
            good = f.read()
        cases = {}
        bad = bytearray(good); bad[0:4] = b"RIFX"
        cases["bad RIFF id"] = bytes(bad)
        bad = bytearray(good); bad[8:12] = b"XXXX"
        cases["bad WAVE id"] = bytes(bad)
        cases["truncated file"] = good[:len(good) - 500]
        bad = bytearray(good)
        struct.pack_into("<I", bad, 40, 1001)  # data size not multiple of align
        cases["misaligned data size"] = bytes(bad)
        bad = bytearray(good)
        struct.pack_into("<H", bad, 32, 99)  # wrong block_align
        cases["wrong block_align"] = bytes(bad)
        bad = bytearray(good)
        struct.pack_into("<H", bad, 20, 3)  # float format, not PCM int
        cases["non-PCM format"] = bytes(bad)
        bad = bytearray(good)
        struct.pack_into("<H", bad, 34, 12)  # 12-bit samples
        cases["bad bit depth"] = bytes(bad)
        for name, blob in cases.items():
            p = self.p("bad.wav")
            with open(p, "wb") as f:
                f.write(blob)
            with self.assertRaises(WavFormatError, msg=name):
                with WavReader(p):
                    pass
            print(f"[corrupt:{name}] rejected OK")

    # ---- gain -------------------------------------------------------------
    def test_gain(self):
        src = sine(4410, 440.0, amp=0.25)
        in_p, out_p = self.p("g_in.wav"), self.p("g_out.wav")
        write_wav(in_p, src)
        run_file(in_p, out_p, [dsp.GainStage(gain_db=6.0)], 1, SR)
        _, out = read_wav(out_p)
        r_in, r_out = rms(src), rms(out)
        ratio = r_out / r_in
        print(f"[gain +6dB] rms_in={r_in:.6f} rms_out={r_out:.6f} "
              f"ratio={ratio:.4f} (expect ~1.9953)")
        self.assertAlmostEqual(ratio, 1.9953, delta=0.01)

    # ---- channel mixing ---------------------------------------------------
    def test_stereo_mix(self):
        frames = []
        for n in range(4410):
            v = 0.5 * math.sin(2.0 * math.pi * 440.0 * n / SR)
            frames.append([v, -v])  # anti-phase stereo
        in_p, out_p = self.p("m_in.wav"), self.p("m_out.wav")
        write_wav(in_p, frames)
        run_file(in_p, out_p, [dsp.MixStage("mono")], 1, SR)
        info, out = read_wav(out_p)
        r = rms(out)
        print(f"[mix mono] anti-phase downmix rms={r:.8f} (expect ~0)")
        self.assertEqual(info.channels, 1)
        self.assertLess(r, 1e-3)
        # mono -> stereo duplication
        in2, out2 = self.p("m2_in.wav"), self.p("m2_out.wav")
        write_wav(in2, sine(1000, 440.0))
        run_file(in2, out2, [dsp.MixStage("stereo")], 2, SR)
        info2, o2 = read_wav(out2)
        diff = max(abs(f[0] - f[1]) for f in o2)
        print(f"[mix stereo] ch={info2.channels} max|L-R|={diff:.8f}")
        self.assertEqual(info2.channels, 2)
        self.assertEqual(diff, 0.0)

    # ---- trim & fade ------------------------------------------------------
    def test_trim(self):
        in_p, out_p = self.p("t_in.wav"), self.p("t_out.wav")
        write_wav(in_p, sine(SR, 440.0))
        stages = [dsp.TrimStage(round(0.25 * SR), round(0.75 * SR))]
        run_file(in_p, out_p, stages, 1, SR)
        info, _ = read_wav(out_p)
        print(f"[trim 0.25-0.75s] out_frames={info.num_frames} (expect 22050)")
        self.assertEqual(info.num_frames, 22050)

    def test_fade(self):
        total = 10000
        fi, fo = 1000, 2000
        frames = [[0.8]] * total
        st = dsp.FadeStage(fi, fo, total)
        out = st.process(frames)
        g_first, g_mid = out[1][0] / 0.8, out[5000][0] / 0.8
        g_last = out[-1][0] / 0.8
        print(f"[fade] gain@1={g_first:.4f} gain@mid={g_mid:.4f} "
              f"gain@last={g_last:.4f}")
        self.assertAlmostEqual(g_first, 1 / fi, places=6)
        self.assertAlmostEqual(g_mid, 1.0, places=6)
        self.assertAlmostEqual(g_last, 1 / fo, places=6)

    # ---- resampling -------------------------------------------------------
    def test_resample_down_and_up(self):
        freq = 440.0
        in_p = self.p("r_in.wav")
        write_wav(in_p, sine(SR, freq, amp=0.6))
        for dst in (22050, 48000):
            out_p = self.p(f"r_{dst}.wav")
            stages = [dsp.LinearResampler(SR, dst, 1, SR)]
            run_file(in_p, out_p, stages, 1, dst)
            info, out = read_wav(out_p)
            expect = round(SR * dst / SR)
            f_est = zero_cross_freq(out, dst)
            print(f"[resample {SR}->{dst}] frames={info.num_frames} "
                  f"(expect {expect}) freq_est={f_est:.2f} Hz "
                  f"(expect {freq})")
            self.assertEqual(info.num_frames, expect)
            self.assertAlmostEqual(f_est, freq, delta=2.0)

    # ---- FIR low-pass -----------------------------------------------------
    def test_lowpass(self):
        f_lo, f_hi = 200.0, 5000.0
        frames = []
        for n in range(SR):
            v = 0.4 * math.sin(2 * math.pi * f_lo * n / SR) \
                + 0.4 * math.sin(2 * math.pi * f_hi * n / SR)
            frames.append([v])
        in_p, out_p = self.p("lp_in.wav"), self.p("lp_out.wav")
        write_wav(in_p, frames)
        coeffs = dsp.design_lowpass(1000.0, SR, 101)
        print(f"[lowpass] taps=101 dc_gain={sum(coeffs):.6f}")
        run_file(in_p, out_p, [dsp.FirStage(coeffs, 1)], 1, SR)
        _, out = read_wav(out_p)
        tail = out[5000:]  # skip filter transient
        a_lo = goertzel_amp(tail, f_lo)
        a_hi = goertzel_amp(tail, f_hi)
        atten = 20 * math.log10(0.4 / max(a_hi, 1e-12))
        print(f"[lowpass] amp@{f_lo}Hz={a_lo:.4f} (in 0.4) "
              f"amp@{f_hi}Hz={a_hi:.6f} (in 0.4) stopband_atten={atten:.1f} dB")
        self.assertAlmostEqual(a_lo, 0.4, delta=0.02)
        self.assertGreater(atten, 40.0)

    def test_custom_coeffs_impulse(self):
        coeffs = [0.25, 0.5, 0.25]
        st = dsp.FirStage(coeffs, 1)
        out = st.process(impulse(8))
        got = [f[0] for f in out[:3]]
        print(f"[fir impulse] response={got} coeffs={coeffs}")
        for g, c in zip(got, coeffs):
            self.assertAlmostEqual(g, c, places=9)

    # ---- clipping / saturation -------------------------------------------
    def test_clipping(self):
        src = sine(4410, 440.0, amp=0.9)
        in_p, out_p = self.p("c_in.wav"), self.p("c_out.wav")
        write_wav(in_p, src)
        run_file(in_p, out_p, [dsp.GainStage(gain_db=12.0)], 1, SR)
        with open(out_p, "rb") as f:
            raw = f.read()[44:]
        vals = struct.unpack(f"<{len(raw) // 2}h", raw)
        mx, mn = max(vals), min(vals)
        # wraparound would flip the sign of large samples
        sign_errors = sum(
            1 for i, v in enumerate(vals)
            if abs(src[i][0]) > 0.5 and (v > 0) != (src[i][0] > 0)
        )
        print(f"[clip +12dB] max={mx} min={mn} sign_errors={sign_errors}")
        self.assertEqual(mx, 32767)
        self.assertEqual(mn, -32768)
        self.assertEqual(sign_errors, 0)

    # ---- chunked equivalence & silence ------------------------------------
    def test_chunked_equals_whole(self):
        in_p = self.p("k_in.wav")
        write_wav(in_p, sine(20011, 440.0, amp=0.6))
        coeffs = dsp.design_lowpass(3000.0, SR, 65)

        def build():
            return [
                dsp.GainStage(gain_db=-3.0),
                dsp.FadeStage(500, 500, 20011),
                dsp.FirStage(coeffs, 1),
                dsp.LinearResampler(SR, 32000, 1, 20011),
            ]

        outs = []
        for block in (4096, 64):
            out_p = self.p(f"k_{block}.wav")
            run_file(in_p, out_p, build(), 1, 32000, block=block)
            with open(out_p, "rb") as f:
                outs.append(f.read())
        same = outs[0] == outs[1]
        print(f"[chunked] block=4096 vs 64 byte_identical={same} "
              f"out_bytes={len(outs[0])}")
        self.assertTrue(same)

    def test_silence(self):
        in_p, out_p = self.p("s_in.wav"), self.p("s_out.wav")
        write_wav(in_p, silence(5000))
        m = run_file(in_p, out_p, [dsp.GainStage(gain_db=20.0)], 1, SR)
        _, out = read_wav(out_p)
        peak = max(abs(x) for f in out for x in f)
        print(f"[silence] frames_out={m.frames_out} peak_out={peak:.8f}")
        self.assertEqual(peak, 0.0)
        self.assertEqual(m.frames_out, 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
