"""Chunked processing pipeline: reader -> stages -> writer.

Only one block of samples (plus small per-stage state) is held in memory
at any time, so arbitrarily large files can be processed without loading
full copies into RAM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .wavio import WavReader, WavWriter


@dataclass
class Metrics:
    frames_in: int = 0
    frames_out: int = 0
    peak_in: float = 0.0
    peak_out: float = 0.0
    sumsq_in: float = 0.0
    sumsq_out: float = 0.0
    samples_out: int = 0

    @property
    def rms_in(self) -> float:
        return math.sqrt(self.sumsq_in / max(1, self._n_in))

    @property
    def rms_out(self) -> float:
        return math.sqrt(self.sumsq_out / max(1, self.samples_out))

    _n_in: int = field(default=0, repr=False)


def run_pipeline(reader: WavReader, writer: WavWriter, stages: list,
                 block_frames: int = 4096) -> Metrics:
    m = Metrics()
    while True:
        block = reader.read_block(block_frames)
        if not block:
            break
        m.frames_in += len(block)
        m._n_in += len(block) * reader.info.channels
        for f in block:
            for x in f:
                ax = abs(x)
                if ax > m.peak_in:
                    m.peak_in = ax
                m.sumsq_in += x * x
        for s in stages:
            block = s.process(block)
        m.frames_out += len(block)
        for f in block:
            for x in f:
                ax = abs(x)
                if ax > m.peak_out:
                    m.peak_out = ax
                m.sumsq_out += x * x
                m.samples_out += 1
        writer.write_block(block)

    # Flush: propagate each stage's tail downstream in order.
    carry: list = []
    for s in stages:
        if carry:
            carry = s.process(carry)
        tail = s.flush()
        if tail:
            carry = carry + tail
    if carry:
        m.frames_out += len(carry)
        for f in carry:
            for x in f:
                ax = abs(x)
                if ax > m.peak_out:
                    m.peak_out = ax
                m.sumsq_out += x * x
                m.samples_out += 1
        writer.write_block(carry)
    return m
