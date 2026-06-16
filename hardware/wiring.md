# Dog-Agent — Wiring Guide

> **Last updated:** June 2026
> **Targets:** Raspberry Pi 5 / Pi Zero 2W ⇄ GPS Module, LilyPad Arduino (via I2C), Audio, Power

---

## 1. GPS Module → Pi (UART)

The Adafruit Ultimate GPS Breakout v3 (MTK3333) connects to the Pi's UART (serial) pins.

### Wiring Table

| GPS Breakout Pin | Pi GPIO Header Pin | Wire Color (example) |
|---|---|---|
| VIN | 3.3 V — Pin 1 or 17 | Red |
| GND | GND — Pin 6, 9, 14, 20, 25, 30, 34, or 39 | Black |
| TX (GPS → Pi) | GPIO 15 (RXD) — Pin 10 | White |
| RX (Pi → GPS) | GPIO 14 (TXD) — Pin 8 | Green |

> **Important:** The MTK3333 runs on **3.3 V logic** — do NOT wire VIN to 5 V or connect to 5 V serial pins.

### Raspberry Pi Configuration

Enable UART on the Pi by editing `config.txt`:

```bash
# Add to /boot/firmware/config.txt (Pi 5) or /boot/config.txt (Pi Zero):
enable_uart=1
dtoverlay=disable-bt  # optional — frees UART0 if Bluetooth not needed
```

Then disable the serial console that normally uses UART:

```bash
sudo raspi-config nonint do_serial_hw 0   # enable hardware serial
sudo raspi-config nonint do_serial_cons 1  # disable login shell on serial
```

Reboot:

```bash
sudo reboot
```

### Verify GPS

After reboot, check the device appears:

```bash
ls -l /dev/serial*
# Should show /dev/ttyAMA0 or /dev/ttyS0
```

Test with `gpsd` or direct read:

```bash
sudo apt install gpsd gpsd-clients
sudo gpsd /dev/ttyAMA0 -F /var/run/gpsd.sock
cgps -s
```

Or raw UART read (9600 baud default):

```bash
sudo cat /dev/ttyAMA0
# You should see NMEA sentences like $GPGGA,... if antenna has sky view
```

---

## 2. LilyPad Arduino → Pi (I2C)

The LilyPad Arduino Main Board acts as an **I2C slave** that collects data from its sewn sensors and makes it available to the Pi.

### Wiring Table

| LilyPad Pin | Pi GPIO Header Pin | Wire Color (example) |
|---|---|---|
| + (3.3 V) | 3.3 V — Pin 1 or 17 | Red |
| – (GND) | GND — Pin 6, 9, etc. | Black |
| A4 (SDA) | GPIO 2 (SDA) — Pin 3 | Yellow |
| A5 (SCL) | GPIO 3 (SCL) — Pin 5 | Blue |

> **Note:** The LilyPad runs on **3.3 V**, matching the Pi's I2C logic level. No level shifter is needed.

### LilyPad Sensor I2C Addresses

| Sensor | I2C Address | Functions |
|---|---|---|
| LilyPad Heart Rate (MAX30101) | **0x57** | Optical heart rate, SpO₂ |
| LilyPad Temperature (MCP9808) | **0x48** | Ambient / skin temperature |
| LilyPad Accelerometer (LIS331) | **0x18** | 3-axis acceleration (activity / fall detection) |

### Verify I2C on Pi

Enable I2C if not already active:

```bash
# Add to /boot/firmware/config.txt (Pi 5):
dtparam=i2c_arm=on

# Install tools
sudo apt install i2c-tools

# Scan the bus
sudo i2cdetect -y 1
```

Expected output (all three sensors connected):

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- 18 -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
40: -- -- -- -- -- -- -- -- 48 -- -- -- -- -- -- --
50: -- -- -- -- -- -- -- -- 57 -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- --
```

---

## 3. Audio

### USB Microphone

Plug any USB microphone into any USB port on the Pi. It is **plug-and-play** — no drivers needed.

Verify:

```bash
arecord -l
# Should list the USB mic as a capture device
```

Test recording:

```bash
arecord -d 5 -f cd test.wav
```

### Speaker

**Option A — 3.5 mm audio jack (simplest):**

Plug a powered speaker or headphones into the Pi's 3.5 mm TRRS jack. The Pi 5 and Pi Zero 2W both have analog audio out.

```bash
# Test audio
speaker-test -t sine -f 440 -l 1
```

**Option B — I2S amplifier (recommended for quality):**

Use the Adafruit MAX98357A I2S 3W Amp Breakout:

| Amp Breakout Pin | Pi GPIO Header Pin |
|---|---|
| VIN | 5 V — Pin 2 or 4 |
| GND | GND |
| BCLK | GPIO 18 (Pin 12) |
| LRC | GPIO 19 (Pin 35) |
| DIN | GPIO 21 (Pin 40) |

Enable I2S in `/boot/firmware/config.txt`:

```
dtparam=i2s=on
dtoverlay=hifiberry-dac
```

Then install `alsa-utils` and test:

```bash
sudo apt install alsa-utils
speaker-test -t sine -f 440 -D plughw:1,0
```

---

## 4. Power

```
┌──────────────────────┐
│   USB Power Bank     │
│    (5 V output)      │
└─────────┬────────────┘
          │
          │ short micro-USB cable
          │
   ┌──────┴──────┐
   │ Pi 5 / Zero │
   │  (USB-C or  │
   │   micro-USB)│
   └──────┬──────┘
          │
          │  3.3 V pin (Pin 1)
          ├──────────────────→ LilyPad V+
          │
          │  GND (Pin 6)
          └──────────────────→ LilyPad GND
```

> **Key point:** The LilyPad Arduino and its sensors are powered **from the Pi's 3.3 V pin**. No separate battery is needed for the sensor subsystem. See parts list for power bank recommendations.

---

## 5. Pi GPIO Pinout Diagram (ASCII)

```
                        ┌────────────────────────────┐
                        │  Raspberry Pi 5 / Zero 2W  │
                        │  GPIO Header (J8 — 40-pin) │
                        │                            │
  ┌─────────────────┐   │  ┌─────┬─────┬─────┬────┐  │
  │  3.3V ◄── LilyPad│   │  │ 1   │ 2   │ 3.3V│    │  │
  │  V+ (red)        │   │  │ 3   │ 4   │ 5V  │    │  │
  │                  │   │  │ 5   │ 6   │ GND ◄── LilyPad GND (black)│
  │  SDA (GPIO 2) ◄──│   │──│ 3   │ 4   │ 14  │    │  │
  │  LilyPad A4      │   │  │ 7   │ 8   │ TXD ◄── GPS RX (green)│
  │   (yellow)       │   │  │ 9   │ 10  │ RXD ◄── GPS TX (white)│
  │  SCL (GPIO 3) ◄──│   │  │ 11  │ 12  │ 18  │    │  │
  │  LilyPad A5      │   │  │ 13  │ 14  │ GND │    │  │
  │   (blue)         │   │  │ 15  │ 16  │ 23  │    │  │
  │                  │   │  │ 17  │ 18  │ 24  │    │  │
  │  GPS TX (white)─►│   │  │ 19  │ 20  │ GND │    │  │
  │     GPIO 15      │   │  │ 21  │ 22  │ 25  │    │  │
  │  GPS RX (green)─►│   │  │ 23  │ 24  │ 8   │    │  │
  │     GPIO 14      │   │  │ 25  │ 26  │ 7   │    │  │
  │                  │   │  ├─────┴─────┴─────┴────┤  │
  │  (I2S if used:)  │   │  │  ... remaining pins  │  │
  │  GPIO 18 (BCLK)  │   │  │  (27-40 unused)      │  │
  │  GPIO 19 (LRC)   │   │  └─────────────────────┘  │
  │  GPIO 21 (DIN)   │   └────────────────────────────┘
  └──────────────────┘
```

**Quick reference — pins used in this project:**

| Pin # | Function | Connects To | Wire |
|---|---|---|---|
| 1 | 3.3 V | LilyPad V+, GPS VIN | Red |
| 3 | GPIO 2 (SDA) | LilyPad A4 | Yellow |
| 5 | GPIO 3 (SCL) | LilyPad A5 | Blue |
| 6 | GND | LilyPad GND, GPS GND | Black |
| 8 | GPIO 14 (TXD) | GPS RX | Green |
| 10 | GPIO 15 (RXD) | GPS TX | White |
| 12 | GPIO 18 (BCLK) | I2S Amp (optional) | — |
| 35 | GPIO 19 (LRC) | I2S Amp (optional) | — |
| 40 | GPIO 21 (DIN) | I2S Amp (optional) | — |

---

## 6. First-Time Wiring Verification Checklist

Go through each item **with the Pi powered off** and **the power bank disconnected** before first boot.

### Before Power-On

- [ ] **GPS** — VIN to 3.3 V (not 5 V)? TX to GPIO 15? RX to GPIO 14?
- [ ] **LilyPad** — V+ to 3.3 V? GND to GND? A4 to GPIO 2? A5 to GPIO 3?
- [ ] **No 5 V connections to sensors** — all sensor rails at 3.3 V
- [ ] **No crossed wires** — UART TX↔RX not swapped (TX→RX, RX→TX)
- [ ] **Power bank** — USB cable plugged into Pi, not connected to bank yet
- [ ] **USB microphone** — plugged into USB port
- [ ] **Speaker** — plugged into 3.5 mm jack (or I2S wired correctly)
- [ ] **All connections** — physically secure (no loose dupont connectors)

### After Power-On

- [ ] **Pi boots** — green LED flashes, HDMI (if connected) shows console
- [ ] **UART enabled** — `ls -l /dev/ttyAMA0` or `/dev/ttyS0` exists
- [ ] **I2C enabled** — `sudo i2cdetect -y 1` shows devices at 0x18, 0x48, 0x57
- [ ] **GPS lock** — `cgps -s` shows satellites after 5–10 min with sky view
- [ ] **Audio in** — `arecord -l` lists the USB mic
- [ ] **Audio out** — `speaker-test -t sine -f 440` produces tone
- [ ] **LilyPad communication** — `sudo i2cget -y 1 0x48 0x00 w` returns temp reading

### Troubleshooting Quick Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| No devices on I2C bus | I2C disabled, or SDA/SCL swapped | Check `dtparam=i2c_arm=on` in config.txt; swap wires |
| GPS shows no NMEA sentences | UART disabled, or TX/RX swapped | Check `enable_uart=1`; swap TX/RX wires |
| GPS sees satellites but no fix | Antenna obstructed | Move outdoors; add external antenna |
| LilyPad not responding | Wrong I2C address or power not connected | Check 3.3 V to LilyPad V+; re-check addresses |
| Audio recording silent | Wrong mic selected in ALSA | `pactl set-default-source` or `alsamixer` |
| Pi won't boot | Short on GPIO, or insufficient power | Disconnect all peripherals, try again with official PSU |