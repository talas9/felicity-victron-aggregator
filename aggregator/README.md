# Felicity RS485 pack reader and D-Bus aggregator

`felicity_reader.py` reads Felicity FLA-series LiFePO4 packs over RS485
Modbus RTU and returns plain Python dicts. It is a **module, not a
daemon** — `dbus-felicity-bank.py` is the daemon that polls it on a
timer and publishes the result to D-Bus as a single aggregated battery
service, `com.victronenergy.battery.felicity_bank`.

Frames are built by hand with `struct` and a local CRC-16/MODBUS
implementation — no `pymodbus` dependency. Only Modbus function code
`0x03` (read holding registers) is ever sent; this module cannot write
to a BMS.

## Why an aggregator instead of the stock driver

The packs are wired in **series** (voltage stacks, capacity does not).
[dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)'s
stock `felicity.py` driver publishes each pack as an independent
Victron battery service, which is correct for packs wired in
*parallel* but wrong for series: `dbus-aggregate-batteries`-style
summing assumes parallel topology and would double-count capacity and
average voltage instead of summing it. This project instead reads both
packs directly (bypassing the stock driver's per-pack service) and
publishes one bank-level service with the correct series combination
rules — see `PARAM-SPEC.md` for exactly how every value is derived.

While building this, several bugs were found and root-caused in the
stock driver on this hardware (unfiltered sentinel values reported as
real cell voltages, an unfiltered temperature average producing
nonsense readings, DVCC-derived current limits collapsing to zero from
a poisoned protection state machine). `PARAM-SPEC.md` documents each
one and the fix applied here; none of these are stock-driver patches —
this is a separate implementation that reads the same registers
correctly from the start.

## Protocol constants (from dbus-serialbattery's `felicity.py`, verified against a live unit)

- Serial: 9600 8-N-1, ~1s timeout, slave address 1 (fixed — this BMS has no DIP switches).
- CRC-16, poly `0xA001`, init `0xFFFF`, appended little-endian; register payloads big-endian.

| Data | Register | Count | Scaling |
|---|---|---|---|
| Cell voltages | 4906 | 16 | uint16 mV, /1000 = V |
| Total V/I | 4870 | 2 | V=uint16/100, I=int16/10 then ×-1 |
| SOC | 4875 | 1 | raw uint16 |
| Status/faults | 4866 | 3 | bitmask |
| DVCC limits | 4892 | 4 | maxV/minV /100, maxChg/maxDis /10 |
| BMS/MOS temp | 4874 | 1 | int16, no scaling |
| Temps 1-3 | 4921 | 5 | int16, no scaling — see below |
| Firmware | 63499 | 1 | uint16 |
| Serial | 63492 | 5 | uint16 each, concatenated as decimal string |

## Sentinel filtering (why)

Unpopulated registers on this BMS return `0x7FFF` (32767), not zero.
This pack is 4S; registers 5-16 of the cell-voltage block are
unpopulated and return the sentinel. `read_pack()` filters `0x7FFF`
out of the cell list and *derives* `cell_count` from what's left (not
hardcoded to 4), so the same code works on other Felicity variants
with more populated cells.

The same sentinel appears in the temperature registers when a sensor
isn't wired, and `read_pack()` filters those too.

**This matters because the stock driver does NOT do this filtering
consistently.** Two concrete examples found on a live unit:
- The stock driver publishes `/System/NrOfCellsPerBattery = 16` and
  `/Voltages/Cell5..16 = 32.767` — the raw sentinel divided by 1000,
  unfiltered, presented as if it were a real 32.767 V cell.
- `/Dc/0/Temperature` reads as a nonsense five-figure value: `battery.py`'s
  `get_temperature()` averages `temperature_1, temperature_2,
  temperature_3` with no sentinel check. With two real sensors around
  37°C and a third, unpopulated sensor reading the 32767 sentinel,
  `(37 + 37 + 32767) / 3 = 10947.0` — traced and reproduced exactly.

## Temperature register: 4921, not 4929

The stock driver's inline comment claims registers 4929-4931, but its
own byte literal `b"\x13\x39\x00\x05"` decodes to register **4921**
(0x1339), count 5 — the comment is wrong, the byte literal is
authoritative. Confirmed by reading a live pack's published values
back through the stock driver's known offset math
(`temperature_1/2/3` = offsets 1/2/3 of this 5-register block, i.e.
registers 4922/4923/4924): offsets 1 and 2 give plausible ~37°C
readings, and offset 3 gives the unpopulated-sensor sentinel —
consistent with a pack that only has 2 of 3 temperature sensors wired.
Register 4921 itself (offset 0) is read but never assigned by the
stock driver.

## SIM mode

Pass the port string `SIM` (or `sim:<label>`, e.g. `sim:B`) instead of
a device path and `read_pack()` returns synthetic data instead of
opening anything. Values are small random jitters around a real
baseline captured non-invasively from a live pack's running D-Bus
service — the serial port itself was never opened for this. Every
simulated dict has `'simulated': True` and `raw` contains only a
`__simulated__` marker, so it can never be mistaken for a real
reading.

This is what lets `discovery.py` pad an unclaimed bank slot with
plausible data (`SIMULATE_UNCLAIMED_SLOTS`) before a second RS485
adapter is physically connected — the full bank/pack/cell hierarchy
can be exercised end-to-end without both packs wired up yet:

```
python3 felicity_reader.py /dev/ttyUSB0 /dev/ttyUSB1
```

`read_all()` handles a missing/unreadable port per-entry — one pack
failing never blocks the other from reading.

## Function contracts

- `read_pack(port, address=1, timeout=1.0) -> dict` — one pack, one port.
  Never raises, never hangs past `timeout` per register read. CRC failure
  or malformed frame → `ok=False` with a descriptive `error`, not partial
  data.
- `read_all(ports, address=1, timeout=1.0) -> list[dict]` — reads every
  port in order; one failing port never aborts the others.
- `__main__` — `python3 felicity_reader.py [port ...]`, defaults to
  `/dev/ttyUSB0 SIM` if no args given. Pretty-prints all packs side by side:
  per-cell voltages, min/max/spread(mV), pack V/I/SoC, temps.

## Grace period on transient read failures

A single failed poll never drops a pack from the bank aggregates.
`discovery.py` carries the previous cycle's data forward through
`FAIL_THRESHOLD` (default 3) consecutive failed reads before a slot is
marked absent; `dbus-felicity-bank.py` polls every `POLL_INTERVAL_MS`
(default 2000ms). Max staleness of carried-forward data before a pack
is fully dropped is therefore about two poll cycles (~4s), with full
removal on the third consecutive failure (~6s after the last good
read).

## Charge/discharge gating

`/Io/AllowToCharge` / `/Io/AllowToDischarge` are derived from a
conservative voltage/temperature guard rather than from the BMS's own
fault bitmap (register 4866), because that register's bit meanings are
verified in the upstream driver against a different Felicity model
(LPBF48250) than the FLA12100 hardware this was built against — see
`FAULT-DECODE.md` for the full analysis. The guard in `params.py`:

- Blocks charge if any present pack's max cell voltage is ≥ 3.65 V, any
  temperature reading is outside 0-50°C, or bank voltage is at or above
  the DVCC-derived `/Info/MaxChargeVoltage`.
- Blocks discharge if any present pack's min cell voltage is ≤ 2.50 V.
- Only trips from a real present pack's actual decoded reading — never
  from sentinel/unread/default state, so it cannot publish a false
  do-not-charge.

The decoded FET state from register 4866/4868 is still exposed
observationally (`/Battery/<n>/ChargeFetObserved` /
`DischargeFetObserved`, plus a divergence-triggered log line comparing
it against the live heuristic) so that agreement between the two can
be tracked over time before ever making the decoded bits authoritative.
**DVCC must stay off** until that decode is trusted.

## Alarms

Two independent alarm sources, both annunciate-only (never gate
charge/discharge):

- **Fault-bitmap alarms** (register 4866/4868, decoded per
  `FAULT-DECODE.md`): the 7 named fault bits from the upstream driver's
  bitmap, OR'd across present packs, published at `/Alarms/*`.
- **Derived threshold alarms** (`params.py`): `LowVoltage`,
  `HighVoltage`, `LowSoc`, `CellImbalance`, computed from bank
  aggregates the aggregator already produces each cycle (bank voltage,
  SoC, cell-voltage spread) — no new register reads. Thresholds are
  named constants at the top of `params.py`, chosen relative to this
  BMS's own DVCC-reported limits and the charger's real absorption
  setpoint so normal operation stays quiet. `LowVoltage`/`HighVoltage`
  only evaluate when both series packs are present (`nr_of_batteries ==
  2`) — a bank voltage reading with one pack absent is a partial-string
  value and is not comparable to a full-string threshold, so it is
  intentionally excluded rather than raising a false alarm.
- `params.py` is a pure, memoryless function (no D-Bus, no I/O, no
  state between calls) — alarms use a plain threshold compare rather
  than a stateful hysteresis band, on the reasoning that bank
  voltage/SoC/cell-spread drift over minutes under normal load rather
  than oscillating at the poll rate, and VRM's own alarm history
  samples well above that rate.

## History / accumulation (`history.py`)

A persistent coulomb/energy counter: `/History/ChargedEnergy`,
`DischargedEnergy`, `TotalAhDrawn`, `MinimumVoltage`, `MaximumVoltage`,
`MinimumCellVoltage`, `MaximumCellVoltage`, `DeepestDischarge`,
`LastDischarge`, `ChargeCycles`, `TimeSinceLastFullCharge`, plus the
real coulomb-counted `/ConsumedAmphours` (supersedes the SoC-derived
approximation once seeded). State persists to `history.json` (atomic
tmp+rename, corruption-safe — malformed/missing file starts fresh,
never a crash), saved at most every 60s plus a forced flush on
SIGTERM. A single inter-cycle gap over 30s (clock jump/restart) skips
integration for that step rather than corrupting the accumulators.
Full-charge/cycle detection is SoC-threshold-based (≥99.5%, hysteresis
re-arm below 97%). State is fixed-size — no per-pack/per-poll growth.

## Known gaps

Not implemented — no register or topology support on this hardware,
not faked: `/Soh`, `/Balancing`, `/Dc/0/MidVoltage`,
`/Alarms/StateOfHealth`. See `FEATURE-MAP.md` for the full path-by-path
gap analysis against the standard Victron battery interface.

## Venus OS boot persistence

`/service` on Venus OS is a **volatile tmpfs, rebuilt from scratch on
every boot** from the `/opt/victronenergy/service` registry — anything
placed there manually (or symlinked from `/data`) without being part of
that registry is wiped at boot and never recreated, so the daemon
simply never launches after a reboot (it does not crash; there is
nothing to crash).

The durable pattern used here: a boot hook appended to `/data/rc.local`
(run late at boot, after `svscan` is already up) recreates the
`/service/dbus-felicity-bank` symlink pointing at this project's
`service/` directory and re-asserts the run scripts' executable bit.
`svscan` then supervises it within a few seconds. Registering the
service under `/opt/victronenergy/service` directly was deliberately
avoided — that tree is replaced wholesale by firmware updates, which
would silently drop the service again.

## Hardware notes

Two FTDI FT232R USB-RS485 adapters, each on its own bus:

- **Battery 1** = adapter FTDI serial `<PACK_A_SERIAL_ADAPTER>`, pack serial `<PACK_A_SERIAL>`
- **Battery 2** = adapter FTDI serial `<PACK_B_SERIAL_ADAPTER>`, pack serial `<PACK_B_SERIAL>`

The udev rule that routes these adapters sets `VE_SERVICE=ignore` for
both adapter serials, so Venus OS's `serial-starter` leaves both ports
free for this aggregator to own instead of trying to claim them for
its own battery/GPS/VE.Direct auto-detection. Pack-slot identity is by
pack serial (persisted in `pack_mapping.json`, via `discovery.py`), not
by port order — `/dev/ttyUSB*` device nodes can and do renumber across
reboots or reconnects.
