"""wavtool: local, offline PCM WAV processing (no external services)."""

from .wavio import WavFormatError, WavInfo, WavReader, WavWriter
from . import dsp

__all__ = ["WavFormatError", "WavInfo", "WavReader", "WavWriter", "dsp"]
__version__ = "0.1.0"
