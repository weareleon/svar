#!/usr/bin/env python3
"""
SVAR streamer
-------------
Ties svar_data.py + svar_tts.py together into a continuous broadcast:
  1. Poll SMHI for conditions/forecast/warnings
  2. Synthesize each phrase with the vintage TTS pipeline
  3. Feed the audio into a persistent ffmpeg process streaming to Icecast
  4. Push a "now playing" title to Icecast's admin API for each segment

Requires: svar_data.py and svar_tts.py in the same folder (this script
imports fetch/phrase functions from svar_data, and shells out to
svar_tts.py's CLI for synthesis).

Config (environment variables, see .env.example):
    ICECAST_SOURCE_PW   required - Icecast source password
    ICECAST_HOST        default: localhost
    ICECAST_PORT        default: 8000
    ICECAST_MOUNT       default: /svar
    ICECAST_ADMIN_USER  default: admin   (only used for now-playing metadata)
    ICECAST_ADMIN_PW    optional - if unset, now-playing updates are skipped
    BROADCAST_TIMEZONE  default: Europe/Stockholm

Usage:
    python3 svar_stream.py
"""

import os
import math
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import svar_data  # reuse fetch_forecast / fetch_warnings / phrase builders

# ---- Config ----------------------------------------------------------
ICECAST_HOST = os.environ.get("ICECAST_HOST", "localhost")
ICECAST_PORT = os.environ.get("ICECAST_PORT", "8000")
ICECAST_MOUNT = os.environ.get("ICECAST_MOUNT", "/svar")
ICECAST_SOURCE_PW = os.environ.get("ICECAST_SOURCE_PW")
ICECAST_ADMIN_USER = os.environ.get("ICECAST_ADMIN_USER", "admin")
ICECAST_ADMIN_PW = os.environ.get("ICECAST_ADMIN_PW")
BROADCAST_TIMEZONE = ZoneInfo(os.environ.get("BROADCAST_TIMEZONE", "Europe/Stockholm"))

REFRESH_INTERVAL_SEC = 600   # re-poll SMHI every 10 minutes
SEGMENT_GAP_SEC = 1.5        # silence between spoken segments
BULLETIN_BEEP_SEC = 0.30      # cue tone separating regional bulletins
SAMPLE_RATE = 44100
CHANNELS = 2

SCRIPT_DIR = Path(__file__).parent
TTS_SCRIPT = SCRIPT_DIR / "svar_tts.py"



def stream_url() -> str:
    # Percent-encode the password so characters like ! @ / : don't break the URL.
    return (f"icecast://source:{quote(ICECAST_SOURCE_PW, safe='')}"
            f"@{ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}")


ADMIN_METADATA_URL = f"http://{ICECAST_HOST}:{ICECAST_PORT}/admin/metadata"

SWEDISH_WEEKDAYS = (
    "måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag",
)
SWEDISH_MONTHS = (
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
)


def start_icecast_process() -> subprocess.Popen:
    """Persistent ffmpeg process: raw PCM in via stdin -> mp3 out to Icecast."""
    cmd = [
        "ffmpeg", "-loglevel", "warning",
        # Pace a pipe input at normal playback speed.  Without this, ffmpeg can
        # consume a complete bulletin as fast as the machine can produce it.
        "-re",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-i", "pipe:0",
        "-c:a", "libmp3lame", "-b:a", "128k",
        "-content_type", "audio/mpeg",
        "-ice_name", "SVAR",
        "-ice_description", "Skanes Vader- och Alarmradio",
        "-ice_genre", "Weather",
        "-f", "mp3", stream_url(),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def update_now_playing(title: str):
    """Push ICY metadata update to Icecast without dropping the stream connection."""
    if not ICECAST_ADMIN_PW:
        return
    try:
        requests.get(
            ADMIN_METADATA_URL,
            params={"mode": "updinfo", "mount": ICECAST_MOUNT, "song": title},
            auth=(ICECAST_ADMIN_USER, ICECAST_ADMIN_PW),
            timeout=5,
        )
    except requests.RequestException as e:
        print(f"[warn] failed to update metadata: {e}", file=sys.stderr)


def synthesize_segment(text: str, is_alert: bool, out_path: Path):
    """Shell out to svar_tts.py to render one phrase with vintage processing."""
    cmd = [sys.executable, str(TTS_SCRIPT), text, str(out_path)]
    if is_alert:
        cmd.append("--alert")
    subprocess.run(cmd, check=True)


def decode_to_pcm(audio_path: Path) -> bytes:
    """Decode an mp3 segment to raw PCM matching the stream format."""
    cmd = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-i", str(audio_path),
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return result.stdout


def silence_pcm(seconds: float) -> bytes:
    num_samples = int(SAMPLE_RATE * seconds * CHANNELS)
    return b"\x00\x00" * num_samples


def bulletin_beep_pcm() -> bytes:
    """Return a short, click-free stereo cue tone in the stream's PCM format."""
    frames = int(SAMPLE_RATE * BULLETIN_BEEP_SEC)
    fade_frames = int(SAMPLE_RATE * 0.015)
    audio = bytearray()
    for frame in range(frames):
        envelope = min(1.0, frame / fade_frames, (frames - frame - 1) / fade_frames)
        sample = int(0.18 * 32767 * envelope * math.sin(2 * math.pi * 1000 * frame / SAMPLE_RATE))
        audio.extend(struct.pack("<hh", sample, sample))
    return bytes(audio)


def broadcast_time_phrase() -> str:
    """Build a fresh local time announcement for the Skåne broadcast area."""
    now = datetime.now(BROADCAST_TIMEZONE)
    return (
        f"SVAR, Skånes Väder- och Alarmradio. "
        f"Det är {SWEDISH_WEEKDAYS[now.weekday()]} den {now.day} "
        f"{SWEDISH_MONTHS[now.month - 1]} {now.year}, "
        f"klockan {now.hour:02d}:{now.minute:02d} i Skåne."
    )


def build_segments():
    """Fetch fresh SMHI data and return a list of (text, is_alert) tuples."""
    segments = []
    try:
        segments.extend((phrase, False) for phrase in svar_data.skane_forecast_phrases())
    except Exception as e:
        # Individual locations are already handled in svar_data; retain this
        # guard so an unexpected bad API response cannot stop the broadcast.
        print(f"[warn] Skåne forecast build failed: {e}", file=sys.stderr)

    try:
        warnings_data = svar_data.fetch_warnings()
        for alert_text in svar_data.active_warning_phrases(warnings_data):
            segments.append((alert_text, True))
    except requests.RequestException as e:
        print(f"[warn] warnings fetch failed: {e}", file=sys.stderr)

    if not segments:
        segments.append(("SVAR, Skånes Väder- och Alarmradio sänder regional information för Skåne.", False))

    return segments


def write_pcm(proc: subprocess.Popen, pcm: bytes) -> subprocess.Popen:
    """Write audio, restarting the source process if its pipe has closed."""
    if proc.poll() is not None:
        print("[warn] Icecast encoder exited; restarting it", file=sys.stderr)
        proc = start_icecast_process()
    try:
        proc.stdin.write(pcm)
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        print("[warn] Icecast encoder pipe broke; restarting it", file=sys.stderr)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        proc = start_icecast_process()
    return proc


def main():
    if not ICECAST_SOURCE_PW:
        sys.exit("ICECAST_SOURCE_PW is not set. Copy .env.example to .env and export it "
                 "(e.g. `set -a; source .env; set +a`).")
    proc = start_icecast_process()
    print(f"Streaming to {ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}")

    last_refresh = 0
    segments = []

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            while True:
                if time.time() - last_refresh > REFRESH_INTERVAL_SEC or not segments:
                    segments = build_segments()
                    last_refresh = time.time()
                    print(f"[info] refreshed data, {len(segments)} segment(s) queued")

                # This is generated for every cycle, not only each data refresh,
                # so listeners receive the actual current local time.
                bulletin = [(broadcast_time_phrase(), False), *segments]
                for text, is_alert in bulletin:
                    seg_path = tmp_path / "segment.mp3"
                    synthesize_segment(text, is_alert, seg_path)
                    pcm = decode_to_pcm(seg_path)

                    title = ("VARNING - " + text[:60]) if is_alert else text[:60]
                    update_now_playing(title)

                    proc = write_pcm(proc, pcm)
                    proc = write_pcm(proc, silence_pcm(SEGMENT_GAP_SEC))

                    # bail out early to re-check for fresh data / alerts
                    if time.time() - last_refresh > REFRESH_INTERVAL_SEC:
                        break

                # Mark the end of one bulletin before immediately starting the
                # next. Fresh SMHI data is still fetched on the interval above.
                proc = write_pcm(proc, bulletin_beep_pcm())

    except KeyboardInterrupt:
        print("\nStopping stream...")
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
