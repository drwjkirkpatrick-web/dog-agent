---
name: dog-agent-skill
description: End-to-end control and monitoring for the dog-agent system — GPS tracking, health monitoring, geofence management, behavior analysis, voice interaction, alerting, and logging.
version: 1.0.0
author: dog-agent team
type: skill
category: iot
tags:
  - dogs
  - iot
  - monitoring
  - gps
  - health
  - geofence
  - voice
  - alerts
---

# dog-agent-skill

## Overview

This skill transforms Hermes into an intelligent dog caretaker agent. It enables real-time monitoring of a dog's location, health, and behavior, management of safe zones, voice interaction with the dog through a speaker, and configurable alerting. All interactions happen via the dog-agent microservice API running on a local Raspberry Pi (or similar device).

## Architecture

The dog-agent system is composed of **nine microservices**, each exposing a REST API on a dedicated port:

```
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
│  GPS    │  │ Sensors │  │ Health  │  │Geofence │
│ :9111   │  │ :9112   │  │ :9113   │  │ :9114   │
└─────────┘  └─────────┘  └─────────┘  └─────────┘
┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
│Behavior │  │  Voice  │  │ Logger  │  │ Alerts  │
│ :9115   │  │ :9116   │  │ :9117   │  │ :9118   │
└─────────┘  └─────────┘  └─────────┘  └─────────┘
```

All services run on **localhost** (`127.0.0.1`). Skills should check that each service responds before attempting operations.

## API Reference

### GPS Service — `http://localhost:9111`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/gps` | Current GPS position (lat, lng, accuracy, timestamp) |
| GET | `/gps/history` | Recent GPS trail entries |

### Sensors Service — `http://localhost:9112`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/sensors` | All sensor readings (accelerometer, temperature, humidity) |
| GET | `/sensors/temperature` | Current ambient temperature |

### Health Service — `http://localhost:9113`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Vitals summary (heart rate, activity level, last meal, last toilet break) |
| POST | `/health/meal` | Log a meal `{"time": "..."}` |
| POST | `/health/toilet` | Log a toilet break |

### Geofence Service — `http://localhost:9114`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/geofence/zones` | List all safe zones |
| POST | `/geofence/zones` | Create a zone `{"name": "...", "lat": ..., "lng": ..., "radius_m": ...}` |
| DELETE | `/geofence/zones/{id}` | Remove a zone |
| GET | `/geofence/status` | Whether dog is inside/outside any zone |

### Behavior Service — `http://localhost:9115`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/behavior/summary` | Daily behavior summary (rest, play, barking, eating periods) |
| GET | `/behavior/deviations` | Unusual behavior patterns detected |
| GET | `/behavior/deviations/{id}` | Details about a specific deviation |

### Voice Service — `http://localhost:9116`
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/voice/say` | Speak through audio output `{"text": "Good dog!", "voice": "default"}` |
| GET | `/voice/bark/status` | Current bark detection status (barking now, recent barks count) |
| GET | `/voice/bark/history` | Recent bark event log |

### Logger Service — `http://localhost:9117`
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/logger/stats` | Storage and log statistics |
| GET | `/logger/events` | Recent system events |
| DELETE | `/logger/events` | Clear event log |

### Alerts Service — `http://localhost:9118`
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/alerts` | Send an alert `{"alert_type": "geo_breach", "severity": "high", "message": "..."}` |
| GET | `/alerts` | Recent alert history |
| DELETE | `/alerts/{id}` | Dismiss an alert |

## Capabilities

The agent can perform the following actions. Each capability includes the exact API call pattern and a natural-language response template.

### 1. Track Location
**Usage:** Report the dog's current GPS position.
```
GET http://localhost:9111/gps
```
**Response template:** "🐾 **Current Location** — Dog is at `{lat}, {lng}` (±{accuracy}m accuracy). Last updated: {timestamp}."

### 2. Check Health
**Usage:** Get a vitals snapshot.
```
GET http://localhost:9113/health
```
**Response template:** "🩺 **Health Check** — Heart rate: {heart_rate}bpm | Activity: {activity_level} | Last meal: {last_meal} | Last toilet break: {last_toilet}."

### 3. Manage Geofence Zones
**Usage:** List, create, or delete safe zones.
- **List:** `GET http://localhost:9114/geofence/zones`
- **Create:** `POST http://localhost:9114/geofence/zones` with body `{"name": "Backyard", "lat": 40.7128, "lng": -74.0060, "radius_m": 50}`
- **Delete:** `DELETE http://localhost:9114/geofence/zones/{id}`

**Response template (list):** "📍 **Safe Zones** — {count} zone(s) configured: {name_1} ({radius_1}m), {name_2} ({radius_2}m)."
**Response template (create):** "✅ Zone '{name}' created successfully (ID: {id})."
**Response template (delete):** "🗑️ Zone '{name}' deleted."

### 4. Daily Behavior Summary
**Usage:** Retrieve today's behavior overview.
```
GET http://localhost:9115/behavior/summary
```
**Response template:** "📊 **Daily Summary** — {summary_text}"

### 5. Check Behavior Deviations
**Usage:** Detect unusual patterns.
```
GET http://localhost:9115/behavior/deviations
```
**Response template:** "⚠️ **Behavior Deviations** — {count} deviation(s) detected: {details}"

### 6. Speak to Dog
**Usage:** Play an audio message through the speaker.
```
POST http://localhost:9116/voice/say
{"text": "Good dog! Time for a walk."}
```
**Response template:** "🔊 Message sent to speaker: '{text}'"

### 7. Check Bark Status
**Usage:** See if the dog is currently barking.
```
GET http://localhost:9116/voice/bark/status
```
**Response template:** "🔊 **Bark Report** — Currently barking: {is_barking} | Barks today: {count}"

### 8. Send Alert
**Usage:** Trigger a system or owner notification.
```
POST http://localhost:9118/alerts
{"alert_type": "geo_breach", "severity": "high", "message": "Dog has left the backyard!"}
```
**Response template:** "🚨 Alert sent — Type: {alert_type} | Severity: {severity}"

### 9. Storage & Log Report
**Usage:** Check logging system health and storage usage.
```
GET http://localhost:9117/logger/stats
```
**Response template:** "📁 **Logger Stats** — Log size: {size_mb}MB | Total events: {total_events} | Uptime: {uptime}"

## Example Conversations

### Example 1: Where is the dog right now?

**User:** "Where's Buddy right now?"

**Agent workflow:**
1. Call `GET http://localhost:9111/gps`
2. Parse lat, lng, accuracy, timestamp
3. Optionally call `GET http://localhost:9114/geofence/status` to see if in a known zone
4. Respond with a clear location report

**Response:** "🐾 **Buddy's Location** — He's at 40.7128, -74.0060 (±8m accuracy). He's inside the **Backyard** safe zone. Last updated 2 minutes ago."

### Example 2: How is the dog doing today?

**User:** "How's Buddy doing today? Any concerns?"

**Agent workflow:**
1. Call `GET http://localhost:9113/health` — vitals
2. Call `GET http://localhost:9115/behavior/summary` — daily summary
3. Call `GET http://localhost:9115/behavior/deviations` — any red flags
4. Synthesize a comprehensive health + behavior report

**Response:** "🩺 **Buddy's Daily Report** — Heart rate 72bpm (normal), activity level: moderate. He's had 2 meals today, last toilet break was 1hr ago. 📊 Daily summary: 4h rest, 2h play, minimal barking. ✅ No deviations detected — everything looks normal!"

### Example 3: Something's wrong — set up a zone and alert

**User:** "Buddy keeps wandering near the road. Create a safe zone around the house and alert me if he leaves it."

**Agent workflow:**
1. Call `GET http://localhost:9111/gps` to get current position as zone center
2. Call `POST http://localhost:9114/geofence/zones` with current position and 100m radius
3. Explain what was created

**Response:** "✅ Done! I've created a **Home Zone** centered on Buddy's current location (40.7128, -74.0060) with a 100m radius. If he leaves this zone, the system will trigger a geo-breach alert. You can check zone status anytime with 'check Buddy's zones.'"

### Example 4: Tell the dog something and check on barking

**User:** "Buddy's been barking — tell him to settle down and check his bark status."

**Agent workflow:**
1. Call `POST http://localhost:9116/voice/say` with a calm-down message
2. Call `GET http://localhost:9116/voice/bark/status` to see current state
3. Report results

**Response:** "🔊 I told Buddy: 'Buddy, settle down. Everything's fine.' Checking his bark status now — he's currently **not barking**. He's had 3 bark events today."

## Configuration Tips

The following settings in `config.yaml` affect how this skill operates:

```yaml
# dog-agent skill configuration
dog_agent:
  # Base URL for all microservices (default: localhost)
  base_url: "http://localhost"

  # Service ports (can be overridden individually)
  ports:
    gps: 9111
    sensors: 9112
    health: 9113
    geofence: 9114
    behavior: 9115
    voice: 9116
    logger: 9117
    alerts: 9118

  # Dog's name for natural-language responses
  dog_name: "Buddy"

  # Alert preferences
  alerts:
    default_severity: "medium"        # low, medium, high, critical
    notify_owner_on_breach: true      # auto-send alert when dog leaves zone

  # Geofence defaults
  geofence:
    default_radius_m: 50

  # Voice defaults
  voice:
    default_voice: "default"          # TTS voice profile
    calm_down_message: "Easy, buddy. Everything's okay."

  # Monitoring interval (seconds between automatic checks)
  monitor_interval: 300
```

### Recommended `config.yaml` for a Raspberry Pi setup:

```yaml
dog_agent:
  dog_name: "Buddy"
  alerts:
    notify_owner_on_breach: true
  geofence:
    default_radius_m: 100
  voice:
    calm_down_message: "Buddy, settle down. It's okay. Good dog."
  monitor_interval: 60
```

Also ensure all nine microservices are listed as dependencies in your systemd or docker-compose setup so they start before this skill attempts to connect.