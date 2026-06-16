# 🐕 Dog Agent — Hermes-Powered Wearable for Your Dog

An open-source AI agent that lives in your dog's sweater. Built on **Hermes Agent** (Nous Research) running on a Raspberry Pi, with GPS tracking, health monitoring, behavioral analysis, and voice interaction — all sewn into a wearable dog sweater.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Dog Sweater                         │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Pi Zero  │  │ Adafruit GPS │  │ LilyPad Arduino │  │
│  │ 2W / 3B+ │  │ (UART GPIO)  │  │ (I2C sensors)  │  │
│  └────┬─────┘  └──────┬───────┘  └───────┬────────┘  │
│       │               │                   │           │
│       └───────────────┼───────────────────┘           │
│                       │                               │
│              ┌────────▼────────┐                      │
│              │  Hermes Agent   │                      │
│              │  (dog profile)   │                      │
│              └────────┬────────┘                      │
│                       │                               │
└───────────────────────┼───────────────────────────────┘
                        │ Telegram / SMS / Voice
                        ▼
                    Dog Owner
```

## Hardware Requirements

| Component | Recommended | Budget Alternative |
|-----------|-------------|-------------------|
| **Pi** | Pi 3 B+ (1GB) | Pi Zero 2 W (512MB) |
| **GPS** | Adafruit Ultimate GPS (PA1616S) | NEO-6M / NEO-8M |
| **Sensors** | LilyPad Arduino + HR/temp/accel | Arduino Nano + standard sensors |
| **Battery** | 5V 10,000mAh power bank | 18650 cells + step-up converter |
| **Cellular** | SIM7600 HAT (optional) | — |
| **Audio** | USB mic + small speaker | I2S MEMS mic + amp |

Full parts list: [hardware/parts_list.md](hardware/parts_list.md)

## Software Modules

| Module | File | Purpose |
|--------|------|---------|
| **GPS Daemon** | `src/gps_daemon.py` | Reads NMEA from UART, serves location via local API |
| **Sensor Daemon** | `src/sensor_daemon.py` | I2C bridge to LilyPad (HR, temp, accelerometer) |
| **Health Monitor** | `src/health_monitor.py` | Vitals analysis, anomaly detection, alert triggers |
| **Geofence** | `src/geofence.py` | Zone management, escape detection, arrival alerts |
| **Behavior** | `src/behavior.py` | Routine learning, pattern detection, daily summaries |
| **Voice** | `src/voice.py` | TTS for talking to dog, STT for commands |
| **Bark Detector** | `src/bark_detector.py` | Microphone-based bark detection and classification |
| **Alert Manager** | `src/alert_manager.py` | Routes alerts to Telegram, SMS, or local log |
| **Data Logger** | `src/data_logger.py` | CSV/JSON logging for all sensor data |
| **Orchestrator** | `src/main.py` | Daemon that runs all modules, serves Hermes API |

## Quick Start

```bash
# 1. Install on the Pi
git clone https://github.com/your-org/dog-agent.git ~/dog-agent
cd ~/dog-agent
bash setup.sh

# 2. Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your Telegram token, home zone, dog's name

# 3. Run
python src/main.py

# 4. Talk to your dog via Hermes
hermes --profile dog
> "Where is Fido?"
> "How was Fido's walk today?"
> "Is Fido's heart rate normal?"
```

## Hermes Integration

The dog agent runs as a **Hermes profile** with a custom skill. The skill teaches Hermes how to:

- Query GPS location
- Check health vitals
- Manage geofences
- Generate daily reports
- Detect barking/patterns
- Talk to the dog via speaker

Install the skill:
```bash
hermes skills install dog-agent
# or
hermes --profile dog --skills dog-agent
```

## Project Structure

```
dog-agent/
├── README.md              # This file
├── setup.sh               # One-command install
├── requirements.txt       # Python dependencies
├── config.example.yaml    # Configuration template
├── src/                   # Python modules
│   ├── gps_daemon.py      # GPS NMEA reader
│   ├── sensor_daemon.py   # LilyPad I2C bridge
│   ├── health_monitor.py  # Vitals analysis
│   ├── geofence.py        # Zone management
│   ├── behavior.py         # Routine learning
│   ├── voice.py           # TTS/STT
│   ├── bark_detector.py   # Bark detection
│   ├── alert_manager.py   # Alert routing
│   ├── data_logger.py     # Data persistence
│   └── main.py            # Orchestrator
├── data/                  # Runtime data
│   ├── health_logs/       # Vitals history
│   ├── gps_tracks/        # GPS track logs
│   ├── behavior/          # Behavior patterns
│   └── zones.json         # Geofence definitions
├── hardware/              # Hardware guides
│   ├── parts_list.md      # BOM
│   ├── wiring.md          # GPIO pinout
│   ├── lilypad_sensor_guide.md
│   └── sweater_pocket_pattern.md
├── cron/                  # Scheduled tasks
│   ├── daily_report.sh
│   ├── health_check.sh
│   └── battery_alert.sh
├── hermes/                # Hermes integration
│   ├── dog-agent-skill.md
│   └── profile.yaml
└── tests/                 # Unit tests
    ├── test_gps.py
    ├── test_geofence.py
    ├── test_health.py
    └── test_behavior.py
```

## License

MIT
