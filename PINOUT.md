# FLA12100 RJ45 Communication Pinout — Authoritative Reference

**THIS IS A PLAN, NOT AN INSTALLATION.** Nothing in this folder is
installed or running. This file records verified facts from the
manufacturer's manual so future wiring decisions are based on evidence,
not inference.

Source: FLA12100 manual, document part number `358-010405-00` (CorelDRAW
export dated 2024-12-12), section 4.1 pin table, verified against the
diagram. Orientation: tab-down, pin 1 on the left.

## The pin table (one single communication socket)

There is only **ONE** communication port on the pack — confirmed: all
four product-overview views in the manual show a single "Communication
port"; no second RJ45 exists anywhere on the enclosure. CAN and RS485
share this one socket.

| Pin | Signal | Notes |
|---|---|---|
| 1 | Trigger-GND | Wake/trigger circuit ground — NOT documented as an RS485 reference |
| 2 | Trigger-VCC | Wake/trigger circuit supply — NOT on the RS485 pair |
| 3 | CANL-PCS | |
| 4 | CANH-PCS | |
| 5 | RS485-B | |
| 6 | RS485-A | |
| 7 | CANL | |
| 8 | CANH | |

## Consequences

### 1. "5B6A" from the packing list is CONFIRMED

Previously an inference from shorthand notation on a packing list. It is
now confirmed against manual section 4.1: pin 5 = RS485-B,
pin 6 = RS485-A. This is a verified fact, not a reading of shorthand.

### 2. No dedicated RS485 ground pin exists

The only ground on the connector is pin 1, "Trigger-GND", which serves
the wake/trigger circuit and is not documented anywhere as an RS485
signal reference. The RS485 connection is therefore A/B only, with no
manufacturer-provided signal-ground reference conductor. With a
galvanically isolated adapter over a short run this is expected to work
(isolated adapters do not need a shared signal ground the way
non-isolated ones do) — treat this as a known constraint, not a solved
problem.

### 3. No supply voltage on pins 5/6

Pin 2 carries "Trigger-VCC", but it is not part of the RS485 pair. An
adapter wired only to pins 5 and 6 is not exposed to pack voltage. This
reduces — but does not eliminate — the miswiring risk: wiring to the
WRONG pins (e.g. landing on pin 2 instead of 5/6) could still reach
Trigger-VCC.

### 4. Single shared socket — one custom cable must carry both buses

Because CAN and RS485 share the one RJ45, RS485 cannot simply be plugged
in alongside the existing CAN cable. A single custom cable is required at
the battery end, with one RJ45 breaking out to two destinations: pins 7/8
(CAN) to the Cerbo, and pins 5/6 (RS485) to the USB-RS485 adapter.

### 5. Bus-sharing prerequisite (PREREQUISITE 2) is RESOLVED — NEGATIVELY

The manual documents no DIP switches (none appear on the product in any
of the four overview views) and no addressing / Modbus-ID / slave-address
scheme anywhere. Combined with `felicity.py` hardcoding the Modbus
command address to `0x01`, both packs are almost certainly fixed at the
same RS485 address and cannot share one RS485 bus. Conclusion: use two
isolated adapters (one per pack), or read one pack at a time.

### 6. No termination documented

The manual mentions no termination resistor, switch, or terminator plug
anywhere for either the CAN or RS485 interface. Whatever termination the
CAN segment needs must come from the Cerbo end or an external terminator,
not from the pack.

### 7. Series/parallel limits and charging spec (section 2.4)

- Series limit, exact quote from section 2.4 note 2: **"If battery packs
  are used in series, the number of battery packs in series should not
  exceed 4."** (This system uses 2 in series — within limit.)
- Charging spec (section 2.4): Max Charging Voltage 14.4V, Floating
  Charging Voltage 14.4V, Max Charging Current **"100A\*N"**, Cut-off
  Voltage 12V. Footnote 1: **"'N' means the number of battery packs
  connected in parallel."** For a SERIES pair, N=1, so the current limit
  is **100A, NOT 200A** — series wiring does not multiply the current
  limit.
- Parallel: **"Up to 16 units in parallel(20.48kWh)"**.

### Contradiction in the manual — minimum packs per inverter size

The manual contradicts itself on the minimum-packs-per-inverter
threshold:
- Safety instruction #9: "at least 2 sets of FLA12V for inverter larger
  than 1.5KVA"
- Section 3.2: "at least 2 sets for inverter larger than 2KVA"

Recorded here as a known internal inconsistency in the source document,
not resolved.

### Owner's existing custom Cerbo cable (working configuration, not a documented one)

The owner's existing custom Cerbo cable uses battery pin 3 → Cerbo pin 7
and battery pin 4 → Cerbo pin 8, i.e. it takes the CANL-PCS / CANH-PCS
pair. This is consistent with the pin table above, but the manual does
not describe this specific mapping (PCS pair to Cerbo) anywhere — treat
it as the owner's working configuration, verified consistent with the
pinout, rather than a manufacturer-documented configuration.

## 8. Still not documented anywhere (open unknowns)

- **RS485 serial parameters**: baud rate, data bits, parity, stop bits.
  Not in the manual. `felicity.py` will have defaults — these must be
  taken from the driver source, not inferred from the manual.
- **Numeric BMS protection setpoints and recovery values.** A fault-code
  table (C01–C14) exists in the manual but only names conditions; no
  volt/amp/temperature values are given anywhere.
- **Whether the comms interconnect cable is mandatory or optional in
  parallel mode.** Not stated.
- **Which CAN pair is intended for which purpose** — pins 3/4
  ("CANL-PCS"/"CANH-PCS") vs pins 7/8 (plain "CANL"/"CANH") — beyond the
  "-PCS" label itself. The manual does not explain the intended use of
  each pair (e.g. inverter link vs pack-to-pack link).
