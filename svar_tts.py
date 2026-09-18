#!/usr/bin/env python3
"""
SVAR TTS pipeline
-----------------
Takes Swedish text -> Piper (NST voice) -> raw WAV
Then runs it through an ffmpeg filter chain to give it that
vintage EAS / weather-radio character (bandpass, crush, echo, static bed).

Requirements:
    pip install piper-tts --break-system-packages
    ffmpeg installed and on PATH

Model download (one-time, do this manually before running):
    Get sv_SE-nst-medium.onnx and its .json config from the Piper voices repo:
    https://huggingface.co/rhasspy/piper-voices/tree/main/sv/sv_SE/nst/medium
    Put both files in ./models/

Usage:
    python3 svar_tts.py "Just nu i Malmö: 14 grader, måttlig vind från sydväst." output.mp3
    python3 svar_tts.py "Text here" output.mp3 --alert   (adds a sting + heavier processing)
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODEL_DIR / "sv_SE-nst-medium.onnx"

STATIC_BED_LEVEL = 0.035     # volume of background static/hiss, keep low
PITCH_SEMITONES_DOWN = 2.5   # cheap "serious announcer" trick


def synthesize_raw(text: str, raw_wav_path: Path):
    """Run Piper to generate raw speech WAV from Swedish text."""
    if not MODEL_PATH.exists():
        sys.exit(f"Missing model file: {MODEL_PATH}\n"
                  f"Download sv_SE-nst-medium.onnx (+ .json) into {MODEL_DIR} first.")

    cmd = [
        "piper",
        "--model", str(MODEL_PATH),
        "--output_file", str(raw_wav_path),
    ]
    subprocess.run(cmd, input=text.encode("utf-8"), check=True)


def build_filter_chain(is_alert: bool) -> str:
    """
    ffmpeg audio filter graph:
    1. pitch shift down slightly (asetrate + atempo trick)
    2. bandpass EQ to sound like an old speaker / PA system
    3. light bitcrush via downsample -> upsample
    4. echo for that "PA slap-back" feel
    5. mix in a low static/hiss bed (anoisesrc)
    """
    # asetrate lowers pitch, atempo compensates speed back to normal-ish
    semitone_ratio = 2 ** (-PITCH_SEMITONES_DOWN / 12)
    new_rate = int(22050 * semitone_ratio)

    voice_chain = (
        f"[0:a]"
        f"asetrate={new_rate},aresample=22050,"
        f"atempo={1/semitone_ratio:.4f},"
        f"highpass=f=300,lowpass=f=3400,"
        f"aresample=8000,aresample=22050,"  # crude bitcrush via downsample/upsample
        f"aecho=0.8:0.7:40:0.25"
        f"[voice]"
    )

    if is_alert:
        # push the effect harder for alert headers
        voice_chain = voice_chain.replace("highpass=f=300,lowpass=f=3400",
                                           "highpass=f=400,lowpass=f=3000")

    noise_chain = (
        f"anoisesrc=color=pink:amplitude={STATIC_BED_LEVEL}[static]"
    )

    mix_chain = (
        f"[voice][static]amix=inputs=2:duration=first:dropout_transition=0[out]"
    )

    return f"{voice_chain};{noise_chain};{mix_chain}"


def process_audio(raw_wav_path: Path, output_path: Path, is_alert: bool):
    filter_complex = build_filter_chain(is_alert)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(raw_wav_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "4",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description="SVAR vintage weather-radio TTS")
    parser.add_argument("text", help="Swedish text to speak")
    parser.add_argument("output", help="Output audio file (e.g. output.mp3)")
    parser.add_argument("--alert", action="store_true",
                         help="Apply heavier alert-style processing")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        raw_wav = Path(tmp) / "raw.wav"
        synthesize_raw(args.text, raw_wav)
        process_audio(raw_wav, Path(args.output), args.alert)

    print(f"Done -> {args.output}")


if __name__ == "__main__":
    main()
