"""Downsample a WAV to the 8 kHz mono 16-bit PCM that FreeSWITCH plays natively.

eSpeak NG only emits at its own rate (22.05 kHz), and the obvious conversion tools
(sox, ffmpeg) are extra installs — so this does the one conversion the prompts need
using nothing but the standard library, which keeps `generate.sh` dependency-free on
any OS.

Each output sample is the mean of the input samples it spans rather than the nearest
one: averaging is a crude low-pass, and without it dropping ~2.8x the samples would
alias high-frequency speech energy down into the voice band as a metallic buzz.
"""
from __future__ import annotations

import array
import sys
import wave

TARGET_RATE = 8000


def resample(src: str, dst: str, rate: int = TARGET_RATE) -> None:
    with wave.open(src, "rb") as source:
        channels, width = source.getnchannels(), source.getsampwidth()
        in_rate, frames = source.getframerate(), source.getnframes()
        if width != 2:
            sys.exit(f"{src}: expected 16-bit PCM, got {width * 8}-bit")
        samples = array.array("h", source.readframes(frames))

    if channels > 1:  # average the channels down to mono
        samples = array.array("h", [
            sum(samples[i:i + channels]) // channels for i in range(0, len(samples), channels)
        ])

    if in_rate == rate:
        out = samples
    else:
        step = in_rate / rate
        out = array.array("h")
        for index in range(int(len(samples) / step)):
            start, end = int(index * step), max(int((index + 1) * step), int(index * step) + 1)
            window = samples[start:end]
            out.append(sum(window) // len(window) if window else 0)

    with wave.open(dst, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(out.tobytes())


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: resample.py <in.wav> <out.wav>")
    resample(sys.argv[1], sys.argv[2])
