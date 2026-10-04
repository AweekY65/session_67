"""Command line interface for the local WAV processing tool.

Example:
    python -m wavtool.cli in.wav out.wav --gain-db -3 --to-mono \
        --trim 1.0 2.5 --fade-in 0.1 --fade-out 0.2 \
        --lowpass 3000 --resample 22050
"""

from __future__ import annotations

import argparse
import sys

from .stream import DEFAULT_BLOCK_FRAMES, Pipeline, process_file
from .wavio import WavError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wavtool",
        description="Local-only WAV (16-bit PCM mono/stereo) processor. "
                    "No servers, no network, no GUI.")
    parser.add_argument("input", help="input WAV file")
    parser.add_argument("output", help="output WAV file")
    parser.add_argument("--gain-db", type=float, default=None,
                        help="gain in decibels (e.g. -3 or 6)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--to-mono", action="store_true",
                       help="mix stereo down to mono (0.5*(L+R))")
    group.add_argument("--to-stereo", action="store_true",
                       help="duplicate mono to stereo")
    parser.add_argument("--trim", nargs=2, type=float,
                        metavar=("START", "END"),
                        help="keep only [START, END) seconds")
    parser.add_argument("--fade-in", type=float, default=0.0,
                        help="linear fade-in length in seconds")
    parser.add_argument("--fade-out", type=float, default=0.0,
                        help="linear fade-out length in seconds")
    parser.add_argument("--resample", type=int, default=None,
                        metavar="RATE",
                        help="output sample rate (linear interpolation)")
    parser.add_argument("--lowpass", type=float, default=None,
                        metavar="CUTOFF",
                        help="FIR low-pass cutoff in Hz (windowed-sinc)")
    parser.add_argument("--taps", type=int, default=101,
                        help="number of FIR taps for --lowpass "
                             "(default: 101)")
    parser.add_argument("--block-frames", type=int,
                        default=DEFAULT_BLOCK_FRAMES,
                        help="streaming block size in frames "
                             "(default: %(default)s)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out_channels = None
    if args.to_mono:
        out_channels = 1
    elif args.to_stereo:
        out_channels = 2
    pipeline = Pipeline(
        gain_db=args.gain_db,
        out_channels=out_channels,
        trim_start=args.trim[0] if args.trim else 0.0,
        trim_end=args.trim[1] if args.trim else None,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        lowpass_cutoff=args.lowpass,
        lowpass_taps=args.taps,
        out_rate=args.resample,
    )
    try:
        stats = process_file(args.input, args.output, pipeline,
                             block_frames=args.block_frames)
    except (WavError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"input : {stats['channels_in']} ch @ "
          f"{stats['sample_rate_in']} Hz, {stats['frames_in']} frames")
    print(f"output: {stats['channels_out']} ch @ "
          f"{stats['sample_rate_out']} Hz, {stats['frames_out']} frames")
    print(f"peak (float, pre-clip): {stats['peak_float']:.6f}")
    print(f"saturated samples     : {stats['clipped_samples']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
