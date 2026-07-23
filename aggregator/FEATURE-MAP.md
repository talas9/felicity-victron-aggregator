# Felicity Bank D-Bus Feature Map & Gap Analysis

Generated 2026-07-21. Read-only survey of the Cerbo GX at `<cerbo-ip>`.

## Sources read
- gui-v2: `/opt/victronenergy/gui-v2/Victron/VenusOS/pages/settings/devicelist/battery/*.qml`
- legacy GUI: `/opt/victronenergy/gui/qml/PageBattery*.qml`
- reference driver: `/data/apps/dbus-serialbattery/dbushelper.py`, `battery.py`, `bms/felicity.py` (Felicity-specific — same protocol as this hardware)
- system consumer: `/opt/victronenergy/dbus-systemcalc-py/dbus_systemcalc.py`
- current state: `dbus -y com.victronenergy.battery.felicity_bank / GetValue` + `/data/rs485-cells/aggregator/dbusservice.py`, `params.py`, `felicity_reader.py`

## Currently published by felicity_bank (instance 514) — confirmed live
`/Connected /System/NrOfBatteries /System/NrOfCellsPerBattery /System/MinCellVoltage /System/MaxCellVoltage /System/MinVoltageCellId /System/MaxVoltageCellId /Dc/0/Voltage /Dc/0/Current /Dc/0/Power /Dc/0/Temperature /Soc /Capacity /InstalledCapacity /Info/MaxChargeVoltage /Info/BatteryLowVoltage /Info/MaxChargeCurrent /Info/MaxDischargeCurrent /Io/AllowToCharge /Io/AllowToDischarge /Redetect /Battery/Request/Id /Mgmt/* /DeviceInstance /ProductId /ProductName /FirmwareVersion /HardwareVersion` + per pack (`/Battery/1|2/Serial,Voltage,Current,Temperature,Capacity,FwVersion,Status,Cell/1-16/Voltage`).

Registers already polled every cycle by `felicity_reader.py` [verified: felicity_reader.py:43-78]: cell voltages (4906), total V/I (4870), SoC (4875), **status/fault (4866, read but not decoded)**, BMS MOS temp (4874), temps 1-3 (4921), DVCC limits (4892), firmware (63499), serial (63492). **No SOH register exists in this protocol** [verified: felicity_reader.py has no SOH register; bms/felicity.py never sets `self.soh`].

## Gap table

| Path | Category | Published now? | How to derive | Notes |
|---|---|---|---|---|
| `/Soh` | CANNOT | No | — | No SOH register in Felicity Modbus map [verified: felicity_reader.py, bms/felicity.py]. `/Alarms/StateOfHealth` is CANNOT too (depends on Soh). |
| `/System/MinCellTemperature`, `/MaxCellTemperature`, `/MinTemperatureCellId`, `/MaxTemperatureCellId` | DERIVABLE NOW | **Yes (2026-07-21)** | min/max of already-read temp1/2/3 (reg 4921) + MOS temp (4874) per pack | Sensors are BMS-board, not true per-cell; name is the Victron-standard path anyway [gui-v2 BatteryDetails.qml binds it]. IDs labeled `B<pack>BMS`/`B<pack>T<n>`. |
| `/System/NrOfModulesOnline`, `/NrOfModulesOffline` | DERIVABLE NOW | **Yes (2026-07-21)** | count of packs that answered this poll vs `BATTERY_SLOT_INDICES` | |
| `/System/NrOfModulesBlockingCharge`, `/NrOfModulesBlockingDischarge` | DERIVABLE NOW | **Yes (2026-07-21)** | `params.py`'s existing `fault_block_charge`/`fault_block_discharge` booleans, cast to 0/1 count | Bank-wide 0/1, not per-pack-attributed -- the underlying heuristic itself is bank-wide (cross-pack min/max), so per-pack attribution would be invented, not derived. |
| `/Alarms/HighCellVoltage`, `/LowCellVoltage`, `/HighChargeCurrent`, `/HighDischargeCurrent`, `/HighInternalTemperature`, `/HighChargeTemperature`, `/LowChargeTemperature` | NEEDS FAULT DECODE | **Yes (2026-07-21)** | bit-decode fault word of reg 4866 (2nd of 3 registers) using bits 2,3,4,5,6,8,9 exactly as `bms/felicity.py` decodes them [verified: bms/felicity.py fault_int bitmap] | Decoded in `felicity_reader._decode_status()`, aggregated bank-wide (OR across present packs) in `params.py`. Annunciate-only, Victron 0/2 convention. Live-verified all 0 (healthy) 2026-07-21. |
| `/Io/AllowToCharge`, `/Io/AllowToDischarge` (real) | NEEDS FAULT DECODE | **Still heuristic-only (deliberate)** | status word of reg 4866, bit0=charge_fet, bit2=discharge_fet [verified: bms/felicity.py] | Decoded and published OBSERVATIONALLY at `/Battery/<n>/ChargeFetObserved`/`DischargeFetObserved` (2026-07-21) plus a divergence-triggered log line -- but deliberately NOT wired into the authoritative `/Io/AllowToCharge`/`Discharge` yet: `felicity.py`'s decode was verified against LPBF48250, not this box's FLA12100 (model mismatch UNVERIFIED, see FAULT-DECODE.md). Revisit after days of observed agreement. |
| `/Alarms/LowVoltage`, `/HighVoltage`, `/LowSoc`, `/CellImbalance` | DERIVABLE NOW | **Yes (2026-07-21)** | compare bank `/Dc/0/Voltage`/`/Soc`/`/Voltages/Diff` against named LiFePO4 threshold constants in `params.py` (same style as existing `fault_block_charge` logic) | Not covered by the 4866 fault bitmap -- purely derived. Annunciate-only (never gates `/Io/AllowToCharge`/`Discharge`). Plain threshold compare, no cross-cycle hysteresis state (`params.py` is a pure/memoryless function by design) -- documented as acceptable given the 2s poll vs. VRM's 60s alarm-history sampling. Thresholds: CellImbalance warn>=0.100V/alarm>=0.200V; LowVoltage warn<=24.0V/alarm<=22.0V (24.0V matches this bank's own live BMS low-cutoff, `/Info/BatteryLowVoltage`); HighVoltage warn>=28.8V/alarm>=29.2V (clear of the 28.4V MultiPlus absorption setpoint); LowSoc warn<=15%/alarm<=10%. Live-verified all 4 read 0 (healthy) 2026-07-21. |
| `/Alarms/BmsCable` | DERIVABLE NOW | No | pack present-but-no-response-this-poll detection (data already tracked) | |
| `/Alarms/InternalFailure`, `/FuseBlown`, `/MidVoltage` | CANNOT | No | — | No register/topology support (no midpoint tap; no internal-failure/fuse signal in this protocol). |
| `/History/ChargedEnergy`, `/DischargedEnergy`, `/TotalAhDrawn`, `/ChargeCycles`, `/DeepestDischarge`, `/LastDischarge`, `/MinimumVoltage`, `/MaximumVoltage`, `/MinimumCellVoltage`, `/MaximumCellVoltage`, `/TimeSinceLastFullCharge` | NEEDS ACCUMULATION | **Yes (2026-07-21)** | coulomb/energy counter integrated from already-read V/I each poll, persisted to `history.json` (new `history.py` module); SoC-threshold full-charge/cycle detection | Persisted, atomic tmp+rename, corruption-safe, throttled to <=1 write/60s + forced flush on SIGTERM. Values start near-zero and grow -- expected. |
| `/AverageDischarge`, `/FullDischarges`, `/MinimumTemperature`, `/MaximumTemperature`, `/LowVoltageAlarms`, `/HighVoltageAlarms`, `/AutomaticSyncs` | NEEDS ACCUMULATION | No | not requested in the 2026-07-21 feature-expansion scope | Left unimplemented -- out of scope, not attempted. |
| `/ConsumedAmphours` | DERIVABLE NOW (approx), superseded by NEEDS ACCUMULATION | **Yes (2026-07-21)** | approx: `InstalledCapacity * (1 - Soc/100)`; real: coulomb-counted in `history.py`, seeded from the approximation | The approximation (params.py) is the seed; once `history.py`'s counter has run at least one cycle, its coulomb-counted value supersedes it at the same D-Bus path. |
| `/TimeToGo` | NEEDS TIME/RUNTIME | No | `capacity_remaining_Ah / abs(current) * 3600` | Only meaningful under sustained load; guard against divide-by-~0. |
| `/Io/AllowToBalance` | DERIVABLE NOW | No | permissive heuristic (all cells in safe range) | Low value — no real balance-status register exists to confirm actual balancing activity. |
| `/Balancing` | CANNOT | No | — | No balance-status register in Felicity Modbus map. |
| CVL/CCL/DCL already published (`/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`, `/Info/MaxDischargeCurrent`, `/Info/BatteryLowVoltage`) | NOT APPLICABLE / DVCC | Yes | already derived from reg 4892 | Display-only while DVCC is OFF — system will not act on them [confirmed: dbus_systemcalc.py's battery consumption is `/Soc`, `/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Power` — it does not read `/Info/*` for a non-DVCC battery]. DVCC MUST stay off per user instruction — do not wire these into a DVCC path. |
| `/Dc/1/Voltage`, `/Dc/0/MidVoltage`, `/Dc/0/MidVoltageDeviation`, `/N2kDeviceInstance`, `/Relay/0/State`, `/Fuse/*`, `/Distributor/*`, `/Diagnostics/*` (Lynx LED/IO status) | NOT APPLICABLE | No | — | Starter-battery, Lynx Distributor, and Lynx Smart BMS hardware features; no such hardware exists on this Modbus-aggregated Felicity bank. |
| top-level `/Serial` | DERIVABLE NOW | **Yes (2026-07-21)** | pack 1's serial (first present pack) | Low priority, cosmetic. |
| `/State`, `/ErrorCode` | DERIVABLE NOW | No | `/State` from allow-to-charge/discharge + online | Not requested in the 2026-07-21 feature-expansion scope -- left unimplemented. |
| `/CustomName` | COSMETIC | **Yes (2026-07-21)** | static default "Felicity Bank (2S)", writeable for GUI rename | Not persisted across a service restart (no `SettingsDevice` wired up) -- acceptable per spec ("or make it settable, defaulting to that"). |

## Counts (updated 2026-07-21 post-implementation)
- DERIVABLE NOW: 13 paths/groups -- **implemented: 15** including
  `/Alarms/LowVoltage`, `/HighVoltage`, `/LowSoc`, `/CellImbalance` (added
  2026-07-21, see row above); `/Alarms/BmsCable`, `/Io/AllowToBalance`,
  `/State`, `/ErrorCode` remain NOT requested/implemented. (Note: this
  bucket's path-vs-group counting predates this pass and does not
  cleanly reconcile to 13 either way -- not re-audited here, out of this
  task's scope.)
- NEEDS FAULT DECODE: 9 paths -- **implemented: 7** (the alarms; the 2 real
  `Io/Allow*` paths remain heuristic-only, deliberately, pending FLA12100
  validation -- see FAULT-DECODE.md)
- NEEDS ACCUMULATION: 18 paths -- **implemented: 11** (the paths explicitly
  requested; `/AverageDischarge`, `/FullDischarges`, `/MinimumTemperature`,
  `/MaximumTemperature`, `/LowVoltageAlarms`, `/HighVoltageAlarms`,
  `/AutomaticSyncs` were not requested and remain unimplemented)
- NEEDS TIME/RUNTIME: 1 path (`/TimeToGo`) -- not implemented
- NOT APPLICABLE / DVCC or hardware-absent: 11 paths/groups -- unchanged
- CANNOT (no data): 6 paths -- unchanged, confirmed still unpublished:
  `/Soh`, `/Balancing`, `/Dc/0/MidVoltage`, `/Alarms/StateOfHealth`

## Highest-value quick wins (DERIVABLE NOW today, no accumulation, no new register reads)
1. `/System/MinCellTemperature`, `/MaxCellTemperature`, `/MinTemperatureCellId`, `/MaxTemperatureCellId` -- **done**
2. `/System/NrOfModulesOnline`, `/System/NrOfModulesOffline` -- **done**
3. `/System/NrOfModulesBlockingCharge`, `/System/NrOfModulesBlockingDischarge` -- **done**
4. `/Alarms/LowVoltage`, `/Alarms/HighVoltage`, `/Alarms/LowSoc`, `/Alarms/CellImbalance` -- **done (2026-07-21)**, thresholds documented as named constants at the top of `params.py`
5. `/ConsumedAmphours` (approximate form) -- **done**, plus the real coulomb-counted version (`history.py`)

## Highest-value fault-decode win
Decoding register 4866 (already polled, currently discarded) unlocks 7 real `/Alarms/*` paths AND replaces the current software-heuristic `/Io/AllowToCharge`/`/Io/AllowToDischarge` with the BMS's actual FET state — the single highest-integrity improvement available, and explicitly flagged as a TODO in `params.py` itself.
