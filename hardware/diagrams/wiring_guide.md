# Dog Agent — Wiring Diagrams & Component Integration

Complete wiring reference for Dog Agent v3.0 hardware configurations.

---

## 📐 System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    DOG AGENT SYSTEM ARCHITECTURE                 │
└─────────────────────────────────────────────────────────────────┘

    ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
    │   POWER     │────▶│  COMPUTING  │────▶│   SENSORS   │
    │   SYSTEM    │     │    (Pi)     │     │   & INPUTS  │
    └─────────────┘     └─────────────┘     └─────────────┘
           │                   │                    │
           │              ┌────┴────┐              │
           │              │         │              │
           ▼              ▼         ▼              ▼
    ┌─────────────┐  ┌────────┐ ┌────────┐  ┌─────────────┐
    │Solar/Backup │  │Display │ │Storage │  │  ACTUATORS  │
    │   Battery   │  │E-Paper │ │  µSD   │  │  (Outputs)  │
    └─────────────┘  └────────┘ └────────┘  └─────────────┘
                                                  │
    ┌─────────────┐     ┌─────────────┐     ┌────┴────┐
    │ COMMUNICATIONS │  │   SAFETY    │     │         │
    │   (GPS/LoRa)   │  │  (Emergency)│     │  Haptic │
    └────────────────┘  └─────────────┘     │   LED   │
                                            │  Voice  │
                                            └─────────┘
```

---

## 🔌 Connector Types for Wearable Applications

### Flexible Connections (Recommended)

| Connector | Type | Use Case | Pros | Cons |
|-----------|------|----------|------|------|
| **JST-SH 1.0mm** | Wire-to-Board | Sensors, small modules | Very small, reliable | Limited current |
| **JST-PH 2.0mm** | Wire-to-Board | Power, larger sensors | Robust, common | Larger |
| **FFC/FPC 0.5mm** | Flat flex | Displays, dense connections | Extremely thin | Fragile, limited bends |
| **Qwiic/Stemma QT** | I2C bus | I2C sensors | Plug-and-play, chainable | Only I2C |
| **Spring Contacts** | Pressure | Temporary/debug | No soldering | Unreliable for movement |
| **Magnetic Pogo** | Magnetic | Charging, data | Easy connection | Expensive |

### Connection Strategy by Location

```
Dog Sweater Connection Points:
============================

    [Collar Area]                    [Back/Shoulder]              [Hind Quarter]
    ┌─────────┐                      ┌─────────────┐            ┌──────────┐
    │  LED    │◄────FFC Cable───────▶│   Main PCB  │◄──JST-SH──▶│  GPS/    │
    │ Ring    │                      │  (Pi + hub) │            │  LoRa    │
    └─────────┘                      └─────────────┘            └──────────┘
           │                              │                          │
           │                              │                          │
           │                    ┌─────────┴─────────┐               │
           │                    │                   │               │
           └────────────────────┤   Heart Rate     │               │
                                │   (LilyPad)      │               │
                                │                  │               │
                    ┌───────────┴───────────┐      │               │
                    │                       │      │               │
                ┌───┴───┐              ┌──┴──┐     │               │
                │Battery│              │Temp │     │               │
                │(under)│             │     │     │               │
                └───────┘              └─────┘     │               │
                                                   ┌─┴───────────┐  │
                                                   │ BNO055 IMU  │◄─┘
                                                   │ (dead reck) │
                                                   └─────────────┘
```

---

## 🔋 Power System Wiring

### Solar Charging Circuit (TP4056 + Protection)

```
Solar Panel Wiring:
==================

Solar Panel (5W-20W)              TP4056 Module                    Battery
┌──────────────┐                  ┌──────────────┐                ┌──────────┐
│              │                  │  +  ────────▶│───────────────▶│  +       │
│   + 5V       ├─────────────────▶│  IN+         │                │          │
│              │    (JST-PH)     │              │   (JST-PH)     │ 18650    │
│   - GND      ├─────────────────▶│  IN-         │                │  LiPo    │
│              │                  │  -  ────────▶│───────────────▶│  -       │
└──────────────┘                  └──────┬─────┘                └────┬─────┘
                                          │                           │
                                          │    Output to Pi           │
                                          ▼                           │
                                    ┌──────────┐                      │
                                    │ 5V Boost │                      │
                                    │  (MT3608)│                      │
                                    └────┬─────┘                      │
                                         │                            │
                                         │ 5V Rail                    │
                                         ▼                            ▼
                                    ┌──────────┐               ┌──────────┐
                                    │ Pi Zero  │               │ nRF52840 │
                                    │  2W      │               │ Beacon   │
                                    └──────────┘               │ (Coin)   │
                                                               └──────────┘

Important:
- Add 10µF capacitor across solar panel for stability
- Battery protection MUST include over-discharge cutoff
- Fuse between battery and TP4056 (1A recommended)
- Diode prevents reverse current if panel shaded
```

### Power Distribution (Pi Zero 2W)

```
Pi Zero 2W Pinout with Dog Agent Connections:
============================================

                ┌─────────────────────────┐
      3.3V ─────┤1  ● ●  2├───── 5V (from boost)
      I2C SDA ──┤3  ● ●  4├───── 5V (from boost)
      I2C SCL ──┤5  ● ●  6├───── GND
                │7  ● ●  8├───── UART TX → GPS
                │9  ● ● 10├───── UART RX ← GPS
                │11 ● ● 12├───── PWM 0  → Haptic
                │13 ● ● 14├───── GND
                │15 ● ● 16├───── GPIO → LED Ring
                │17 ● ● 18├───── GPIO → Status LED
                │19 ● ● 20├───── GND
      SPI MOSI ─┤21 ● ● 22├───── GPIO → Panic Button
      SPI MISO ─┤23 ● ● 24├───── SPI SCLK
                │25 ● ● 26├───── SPI CE0 → LoRa
                │27 ● ● 28├───── ID_SD (reserved)
                │29 ● ● 30├───── GND
                │31 ● ● 32├───── GPIO → Solar Monitor
                │33 ● ● 34├───── GND
      SPI MISO ─┤35 ● ● 36├───── GPIO → BLE Heartbeat
                │37 ● ● 38├───── SPI DIN → DAC/E-Paper
                │39 ● ● 40├───── SPI SCLK
                └───────────┘
                 USB = GPS/LoRa via hub

Power Budget (Pi Zero 2W):
- Pi idle: ~100mA @ 5V = 0.5W
- Pi active: ~200mA @ 5V = 1W
- All modules: +300mA = 1.5W total active
- Deep sleep: ~50mA = 0.25W
- 18650 3000mAh: ~20 hours active, 5 days conservative
```

---

## 📡 Communications Wiring

### Multi-Constellation GPS (NEO-M9N)

```
NEO-M9N GPS Module Wiring:
=========================

                ┌─────────────────────┐
                │    NEO-M9N GPS      │
                │                     │
    5V ────────▶│ VCC          GND  │◀──── GND
    GND ────────▶│ GND          TX   │◀──── Pi UART RX (Pin 10)
    Pi TX ──────▶│ RX           PPS  │◀──── GPIO (optional pulse)
    I2C SDA ────▶│ SDA          SCL  │◀──── I2C SCL (alt interface)
                └─────────────────────┘

Notes:
- UART is primary interface (9600 baud default, configurable to 115200)
- PPS (Pulse Per Second) for time sync (optional)
- External antenna recommended (u.FL connector)
- Add 100µF capacitor near module for GPS brownout protection
- Keep antenna away from Pi's switching noise
```

### LoRaWAN Module (RFM95W)

```
RFM95W LoRa Module Wiring:
===========================

              ┌───────────────────────┐
              │      RFM95W           │
              │                       │
    3.3V ─────▶│ VCC            GND  │◀──── GND
    GND ──────▶│ GND          MISO  │◀──── Pi GPIO 9 (MISO)
    Pi GPIO 19 ─▶│ MOSI          SCK  │◀──── Pi GPIO 11 (SCLK)
    Pi GPIO 26 ─▶│ NSS            RST │◀──── Pi GPIO 25
    Pi GPIO 22 ─▶│ DIO0           DIO1 │◀──── Pi GPIO 27 (optional)
              └───────────────────────┘

DIO0: Packet received / TX complete interrupt
DIO1: CAD detect / other functions

Antenna: 868/915MHz wire monopole or purchased antenna
Length: 78mm for 915MHz, 86mm for 868MHz

Range Testing:
- Urban: 2-5km
- Suburban: 5-10km  
- Rural/LOS: 10km+
```

---

## 🩺 Sensor Wiring

### LilyPad Heart Rate + Temperature (I2C)

```
LilyPad Sensor Integration:
==========================

                         Dog Sweater (Vest)
                         
    ┌─────────────────────────────────────────────┐
    │                                             │
    │    ┌─────────────┐                          │
    │    │   Heart     │                          │
    │    │   Rate      │◄── Elastic conductive    │
    │    │   Sensor    │    thread connection      │
    │    │  (LilyPad)  │                          │
    │    └──────┬──────┘                          │
    │           │ I2C (JST-SH 4-pin)              │
    │           │                                 │
    │    ┌──────┴──────┐                         │
    │    │ Temperature │◄── Placed under armpit   │
    │    │   Sensor    │    (most accurate)       │
    │    │  (LilyPad)  │                          │
    │    └──────┬──────┘                         │
    │           │                                 │
    │           │ I2C Bus (daisy-chain)          │
    │           │                                 │
    │    ┌──────┴──────┐                         │
    │    │   Flex      │◄── Routes to Pi         │
    │    │   Cable     │                          │
    │    │  (JST-SH)   │                          │
    │    └─────────────┘                          │
    │                                             │
    └─────────────────────────────────────────────┘

LilyPad Pinout (I2C):
- VCC (3.3V)
- GND
- SDA (I2C data)
- SCL (I2C clock)

I2C Address Conflicts:
- LilyPad Heart Rate: 0x04
- LilyPad Temperature: 0x48 (configurable via jumpers)
- BME280: 0x76 or 0x77
- BNO055: 0x28

Resolution: Use TCA9548A I2C multiplexer if more than 3 devices
```

### BNO055 IMU (9-DOF)

```
BNO055 IMU Wiring (I2C):
========================

              ┌─────────────────┐
              │     BNO055      │
              │    IMU          │
              │                 │
    3.3V ─────▶│ VIN        GND │◀──── GND
    GND ──────▶│ GND      SDA  │◀──── I2C SDA (Pi Pin 3)
    I2C SDA ──▶│ SDA      SCL  │◀──── I2C SCL (Pi Pin 5)
    I2C SCL ──▶│ SCL      INT  │◀──── Pi GPIO (optional interrupt)
    Pi GPIO ──▶│ RST            │
              └─────────────────┘

Placement:
- Mount near center of mass (shoulder blade area)
- Avoid rigid mounting (dog's movement)
- Use foam or fabric padding
- Calibrate after each power-on

Calibration Status:
- Gyro: Calibrated automatically
- Accel: Requires still position
- Mag: Requires figure-8 motion
```

### Environmental Sensors (BME280 + UV + Light)

```
Environmental Sensor Array:
==========================

    BME280 (Temp/Humidity/Pressure)
    ┌─────────────────┐
    │                 │
    │ VCC     GND    │◀──── GND
    │  │       │      │
    │ SCL ─────┼────▶│───── I2C SCL (shared bus)
    │ SDA ─────┼────▶│───── I2C SDA (shared bus)
    │     CSB      │
    │     SDO      │
    └─────────────────┘
    
    VEML6070 (UV Index)
    ┌─────────────────┐
    │                 │
    │ VCC     GND    │◀──── GND
    │  │       │      │
    │ SCL ─────┼────▶│───── I2C SCL (shared)
    │ SDA ─────┼────▶│───── I2C SDA (shared)
    └─────────────────┘
    Address: 0x38 (fixed)
    
    LTR-329 (Ambient Light)
    ┌─────────────────┐
    │                 │
    │ VCC     GND    │◀──── GND
    │ SCL     INT    │◀──── Pi GPIO (optional)
    │ SDA            │
    └─────────────────┘
    Address: 0x29 (configurable)

All on shared I2C bus with pull-up resistors (4.7kΩ)
```

---

## 🖥️ Display & Output Wiring

### E-Paper Display (2.13")

```
Waveshare 2.13" E-Paper (SPI):
===============================

              ┌──────────────────────┐
              │   E-Paper Display    │
              │     (2.13")          │
              │                      │
    3.3V ─────▶│ VCC      GND      │◀──── GND
    GND ──────▶│ GND      DIN      │◀──── Pi MOSI (GPIO 10)
    Pi GPIO 8 ─▶│ CLK      CS       │◀──── Pi GPIO 8 (CE0)
    Pi GPIO 25 ─▶│ DC       RST     │◀──── Pi GPIO 25
    Pi GPIO 24 ─▶│ RST      BUSY    │◀──── Pi GPIO 24 (optional)
              └──────────────────────┘

Flexible Connection:
- Use 10-pin FFC cable (0.5mm pitch)
- Or discrete wires with JST-SH connectors
- Display module can be mounted on collar or back

Power:
- Only draws power during refresh (~100mA for 1 second)
- Zero power between updates (e-paper holds image)
- Update every 5 minutes for status
```

### WS2812B LED Ring (Collar)

```
LED Ring Wiring:
===============

    Pi GPIO 18 ──[330Ω]──▶ Data In
    5V Rail ─────────────▶ VCC (ring)
    GND ─────────────────▶ GND (ring)
    
    Single LED power: 60mA max (full white)
    16-LED ring: ~960mA max (all white, full bright)
    
    Recommended: Limit brightness to 30% = ~300mA
    Or use fewer LEDs (8 LEDs = ~150mA @ 30%)
    
    Level Shifting (Pi 3.3V → LED 5V):
    ┌─────────────┐
    │  74HCT245   │
    │  or 74AHCT  │
    │  (3.3V→5V)  │
    └─────────────┘
    
    Or use 3.3V-tolerant LEDs (some clones work)
    Add 1000µF capacitor across power pins
```

### Piezo Haptic Motor

```
Haptic Feedback Circuit:
========================

    Pi GPIO 12 (PWM) ──[100Ω]──▶ Piezo (+)
    GND ────────────────────────▶ Piezo (-)
    
    For stronger vibration:
    
    Pi GPIO ──[1kΩ]──▶┬──▶ NPN Transistor (2N2222)
                      │
    5V ──────────────┤    Collector
                      │      │
    Motor (+) ───────┘      │
    Motor (-) ───────────────┘
                      Emitter ──▶ GND
    
    Protection Diode (1N4001) across motor
    (Cathode to +, Anode to -)
```

---

## 🚨 Safety Systems Wiring

### Emergency BLE Beacon (nRF52840)

```
nRF52840 BLE Beacon Wiring:
==========================

              ┌─────────────────────┐
              │    nRF52840         │
              │   (coin cell)       │
              │                     │
    CR2032+ ──▶│ VCC          GND  │◀──── GND
    CR2032- ──▶│ GND          P0.05│◀──── Pi GPIO (heartbeat in)
    Pi UART ──▶│ TX           P0.06│◀──── Pi UART RX (optional)
    Pi UART ──▶│ RX           P0.07│◀──── Pi GPIO (config)
              └─────────────────────┘

Standalone Mode:
- Runs completely independent of Pi
- Coin cell provides 6+ months backup
- Monitors Pi heartbeat via GPIO
- If Pi stops → beacon activates automatically
- Can be configured via UART when Pi is running

Heart Beat:
- Pi toggles GPIO every 10 seconds
- If no toggle for 60 seconds → Pi considered dead
- Beacon starts advertising immediately
```

### Panic Button

```
Panic Button Circuit:
=====================

    3.3V ─────[10kΩ Pull-up]──┬──▶ Pi GPIO (input)
                               │
    Button ────────────────────┘
    (Normally Open)          │
                             ▼
                           GND
    
    Software debouncing recommended
    Detect patterns: single, double, long-press
```

---

## 🧵 Flexible Component Recommendations

### Recommended Flexible PCBs & Cables

| Component | Recommended Part | Source | Price |
|-----------|----------------|--------|-------|
| **Flex PCB for Pi Zero** | Oshpark Flex (2-layer) | oshpark.com | $10/sq in |
| **FFC Cable 10-pin** | Amphenol 10051922-1010 | digikey.com | $2 |
| **JST-SH 4-pin** | SM04B-SRSS-TB | digikey.com | $0.50 |
| **JST-PH 2-pin** | B2B-PH-K-S | digikey.com | $0.30 |
| **Qwiic Cable** | Sparkfun PRT-14427 | sparkfun.com | $1 |
| **Conductive Thread** | Shieldex 117/17 | adafruit.com | $15 |
| **Copper Tape** | 3M 1181 EMI | amazon.com | $10 |
| **Flex Sensor** | Spectra Symbol | sparkfun.com | $8 |

### Flexible Connection Strategy

```
Wearable Integration Best Practices:
=====================================

1. DIVIDE INTO ZONES
   - Collar (LED, Beacon, Light sensor)
   - Shoulder/Chest (Main PCB, Battery)
   - Back (GPS, LoRa antenna)
   - Belly (Heart rate electrodes)
   - Hind (Temperature sensor)

2. USE FLEXIBLE INTERCONNECTS
   - FFC cables between rigid sections
   - Conductive thread for electrodes
   - JST-SH for module connections
   - Spiral wrap for strain relief

3. STRAIN RELIEF
   - Knots in cables before connectors
   - Hot glue at flex points
   - Loop excess cable in service loops
   - Avoid 90° bends

4. MODULAR DESIGN
   - Each zone has connector
   - Can replace modules without rewiring
   - Quick disconnect for washing

5. WASHABILITY
   - Waterproof connectors (IP67)
   - Removable electronics
   - Sweat-resistant coating
```

---

## 📋 Bill of Materials - Wiring Components

### Essential Connectors

| Item | Qty | Price | Notes |
|------|-----|-------|-------|
| JST-SH 4-pin headers | 10 | $5 | For I2C sensors |
| JST-SH 6-pin headers | 5 | $3 | For SPI modules |
| JST-PH 2-pin headers | 10 | $3 | For power distribution |
| JST-PH 4-pin headers | 5 | $2 | For battery connections |
| FFC 10-pin 0.5mm | 3 | $6 | For display, dense areas |
| Qwiic cables 50mm | 5 | $5 | Quick I2C connections |
| Qwiic cables 100mm | 5 | $5 | Flex areas |
| Dupont wires F-F | 40 | $3 | Prototyping |
| Heat shrink tubing | 1 | $5 | Strain relief |
| Spiral cable wrap | 1 | $4 | Protection |
| Kapton tape | 1 | $8 | Insulation, flexible |
| Conductive thread | 1 | $15 | E-textile connections |
| Copper tape | 1 | $10 | Ground planes |

**Total Wiring Components: ~$75**

---

## 🔧 Assembly Order

### Recommended Build Sequence

1. **Power System First**
   - Test TP4056 + battery alone
   - Verify boost converter output
   - Check solar charging

2. **Pi + Core Modules**
   - Flash SD card with Dog Agent OS
   - Connect USB hub + GPS
   - Verify boot and SSH access

3. **Add I2C Sensors**
   - Scan bus: `i2cdetect -y 1`
   - Test each sensor individually
   - Check addresses don't conflict

4. **Add SPI Modules**
   - LoRa module
   - E-paper display

5. **Add Safety Systems**
   - BLE beacon
   - Panic button
   - LED ring

6. **Integration Testing**
   - Run `main.py --simulate`
   - Check all APIs respond
   - Verify power consumption

7. **Sew Into Garment**
   - Mark positions on vest
   - Create cable channels
   - Add strain relief
   - Test flexibility

---

## 🎨 Diagram File References

This directory contains:

- `wiring_guide.md` — This file
- `system_architecture.svg` — High-level block diagram
- `pi_zero_pinout.svg` — Detailed GPIO allocation
- `power_distribution.svg` — Power system schematic
- `sensor_placement.svg` — Wearable positioning guide
- `flex_cable_routing.svg` — FFC/JST routing diagram

All diagrams are in SVG format for editing in Inkscape or similar.

---

## 📞 Support

Questions about wiring?
- Check module-specific READMEs in `hardware/`
- Run diagnostics: `python src/main.py --diagnose`
- Join discussions: GitHub Issues

**Next: See `sewing_guide.md` for garment integration**