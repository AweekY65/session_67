"""Local-only WAV audio processing tool.

Everything runs on local files / in memory. No audio servers, no cloud
APIs, no media services, no network access.
"""

from .wavio import WavInfo, WavError, read_wav, write_wav
from . import dsp
from .stream import Pipeline, process_file

__all__ = [
    "WavInfo",
    "WavError",
    "read_wav",
    "write_wav",
    "dsp",
    "Pipeline",
    "process_file",
]

__version__ = "0.1.0"
