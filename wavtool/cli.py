"""Command-line interface for the local WAV processing tool."""

from __future__ import annotations

import argparse
import sys

from . import dsp
from .pipeline import run_pipeline
from .wavio import WavFormatError, WavReader, WavWriter


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wavtool",
        description="Local, offline PCM WAV processing (no servers, no cloud).",
    )
    p.add_argument("input", help="input WAV file")
    p.add_argument("output", help="output WAV file")
    p.add_argument("--gain-db", type=float, default=None, help="gain in dB")
    p.add_argument("--to-mono", action="store_true", help="average all channels")
    p.add_argument("--to-stereo", action="store_true", help="duplicate mono to stereo")
    p.add_argument("--trim", nargs=2, type=float, metavar=("START", "END"),
                   help="keep [START, END) seconds")
    p.add_argument("--fade-in", type=float, default=0.0, help="fade-in seconds")
    p.add_argument("--fade-out", type=float, default=0.0, help="fade-out seconds")
    p.add_argument("--resample", type=int, metavar="RATE",
                   help="output sample rate (linear interpolation)")
    p.add_argument("--lowpass", type=float, metavar="CUTOFF_HZ",
                   help="FIR low-pass cutoff (windowed-sinc design)")
    p.add_argument("--taps", type=int, default=101, help="FIR taps for --lowpass")
    p.add_argument("--coeffs", type=str, default=None,
                   help="comma-separated custom FIR coefficients")
    p.add_argument("--bits", type=int, default=16, choices=(8, 16, 24, 32),
                   help="output bit depth (default 16)")
    p.add_argument("--block-frames", type=int, default=4096,
                   help="streaming block size in frames")
    return p


def build_stages(args, in_channels: int, in_rate: int, in_frames: int):
    """Build the ordered stage list; returns (stages, out_channels, out_rate)."""
    stages = []
    channels = in_channels
    rate = in_rate

    start_f, end_f = 0, in_frames
    if args.trim:
        start_f = min(in_frames, max(0, round(args.trim[0] * rate)))
        end_f = min(in_frames, max(start_f, round(args.trim[1] * rate)))
        stages.append(dsp.TrimStage(start_f, end_f))
    total = end_f - start_f

    if args.fade_in > 0.0 or args.fade_out > 0.0:
        stages.append(dsp.FadeStage(
            round(args.fade_in * rate), round(args.fade_out * rate), total))

    if args.gain_db is not None:
        stages.append(dsp.GainStage(gain_db=args.gain_db))

    if args.to_mono:
        stages.append(dsp.MixStage("mono"))
        channels = 1
    elif args.to_stereo and channels == 1:
        stages.append(dsp.MixStage("stereo"))
        channels = 2

    if args.coeffs:
        coeffs = [float(x) for x in args.coeffs.split(",") if x.strip()]
        stages.append(dsp.FirStage(coeffs, channels))
    elif args.lowpass is not None:
        coeffs = dsp.design_lowpass(args.lowpass, rate, args.taps)
        stages.append(dsp.FirStage(coeffs, channels))

    if args.resample and args.resample != rate:
        stages.append(dsp.LinearResampler(rate, args.resample, channels, total))
        rate = args.resample

    return stages, channels, rate


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with WavReader(args.input) as reader:
            info = reader.info
            stages, out_ch, out_rate = build_stages(
                args, info.channels, info.sample_rate, info.num_frames)
            with WavWriter(args.output, out_ch, out_rate, args.bits) as writer:
                m = run_pipeline(reader, writer, stages, args.block_frames)
    except (WavFormatError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"input : {info.channels} ch, {info.sample_rate} Hz, "
          f"{info.bits_per_sample}-bit, {info.num_frames} frames "
          f"({info.duration:.3f} s)")
    print(f"output: {out_ch} ch, {out_rate} Hz, {args.bits}-bit, "
          f"{m.frames_out} frames ({m.frames_out / out_rate:.3f} s)")
    print(f"frames: in={m.frames_in} out={m.frames_out}")
    print(f"peak  : in={m.peak_in:.6f} out={m.peak_out:.6f}")
    print(f"rms   : in={m.rms_in:.6f} out={m.rms_out:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
