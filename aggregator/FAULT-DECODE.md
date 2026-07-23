# Felicity BMS status/fault register decode (register 4866, 3 regs / 6 bytes)

Sources: `felicity.py` (github.com/mr-manuel/venus-os_dbus-serialbattery,
`dbus-serialbattery/bms/felicity.py`, PR #116 by `versager`, "tested with
LPBF48250 fw 418"), `battery.py` (`Protection` class), this box's
`/data/rs485-cells/aggregator/felicity_reader.py`,
`dbus-felicity-bank.py`, `PARAM-SPEC.md`.

## felicity_py_decode
felicity.py reads 6 bytes (3 registers: 4866,4867,4868) with
`command_status = b"\x13\x02\x00\x03"`. Register **4867 is read but never
parsed** — unexplained gap.

```python
status_int = unpack_from(">H", status_data)[0]                 # reg 4866
self.charge_fet    = True if (status_int & 0b0000000000000001) > 0 else False
self.discharge_fet = True if (status_int & 0b0000000000000100) > 0 else False

fault_int = unpack_from(">H", status_data, 2 * 2)[0]            # reg 4868
self.protection.high_cell_voltage        = 2 if (fault_int & 0b0000000000000100) > 0 else 0
self.protection.low_cell_voltage         = 2 if (fault_int & 0b0000000000001000) > 0 else 0
self.protection.high_charge_current      = 2 if (fault_int & 0b0000000000010000) > 0 else 0
self.protection.high_discharge_current   = 2 if (fault_int & 0b0000000000100000) > 0 else 0
self.protection.high_internal_temperature= 2 if (fault_int & 0b0000000001000000) > 0 else 0
self.protection.high_charge_temperature  = 2 if (fault_int & 0b0000000100000000) > 0 else 0
self.protection.low_charge_temperature   = 2 if (fault_int & 0b0000001000000000) > 0 else 0
```
No pack-level HighVoltage/LowVoltage, no CellImbalance, no InternalFailure,
no balancing bit, no MOSFET-fault bit — felicity.py simply doesn't decode them.

## register_bitmap
Reg 4866 (status): bit0=charge_fet enable [source, high], bit2=discharge_fet
enable [source, high]; bits 1,3-15 unknown.
Reg 4867: entirely unused by any known driver — unknown.
Reg 4868 (fault): bit2=high_cell_voltage, bit3=low_cell_voltage,
bit4=high_charge_current, bit5=high_discharge_current,
bit6=high_internal_temperature, bit8=high_charge_temperature,
bit9=low_charge_temperature — all [source, high]. Bits 0,1,7,10-15 unknown.
No community/manufacturer doc found: powerforum.co.za guide is
Cloudflare-gated (403/JS challenge, unreachable); no GitHub issue/PR comment
adds bit detail beyond the code; on-box manual (FLA12100, doc
358-010405-00, see PINOUT.md §8) explicitly states protection setpoints/regs
are undocumented.

## victron_alarm_mapping
Per `Protection` class docstring in battery.py ("alarm name in GUI = variable
name") [source]: high_cell_voltage→/Alarms/HighCellVoltage,
low_cell_voltage→/Alarms/LowCellVoltage,
high_charge_current→/Alarms/HighChargeCurrent,
high_discharge_current→/Alarms/HighDischargeCurrent,
high_charge_temperature→/Alarms/HighChargeTemperature,
low_charge_temperature→/Alarms/LowChargeTemperature,
high_internal_temperature→/Alarms/HighInternalTemperature (BMS board temp,
NOT cell temp — do not conflate with HighTemperature). No source bit for
HighVoltage, LowVoltage, HighTemperature, LowTemperature, CellImbalance,
InternalFailure — keep heuristic for these.

## allow_to_charge_source
status_int bit0 (charge_fet) / bit2 (discharge_fet) are the BMS's own MOSFET
enable flags — a direct hardware state, not an inferred fault interpretation.
Best real candidate for /Io/AllowToCharge and /Io/AllowToDischarge.

## safe_to_implement_now
HIGH confidence, safe to wire now: charge_fet→AllowToCharge, discharge_fet→
AllowToDischarge, and the 7 named fault bits→their exact alarm paths above —
all are verbatim from the merged, maintained felicity.py source.
KEEP HEURISTIC: reg 4867 (unused/unexplained), fault bits 0,1,7,10-15
(unknown), pack-level HighVoltage/LowVoltage, HighTemperature/LowTemperature,
CellImbalance, InternalFailure.
**Material caveat**: felicity.py was tested against model LPBF48250 fw418;
this box's pack is documented (see `PINOUT.md`) as FLA12100 — a
different Felicity model. Same-family register-map reuse is plausible but
UNVERIFIED. A wrong bit → false "do not charge" is worse than the current
heuristic, so before trusting charge_fet/discharge_fet for real
AllowToCharge/Discharge gating, cross-check readback below over time,
ideally through a charge/discharge cycle.

## live_healthy_readback
Could not obtain raw hex of register 4866 read-only: felicity_reader.py
stores it in an internal `raw{}` dict but never publishes it to D-Bus, and
only logs it at DEBUG level (this run logs INFO/WARNING only) — reading it
would require restarting the daemon with debug logging or opening the port
ourselves, both disallowed. Live D-Bus readback (`dbus -y
com.victronenergy.battery.felicity_bank / GetValue`) shows `Battery/1/Status`
= `Battery/2/Status` = 1 (defined in INTERFACE.md as "responding normally",
NOT a decoded BMS fault) and `Io/AllowToCharge` = `Io/AllowToDischarge` = 1 —
but per PARAM-SPEC.md this is the documented "default allow=1 whenever ≥1
pack connected" heuristic, not derived from decoded 4866 bits. So: no direct
confirmation the fault bits currently read all-clear; only indirect evidence
(pack is in normal service, no alarms reported anywhere).

## file_written
Confirmed: `/data/rs485-cells/aggregator/FAULT-DECODE.md`.

## register_4866_4868_implementation_status (2026-07-21, post sentinel-fix)
Settled: **both reg 4866 (status) and reg 4868 (fault) ARE implemented on
this live FLA12100 pack — neither reads as the 0x7FFF sentinel.**

`_decode_status()` in felicity_reader.py was fixed to filter SENTINEL_INT16
(0x7FFF) before bitmasking, mirroring the sentinel-filter already used for
cells/temps/DVCC elsewhere in the file (previously the fault register was
bitmasked directly — if 4868 had returned the sentinel, bits 2,3,4,5,6,8,9
would all be set, firing and latching all 7 `/Alarms/*` at once from a
*successful* read of unpopulated data). Direct raw hex of the register
still could not be read (daemon holds ttyUSB0; no raw{} exposure on D-Bus —
see `live_healthy_readback` above), so this was settled indirectly via a
before/after live comparison, which turned out to be conclusive:

- **Pre-fix** (old code, no sentinel filtering): all 7 `/Alarms/*` = 0,
  `Battery/{1,2}/{Charge,Discharge}FetObserved` = 1. If fault_int (4868)
  had been 0x7FFF, alarms would have shown 2 (active), not 0 — so 4868 was
  already provably non-sentinel before the fix. Charge/discharge FET bits
  alone were ambiguous pre-fix, since 0x7FFF also happens to have bits 0
  and 2 set (the same bits used for charge/discharge FET), so "FETs=1"
  could not by itself distinguish real data from sentinel.
- **Post-fix** (sentinel now forces `fet_charge_observed`/
  `fet_discharge_observed` to `None` if status_int==0x7FFF): daemon
  restarted cleanly, and `Battery/{1,2}/{Charge,Discharge}FetObserved`
  **stayed at 1** (did not flip to `None`/unavailable). This is the
  disambiguating evidence: had reg 4866 actually been the sentinel, the
  fix would have forced these to `None` immediately. They didn't move, so
  reg 4866 is genuinely populated with real BMS status data (both FETs
  reporting closed/enabled), not the unpopulated sentinel.

Net: the fault register (4868) determination requested for this task is
**implemented, not sentinel** — confirmed by both the pre-fix alarm
readback and the post-fix FET readback. The **decoded alarms are
trustworthy on this pack** (not spurious, not suppressed-by-default) and
the sentinel fix is a genuine correctness fix for a risk that was live-real
on this hardware, not just theoretical.

This resolves whether the registers are *populated*. It does **not**
resolve whether the specific *bit meanings* felicity.py assigns are correct
for FLA12100 vs the LPBF48250 it was verified against (see
`safe_to_implement_now` above) — that model-mismatch question is unchanged
and is why AllowToCharge/AllowToDischarge still deliberately do not consume
the FET bits (params.py heuristic remains authoritative).

## uncertainty
Register 4867 unexplained; fault bits 0,1,7,10-15 unknown (could hide real
faults — safe direction, since unknown=no-alarm, not false-alarm); no
independent community/manufacturer doc reachable to cross-check; **model
mismatch (LPBF48250 vs FLA12100) is unverified** and is the largest single
risk factor. Recommend implementing AllowToCharge/Discharge + the 7 named
alarms from felicity.py now (high confidence, safe direction on unknowns),
but monitor logged transitions for a few weeks before fully trusting
AllowToCharge/Discharge over the current heuristic.

UPDATE (2026-07-21): the "is the register even populated" half of this
uncertainty is now resolved (see `register_4866_4868_implementation_status`
above) — both 4866 and 4868 are real, populated data on this pack, not
sentinel. The remaining open risk is narrower: whether felicity.py's *bit
meanings* (verified against LPBF48250) transfer correctly to FLA12100. The
7 named fault bits are still treated as higher-confidence (verbatim from
the merged driver source) and stay wired into `/Alarms/*` (annunciate-only).
The FET bits stay observational-only pending further cross-checking, per
`safe_to_implement_now`.
