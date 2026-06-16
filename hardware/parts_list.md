# Dog-Agent — Bill of Materials (Parts List)

> **Last updated:** June 2026
> **Est. total (recommended):** ~$175
> **Est. total (budget):** ~$105

---

## Compute

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| Raspberry Pi 5 (4 GB) | SC1112 | 1 | — | $60 | Main computer, runs Python dog-agent stack |
| Raspberry Pi Zero 2 W | SC0510 | 1 | $15 | — | Budget alternative — lower power, slower CPU |
| MicroSD card (32 GB) | SanDisk Extreme Pro A2 | 1 | $8 | $12 | Boot disk for Pi OS Lite |
| Pi 5 official power supply | SC1110 (27W USB-C) | 1 | — | $12 | — |
| Micro-USB power cable (for Zero) | Generic | 1 | $5 | $5 | — |

## GPS

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| Adafruit Ultimate GPS Breakout v3 | MTK3333 — PID 746 | 1 | $30 | $40 | 66-channel, 10 Hz update, -165 dBm sensitivity |
| External active GPS antenna | SMA — Adafruit PID 960 | 1 | — | $13 | Better rooftop / tree canopy reception |
| Jumper wires (F-F, 4-pin) | Generic | 1 | $2 | $3 | Connect GPS to Pi UART |

> **Notes:**
> - The MTK3333 is our recommended GPS. It has a built-in ceramic patch antenna that works well outdoors. For indoor / dense urban use, add the external active antenna.
> - **Budget alternative:** NEO-6M GPS module (~$10 on Amazon) — no external antenna port, lower sensitivity, 5 Hz max update rate.

## Sensors (LilyPad Ecosystem)

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| LilyPad Arduino 328 Main Board | DEV-13321 (SparkFun) | 1 | — | $21 | Microcontroller sewing into dog sweater, polls sensors |
| LilyPad Heart Rate Monitor | SEN-14050 (SparkFun) | 1 | $13 | $15 | I2C optical HR sensor (MAX30101), addr 0x57 |
| LilyPad Temperature Sensor | SEN-10974 (SparkFun) | 1 | $8 | $10 | I2C temp (MCP9808), ±0.25°C, addr 0x48 |
| LilyPad Accelerometer (LIS331) | DEV-10886 (SparkFun) | 1 | $10 | $13 | 3-axis ±24 g, I2C addr 0x18 |
| LilyPad FTDI Basic Breakout | DEV-09716 (SparkFun) | 1 | — | $15 | Programming the LilyPad Arduino over USB |
| Conductive thread (stainless steel / silver-plated nylon) | Adafruit PID 640 / 641 | 1 spool | $5 | $9 | Sews LilyPad sensors to sweater fabric |

> **Notes:**
> - LilyPad sensors use sewable tabs — no solder required for fabric installation.
> - **Budget alternative:** Use regular stranded hookup wire + small alligator clips instead of conductive thread for prototyping (not wearable long-term).
> - The FTDI breakout is only needed during initial programming of the LilyPad. It is not part of the final wearable.

## Power

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| Anker PowerCore 10000 mAh power bank | A1234 (Anker) | 1 | — | $25 | Powers Pi for 8–12 hours of runtime |
| Budget USB power bank 5000 mAh | Generic | 1 | $10 | — | Lighter, ~4–6 hours runtime |
| Short micro-USB cable (6–12") | Generic | 1 | $3 | $5 | Connects Pi to power bank in sweater pocket |

> **Notes:**
> - The Pi 5 draws ~3 W idle / ~7 W under load. A 10000 mAh bank gives roughly 8–12 hours.
> - **Pi Zero 2 W** draws ~1.2 W idle — the same 10000 mAh bank would last 30+ hours.
> - The LilyPad Arduino and its sensors draw power **from the Pi's 3.3 V pin** — no separate battery needed for the sensor subsystem.
> - If using cellular (optional), add a dedicated 3.7 V LiPo for the LTE modem to avoid brownouts.

## Audio

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| USB microphone (small) | UGREEN USB mic | 1 | $12 | $20 | Plug-and-play audio input for bark detection / commands |
| Mini speaker (3.5 mm) | Adafruit PID 1313 or generic | 1 | $5 | $7 | Audio output — tone generation, voice responses |

> **Notes:**
> - The USB microphone is **plug-and-play** on Raspberry Pi OS — no extra driver.
> - For higher-quality audio output, consider an **I2S DAC + amp** (e.g., Adafruit I2S 3W Amp Breakout — MAX98357A).
> - Audio is the least critical subsystem — budget options work fine for voice/tone notification.

## Cellular (Optional)

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| Adafruit FONA 3G (or 4G LTE) | Adafruit PID 2687 (3G) / 3968 (4G LTE) | 1 | — | $50 | Optional cellular uplink when WiFi isn't available |
| GPS + Cellular combo (alternative) | SIM7000E / SIM7600 — Waveshare hat | 1 | $30 | $45 | Combines GPS + LTE in one hat |
| Active antenna (LTE) | Adafruit PID 3278 | 1 | — | $5 | Required for cellular reception |

> **Notes:**
> - Cellular is **strictly optional** — skip it unless the dog roams beyond WiFi range.
> - The SIM7000/CDMA hat also provides GPS, so you can eliminate the separate MTK3333 if you go this route.
> - Requires a nano-SIM with a data plan.

## Tools / Materials

| Component | Model / Part # | Qty | Budget Price | Recommended Price | Purpose |
|---|---|---|---|---|---|
| Multimeter | Any cheap digital multimeter | 1 | $10 | $20 | Continuity checks on conductive thread, voltage verification |
| Sewing needle (large eye) | Embroidery needle pack | 1 | $3 | $5 | Sewing conductive thread through LilyPad tabs |
| Liquid electrical tape | PlastiDip or Gardner Bender | 1 | $5 | $7 | Waterproof sensor contacts, strain relief |
| Heat-sealable fabric pouch | — | 1 | $3 | $5 | Waterproof enclosure for sensors on sweater |
| Small zip ties | — | 10 | $1 | $2 | Cable management inside sweater pocket |
| Velcro strips | — | 2 | $2 | $3 | Securing Pi / battery inside pocket |
| Soldering iron (for prototype only) | — | 1 | (owned) | $20 | Only needed if using hookup wire instead of thread |

---

## Summary

| Tier | Compute | GPS | Sensors | Power | Audio | Cellular | Tools | **Total** |
|---|---|---|---|---|---|---|---|---|
| **Budget** | Pi Zero 2W + SD + cable ($28) | NEO-6M ($10) | LilyPad sensors + thread + FTDI ($36) | Generic 5000 mAh bank + cable ($13) | USB mic + speaker ($17) | Skip ($0) | Multimeter + needle + tape + pouches ($28) | **~$132** |
| **Recommended** | Pi 5 + SD + PSU ($84) | MTK3333 + ext antenna ($53) | LilyPad sensors + thread + FTDI ($59) | Anker 10000 mAh ($25) | USB mic + speaker ($27) | Skip ($0) | All tools + soldering iron ($55) | **~$303** |

> **Notes:**
> - The budget column uses a Pi Zero 2W instead of Pi 5, a NEO-6M instead of MTK3333, and generic USB power. Sensors and thread are the same.
> - The "recommended" total includes the FTDI breakout and soldering iron as one-time costs. Subtract ~$35 if you already own those.
> - Cellular adds ~$55 (modem + antenna) regardless of tier.