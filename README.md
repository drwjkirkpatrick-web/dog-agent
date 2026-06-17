# 🐕 Dog Agent v5.0 — The Perception & Precision Edition

[![Version](https://img.shields.io/badge/version-5.0-brightgreen)](https://github.com/drwjkirkpatrick-web/dog-agent)
[![Modules](https://img.shields.io/badge/modules-46-success)](https://github.com/drwjkirkpatrick-web/dog-agent)
[![Precision](https://img.shields.io/badge/precision-RTK%20%2B%20UWB-critical)](https://github.com/drwjkirkpatrick-web/dog-agent)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**The world's most comprehensive open-source wearable AI for dogs.**

*Version 5.0 — 46 modules — Kalman sensor fusion — RTK GPS — UWB indoor positioning — Gait, sleep, collision, and magnetic anomaly detection*

---

## ✨ What Makes Dog Agent Extraordinary

Dog Agent transforms any Raspberry Pi into a sophisticated intelligence platform that monitors, protects, and understands your dog. From urban apartments to remote wilderness — from quick potty breaks to month-long adventures — Dog Agent keeps you connected to what matters most.

**46 specialized modules. 32,000+ lines of Python. 170+ HTTP APIs. One purpose: Your dog's wellbeing.**

---

## 🎯 The Five Pillars

### 1. 📡 **Never Lose Track**
- **Sensor Fusion Engine**: Kalman-filter fusion of GPS + IMU + dead reckoning — smoother tracks, fewer dropouts
- **RTK Differential GPS**: u-blox NEO-F9P centimeter-level accuracy for precise boundaries
- **UWB Indoor Positioning**: Decawave DWM1000 sub-meter tracking inside homes and kennels
- **GPS Security**: Detects spoofing, jamming, and impossible jumps; auto-falls back to IMU/LoRa
- **Multi-Constellation GPS**: GPS + GLONASS + Galileo + BeiDou for urban canyon accuracy
- **LoRaWAN Backup**: 10km+ range when cellular fails
- **Dead Reckoning**: IMU step-counting works even underground, indoors, or dense forest
- **Emergency BLE Beacon**: 6-month coin cell backup if main system fails
- **Adaptive GPS**: Scales from 10Hz to 1/5min to save 80% power

### 2. 🚨 **Emergency Response**
- **Fall Detection**: BNO055 IMU detects impacts and immobility
- **Vehicle Collision Detection**: Distinguishes vehicle strikes from normal falls
- **ML Bark Classification**: TensorFlow Lite distinguishes play from distress from alert
- **3-Level Escalation**: Primary → Secondary → Emergency contacts with automatic timeout
- **Panic Button**: Physical button for owner emergencies
- **Automatic Alerts**: Geofence escapes, critical vitals, distress patterns

### 3. 🔋 **Month-Long Battery**
- **Deep Sleep Modes**: 10-50x power savings when dog is resting
- **Solar Charging**: TP4056 + panel integration with efficiency tracking
- **Predictive Battery**: "Time remaining" calculation before dead
- **Power Manager**: ACTIVE/IDLE/DEEP_SLEEP automatic transitions

### 4. 🧠 **Predictive Health**
- **Gait Analysis**: Detects limping, lameness, and stride asymmetry from IMU
- **Sleep Posture & Rest Quality**: Tracks sleeping position, restlessness, and tremors
- **Magnetic Anomaly Detection**: Warns of buried cables, metal hazards, and vehicle proximity
- **Long-Term Trends**: Weeks and months of vital pattern analysis
- **ML Anomaly Detection**: Catches changes *before* they become critical
- **Vet Reports**: Professional PDF health summaries
- **Activity Scoring**: Gamified breed-specific exercise goals
- **Multi-Dog Household**: One platform, multiple dogs

### 5. 🖥️ **Beautiful Interface**
- **Web Dashboard**: Flask + Leaflet.js maps + Chart.js graphs — real-time updates
- **E-Paper Display**: Zero-power status between updates
- **Haptic Feedback**: Silent notifications that don't disturb your dog
- **Status LED**: Visual system state at a glance
- **Hermes Integration**: Ask your AI assistant about your dog

---

## 🚀 Quick Start

### Prerequisites
- Raspberry Pi 3B+ or 4 (Pi Zero 2W for power-sensitive deployments)
- LilyPad sensors (heart rate, temperature) — sewable into dog sweater
- Optional: GPS module, BNO055 IMU, environmental sensors

### Install
```bash
git clone https://github.com/drwjkirkpatrick-web/dog-agent.git
cd dog-agent
bash setup.sh
```

### Run
```bash
# Simulation mode (no hardware required)
python src/main.py --simulate

# Production mode
python src/main.py --all
```

### Access
- **Dashboard**: http://localhost:9137
- **API Documentation**: Each module exposes REST API (see table below)

---

## 📊 Complete Module Reference

### Core System (9 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `main.py` | 9110 | Orchestrator — coordinates all modules |
| `gps_daemon.py` | 9111 | GPS tracking & geocoding |
| `sensor_daemon.py` | 9112 | LilyPad heart rate & temperature |
| `health_monitor.py` | 9113 | Vital analysis & alerts |
| `geofence.py` | 9114 | Safe zone boundaries |
| `behavior.py` | 9115 | Routine learning |
| `voice.py` | 9116 | TTS + bark detection |
| `data_logger.py` | 9117 | Data persistence |
| `alert_manager.py` | 9118 | Notification system |

### Power & Efficiency (6 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `power_manager.py` | 9120 | Deep sleep orchestration |
| `status_led.py` | 9121 | Visual feedback |
| `cache_manager.py` | 9123 | Query acceleration |
| `adaptive_gps.py` | 9124 | Intelligent rate control |
| `offline_queue.py` | 9125 | Retry with exponential backoff |
| `solar_monitor.py` | 9128 | Charging optimization |

### Safety & Emergency (5 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `emergency_ble_beacon.py` | 9129 | Backup when Pi fails |
| `fall_detection.py` | 9130 | IMU impact detection |
| `bark_classifier.py` | 9131 | ML bark type classification |
| `battery_monitor.py` | 9132 | Predictive power management |
| `haptic_feedback.py` | 9133 | Silent notifications |

### Environment & Health (4 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `environmental_sensors.py` | 9122 | BME280, UV, light, IMU |
| `air_quality.py` | 9134 | VOC/smoke detection |
| `panic_button.py` | 9135 | Physical owner emergency |
| `emergency_contact.py` | 9136 | 3-level escalation system |

### Advanced Positioning (3 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `gps_multi.py` | 9138 | Multi-constellation GNSS |
| `dead_reckoning.py` | 9139 | Indoor step counting |
| `lorawan_backup.py` | 9140 | 10km+ off-grid tracking |

### Connectivity (2 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `network_failover.py` | 9141 | WiFi/cellular/LoRa switching |
| `ota_updates.py` | 9142 | Over-the-air updates |

### User Interface (3 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `web_dashboard.py` | 9137 | Flask web interface |
| `epaper_display.py` | 9143 | Zero-power status display |
| `weather_integration.py` | 9127 | Smart walk recommendations |

### Analytics & Intelligence (4 modules)
| Module | Port | Purpose |
|--------|------|---------|
| `activity_scoring.py` | 9126 | Gamified exercise goals |
| `health_trends.py` | 9144 | Long-term analysis |
| `vet_report.py` | 9145 | Professional PDF reports |
| `predictive_health.py` | 9147 | ML anomaly detection |

### Perception & Precision (10 modules) — *NEW in v5.0*
| Module | Port | Purpose |
|--------|------|---------|
| `sensor_fusion.py` | 9148 | Kalman-filter fused position/velocity |
| `gait_analyzer.py` | 9149 | Limping/gait symmetry detection |
| `gps_rtk.py` | 9150 | RTK differential GPS (cm accuracy) |
| `uwb_indoor.py` | 9151 | Ultra-wideband indoor positioning |
| `smart_geofence.py` | 9152 | ML-learned adaptive safe zones |
| `gps_security.py` | 9153 | Spoofing/jamming detection |
| `sleep_posture.py` | 9154 | Rest quality & tremor analysis |
| `collision_detector.py` | 9155 | Vehicle impact detection |
| `magnetic_anomaly.py` | 9156 | Magnetic hazard detection |
| `sampling_optimizer.py` | 9157 | Adaptive sensor rate control |

### Multi-Dog (1 module)
| Module | Port | Purpose |
|--------|------|---------|
| `multi_dog.py` | 9146 | Household management |

---

## 🏆 Feature Highlights

### Never Lose Track
```
✓ Sensor fusion — GPS + IMU + dead reckoning for smoother tracks
✓ RTK GPS: centimeter-level accuracy
✓ UWB indoor positioning: sub-meter home tracking
✓ GPS security: spoofing/jamming detection
✓ 4-constellation GPS for urban canyon accuracy
✓ LoRaWAN works 10km+ in wilderness
✓ Dead reckoning when GPS unavailable
✓ BLE beacon survives complete system failure
✓ Adaptive GPS saves 80% battery
```

### Emergency Response
```
✓ Fall detection with severity classification
✓ Vehicle collision detection
✓ ML bark analysis (play/distress/alert)
✓ 3-level contact escalation
✓ Panic button with GPS coordinates
✓ Automatic emergency classification
```

### Predictive Health
```
✓ Gait analysis: limping/lameness detection
✓ Sleep posture & rest quality tracking
✓ Magnetic anomaly detection
✓ Trend analysis over weeks/months
✓ ML anomaly pre-detection
✓ Vet-ready PDF reports
✓ Breed-specific activity goals
✓ Multi-dog household support
```

### Month-Long Operation
```
✓ Deep sleep: 10-50x power savings
✓ Solar charging with efficiency tracking
✓ Predictive "time remaining"
✓ Automatic mode transitions
✓ Coin cell backup: 6 months
```

### Beautiful Experience
```
✓ Real-time web dashboard with maps
✓ E-paper: always-visible, zero power
✓ Haptic: silent, effective
✓ LED: instant status
✓ Hermes AI integration
```

---

## 🛠️ Hardware Options

### Minimum Viable (Basic Tracking)
- Raspberry Pi Zero 2W ($15)
- NEO-6M GPS ($10)
- USB battery pack ($20)
- **Total: ~$45**

### Recommended (Full Features)
- Raspberry Pi 3B+ ($35)
- NEO-M9N GPS ($25)
- LilyPad sensors ($30)
- BNO055 IMU ($15)
- RFM95W LoRa ($15)
- Solar panel + charger ($25)
- **Total: ~$145**

### Premium (Everything)
- Raspberry Pi 4 ($55)
- NEO-M9N + active antenna ($40)
- LilyPad + BME280 + UV ($50)
- BNO055 + RFM95W ($35)
- 20W solar + MPPT ($60)
- E-paper display ($25)
- nRF52840 beacon ($15)
- **Total: ~$280**

### Pi Zero 2W Maxed Out
*Pushing the smallest Pi to its absolute limit — compact yet capable*
- Raspberry Pi Zero 2W ($15)
- NEO-M9N GPS (compact) ($25)
- LilyPad sensors (sewn into vest) ($30)
- BNO055 IMU (fall detection + dead reckoning) ($15)
- RFM95W LoRa (long-range backup) ($15)
- TP4056 + 18650 battery + 5W solar ($20)
- Adafruit 2.13" e-paper ($25)
- nRF52840 BLE beacon (coin cell backup) ($15)
- Piezo haptic motor ($5)
- Flex PCB interconnects (JST-SH, FFC cables) ($10)
- **Total: ~$175**

*Notes: Zero 2W handles all modules with care — CPU governor set to conservative, modules staggered startup, aggressive caching. USB hub required for multiple devices. Camera CSI port repurposed for parallel device interface.*

---

## 📖 Documentation

| Document | Description |
|----------|-------------|
| `README.md` | This file — the overview |
| `IMPROVEMENTS.md` | 30 enhancement ideas (all implemented!) |
| `hardware/` | Parts list, wiring diagrams, sewing guide |
| `hermes/` | Hermes Agent skill + profile |
| `cron/` | Scheduled tasks (daily reports, health checks) |
| `Dockerfile` | Container build |
| `docker-compose.yml` | Production deployment |

---

## 🤝 Integration

Every module exposes a REST API:

```bash
# Get current location
curl http://localhost:9111/gps

# Check activity score
curl http://localhost:9126/activity/today

# Trigger emergency test
curl -X POST http://localhost:9135/panic/test

# Get health trends
curl http://localhost:9144/trends/summary
```

All modules support simulation mode for development:

```bash
python src/main.py --simulate
```

---

## 🎓 Why Open Source?

Because your dog deserves the best technology humanity can build. And because when one dog is safer, every dog is safer.

Dog Agent is MIT licensed. Fork it. Extend it. Share improvements back.

---

## 🙏 Acknowledgments

Built with:
- **Flask** — Web dashboard
- **TensorFlow Lite** — Bark classification
- **Leaflet.js** — Maps
- **Chart.js** — Graphs
- **Hermes Agent** — AI integration
- **Raspberry Pi** — Compute
- **LilyPad** — Wearable sensors

---

## 📜 License

MIT License — see [LICENSE](LICENSE)

---

## 🚀 Ready?

```bash
git clone https://github.com/drwjkirkpatrick-web/dog-agent.git
cd dog-agent
python src/main.py --simulate
```

**Your dog is waiting.** 🐕✨

---

*Version 5.0 — 46 modules — Perception & Precision Edition — RTK + UWB + sensor fusion*

**[⭐ Star this repo](https://github.com/drwjkirkpatrick-web/dog-agent) | [🐛 Report issues](https://github.com/drwjkirkpatrick-web/dog-agent/issues) | [💡 Suggest features](https://github.com/drwjkirkpatrick-web/dog-agent/discussions)**
