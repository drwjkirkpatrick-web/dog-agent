# Dog-Agent — LilyPad Sensor Sewing Guide

> **Last updated:** June 2026
> **Purpose:** How to sew LilyPad sensors into a dog sweater using conductive thread, and how to make the installation durable, waterproof, and washable.

---

## 1. Conductive Thread Basics

### What to Use

- **Stainless steel thread** (Adafruit PID 640) — strong, conductive (~30 Ω/ft), less likely to fray
- **Silver-plated nylon thread** (Adafruit PID 641) — lower resistance (~14 Ω/ft), softer, but can fray more easily

Both are washable (with care) and designed for e-textile projects.

### Stitch Types

| Stitch | When to Use | Technique |
|---|---|---|
| **Running stitch** | Standard trace between sensor tabs | In-and-out through fabric, ~3 mm apart. Every 3–4 stitches, pull gently to even tension. |
| **Zigzag (buttonhole)** | Around LilyPad tabs for strain relief | Sew through the tab hole, cross over the top, then back through the next hole. Creates a strong mechanical bond. |
| **Backstitch** | High-stress areas (battery pocket edges) | Double layer — each stitch goes back to fill the gap. More conductive material = lower resistance. |
| **Overcast stitch** | Waterproofing edges of pouches | Whip stitch over the edge of heat-sealed pouches to keep moisture out. |

### Tension

- **Too loose:** Thread loops and may snag on things (bushes, furniture). High resistance.
- **Too tight:** Puckers the fabric and can break thread. The sweater fabric should lie flat.
- **Just right:** Thread sits gently against fabric. You can slide a fingernail under it but it doesn't lift on its own.

**Rule of thumb:** After every 5 stitches, let the needle dangle to release twist — twisted thread increases resistance.

### Insulation

Conductive thread **must not touch other conductive traces** — this shorts the circuit. Use these techniques:

1. **Physical spacing:** Keep parallel traces at least 5 mm apart.
2. **Crossing traces:** Never cross one conductive thread over another. If unavoidable, use a small piece of electrical tape or fabric patch as a bridge.
3. **Fabric glue or liquid electrical tape:** Dab over finished stitches to insulate exposed thread (see Waterproofing §4).

---

## 2. Attaching Each Sensor

### General Method (All Sensors)

1. Place the LilyPad sensor **on the outside of the sweater** (the tabs face you).
2. Thread your needle with **~50 cm** of conductive thread. Do not knot the tail — leave ~5 cm trailing.
3. Start from the **back side of the fabric** — push the needle up through a tab hole.
4. Sew 3–5 tight loops through that hole (buttonhole stitch).
5. Run the thread along the fabric to the **next connection point** (another sensor tab or the LilyPad main board).
6. At the destination tab, sew 3–5 tight loops through the hole.
7. Tie off with a double knot on the **back side** of the fabric. Dab with fabric glue.

### Heart Rate Sensor — LilyPad Heart Rate Monitor (SEN-14050)

**Component:** MAX30101 optical heart rate + SpO₂ sensor on a sewable LilyPad board.

**Placement:**
- Attach to the **inside of the sweater**, over the dog's left chest area (behind the front leg).
- The sensor window (the flat, transparent part) must press **firmly against the dog's skin** with no fabric between.
- Use a stretchy fabric panel or adjust the sweater fit so the sensor stays in contact during movement.

**Sewing connections — each tab goes to the LilyPad main board:**

| HR Sensor Tab | LilyPad Main Board Tab | Notes |
|---|---|---|
| V+ (+) | + (3.3 V) | Power |
| GND (–) | – (GND) | Ground |
| SDA (D) | A4 (SDA) | I2C data — **must match Pi I2C wiring too** |
| SCL (C) | A5 (SCL) | I2C clock — **must match Pi I2C wiring too** |

> **I2C address:** The MAX30101 on this board responds at **0x57**. It shares the I2C bus with the temp and accel sensors.

**Tips:**
- Sew the V+ and GND traces first (power), then SDA and SCL.
- Keep the SDA and SCL traces **parallel and separated by at least 5 mm** to avoid crosstalk.
- The sensor works best on **hairless or short-haired areas**. For double-coated breeds, consider shaving a small patch under the sensor location (with a vet's advice).

### Temperature Sensor — LilyPad Temperature Sensor (SEN-10974)

**Component:** MCP9808 high-accuracy I2C temperature sensor, ±0.25 °C typical.

**Placement:**
- Sew to the **inside of the sweater**, positioned **under the front armpit** (axilla region).
- This location reads core-adjacent skin temperature while being protected from direct sun/wind.
- Do NOT place over a thick muscle or fat pad — the reading will lag behind true body temp.

**Sewing connections:**

| Temp Sensor Tab | LilyPad Main Board Tab |
|---|---|
| + (3.3 V) | + |
| – (GND) | – |
| SDA | A4 (shared bus with HR sensor) |
| SCL | A5 (shared bus with HR sensor) |

> **I2C address:** 0x48. The MCP9808 has three address pins (A0, A1, A2) that are tied to GND on this board, giving the fixed address 0x48.

**Tips:**
- Since SDA and SCL are shared on the same bus with the HR sensor and accelerometer, you only need to sew ONE set of I2C traces from the LilyPad main board — then each sensor connects its own V+/GND/SDA/SCL to the nearest convenient point on the shared trace.
- **Do NOT bridge SDA/SCL across sensors in series** — this creates a stub topology. Instead, sew each sensor's SDA/SCL back to the main board's A4/A5 tabs (star topology).

### Accelerometer — LilyPad Accelerometer (LIS331)

**Component:** LIS331DLH triple-axis accelerometer, ±2 g / ±4 g / ±8 g / ±16 g selectable.

**Placement:**
- Sew to the **sweater fabric on the dog's back**, between the shoulder blades (mid-scapular).
- This central location gives the best overall activity readings (walking, trotting, galloping, lying down).
- Orient the sensor so the arrow on the board points **toward the dog's head** (X-axis forward).

**Sewing connections:**

| Accel Sensor Tab | LilyPad Main Board Tab |
|---|---|
| + (3.3 V) | + |
| – (GND) | – |
| SDA | A4 (shared bus) |
| SCL | A5 (shared bus) |

> **I2C address:** 0x18 (SDO/SA0 pin tied to GND).

**Tips:**
- The accelerometer is sensitive to vibration — use **securing stitches** through all four corner tabs to keep it from flopping. Don't rely on just the power/signal connections for mechanical attachment.
- Calibrate on the dog at rest (standing still, lying down) to get baseline readings. The LIS331 outputs raw counts — you'll convert to m/s² in software.
- Use ±8 g range for a dog (running, sudden turns can spike to 4–6 g).

---

## 3. Sweater Layout — Full Assembly

```
                   ┌─────────────────────┐
                   │       DOG NECK       │
                   └─────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │    [Accelerometer] 0x18        │
              │    (between shoulder blades)   │
              ├───────────────────────────────┤
              │                               │
              │    ┌─────────────────┐         │
     LEFT     │    │  LilyPad Main   │         │     RIGHT
    (HR side) │    │  Board (I2C     │         │  (temp side)
              │    │   master on     │         │
              │    │   Pi's bus)     │         │
              │    └─────────────────┘         │
              │         │    │                 │
              │     [HR Sensor]   [Temp Sensor]│
              │     (chest,        (under front│
              │      behind leg)    armpit)    │
              │     0x57           0x48       │
              └───────────────────────────────┘
                              │
                   ┌─────────────────────┐
                   │  Sweater hem        │
                   │  (Pi + battery in   │
                   │   belly pocket)     │
                   └─────────────────────┘
```

**Wiring topology (star from LilyPad Main Board):**

```
                               LilyPad Main Board
                              ┌── V+  ─┬── SDA ─┬── SCL ─┬── GND ─┐
                              │        │        │        │         │
                              │        │        │        │         │
                              │   ┌────┘        │        │         │
               ┌──────────────┤   │   ┌─────────┘        │         │
               │              │   │   │    ───────────────┘         │
               │              │   │   │                             │
            ┌──┴──┐      ┌───┴───┴───┴───┐                   ┌─────┴─────┐
            │ HR  │      │  Temp Sensor  │                   │ Accel     │
            │0x57 │      │    0x48       │                   │  0x18     │
            └─────┘      └───────────────┘                   └───────────┘
```

Each sensor gets its own **dedicated** V+, GND, SDA, SCL trace back to the LilyPad main board. Do not daisy-chain.

---

## 4. Waterproofing

The dog will be outside in rain, snow, mud, and drool. All electronics **must be waterproofed**.

### Method 1 — Liquid Electrical Tape (for connector points)

1. After sewing all connections and verifying continuity (see §6), dab **liquid electrical tape** over every LilyPad tab and the first 5 mm of thread exiting the tab.
2. Let it dry for 2–3 hours (24 hours for full cure).
3. Apply a second coat over high-movement areas (where the thread bends at the sensor edge).

> Do NOT get liquid tape on the optical window of the HR sensor.

### Method 2 — Heat-Sealed Pouches (for boards)

1. Cut two layers of **heat-sealable fabric** (e.g., Tyvek or iron-on adhesive patch).
2. Place the LilyPad board inside, with thread tails exiting through a small slit at the edge.
3. Use a **soldering iron on low heat** (or a fabric heat press) to seal the edges — leave a 5 mm gap where thread tails exit.
4. Dab a drop of **silicone caulk** or hot glue over the thread exit hole.

### Which Sensors Need Pouches

| Sensor | Pouch Needed? | Notes |
|---|---|---|
| LilyPad Main Board | **Yes** | Most expensive component — protect fully |
| Heart Rate Sensor | **No** | Optical window needs skin contact. Just liquid tape the tabs. |
| Temp Sensor | **No** | Needs contact with skin / armpit. Liquid tape tabs only. |
| Accelerometer | **Optional** | Can be sealed with liquid tape if not submerged. Pouch for rain/snow use. |

> **Do NOT submerge the dog in water while wearing the sweater.** These are weatherproofing measures, not dive-proof.

---

## 5. Washing Instructions

Give to the owner as a printed card or include in a README.

---

### Before Washing

1. **Remove the Raspberry Pi** from the belly pocket.
2. **Remove the USB power bank** from the belly pocket.
3. **Disconnect any loose JST/dupont connectors** from the LilyPad main board (only if they pass through the pocket wall).
4. **Close all pockets** with Velcro so nothing snags in the wash.

### Washing the Sweater

| Step | Instruction |
|---|---|
| **Water temp** | Cold (≤ 30 °C / 85 °F) — hot water degrades conductive thread |
| **Detergent** | Mild, no bleach, no fabric softener (softener coats the thread and increases resistance) |
| **Machine** | **Hand wash only** or delicate cycle in a **mesh laundry bag** |
| **Drying** | **Air dry only** — lay flat. Do NOT put in a dryer (heat damages thread conductivity). |

### After Washing

1. Check that all conductive thread traces are still intact — no broken strands.
2. Check continuity with a multimeter (see §6).
3. Let the sweater dry completely (24 hours) before reinstalling electronics.
4. Reapply liquid electrical tape if any has peeled off.

### How Often to Wash

- **Every 2–4 weeks** during active use, depending on dirt and odor.
- Between washes, spot-clean with a damp cloth.

---

## 6. Testing Continuity with a Multimeter

**Always test before first power-on and after every wash.**

### What You Need

- Digital multimeter set to **continuity mode** (the "sound wave" or diode symbol) or **resistance mode (200 Ω range)**.

### Test Procedure

1. **Unplug all power** — no battery, no USB cable connected.
2. Touch one probe to the **LilyPad main board tab** (e.g., SDA tab).
3. Touch the other probe to the **corresponding sensor tab** (e.g., HR sensor SDA tab).
4. **Expected result:** The multimeter beeps (continuity) or reads **< 10 Ω** (ideally 1–5 Ω for a good stitch).

### What to Check

| Trace | Expected Resistance (Ω) | Upper Limit | Action if Out of Range |
|---|---|---|---|
| V+ (all sensors) | < 5 Ω | < 10 Ω | Check all stitches on V+ trace — re-sew loose sections |
| GND (all sensors) | < 5 Ω | < 10 Ω | Same as V+ |
| SDA (all sensors) | < 10 Ω | < 20 Ω | Higher resistance here affects I2C signal integrity |
| SCL (all sensors) | < 10 Ω | < 20 Ω | Same as SDA |

### Checking for Shorts

1. **V+ to GND:** Probes on V+ and GND tabs of the same sensor. Should read **OL (open line)** or > 1 MΩ. If it beeps or reads low resistance (< 1 kΩ), there's a short.
2. **SDA to SCL:** Probes on SDA and SCL tabs of the same sensor. Should read **OL**. If it beeps, traces are bridged.
3. **SDA to GND / SCL to GND:** Should read **OL**. If not, a thread is touching exposed ground thread.

### Fixing Problems

| Problem | Fix |
|---|---|
| Open circuit (no continuity) | Find the break by moving probes along the trace. Re-sew the broken section with a few extra stitches. |
| High resistance (> 20 Ω) | The thread tension may be too loose, or the stitch count is too low. Add more stitches over the trace. |
| Short between traces | Carefully cut the bridging thread strand with a scalpel or small scissors. Apply liquid electrical tape over the cut area. |
| Intermittent connection | The thread may be fraying. Replace that trace entirely with a fresh length of thread. |

---

## 7. Quick Reference Card

```
┌──────────────────────────────────────────────────────────────┐
│                 DOG-AGENT SEWING QUICK REFERENCE             │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  SENSOR        I2C ADDR    PLACEMENT                         │
│  ───────       ────────    ────────                          │
│  HR Sensor     0x57        Left chest, behind front leg      │
│  Temp Sensor   0x48        Under front armpit                │
│  Accelerometer 0x18        Between shoulder blades           │
│                                                              │
│  TRACES (all go to LilyPad Main Board):                      │
│    V+  → + tab            GND → - tab                       │
│    SDA → A4 tab           SCL → A5 tab                      │
│                                                              │
│  STITCHING:                                                  │
│    Running stitch for traces, buttonhole at tabs             │
│    Traces ≥ 5 mm apart, never cross                          │
│    Dab liquid tape over all tab connections                  │
│                                                              │
│  TESTING (multimeter):                                       │
│    Each trace < 10 Ω                                        │
│    No shorts between traces (OL = good)                     │
│    Test before power-on and after every wash                 │
│                                                              │
│  WASHING:                                                    │
│    Remove Pi + battery → hand wash cold → air dry flat      │
│    No bleach, no fabric softener, no dryer                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```