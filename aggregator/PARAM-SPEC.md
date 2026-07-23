# PARAM-SPEC.md — Felicity Bank Aggregator: D-Bus Parameter Derivations

Authoritative reference for how every published D-Bus parameter is computed.
Fixes specific bugs verified in the stock `dbus-serialbattery` driver
(`/data/apps/dbus-serialbattery/bms/felicity.py`) on this hardware. All
"correct-value" figures below are either live values read from the running
`com.victronenergy.battery.felicity_bank` service (DeviceInstance 514,
verified 2026-07-21) or the formula that produces them — never guessed.

Inputs: a list of per-pack dicts from `felicity_reader.read_all(ports)`
(see INTERFACE.md `params.py` for the exact normalized-dict contract this
table maps onto D-Bus paths).

## Stock bugs, root-caused against `felicity.py`

| Parameter | Stock-wrong value | Correct value | How derived |
|---|---|---|---|
| `/System/MaxCellVoltage`, `/System/MinCellVoltage` (+`CellId`) | `32.767` V — `felicity.py:117` hardcodes `self.cell_count = 16` and appends 16 `Cell()` objects regardless of how many are physically populated; unpopulated cell registers hold the `0x7FFF` sentinel, which the driver never filters before taking min/max, so it surfaces as a bogus 32.767 V cell. | Real reading, e.g. `3.434` / `3.347` V. | `felicity_reader.read_pack()` already drops any raw register `== 0x7FFF` before building `cells[]` (register 4906, 16 regs, mV/1000). The aggregator takes `min()`/`max()` of every real cell across **both** connected packs (not per-pack), tagging the winner `CellId = "B{pack_index}C{cell_index}"` (1-based), e.g. `B1C2`. |
| `/System/NrOfCellsPerBattery` | `16` — hardcoded at `felicity.py:117`, never derived from what the pack actually reports. | Derived count, e.g. `4` on this 4S hardware. | `cell_count = len(cells)` after sentinel filtering, inside `read_pack()`. The aggregator publishes `max(cell_count across connected packs)` (falls back to the last-known value if no pack is currently connected, so the path never goes stale-empty). Never a literal constant. |
| `/Dc/0/Temperature` | `10947.0` — exact reproduction: `felicity.py:241-243` reads `temperature_1/2/3` from register block 4921 (5 regs; the driver's own comment claims 4929, its byte literal `\x13\x39\x00\x05` decodes to 4921 — comment is wrong, byte literal is authoritative) with **no sentinel check**, then the generic `battery.py` base class averages the three: on the live unit `temperature_1=37, temperature_2=37, temperature_3=32767` (3rd sensor unpopulated) → `(37+37+32767)/3 = 10947.0`. | Real reading, e.g. `37.6` °C. | `felicity_reader.read_pack()` reads `temp_bms` (register 4874, 1 reg) and `temps[1:3]` from the 4921 block, filtering `0x7FFF` from `temps[]` before returning. The aggregator collects `temp_bms` + all filtered `temps[]` from **every connected pack** into one list and takes `max()` — never an average, and sentinels are filtered *before* the max, not averaged in. |
| `/Info/MaxChargeCurrent`, `/Info/MaxDischargeCurrent` | `0.0` A with a false "Cell OVP" fault — DVCC read (register 4892) only fires `if utils.USE_BMS_DVCC_VALUES`; when it's off (or the reading is skipped), the generic protection state machine derives CCL/DCL from cell voltage min/max, and the 32.767 V sentinel cell (see row 1) trips a spurious over-voltage protection that zeroes both currents. | Real DVCC-derived value, e.g. `10.0` A charge / `50.0` A discharge (bank-level, live). | `felicity_reader.read_pack()` always reads register 4892 (4 regs: `maxV`, `minV` at offsets 0/1 scaled `/100`; `maxChg`, `maxDis` at offsets 2/3 scaled `/10`), sentinel-filtered to `None` per field. Bank-level: these are **current** limits on a series string, so the whole string is capped by whichever pack is more restrictive → `MIN()` across connected packs' `dvcc_max_charge_current` / `dvcc_max_discharge_current` (not sum, not average). Companion voltage limits (`/Info/MaxChargeVoltage`, `/Info/BatteryLowVoltage`) are series-*additive* (`SUM()` of `dvcc_max_v` / `dvcc_min_v`) since voltage stacks across packs in series — different rule from current, stated explicitly here to prevent the two being conflated. |
| `/Io/AllowToCharge`, `/Io/AllowToDischarge` | Same false-OVP mechanism as above can also force these to 0 via the sentinel-poisoned protection state machine. | `1` (allowed) whenever ≥1 pack is connected, **explicitly undecoded** from the fault bitmap. | Register 4866 (3 regs, status/fault bitmask) is read and preserved in `felicity_reader`'s `raw{}` for inspection, but **no verified bit-map for this register exists** — reverse-engineering it wrong risks a confident-but-wrong "fault" reading, which is worse than an honest default. Per the design rule for this parameter: **a false "do not charge" is worse than not publishing a decoded fault**, so the aggregator defaults `allow = 1` whenever at least one pack is connected and does not attempt to decode 4866 into these two paths. This is a documented, deliberate simplification — not a placeholder to "fix later" — until someone captures a verified 4866 bit-map against a real fault condition. |
| `/InstalledCapacity` (bank) and per-pack `/Battery/<n>/Capacity` | `49.5` / `50` Ah-class values from `utils.BATTERY_CAPACITY` config defaults, not derived from the actual pack count/topology. | `100.0` Ah at **both** levels. | No register in the read set reports Ah capacity — it's a known hardware fact (each Felicity FLA pack is nameplate 100 Ah), not something read off the wire. Packs are wired in **series** (voltage stacks, capacity does not), so bank `/InstalledCapacity` = **one pack's** capacity = `100.0` Ah, not `2 × 100 = 200`. Each `/Battery/<n>/Capacity` independently publishes `100.0` (nameplate, per pack). |
| `/TimeToGo` | Not applicable (stock driver's own TTG logic inherits whatever corrupted current/capacity it computed). | **Omitted** — not published. | No current implementation of `/TimeToGo` exists; `/Capacity` (remaining Ah = `InstalledCapacity × Soc / 100`) and `/Dc/0/Current` are both real and available so a real TTG *could* be derived (`remaining_Ah / |current|` when discharging), but it is not currently wired up. Stated explicitly rather than silently omitted: if TTG is wanted, compute it from `Capacity` and `Dc/0/Current` — do not port the stock driver's TTG math, it inherits the same corrupted inputs documented above. |

## Bank-level combination rules (not stock-vs-fixed, but must be stated explicitly per spec)

| Parameter | Rule | Why |
|---|---|---|
| `/Dc/0/Voltage` (bank) | `SUM()` of both packs' `voltage`. | Packs are wired in **series** — series stacking adds voltage. Live: `13.54 + 13.56 = 27.10` V. |
| `/Dc/0/Current` (bank) | **AVERAGE** of both packs' `current` readings (`sum(currents) / len(currents)`) — chosen over "use pack-1's reading only". | Physically, one series string carries one current, so both packs' independently-measured `current` *should* read identically; any difference is per-shunt/per-BMS measurement noise, not a real second current. Averaging cancels that noise symmetrically. Picking pack-1 arbitrarily would silently bias the bank reading toward whichever BMS happens to be less accurately calibrated, with no way to detect it. Averaging is also robust to either pack briefly dropping out (falls back to the single connected pack's reading). |
| `/Soc` (bank) | `MIN()` of both packs' `soc`. | The weakest pack limits the usable capacity of the whole series string — always the conservative choice, never an average (an average could report headroom that doesn't actually exist on the weaker pack). |
| `/Capacity` (bank, remaining Ah) | `InstalledCapacity × bank_Soc / 100`. | Derived, not read from a register — consistent with `/InstalledCapacity` = 100 Ah and `/Soc` = MIN rule above. |

## Verification note

The stock-bug values in the first table are grounded in the running driver
source at `/data/apps/dbus-serialbattery/bms/felicity.py` (hardcoded
`cell_count = 16` at line 117; unfiltered `temperature_1/2/3` at lines
241-243; conditional/skippable DVCC read at lines 149-163) — read directly,
not assumed. The "correct value" figures are live values pulled from
`com.victronenergy.battery.felicity_bank` (`DeviceInstance` 514) on
2026-07-21, with Battery 1 real (`/dev/ttyUSB0`, serial `<PACK_A_SERIAL>`)
and Battery 2 simulated (`SIMULATE_PACK2 = True`, pending the second RS485
adapter) — both are exercising the identical aggregation code path, so the
combination-rule math (SUM/MIN/MAX/AVERAGE above) is proven on live output,
not just described.
