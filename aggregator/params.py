#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
params.py -- parameter-normalization layer for the Felicity bank aggregator.

Pure function module: no D-Bus, no I/O, no hardware access. Takes the
per-cycle dict[int, discovery.PackStatus] that discovery.py already
produced and turns it into ONE normalized dict of D-Bus-path-keyed values,
ready for dbusservice.py.update(). See INTERFACE.md for the exact schema
and PARAM-SPEC.md for the derivation/rationale of every value below.

Every stock-driver bug this module fixes is root-caused against
`/data/apps/dbus-serialbattery/bms/felicity.py` in PARAM-SPEC.md -- this
module does not re-derive those bugs, it consumes felicity_reader.py's
already-sentinel-filtered `read_pack()` output (cells[], temps[] have the
0x7FFF sentinel already stripped; cell_count is already derived, not the
stock hardcoded 16).

This module does not import discovery.py -- the `packs` parameter is
duck-typed against discovery.PackStatus's documented shape (attributes:
index, port, serial, present, simulated, real_claimed, fail_count, data).
The type hint is a bare string (`"discovery.PackStatus"`) precisely so
this file has zero import-time coupling to discovery.py, per the "write
ONLY your one assigned file" boundary.
"""

from __future__ import annotations

# Nameplate fact, not a register read -- see PARAM-SPEC.md
# "/InstalledCapacity (bank) and per-pack /Battery/<n>/Capacity" row.
# Packs are wired in series, so capacity does not stack: bank capacity
# equals one pack's capacity, not the sum of both.
PACK_CAPACITY_AH = 100.0
BANK_CAPACITY_AH = 100.0

# felicity_reader.py's SENTINEL_INT16 (0x7FFF = 32767). temps[] is already
# filtered by felicity_reader before it reaches this module; temp_bms is
# NOT filtered by felicity_reader (single-register read, no sentinel check
# in that code path), so this module filters it defensively before taking
# any max() -- matching the "sentinel filtered before max" rule for
# /Dc/0/Temperature in PARAM-SPEC.md.
_SENTINEL = 32767.0


def _pack_ok(pack_status) -> bool:
    """True only if this slot is present AND carries real read_pack() data
    with ok=True. Mirrors discovery.PackStatus.present's documented
    meaning but re-checked here defensively rather than trusted blindly,
    since this module has no way to verify discovery.py's invariant held."""
    data = getattr(pack_status, "data", None)
    return bool(getattr(pack_status, "present", False)) and bool(data) and bool(data.get("ok"))


def _present_entries(packs: dict) -> list[tuple[int, dict]]:
    """(slot_index, read_pack()-shaped data dict) for every present, ok
    slot, sorted by slot index for deterministic CellId assignment."""
    out = []
    for idx in sorted(packs.keys()):
        ps = packs[idx]
        if _pack_ok(ps):
            out.append((idx, ps.data))
    return out


def _temp_readings(data: dict) -> list[float]:
    """temp_bms + temps[] for one pack, sentinel-filtered, None-safe."""
    vals: list[float] = []
    tb = data.get("temp_bms")
    if tb is not None and tb != _SENTINEL:
        vals.append(tb)
    for t in data.get("temps") or []:
        if t is not None and t != _SENTINEL:
            vals.append(t)
    return vals


def _temp_readings_labeled(data: dict) -> list[tuple[str, float]]:
    """Same readings as _temp_readings(), paired with a sensor label for
    /System/Min|MaxTemperatureCellId. "BMS" = the MOS/BMS-board sensor
    (register 4874); "T<n>" = the n-th populated entry of the temps[]
    block (register 4921 block, sentinel already stripped by
    felicity_reader). These are BMS-board sensors, not true per-cell
    sensors -- Victron's own path is named this way regardless (gui-v2
    binds it the same for any battery), see FEATURE-MAP.md."""
    vals: list[tuple[str, float]] = []
    tb = data.get("temp_bms")
    if tb is not None and tb != _SENTINEL:
        vals.append(("BMS", tb))
    for i, t in enumerate(data.get("temps") or [], start=1):
        if t is not None and t != _SENTINEL:
            vals.append((f"T{i}", t))
    return vals


def _avg(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


# ---------------------------------------------------------------------
# Derived threshold alarms (FEATURE-MAP.md "DERIVABLE NOW", added
# 2026-07-21). Computed from bank aggregates this module ALREADY
# produces each cycle (voltages_diff, bank_voltage, bank_soc) -- no new
# Modbus reads. ANNUNCIATE-ONLY, same as the Group B fault alarms: these
# must never feed /Io/AllowToCharge or /Io/AllowToDischarge. Victron
# 0/1/2 convention (0=ok, 1=warning, 2=alarm). Inputs are the already
# sentinel-filtered bank aggregates (voltages_diff/bank_voltage/bank_soc
# are all None, never a 0x7FFF-derived number, whenever no pack is
# present/ok -- see _pack_ok()/_present_entries() above), so a missing
# pack or missing SoC always yields alarm=0 (unknown), never a false trip.
#
# Hysteresis: NOT implemented as a stateful set/clear band. This module
# is documented (module docstring, plus the /History/* comment further
# down) as a PURE, memoryless function -- no D-Bus, no I/O, no state
# carried between calls. Adding a set/clear band would require either a
# module-level global (breaks "pure function") or a new prev-alarms
# parameter threaded through the daemon's per-cycle call in
# dbus-felicity-bank.py (a bigger, separately-reviewable change). Given
# these are monitoring-only annunciations -- not a charge/discharge gate
# -- a plain threshold compare is used instead, accepted here per this
# task's own documented fallback. This is judged acceptable because: (a)
# bank voltage/SoC/cell-spread drift over minutes under normal load, they
# do not oscillate register-to-register at the 2s poll rate the way a
# noisy instantaneous current would; (b) VRM's own alarm history samples
# at 60s, far above the 2s poll, so a handful of local D-Bus toggles right
# at a threshold are unlikely to show up as VRM spam. If real-world use
# shows flapping, add a `prev_alarms: dict | None = None` parameter to
# build_bank_params() (supplied by the daemon's own retained per-cycle
# state) rather than adding hidden state to this module.

# CellImbalance: from the WORST single pack's OWN intra-pack cell spread
# (max_intra_pack_spread, computed per-pack below from each present
# pack's own cells[]) -- NOT the bank-wide cross-pack /Voltages/Diff
# (voltages_diff below, built from the cross-pack min_cell_v/max_cell_v
# computed further up). The bank-wide figure conflates benign INTER-pack
# offset (e.g. one pack charged/discharged individually while the other
# sits idle) with true INTRA-pack cell imbalance: live read 2026-07-23
# showed bank-wide /Voltages/Diff = 0.233 V (would fire ALARM) while each
# pack's own cells were only 56 mV (pack 1) and 95 mV (pack 2) apart --
# both well under WARN. Thresholds below apply to the per-pack metric
# only; voltages_diff stays published at /Voltages/Diff as an
# informational bank-wide figure, never gating an alarm.
_CELL_IMBALANCE_WARN_V = 0.100   # 100 mV -- above this bank's known ~85-99mV baseline
_CELL_IMBALANCE_ALARM_V = 0.200  # 200 mV -- clear real-divergence signal

# LowVoltage / HighVoltage: bank /Dc/0/Voltage (24V nominal, 8S LiFePO4 --
# 2x 4S packs wired in series). Cross-checked against THIS bank's own
# BMS-reported limits (register 4892, already published at
# /Info/BatteryLowVoltage and /Info/MaxChargeVoltage): live read
# 2026-07-21 showed /Info/BatteryLowVoltage = 24 V and
# /Info/MaxChargeVoltage = 29 V, confirming the assumed "12V/pack = 24V
# string" low cutoff and giving a real ceiling to design HighVoltage
# around. Note DVCC is OFF on this system -- the MultiPlus's own charge algorithm, not this aggregator's
# /Info/MaxChargeVoltage, sets the real absorption target (28.4V per the
# MultiPlus config), so HighVoltage thresholds are chosen relative to
# that real absorption setpoint, not the (currently inert) DVCC ceiling.
_BANK_LOW_VOLTAGE_WARN_V = 24.0    # 3.00 V/cell -- AT this bank's own BMS low-voltage cutoff (register 4892 dvcc_min_v, summed): an early heads-up right as the BMS's own protection would engage
_BANK_LOW_VOLTAGE_ALARM_V = 22.0   # 2.75 V/cell -- BELOW the BMS's own cutoff: reaching this means the BMS should already have disconnected, so this level means "investigate the BMS", not "approaching empty"
_BANK_HIGH_VOLTAGE_WARN_V = 28.8   # 3.60 V/cell manufacturer max -- 0.4V above the MultiPlus's 28.4V absorption setpoint, so normal charging never trips it
_BANK_HIGH_VOLTAGE_ALARM_V = 29.2  # above this bank's own live BMS charge ceiling (/Info/MaxChargeVoltage = 29V) -- should never be reached under normal DVCC-off operation

# LowSoc: bank /Soc (MIN across present packs -- the weaker pack limits
# the string -- already computed below as bank_soc).
_LOW_SOC_WARN_PCT = 15.0
_LOW_SOC_ALARM_PCT = 10.0


def _threshold_alarm(value: float | None, warn_thresh: float, alarm_thresh: float, *, high: bool) -> int:
    """Victron 0/1/2 threshold compare. `high=True` -- alarm as the value
    rises (value >= thresh); `high=False` -- alarm as the value falls
    (value <= thresh). None (pack/SoC absent or not-yet-known) always
    returns 0 (ok/unknown), never a false alarm -- callers must not pass
    an unfiltered/sentinel value here (see module docstring: every value
    reaching this module's aggregates is already sentinel-filtered
    upstream)."""
    if value is None:
        return 0
    if high:
        if value >= alarm_thresh:
            return 2
        if value >= warn_thresh:
            return 1
        return 0
    else:
        if value <= alarm_thresh:
            return 2
        if value <= warn_thresh:
            return 1
        return 0


def build_bank_params(packs: dict[int, "discovery.PackStatus"]) -> dict:
    """
    Input: discovery.py's PackStatus dict (indices 1, 2).
    Output: ONE normalized dict, ready for dbusservice.py.update(). See
    INTERFACE.md for the authoritative schema this mirrors exactly.

    Never raises. A pack with data=None (or present=False, or ok=False)
    simply contributes nothing to the cross-pack aggregates -- its slot's
    "packs" entry is still present in the output, values None/0/Capacity-
    constant as documented per field below.
    """
    present = _present_entries(packs)
    nr_of_batteries = len(present)
    connected = nr_of_batteries > 0

    # --- cell aggregates: cross-pack min/max + CellId, per PARAM-SPEC.md
    # "/System/MaxCellVoltage, /System/MinCellVoltage (+CellId)" row.
    # cells[] arriving here is already sentinel-filtered by
    # felicity_reader.read_pack() -- no 32.767V ghost cell is possible.
    min_cell_v: float | None = None
    max_cell_v: float | None = None
    min_cell_id: str | None = None
    max_cell_id: str | None = None
    for idx, data in present:
        for cell_num, v in enumerate(data.get("cells") or [], start=1):
            if min_cell_v is None or v < min_cell_v:
                min_cell_v = v
                min_cell_id = f"B{idx}C{cell_num}"
            if max_cell_v is None or v > max_cell_v:
                max_cell_v = v
                max_cell_id = f"B{idx}C{cell_num}"

    # Per-pack intra-pack cell spread (max-min WITHIN one pack's own
    # cells[], never mixed across packs) -- feeds /Alarms/CellImbalance
    # below instead of the cross-pack voltages_diff. A pack with fewer
    # than 2 read cells has no meaningful spread and is skipped, same
    # None-safe/defensive posture as the rest of this module; a single
    # pack present (the other absent/not ok) still works, since only
    # present packs are iterated here.
    max_intra_pack_spread: float | None = None
    for idx, data in present:
        pack_cells = data.get("cells") or []
        if len(pack_cells) < 2:
            continue
        pack_spread = max(pack_cells) - min(pack_cells)
        if max_intra_pack_spread is None or pack_spread > max_intra_pack_spread:
            max_intra_pack_spread = pack_spread

    # /System/NrOfCellsPerBattery: max(cell_count) across present packs,
    # per PARAM-SPEC.md "/System/NrOfCellsPerBattery" row. This is a pure
    # function with no state, so with zero packs present it is 0 rather
    # than a "last-known" fallback (that fallback, described in
    # PARAM-SPEC.md for the monolithic reference implementation, is a
    # stateful daemon/dbusservice concern, not something this pure
    # function can honestly do).
    nr_of_cells = max((data.get("cell_count", 0) for _, data in present), default=0)

    # /Dc/0/Voltage: SUM across present packs (series stacking).
    voltages = [data["voltage"] for _, data in present if data.get("voltage") is not None]
    bank_voltage = sum(voltages) if voltages else None

    # /Dc/0/Current: AVERAGE across present packs (see PARAM-SPEC.md
    # rationale -- both packs' shunts measure the same series current;
    # averaging cancels per-BMS measurement noise instead of picking one
    # pack's reading arbitrarily).
    currents = [data["current"] for _, data in present if data.get("current") is not None]
    bank_current = _avg(currents)

    bank_power = (bank_voltage * bank_current) if (bank_voltage is not None and bank_current is not None) else None

    # /Dc/0/Temperature: MAX of sentinel-filtered temps across all present
    # packs (never an average -- see PARAM-SPEC.md row for why the stock
    # driver's average-including-a-sentinel is wrong).
    all_temps: list[float] = []
    for _, data in present:
        all_temps.extend(_temp_readings(data))
    bank_temperature = max(all_temps) if all_temps else None

    # /System/MinCellTemperature, /MaxCellTemperature (+CellId): same
    # cross-pack min/max pattern as the cell-voltage block above, applied
    # to the labeled temp readings (BMS-board sensors, not true per-cell --
    # see _temp_readings_labeled docstring). DERIVABLE NOW per
    # FEATURE-MAP.md: no new register reads, reuses temp_bms/temps already
    # read for /Dc/0/Temperature above.
    min_temp: float | None = None
    max_temp: float | None = None
    min_temp_id: str | None = None
    max_temp_id: str | None = None
    for idx, data in present:
        for label, t in _temp_readings_labeled(data):
            if min_temp is None or t < min_temp:
                min_temp = t
                min_temp_id = f"B{idx}{label}"
            if max_temp is None or t > max_temp:
                max_temp = t
                max_temp_id = f"B{idx}{label}"

    # /Soc: MIN across present packs -- the weaker pack limits the string.
    socs = [data["soc"] for _, data in present if data.get("soc") is not None]
    bank_soc = min(socs) if socs else None

    # /Capacity: remaining Ah, derived (not read) from InstalledCapacity * Soc/100.
    bank_capacity_remaining = (BANK_CAPACITY_AH * bank_soc / 100.0) if bank_soc is not None else None

    # /ConsumedAmphours (approximate form, Group A): derived purely from
    # bank Soc, NOT coulomb-counted. This is an instantaneous SoC-derived
    # estimate only -- the real coulomb-counted version (accumulated from
    # V*I over time, persisted across restarts) lives in history.py /
    # dbus-felicity-bank.py and OVERWRITES this key in the dict the daemon
    # actually publishes once the counter has run at least one cycle. This
    # value remains the correct fallback/seed before that has happened.
    consumed_amphours_approx = (
        BANK_CAPACITY_AH * (1.0 - bank_soc / 100.0) if bank_soc is not None else None
    )

    # /System/NrOfModulesOnline, /NrOfModulesOffline: bank is hard 2S, so
    # total module count is always len(packs) (== 2); online = packs that
    # answered ok this poll, offline = the rest. DERIVABLE NOW: pure
    # presence bookkeeping already computed above (present/packs).
    nr_of_modules_total = len(packs)
    nr_of_modules_online = nr_of_batteries
    nr_of_modules_offline = max(nr_of_modules_total - nr_of_modules_online, 0)

    # DVCC-derived limits (register 4892). Voltage limits are series-
    # additive (SUM); current limits are capped by the more restrictive
    # pack (MIN) -- explicit split per PARAM-SPEC.md "Companion voltage
    # limits ... are series-additive ... different rule from current".
    dvcc_max_v = [data["dvcc_max_v"] for _, data in present if data.get("dvcc_max_v") is not None]
    dvcc_min_v = [data["dvcc_min_v"] for _, data in present if data.get("dvcc_min_v") is not None]
    dvcc_max_chg = [data["dvcc_max_charge_current"] for _, data in present if data.get("dvcc_max_charge_current") is not None]
    dvcc_max_dis = [data["dvcc_max_discharge_current"] for _, data in present if data.get("dvcc_max_discharge_current") is not None]

    max_charge_voltage = sum(dvcc_max_v) if dvcc_max_v else None
    battery_low_voltage = sum(dvcc_min_v) if dvcc_min_v else None
    max_charge_current = min(dvcc_max_chg) if dvcc_max_chg else None
    max_discharge_current = min(dvcc_max_dis) if dvcc_max_dis else None

    # /Io/AllowToCharge, /Io/AllowToDischarge: conservative safety guard.
    #
    # Registers 4866/4868 (status/fault bitmap) ARE now decoded --
    # felicity_reader._decode_status() turns them into
    # fet_charge_observed/fet_discharge_observed (reg 4866) and fault_flags
    # (reg 4868, sentinel-filtered), and the 7 named fault bits ARE
    # published as /Alarms/* below (Group B). AllowToCharge/
    # AllowToDischarge deliberately do NOT consume that decode, though:
    # felicity.py's bit-map was verified against model LPBF48250 fw418, not
    # this box's FLA12100 -- same-family register reuse is plausible but
    # UNVERIFIED for the FET bits specifically (see FAULT-DECODE.md
    # "uncertainty"). Gating charge/discharge on an unverified bit risks a
    # false do-not-charge (or worse, a false ALL-CLEAR on a real fault), so
    # this heuristic stays authoritative pending FLA12100 validation of the
    # FET bits over time.
    #
    # Guard derived instead from data this module ALREADY decodes reliably:
    # per-cell voltages (min_cell_v/max_cell_v, computed above), temps
    # (all_temps, computed above), and
    # the bank's own DVCC voltage ceiling (max_charge_voltage, computed
    # above). Conservative LiFePO4 thresholds. This can only ever turn
    # allow_to_charge/discharge OFF from a real present pack's actual
    # decoded reading -- it never trips from sentinel, unread, or default
    # state, so it cannot publish a false do-not-charge when nothing is
    # actually wrong (today: no active fault, so both stay 1, matching
    # pre-fix behavior).
    _CELL_OVERVOLTAGE_BLOCK_CHARGE_V = 3.65     # LiFePO4 hard ceiling
    _CELL_UNDERVOLTAGE_BLOCK_DISCHARGE_V = 2.50  # LiFePO4 hard floor
    _CHARGE_TEMP_MIN_C = 0.0    # charging LiFePO4 below freezing is unsafe
    _CHARGE_TEMP_MAX_C = 50.0

    fault_block_charge = (
        (max_cell_v is not None and max_cell_v >= _CELL_OVERVOLTAGE_BLOCK_CHARGE_V)
        or (all_temps and (max(all_temps) > _CHARGE_TEMP_MAX_C or min(all_temps) < _CHARGE_TEMP_MIN_C))
        or (bank_voltage is not None and max_charge_voltage is not None and bank_voltage >= max_charge_voltage)
    )
    fault_block_discharge = (
        min_cell_v is not None and min_cell_v <= _CELL_UNDERVOLTAGE_BLOCK_DISCHARGE_V
    )

    allow_to_charge = 1 if (connected and not fault_block_charge) else 0
    allow_to_discharge = 1 if (connected and not fault_block_discharge) else 0

    # /System/NrOfModulesBlockingCharge, /NrOfModulesBlockingDischarge:
    # per FEATURE-MAP.md "Logic already exists, just not exposed as its
    # own path" -- fault_block_charge/discharge above are BANK-WIDE
    # booleans (the heuristic compares cross-pack min/max cell voltage and
    # all_temps, not any one pack's own reading), so there is no honest
    # way to attribute the block to a specific pack without inventing
    # per-pack analysis nobody asked for. Cast the existing boolean to a
    # 0/1 count exactly as the gap analysis specifies, rather than
    # guessing which of the (possibly two) packs is at fault.
    nr_of_modules_blocking_charge = 1 if fault_block_charge else 0
    nr_of_modules_blocking_discharge = 1 if fault_block_discharge else 0

    # --- Group B: register 4866/4868 fault-bit decode (FAULT-DECODE.md).
    #
    # /Alarms/*: annunciate-only (never gate charge/discharge). Bank-level
    # alarm is active (2) if ANY present pack reports that fault bit set,
    # else inactive (0). Victron 0/1/2 alarm convention -- these are named
    # hard faults, so "active" is always 2, never the softer "1" (warning).
    _ALARM_NAME_TO_FAULT_KEY = {
        "HighCellVoltage": "high_cell_voltage",
        "LowCellVoltage": "low_cell_voltage",
        "HighChargeCurrent": "high_charge_current",
        "HighDischargeCurrent": "high_discharge_current",
        "HighInternalTemperature": "high_internal_temperature",
        "HighChargeTemperature": "high_charge_temperature",
        "LowChargeTemperature": "low_charge_temperature",
    }
    alarms: dict[str, int] = {}
    for alarm_name, fault_key in _ALARM_NAME_TO_FAULT_KEY.items():
        active = any(
            bool((data.get("fault_flags") or {}).get(fault_key)) for _, data in present
        )
        alarms[f"/Alarms/{alarm_name}"] = 2 if active else 0

    # /Voltages/Diff: bank-wide cell spread (max - min), reusing the
    # cross-pack min_cell_v/max_cell_v computed above -- no new data.
    voltages_diff = (max_cell_v - min_cell_v) if (min_cell_v is not None and max_cell_v is not None) else None

    # Derived threshold alarms (see constants + _threshold_alarm() above).
    # Reuses voltages_diff/bank_voltage/bank_soc computed above -- no new
    # reads. Merged into `alarms` alongside the Group B fault alarms so
    # both go through the same result.update(alarms) below.
    alarms["/Alarms/CellImbalance"] = _threshold_alarm(
        max_intra_pack_spread, _CELL_IMBALANCE_WARN_V, _CELL_IMBALANCE_ALARM_V, high=True
    )
    # LowVoltage/HighVoltage compare against bank_voltage, which is a SUM
    # across only the *present* packs (see "/Dc/0/Voltage: SUM across
    # present packs" above) -- it is NOT gated on both series packs being
    # present. With one pack absent (reconnect, adapter glitch, restart-
    # timing, 2-master collision), bank_voltage silently becomes a single
    # pack's ~13.4V, which reads as a full-string low-voltage condition
    # against these full-string (24V/8S) thresholds -- a false alarm, not
    # a real low-voltage event. A partial-string voltage is not comparable
    # to a full-string threshold, so only evaluate these two alarms when
    # nr_of_batteries == 2 (both expected series packs present and ok);
    # otherwise pass None through, which _threshold_alarm() already
    # defines as 0/unknown, never a false trip.
    bank_voltage_full_string = bank_voltage if nr_of_batteries == 2 else None
    alarms["/Alarms/LowVoltage"] = _threshold_alarm(
        bank_voltage_full_string, _BANK_LOW_VOLTAGE_WARN_V, _BANK_LOW_VOLTAGE_ALARM_V, high=False
    )
    alarms["/Alarms/HighVoltage"] = _threshold_alarm(
        bank_voltage_full_string, _BANK_HIGH_VOLTAGE_WARN_V, _BANK_HIGH_VOLTAGE_ALARM_V, high=True
    )
    alarms["/Alarms/LowSoc"] = _threshold_alarm(
        bank_soc, _LOW_SOC_WARN_PCT, _LOW_SOC_ALARM_PCT, high=False
    )

    result: dict = {
        "/Connected": 1 if connected else 0,
        "/System/NrOfBatteries": nr_of_batteries,
        "/System/NrOfCellsPerBattery": nr_of_cells,
        "/System/MinCellVoltage": min_cell_v,
        "/System/MaxCellVoltage": max_cell_v,
        "/System/MinVoltageCellId": min_cell_id,
        "/System/MaxVoltageCellId": max_cell_id,
        "/Dc/0/Voltage": bank_voltage,
        "/Dc/0/Current": bank_current,
        "/Dc/0/Power": bank_power,
        "/Dc/0/Temperature": bank_temperature,
        "/Soc": bank_soc,
        "/Capacity": bank_capacity_remaining,
        "/InstalledCapacity": BANK_CAPACITY_AH,
        "/Info/MaxChargeVoltage": max_charge_voltage,
        "/Info/BatteryLowVoltage": battery_low_voltage,
        "/Info/MaxChargeCurrent": max_charge_current,
        "/Info/MaxDischargeCurrent": max_discharge_current,
        "/Io/AllowToCharge": allow_to_charge,
        "/Io/AllowToDischarge": allow_to_discharge,
        "/Serial": present[0][1].get("serial") if present else None,
        "/Voltages/Diff": voltages_diff,
        "/System/MinCellTemperature": min_temp,
        "/System/MaxCellTemperature": max_temp,
        "/System/MinTemperatureCellId": min_temp_id,
        "/System/MaxTemperatureCellId": max_temp_id,
        "/System/NrOfModulesOnline": nr_of_modules_online,
        "/System/NrOfModulesOffline": nr_of_modules_offline,
        "/System/NrOfModulesBlockingCharge": nr_of_modules_blocking_charge,
        "/System/NrOfModulesBlockingDischarge": nr_of_modules_blocking_discharge,
        "/ConsumedAmphours": consumed_amphours_approx,
        "packs": {},
    }
    result.update(alarms)

    for idx in sorted(packs.keys()):
        ps = packs[idx]
        ok = _pack_ok(ps)
        data = ps.data if ok else None
        if ok:
            temps = _temp_readings(data)
            fet_c = data.get("fet_charge_observed")
            fet_d = data.get("fet_discharge_observed")
            result["packs"][idx] = {
                "Serial": data.get("serial"),
                "Voltage": data.get("voltage"),
                "Current": data.get("current"),
                "Temperature": max(temps) if temps else None,
                # Each pack's own SoC (register 4875) -- was flagged missing
                # in FEATURE-MAP.md. Independent from bank /Soc (MIN across
                # packs, computed above).
                "Soc": data.get("soc"),
                "Capacity": PACK_CAPACITY_AH,
                "FwVersion": data.get("firmware"),
                "Status": 1,
                "Cells": list(data.get("cells") or []),
                # OBSERVATIONAL ONLY (Group B / FAULT-DECODE.md): the BMS's
                # own reported MOSFET-enable state, decoded from register
                # 4866. Deliberately NOT the authoritative charge/discharge
                # gate -- that stays the conservative voltage/temp heuristic
                # (/Io/AllowToCharge, /Io/AllowToDischarge above). Exposed
                # here so real behaviour can be cross-checked against the
                # heuristic over days before this decode is ever trusted
                # (felicity.py was verified against LPBF48250, not this
                # box's FLA12100 -- model mismatch is UNVERIFIED, see
                # FAULT-DECODE.md "uncertainty"). None if undecodable this
                # cycle; 1/0 otherwise.
                "ChargeFetObserved": None if fet_c is None else int(bool(fet_c)),
                "DischargeFetObserved": None if fet_d is None else int(bool(fet_d)),
                "simulated": bool(data.get("simulated", False)) or bool(getattr(ps, "simulated", False)),
            }
        else:
            result["packs"][idx] = {
                "Serial": None,
                "Voltage": None,
                "Current": None,
                "Temperature": None,
                "Soc": None,
                # Nameplate constant, published regardless of live presence
                # -- per PARAM-SPEC.md "Each /Battery/<n>/Capacity
                # independently publishes 100.0 (nameplate, per pack)".
                "Capacity": PACK_CAPACITY_AH,
                "FwVersion": None,
                "Status": 0,
                "Cells": [],
                "ChargeFetObserved": None,
                "DischargeFetObserved": None,
                "simulated": bool(getattr(ps, "simulated", False)),
            }

    return result


# ---------------------------------------------------------------------
# __main__: self-test against felicity_reader.read_all() directly.
#
# build_bank_params() takes discovery.PackStatus-shaped input, not raw
# felicity_reader.read_pack() dicts, so this block wraps each read_all()
# result in a minimal local stand-in carrying exactly the attributes
# INTERFACE.md documents for PackStatus (index, port, serial, present,
# simulated, real_claimed, fail_count, data). This stand-in is scoped to
# this self-test only -- it is not part of this module's exported
# contract and does not substitute for discovery.py.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys
    from dataclasses import dataclass

    import felicity_reader

    @dataclass
    class _PackStatusStub:
        index: int
        port: str | None
        serial: str | None
        present: bool
        simulated: bool
        real_claimed: bool
        fail_count: int
        data: dict | None

    def _wrap(idx: int, port: str, data: dict) -> _PackStatusStub:
        ok = bool(data.get("ok"))
        sim = bool(data.get("simulated", False))
        return _PackStatusStub(
            index=idx,
            port=port,
            serial=data.get("serial"),
            present=ok,
            simulated=sim,
            real_claimed=ok and not sim,
            fail_count=0 if ok else 1,
            data=data if ok else None,
        )

    argv_ports = sys.argv[1:] or ["/dev/ttyUSB0", "SIM"]
    read_results = felicity_reader.read_all(argv_ports)
    stub_packs = {
        i + 1: _wrap(i + 1, port, data)
        for i, (port, data) in enumerate(zip(argv_ports, read_results))
    }

    normalized = build_bank_params(stub_packs)
    print(json.dumps(normalized, indent=2, default=str))
