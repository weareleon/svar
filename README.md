# SVAR – Skånes Väder- och Alarmradio

A self-hosted, NOAA Weather Radio–style station for southern Sweden. SVAR pulls live
forecasts and active warnings from [SMHI](https://opendata.smhi.se/), turns them into spoken
Swedish, runs the voice through a vintage PA/EAS-style filter chain, and streams it 24/7 to
an Icecast mount.

No API keys needed – SMHI's open data is free.

## How it works

```
SMHI forecast API ─┐
                   ├─► svar_data.py ─► Swedish phrases
SMHI warnings API ─┘                        │
                                            ▼
                     svar_tts.py  (Piper sv_SE-nst voice → ffmpeg vintage filters)
                                            │
                                            ▼
                     svar_stream.py ─► persistent ffmpeg ─► Icecast mount
                                   └─► ICY "now playing" metadata
```

| File | Role |
|---|---|
| `svar_data.py` | Fetches forecasts for six Skåne locations (Malmö, Helsingborg, Landskrona, Hässleholm, Kristianstad, Ystad) and active Skåne warnings; builds Swedish phrases. Also runnable standalone. |
| `svar_tts.py` | Text → Piper → ffmpeg filter chain (pitch-down, bandpass, bitcrush, slap-back echo, static bed). `--alert` applies heavier processing. |
| `svar_stream.py` | Main loop: time announcement + forecasts + warnings, repeated continuously, with a cue beep between bulletins. Re-polls SMHI every 10 minutes. |

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) on your `PATH` (with `libmp3lame`)
- An Icecast server you can source to
- [Piper](https://github.com/rhasspy/piper) (`piper-tts`) and the Swedish NST voice

## Setup

```bash
git clone https://github.com/weareleon/svar svar && cd svar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Download the voice model (one-time) into `./models/`:

- `sv_SE-nst-medium.onnx`
- `sv_SE-nst-medium.onnx.json`

from <https://huggingface.co/rhasspy/piper-voices/tree/main/sv/sv_SE/nst/medium>.

Configure Icecast credentials:

```bash
cp .env.example .env
# edit .env, then load it:
set -a; source .env; set +a
```

| Variable | Default | Notes |
|---|---|---|
| `ICECAST_SOURCE_PW` | – | **Required.** Icecast source password. |
| `ICECAST_HOST` | `localhost` | |
| `ICECAST_PORT` | `8000` | |
| `ICECAST_MOUNT` | `/svar` | |
| `ICECAST_ADMIN_USER` | `admin` | Used for now-playing metadata. |
| `ICECAST_ADMIN_PW` | – | Optional. If unset, metadata updates are skipped. |
| `BROADCAST_TIMEZONE` | `Europe/Stockholm` | Used for the spoken time announcement. |

## Usage

Try each stage on its own first:

```bash
# Print phrases for one place / for all of Skåne (plus active warnings)
python3 svar_data.py --place "Malmö" --lat 55.6050 --lon 13.0038
python3 svar_data.py --skane

# Render a single line of vintage-processed speech
python3 svar_tts.py "Just nu i Malmö: 14 grader, måttlig vind från sydväst." out.mp3
python3 svar_tts.py "Varning: gul varning för vind i Skåne." alert.mp3 --alert
```

Then go live:

```bash
python3 svar_stream.py
```

Listen at `http://<ICECAST_HOST>:<ICECAST_PORT>/svar`.

### Running as a service (systemd)

```ini
[Unit]
Description=SVAR weather radio
After=network-online.target

[Service]
WorkingDirectory=/opt/svar
EnvironmentFile=/opt/svar/.env
ExecStart=/opt/svar/.venv/bin/python svar_stream.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Customising

- **Locations:** edit `SKANE_LOCATIONS` in `svar_data.py`.
- **Warning area:** edit `SKANE_DISTRICT_NAMES` in `svar_data.py`.
- **Voice character:** tweak `PITCH_SEMITONES_DOWN`, `STATIC_BED_LEVEL` and `build_filter_chain()` in `svar_tts.py`.
- **Timing:** `REFRESH_INTERVAL_SEC`, `SEGMENT_GAP_SEC`, `BULLETIN_BEEP_SEC` in `svar_stream.py`.

## Notes & limitations

- Each phrase is synthesised on demand, so the very first segment after start-up has a short delay.
- Warnings come from SMHI's impact-based warning feed and are matched by district name; check the output of `svar_data.py` against SMHI's site for your area.
- This is a hobby project, **not an official or safety-critical warning service**. Always use official channels (SMHI, 112, Viktigt Meddelande till Allmänheten) for real alerts.

## Credits

- Weather data: [SMHI Open Data](https://opendata.smhi.se/) (CC BY 4.0 – attribute SMHI)
- Speech: [Piper](https://github.com/rhasspy/piper) with the NST Swedish voice
- Streaming: [Icecast](https://icecast.org/) + [FFmpeg](https://ffmpeg.org/)

## License

MIT – see [LICENSE](LICENSE).
