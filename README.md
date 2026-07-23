# felicity-victron-aggregator

A D-Bus service for Venus OS (Victron Cerbo GX and similar) that reads
two Felicity FLA-series LiFePO4 battery packs over RS485 Modbus RTU and
publishes them as a single, correctly-aggregated battery service —
built for packs wired in **series**, where a naive per-pack or
parallel-style aggregation gives wrong voltage and capacity numbers.

It exists because the stock [dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)
driver's Felicity support has several bugs that are easy to miss on a
live system (a sentinel register value read as a real 32.767V cell, an
unfiltered temperature average producing a nonsense five-figure
reading, DVCC current limits collapsing to zero from a poisoned
protection state machine) and, more fundamentally, publishes each pack
as its own Victron battery service — correct for parallel packs, wrong
for series. See `aggregator/PARAM-SPEC.md` for the full analysis and
`aggregator/README.md` for the working implementation notes.

New to this and just want to get it running on your own Cerbo GX? Start
with **[INSTALL.md](INSTALL.md)** — a beginner-friendly, step-by-step
walkthrough from an out-of-the-box Venus OS device to a working,
reboot-proof install.

## Repository layout

```
README.md                  this file
LICENSE                    MIT
CREDITS.md                 upstream projects this builds on
PINOUT.md                  FLA12100 RJ45 communication pinout, from the manufacturer manual
REGISTERS.md               early Modbus register survey (see aggregator/PARAM-SPEC.md for the confirmed map)

aggregator/                 the D-Bus aggregator service
  README.md                 protocol notes, sentinel filtering, alarms, boot persistence
  INTERFACE.md               module contracts between discovery.py / params.py / dbusservice.py
  PARAM-SPEC.md              authoritative derivation of every published D-Bus parameter
  FEATURE-MAP.md             gap analysis against the standard Victron battery D-Bus interface
  FAULT-DECODE.md             analysis of the BMS status/fault register bitmap

  felicity_reader.py          RS485 Modbus RTU read layer (no D-Bus, no daemon logic)
  discovery.py                 port autodiscovery + serial-based stable pack identification
  params.py                    pure function: pack data in, D-Bus parameter dict out
  history.py                   persistent coulomb/energy accumulator
  dbusservice.py               D-Bus service wrapper (creates paths, publishes updates)
  dbus-felicity-bank.py         the daemon: wires the above together and runs the poll loop

  pack_mapping.json             serial -> bank-slot persistence (empty template)
  history.json                  accumulator state persistence (empty template)

  ext/velib_python/            Victron's velib_python helpers (vendored, see CREDITS.md)
  simtest/                      unit tests exercised against simulated pack data
```

## How it works

`dbus-felicity-bank.py` runs a poll loop: `discovery.py` identifies
which RS485 ports have a responding pack and maps each one to a stable
bank slot by pack serial number (not port order, since `/dev/ttyUSB*`
device nodes can renumber across reboots); `felicity_reader.py` reads
the raw registers for each present pack; `params.py` turns the raw
per-pack readings into one aggregated bank-level parameter set using
the series-correct combination rules documented in `PARAM-SPEC.md`
(voltage sums across packs, current averages, SoC takes the minimum,
etc.); `dbusservice.py` publishes the result as
`com.victronenergy.battery.felicity_bank` on the system D-Bus.

The bank can run with only one pack physically connected — the other
slot is padded with clearly-marked simulated data
(`SIMULATE_UNCLAIMED_SLOTS` in `discovery.py`) so the full bank/pack/cell
hierarchy is exercised end-to-end before the second RS485 adapter is
wired up.

## Status

Built and verified against real FLA12100 hardware: two packs wired in
series behind two isolated USB-RS485 adapters, publishing to a Cerbo
GX. `aggregator/README.md` documents the Venus OS boot-persistence
setup this depends on (`/service` is a tmpfs rebuilt from the firmware
registry on every boot, so the daemon needs a `/data/rc.local` hook to
survive a reboot).

DVCC (`DvccControlAllMultis`) is intentionally kept off — see
`aggregator/README.md` "Charge/discharge gating" for why the BMS fault
bitmap isn't yet trusted as the authoritative charge/discharge gate.

## License

MIT — see `LICENSE`. Builds on the open-source projects listed in
`CREDITS.md`.
