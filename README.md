# 🐕 Dog Agent — Your Dog's Personal AI Companion

[![Version](https://img.shields.io/badge/version-2.0-blue)](https://github.com/drwjkirkpatrick-web/dog-agent)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Never lose track of your best friend. Know they're safe, healthy, and happy — even when you're not there.**

## 🆕 Version 2.0 — "Supercharged"

We've added **8 new modules** to make Dog Agent more powerful than ever:

| New Feature | Benefit |
|-------------|---------|
| 🔋 **Deep Sleep Mode** | Battery lasts 3-5 days instead of hours |
| 💡 **Status LED** | See system status at a glance on the collar |
| 🌡️ **Environmental Sensors** | Detect heat stress, falls, UV exposure |
| ⚡ **Smart GPS** | 80% power savings with adaptive rate control |
| 📶 **Offline Queue** | No data lost when WiFi drops |
| 🏆 **Activity Scoring** | Gamified goals keep your dog healthy |
| 🌦️ **Weather Integration** | "Too hot for long walks" alerts |
| ☀️ **Solar Monitoring** | Track solar charging efficiency |

**Total: 17 modules, 13,700 lines of code, fully open source.**

Dog Agent is a lightweight AI system that lives right in your dog's collar or sweater. It tracks location, monitors health, learns routines, and alerts you to anything unusual. Built for Raspberry Pi with optional sensors you can sew right into the fabric.

---

## ✨ What Dog Agent Does For You

### 🗺️ **Always Know Where They Are**
Your dog goes for an unscheduled solo adventure? Dog Agent sends you their exact GPS location instantly. Get alerts the moment they leave your yard, with real-time tracking on a map. Even shows you which direction they're heading.

**The peace of mind:** Whether your dog is a known escape artist or just likes to explore, you'll know within seconds if they've gone beyond safe boundaries.

---

### ❤️ **Catch Health Issues Early**
Dog Agent monitors heart rate, temperature, and activity 24/7. It learns what's normal *for your specific dog* and alerts you when something's off.

- **Heat stress alerts** when it's too hot for long walks
- **Lethargy detection** when activity drops unexpectedly
- **Nighttime restlessness** tracking (possible discomfort)
- **Fever detection** from temperature trends

**The peace of mind:** Early warning means early intervention. Small changes caught early can prevent bigger problems later.

---

### 🧠 **Understand Their Day**
Ever wonder "What does my dog do all day?" Dog Agent learns their routine and gives you a daily summary:

- How many walks they took and for how long
- Quality of rest during the night
- Activity level compared to their normal
- Moments of excitement or stress

**The insight:** Patterns reveal what makes your dog happy, when they need more exercise, or if something's disrupting their normal behavior.

---

### 🎤 **Talk to Your Dog (Really)**
Heading home from work? Send a voice message through Dog Agent and it plays through a speaker in their sweater. They hear your voice, you feel more connected.

**The connection:** Separation is easier when they know you're thinking of them.

---

### 🚨 **Smart Alerts That Matter**
Dog Agent only bothers you when something actually needs your attention:

| Alert | What It Means | What You Should Do |
|-------|---------------|-------------------|
| 🚨 **ESCAPE** | Dog left safe zone | Check map, call them back |
| ⚠️ **HEALTH** | Heart rate/temp abnormal | Monitor, consider vet visit |
| 🌡️ **HEAT** | Too hot for current activity | Shorten walk, find shade |
| 📉 **ROUTINE** | Missed usual walk time | Make time for them today |
| 🔋 **BATTERY** | Device needs charging | Plug in tonight |

**The peace of mind:** No spam, no false alarms. Just the important stuff.

---

## 🛠️ How It Works

### The Hardware
A small Raspberry Pi computer (about the size of a credit card) plus:

- **GPS module** — Knows exact location
- **Heart rate sensor** — Sewn into chest area of sweater
- **Temperature sensor** — Monitors body temp
- **Accelerometer** — Detects movement, rest, and activity
- **Small speaker** — Plays your voice messages
- **LED status light** — Visual indicator on collar

**Battery:** A small power bank provides 8-12 hours of runtime. Add the optional solar panel for continuous outdoor use.

### The Software
9 specialized modules work together, each handling one job:

| Module | Port | What It Does |
|--------|------|--------------|
| **Orchestrator** | 9110 | Coordinates everything, shows dashboard |
| **GPS** | 9111 | Tracks location, records routes |
| **Sensors** | 9112 | Reads heart rate, temperature, movement |
| **Health** | 9113 | Watches for concerning patterns |
| **Geofence** | 9114 | Knows safe zones, alerts on escape |
| **Behavior** | 9115 | Learns routines, detects deviations |
| **Voice** | 9116 | Plays messages, listens for barks |
| **Alerts** | 9118 | Sends notifications to your phone |
| **Power** | 9120 | Manages battery life, sleep modes |

**Plus new modules:** LED status, environmental sensors, adaptive GPS, offline queue, activity scoring, and weather integration.

---

## 🚀 Getting Started (5 Minutes)

### Option 1: Run in Simulation Mode (No Hardware Needed)
```bash
git clone https://github.com/drwjkirkpatrick-web/dog-agent.git
cd dog-agent
pip install -r requirements.txt
python src/main.py --simulate
```

This generates fake GPS tracks, heart rate data, and sensor readings so you can see how everything works.

### Option 2: Run on Raspberry Pi
```bash
git clone https://github.com/drwjkirkpatrick-web/dog-agent.git ~/dog-agent
cd ~/dog-agent
bash setup.sh
# Edit config.yaml with your settings
python src/main.py --all
```

### Option 3: Docker (Easiest)
```bash
docker-compose -f docker-compose.sim.yml up
```

---

## 🏠 The Perfect Setup

### For Apartment Dogs
- **Core features:** Geofence, health monitoring, daily summaries
- **Battery:** 10,000mAh power bank (charges weekly)
- **Best part:** Know they're safe while you're at work

### For Adventure Dogs
- **Add:** Solar panel, waterproof case, rugged harness
- **Features:** GPS tracking, environmental monitoring, weather alerts
- **Battery:** Solar keeps it running indefinitely
- **Best part:** Track hikes, camping trips, off-leash time

### For Senior Dogs
- **Focus:** Health monitoring, gentle activity tracking
- **Add:** Fall detection, medication reminders
- **Best part:** Catch health changes early, peace of mind

### For Working/Herding Breeds
- **Add:** Activity scoring, detailed exercise tracking
- **Features:** Compare to breed averages, set goals
- **Best part:** Ensure they're getting the stimulation they need

---

## 📱 Talking to Your Dog Agent

Once running, use **Hermes Agent** (or curl) to ask questions:

```bash
# Where is my dog?
curl http://localhost:9111/gps

# How is their health?
curl http://localhost:9113/health

# What's today's activity summary?
curl http://localhost:9115/behavior/summary

# Send a voice message
curl -X POST http://localhost:9116/voice/say \
  -d '{"text": "I\'ll be home soon!"}'
```

Or install the Hermes skill for natural language:
```bash
hermes --profile dog
> "Where is Fido?"
> "Is everything okay with the dog?"
> "How was Fido's walk today?"
```

---

## 🔋 Battery Life Guide

| Mode | Configuration | Runtime |
|------|---------------|---------|
| **Always On** | All features active | 8-12 hours |
| **Smart Sleep** | Auto sleep at home | 24-36 hours |
| **Deep Sleep** | Wake every 5 minutes | 3-5 days |
| **+ Solar** | 2W solar panel | Indefinite (sunny days) |

**Tips:**
- Deep sleep mode wakes for 30 seconds every 5 minutes to check GPS
- Solar panel sewn into sweater back charges during walks
- LED auto-dims at night to save power

---

## 🧵 The Sweater (Yes, Really)

Your dog wears the computer in a custom-fitted sweater:

- **Double-layer back panel** — Inner layer holds electronics, outer is normal fabric
- **Removable pocket** — Velcro closure for washing
- **Conductive thread** — Sewn-in sensors connect without wires
- **Waterproof pouch** — Pi and battery stay dry
- **Status LED** — Sewn into collar for visibility

See `hardware/lilypad_sensor_guide.md` for sewing instructions. Or ask your crafty friend for help — the tech part is all figured out!

---

## 📊 What You Get

Every day, Dog Agent generates:

- **GPS track:** Where they went, how fast, total distance
- **Health summary:** Heart rate range, temperature, rest quality
- **Activity score:** Did they meet their exercise goals?
- **Behavior notes:** Anything unusual about today
- **Photo-worthy moments:** High activity detected (maybe a squirrel?)

**Monthly:**
- Trend analysis showing activity patterns
- Health changes over time
- Best walk routes discovered
- Achievements unlocked

---

## 🔒 Privacy & Security

- **Your data stays yours:** All data stored locally on the Pi
- **Optional cloud:** Only if you configure Telegram or email
- **No tracking:** We don't know where your dog is
- **Open source:** You can audit every line of code

---

## 🆘 When Things Go Wrong

| Problem | Solution |
|---------|----------|
| Lost GPS signal | LED turns yellow, tries again in 30 sec |
| Battery dies | Last known location saved, BLE beacon activates |
| WiFi disconnects | Offline queue stores data, uploads when reconnected |
| False escape alert | Adjust geofence radius in config |
| Too many notifications | Turn off non-critical alerts in config |

---

## 💡 30 Ways to Make Dog Agent Better

See `IMPROVEMENTS.md` for the full roadmap. Highlights include:

- **ML bark classification** — Know if it's play vs distress
- **Fall detection** — For senior dogs
- **Weather integration** — "Too hot for long walks" alerts
- **Activity scoring** — Gamification to meet exercise goals
- **Vet report generator** — Share health data with your vet
- **Multi-dog support** — Track the whole pack

---

## 🎁 Why We Built This

Because dogs are family. Because "they ran off" shouldn't be how a story ends. Because knowing they're okay — even when you can't be there — is priceless.

**Dog Agent isn't about tracking. It's about caring.**

---

## 📚 Full Documentation

| Document | What's Inside |
|----------|---------------|
| `README.md` | This file — the overview |
| `IMPROVEMENTS.md` | 30 ideas for future enhancements |
| `Dockerfile` | Container build for Raspberry Pi |
| `docker-compose.yml` | Production deployment |
| `docker-compose.sim.yml` | Simulation mode (no hardware) |
| `hardware/parts_list.md` | Complete shopping list ($132-$303) |
| `hardware/wiring.md` | GPIO pinout diagrams |
| `hardware/lilypad_sensor_guide.md` | Sewing conductive thread guide |
| `config.example.yaml` | All 150+ configuration options |
| `setup.sh` | One-command installation script |
| `hermes/dog-agent-skill.md` | Hermes Agent skill documentation |
| `hermes/profile.yaml` | Agent personality configuration |

---

## 🤝 Contributing

Found a bug? Have an idea? Dog Agent is open source!

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

See `CONTRIBUTING.md` for guidelines.

---

## 📄 License

MIT — Use it, modify it, share it. Just keep the attribution.

---

## 🙏 Acknowledgments

- Built with **Hermes Agent** by Nous Research
- Powered by **Raspberry Pi** and open source
- Inspired by dogs everywhere who just want to know where their people are

---

**Ready to build one?** Start with simulation mode: `python src/main.py --simulate`

**Questions?** Open an issue on [GitHub](https://github.com/drwjkirkpatrick-web/dog-agent) or reach out to the community.

---

**Version 2.0** — 17 modules | 13,700 lines | Open source forever

*Built with ❤️ for dogs and their people.*
