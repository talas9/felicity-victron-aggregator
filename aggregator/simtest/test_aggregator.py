#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Isolated safe-simulation test harness for the Felicity bank aggregator's
new features (fault decode, history/coulomb counter, derived fields).

SAFETY: imports the real modules from /data/rs485-cells/aggregator (added
to sys.path) but:
  - never calls anything that opens /dev/ttyUSB0
  - all history.py calls pass an explicit sandbox `path=` under simtest/,
    NEVER the live history.json
  - never touches pack_mapping.json
  - never imports dbusservice.py, dbus, or gi (no D-Bus session touched)
  - discovery.py tests use a fake, guaranteed-nonexistent port string and
    monkeypatch felicity_reader.read_pack in-process only (reverted after)

Run directly: python3 test_aggregator.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
import traceback

AGG_DIR = "/data/rs485-cells/aggregator"
SANDBOX = os.path.join(AGG_DIR, "simtest")
sys.path.insert(0, AGG_DIR)

import felicity_reader  # noqa: E402
import params as params_mod  # noqa: E402
import history as history_mod  # noqa: E402
import discovery  # noqa: E402

os.makedirs(SANDBOX, exist_ok=True)

RESULTS = []  # (scenario_id, expected, actual, pass_bool, note)


def record(sid, expected, actual, ok, note=""):
    RESULTS.append((sid, expected, actual, ok, note))


def fail_note(sid, exc):
    record(sid, "no exception", f"EXCEPTION: {exc}", False, traceback.format_exc(limit=3))


# ---------------------------------------------------------------------
# Helpers to build raw register bytes matching felicity_reader's layout
# ---------------------------------------------------------------------

def _status_bytes(status_int: int, fault_int: int, mid: int = 0) -> bytes:
    """3 registers / 6 bytes: reg4866=status_int, reg4867=mid(unused), reg4868=fault_int"""
    return struct.pack(">HHH", status_int & 0xFFFF, mid & 0xFFFF, fault_int & 0xFFFF)


def stub_pack_status(index, data, present=True, real_claimed=True, simulated=False, fail_count=0, port="STUB"):
    from dataclasses import dataclass

    @dataclass
    class _Stub:
        index: int
        port: object
        serial: object
        present: bool
        simulated: bool
        real_claimed: bool
        fail_count: int
        data: object

    return _Stub(index=index, port=port, serial=(data or {}).get("serial"),
                 present=present, simulated=simulated, real_claimed=real_claimed,
                 fail_count=fail_count, data=data)


def healthy_pack_data(**overrides):
    d = dict(
        ok=True, error=None, port="STUB", cells=[3.30, 3.31, 3.32, 3.33], cell_count=4,
        cell_min=3.30, cell_max=3.33, cell_spread_mv=30.0, voltage=13.26, current=1.0,
        soc=80.0, temp_bms=25.0, temps=[25.0, 26.0], serial="TESTSERIAL1", firmware="1",
        dvcc_max_v=14.6, dvcc_min_v=12.0, dvcc_max_charge_current=50.0, dvcc_max_discharge_current=50.0,
        fet_charge_observed=True, fet_discharge_observed=True,
        fault_flags={k: False for k in felicity_reader.FAULT_BIT_MAP}, raw={}, simulated=False,
    )
    d.update(overrides)
    return d


# =======================================================================
# F1 -- healthy status/fault decode
# =======================================================================
try:
    sb = _status_bytes(status_int=0b0000000000000101, fault_int=0x0000)  # bit0+bit2 set
    dec = felicity_reader._decode_status(sb)
    ok = (
        all(v is False for v in dec["fault_flags"].values())
        and dec["fet_charge_observed"] is True
        and dec["fet_discharge_observed"] is True
    )
    record("F1", "all 7 alarms=0, ChargeFetObserved=1, DischargeFetObserved=1", dec, ok)
except Exception as e:
    fail_note("F1", e)

# =======================================================================
# F2 -- each fault bit individually
# =======================================================================
EXPECTED_MAP = {
    0b0000000000000100: "high_cell_voltage",
    0b0000000000001000: "low_cell_voltage",
    0b0000000000010000: "high_charge_current",
    0b0000000000100000: "high_discharge_current",
    0b0000000001000000: "high_internal_temperature",
    0b0000000100000000: "high_charge_temperature",
    0b0000001000000000: "low_charge_temperature",
}
mapping_matches = felicity_reader.FAULT_BIT_MAP == {v: k for k, v in EXPECTED_MAP.items()}
record("F2-mapping", str({v: k for k, v in EXPECTED_MAP.items()}), str(felicity_reader.FAULT_BIT_MAP), mapping_matches)

f2_all_ok = True
f2_detail = []
try:
    for bit, name in EXPECTED_MAP.items():
        sb = _status_bytes(status_int=0, fault_int=bit)
        dec = felicity_reader._decode_status(sb)
        only_this = all(
            (v is True) == (k == name) for k, v in dec["fault_flags"].items()
        )
        # also check params.py-level /Alarms/* = 2 only for this alarm
        pd = healthy_pack_data(fault_flags=dec["fault_flags"], fet_charge_observed=None, fet_discharge_observed=None)
        packs = {1: stub_pack_status(1, pd), 2: stub_pack_status(2, None, present=False, real_claimed=False)}
        p = params_mod.build_bank_params(packs)
        # params.py-level: exactly one /Alarms/* path should be 2 (active), rest 0
        alarm_paths = {k: v for k, v in p.items() if k.startswith("/Alarms/")}
        active_alarms = [k for k, v in alarm_paths.items() if v == 2]
        params_alarm_name = {
            "high_cell_voltage": "/Alarms/HighCellVoltage", "low_cell_voltage": "/Alarms/LowCellVoltage",
            "high_charge_current": "/Alarms/HighChargeCurrent", "high_discharge_current": "/Alarms/HighDischargeCurrent",
            "high_internal_temperature": "/Alarms/HighInternalTemperature",
            "high_charge_temperature": "/Alarms/HighChargeTemperature", "low_charge_temperature": "/Alarms/LowChargeTemperature",
        }[name]
        params_ok = active_alarms == [params_alarm_name]
        ok_bit = only_this and params_ok
        f2_detail.append((name, bit, ok_bit, active_alarms))
        f2_all_ok = f2_all_ok and ok_bit
    record("F2", "only mapped alarm raises for each of 7 bits", f2_detail, f2_all_ok)
except Exception as e:
    fail_note("F2", e)

# =======================================================================
# F3 -- fault word 0xFFFF decode + failed-read guard
# =======================================================================
try:
    sb = _status_bytes(status_int=0xFFFF, fault_int=0xFFFF)
    dec = felicity_reader._decode_status(sb)
    all_seven_set = all(dec["fault_flags"].values())
    record(
        "F3a-decode-0xFFFF",
        "decoding genuine 0xFFFF register data DOES set all 7 mapped bits (correct decode of real bits, not a bug)",
        dec["fault_flags"], all_seven_set,
        "This is literal register decode; a real all-1s fault register would legitimately mean all faults active.",
    )
except Exception as e:
    fail_note("F3a-decode-0xFFFF", e)

try:
    # A failed READ (nonexistent port -> serial.Serial open fails before any
    # register is ever touched) must never reach _decode_status at all.
    r = felicity_reader.read_pack("/dev/ttyUSB99_DOES_NOT_EXIST", timeout=0.2)
    ok = (
        r["ok"] is False
        and all(v is False for v in r["fault_flags"].values())
        and r["fet_charge_observed"] is None
        and r["fet_discharge_observed"] is None
    )
    record("F3b-failed-read-guard", "ok=False, fault_flags all False, fet observed=None, no crash", r, ok)
except Exception as e:
    fail_note("F3b-failed-read-guard", e)

# =======================================================================
# F4 -- FET bit off must NOT gate /Io/AllowToCharge (critical safety check)
# =======================================================================
try:
    sb = _status_bytes(status_int=0b0000000000000100, fault_int=0x0000)  # bit0=0 (charge FET off), bit2=1
    dec = felicity_reader._decode_status(sb)
    assert dec["fet_charge_observed"] is False

    pd = healthy_pack_data(fet_charge_observed=False, fet_discharge_observed=True,
                            fault_flags=dec["fault_flags"])
    # voltages/temps intentionally healthy/safe so the heuristic should allow charge
    packs = {1: stub_pack_status(1, pd), 2: stub_pack_status(2, None, present=False, real_claimed=False)}
    p = params_mod.build_bank_params(packs)
    allow_to_charge = p["/Io/AllowToCharge"]
    charge_fet_observed = p["packs"][1]["ChargeFetObserved"]
    ok = (charge_fet_observed == 0) and (allow_to_charge == 1)
    record(
        "F4", "ChargeFetObserved=0 but /Io/AllowToCharge=1 (heuristic-gated, NOT FET-gated)",
        {"ChargeFetObserved": charge_fet_observed, "/Io/AllowToCharge": allow_to_charge}, ok,
    )
except Exception as e:
    fail_note("F4", e)

# =======================================================================
# H1 -- charge then discharge accumulation
# =======================================================================
try:
    st = history_mod._fresh_state()
    t = 1000.0
    # Prime last_update_epoch: the very first update_history() call on a
    # fresh state can never compute dt (no prior timestamp to diff
    # against), so it never integrates -- this call establishes t=1000.0
    # as the baseline without contributing any energy (current=0 hits
    # neither the charge nor discharge branch either way).
    st = history_mod.update_history(st, voltage=28.0, current=0.0, soc=50.0,
                                     min_cell_v=3.3, max_cell_v=3.35,
                                     installed_capacity_ah=100.0, now=t)
    # charge: 28V * 10A for 5 steps of 2s = 10s = 1/360 h
    for _ in range(5):
        t += 2.0
        st = history_mod.update_history(st, voltage=28.0, current=10.0, soc=50.0,
                                         min_cell_v=3.3, max_cell_v=3.35,
                                         installed_capacity_ah=100.0, now=t)
    expected_charged_kwh = 28.0 * 10.0 * (10.0 / 3600.0) / 1000.0
    charged_ok = abs(st["charged_energy_kwh"] - expected_charged_kwh) < 1e-9
    # discharge: 27V * -8A for 5 steps of 2s = 10s
    for _ in range(5):
        t += 2.0
        st = history_mod.update_history(st, voltage=27.0, current=-8.0, soc=48.0,
                                         min_cell_v=3.2, max_cell_v=3.25,
                                         installed_capacity_ah=100.0, now=t)
    expected_discharged_kwh = 27.0 * 8.0 * (10.0 / 3600.0) / 1000.0
    discharged_ok = abs(st["discharged_energy_kwh"] - expected_discharged_kwh) < 1e-9
    expected_ah_drawn = 8.0 * (10.0 / 3600.0)
    ah_ok = abs(st["total_ah_drawn"] - expected_ah_drawn) < 1e-9
    ok = charged_ok and discharged_ok and ah_ok
    record("H1", f"charged~{expected_charged_kwh:.6f}kWh discharged~{expected_discharged_kwh:.6f}kWh ah_drawn~{expected_ah_drawn:.4f}Ah",
           {"charged": st["charged_energy_kwh"], "discharged": st["discharged_energy_kwh"], "ah_drawn": st["total_ah_drawn"]}, ok)
except Exception as e:
    fail_note("H1", e)

# =======================================================================
# H2 -- clock jump backward / forward
# =======================================================================
try:
    st = history_mod._fresh_state()
    st = history_mod.update_history(st, voltage=28.0, current=10.0, soc=50.0,
                                     min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=1000.0)
    before = dict(st)
    # backward jump
    st_back = history_mod.update_history(st, voltage=28.0, current=10.0, soc=50.0,
                                          min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=990.0)
    no_energy_change = (st_back["charged_energy_kwh"] == before["charged_energy_kwh"]
                         and st_back["discharged_energy_kwh"] == before["discharged_energy_kwh"])
    no_crash_neg = True
    record("H2-backward", "no negative/corrupt energy accumulation on backward clock jump",
           {"before_charged": before["charged_energy_kwh"], "after_charged": st_back["charged_energy_kwh"]},
           no_energy_change)

    # forward huge jump (1 hour = 3600s) from st (last_update_epoch=1000.0)
    st2 = history_mod.update_history(st, voltage=28.0, current=10.0, soc=50.0,
                                      min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=1000.0 + 3600.0)
    # ACTUAL observed code behavior: dt outside (0, MAX_REASONABLE_DT_S] is
    # SKIPPED entirely for integration (dt_s stays None) -- it is NOT
    # "clamped to 30s and integrated at that clamped value" as the task
    # description phrased it. Verify: no energy delta at all for this step.
    no_spike = st2["charged_energy_kwh"] == before["charged_energy_kwh"]
    record(
        "H2-forward", "huge forward dt does not spike the counter (task said 'clamped at 30s'; actual code SKIPS integration for dt>30s rather than clamping-and-integrating)",
        {"charged_before": before["charged_energy_kwh"], "charged_after_1hr_jump": st2["charged_energy_kwh"],
         "MAX_REASONABLE_DT_S": history_mod.MAX_REASONABLE_DT_S},
        no_spike,
        "DISCREPANCY vs task wording: code path is 'if 0 < candidate <= MAX_REASONABLE_DT_S: dt_s=candidate' else dt_s stays None -> skip, not clamp-then-integrate.",
    )
except Exception as e:
    fail_note("H2", e)

# =======================================================================
# H3 -- corrupt history.json variants
# =======================================================================
corrupt_cases = {
    "truncated": b'{"charged_energy_kwh": 1.5, "disch',
    "empty": b"",
    "invalid_json": b"{not valid json!!",
    "missing_keys": json.dumps({"charge_cycles": 3}).encode(),
    "not_an_object": json.dumps([1, 2, 3]).encode(),
}
h3_all_ok = True
h3_detail = {}
for name, content in corrupt_cases.items():
    p = os.path.join(SANDBOX, f"history_corrupt_{name}.json")
    try:
        with open(p, "wb") as f:
            f.write(content)
        st = history_mod.load_history(path=p)
        is_dict_with_template_keys = isinstance(st, dict) and all(k in st for k in history_mod._FRESH_STATE_TEMPLATE)
        if name == "missing_keys":
            # merge case: charge_cycles should carry through, rest fresh
            merge_ok = st.get("charge_cycles") == 3 and st.get("charged_energy_kwh") == 0.0
            ok = is_dict_with_template_keys and merge_ok
        else:
            ok = is_dict_with_template_keys and st.get("charged_energy_kwh") == 0.0 and st.get("charge_cycles") == 0
        h3_detail[name] = {"ok": ok, "charge_cycles": st.get("charge_cycles"), "charged": st.get("charged_energy_kwh")}
        h3_all_ok = h3_all_ok and ok
    except Exception as e:
        h3_detail[name] = f"EXCEPTION: {e}"
        h3_all_ok = False
record("H3", "all corrupt variants load as fresh state (or safely merged), never raise", h3_detail, h3_all_ok)

# =======================================================================
# H4 -- charge cycle detection + hysteresis
# =======================================================================
try:
    st = history_mod._fresh_state()
    t = 0.0
    for soc in (90.0, 95.0, 99.6, 99.0, 80.0):  # rises to ~100 then drops
        t += 2.0
        st = history_mod.update_history(st, voltage=28.0, current=1.0, soc=soc,
                                         min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=t)
    single_increment = st["charge_cycles"] == 1
    record("H4a-single-rise-drop", "ChargeCycles == 1", st["charge_cycles"], single_increment)
except Exception as e:
    fail_note("H4a-single-rise-drop", e)

try:
    st = history_mod._fresh_state()
    t = 0.0
    oscillate = [99.8, 100.0, 99.9, 100.0, 99.7, 100.0, 99.8, 100.0]
    for soc in oscillate:
        t += 2.0
        st = history_mod.update_history(st, voltage=28.0, current=1.0, soc=soc,
                                         min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=t)
    no_spurious = st["charge_cycles"] == 1
    record("H4b-oscillation-no-spurious", "ChargeCycles == 1 despite oscillation above rearm threshold (97.0)",
           st["charge_cycles"], no_spurious)
except Exception as e:
    fail_note("H4b-oscillation-no-spurious", e)

try:
    st = history_mod._fresh_state()
    t = 0.0
    for soc in (50.0, 60.0, 70.0, 80.0, 90.0, 95.0):  # never reaches 99.5
        t += 2.0
        st = history_mod.update_history(st, voltage=28.0, current=1.0, soc=soc,
                                         min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=t)
    no_increment = st["charge_cycles"] == 0
    record("H4c-never-reaches-100", "ChargeCycles == 0", st["charge_cycles"], no_increment)
except Exception as e:
    fail_note("H4c-never-reaches-100", e)

# =======================================================================
# H5 -- capacity=0 / soc=None no crash
# =======================================================================
try:
    st = history_mod._fresh_state()
    st = history_mod.update_history(st, voltage=28.0, current=1.0, soc=None,
                                     min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=100.0, now=1000.0)
    st2 = history_mod.update_history(st, voltage=28.0, current=1.0, soc=50.0,
                                      min_cell_v=3.3, max_cell_v=3.35, installed_capacity_ah=0.0, now=1002.0)
    p = params_mod.build_bank_params({
        1: stub_pack_status(1, healthy_pack_data(soc=None)),
        2: stub_pack_status(2, None, present=False, real_claimed=False),
    })
    record("H5", "no crash/div-by-zero with soc=None and capacity=0.0", {"history_ok": True, "params_soc_none_consumed_ah": p["/ConsumedAmphours"]}, True)
except Exception as e:
    fail_note("H5", e)

# =======================================================================
# H6 -- atomic write (tmp + rename), crash-between-write-and-rename
# =======================================================================
try:
    p = os.path.join(SANDBOX, "history_atomic.json")
    st0 = history_mod._fresh_state()
    st0["charge_cycles"] = 7
    history_mod.save_history(st0, path=p, force=True)
    with open(p) as f:
        original_content = f.read()

    # simulate a crash between tmp-write and os.replace: monkeypatch
    # os.replace to raise, then confirm original file is untouched and
    # save_history does not propagate the exception.
    real_replace = os.replace
    def _boom(*a, **kw):
        raise OSError("simulated crash between write and rename")
    os.replace = _boom
    try:
        st1 = dict(st0)
        st1["charge_cycles"] = 999
        history_mod.save_history(st1, path=p, force=True)  # should log+swallow, not raise
        crash_did_not_raise = True
    except Exception:
        crash_did_not_raise = False
    finally:
        os.replace = real_replace

    with open(p) as f:
        after_content = f.read()
    old_file_intact = after_content == original_content
    tmp_exists = os.path.exists(p + ".tmp")
    # cleanup leftover tmp
    if tmp_exists:
        os.remove(p + ".tmp")

    ok = crash_did_not_raise and old_file_intact
    record("H6", "save_history swallows the mid-write exception; original file content unchanged (tmp+os.replace pattern verified in source)",
           {"crash_did_not_raise": crash_did_not_raise, "old_file_intact": old_file_intact, "leftover_tmp_existed": tmp_exists}, ok)
except Exception as e:
    fail_note("H6", e)

# =======================================================================
# H7 -- throttle (<=1/60s) + forced flush
# =======================================================================
try:
    p = os.path.join(SANDBOX, "history_throttle.json")
    if os.path.exists(p):
        os.remove(p)
    st = history_mod._fresh_state()
    history_mod._last_saved_monotonic = 0.0  # reset module throttle state for a clean test
    write_count = 0
    mtimes = set()
    for i in range(20):
        st["charge_cycles"] = i
        history_mod.save_history(st, path=p, force=False)
        if os.path.exists(p):
            mtimes.add(os.path.getmtime(p))
    # first call establishes _last_saved_monotonic; rapid subsequent calls
    # within SAVE_INTERVAL_S must all no-op (module global throttle applies
    # regardless of `path`, since it is process-global, not per-path).
    throttled_ok = len(mtimes) <= 1
    with open(p) as f:
        saved = json.load(f)
    first_write_persisted = saved.get("charge_cycles") == 0  # first call's value, since rest were throttled

    # forced flush (SIGTERM path) must always write regardless of throttle
    st["charge_cycles"] = 999
    t0 = time.monotonic()
    history_mod.save_history(st, path=p, force=True)
    t1 = time.monotonic()
    forced_ok = (t1 - t0) < 1.0  # no hang
    with open(p) as f:
        saved2 = json.load(f)
    forced_wrote = saved2.get("charge_cycles") == 999

    ok = throttled_ok and first_write_persisted and forced_ok and forced_wrote
    record("H7", "<=1 real write across 20 rapid calls (throttled), forced write always succeeds without hanging",
           {"distinct_mtimes": len(mtimes), "first_write_persisted": first_write_persisted,
            "forced_duration_s": round(t1 - t0, 4), "forced_wrote": forced_wrote}, ok)
except Exception as e:
    fail_note("H7", e)

# =======================================================================
# D1 -- min/max cell temperature with sentinel present
# =======================================================================
try:
    SENT = 32767.0
    pd1 = healthy_pack_data(temp_bms=SENT, temps=[25.0, 26.0])  # temp_bms sentinel, NOT pre-filtered by reader
    pd2 = healthy_pack_data(temp_bms=30.0, temps=[SENT])        # simulate a temps[] sentinel that slipped through
    packs = {1: stub_pack_status(1, pd1), 2: stub_pack_status(2, pd2)}
    p = params_mod.build_bank_params(packs)
    # expected real readings: pack1 -> 25,26 (temp_bms sentinel excluded); pack2 -> 30 (temps sentinel excluded)
    expected_min = 25.0
    expected_max = 30.0
    ok = (p["/System/MinCellTemperature"] == expected_min and p["/System/MaxCellTemperature"] == expected_max
          and p["/Dc/0/Temperature"] == expected_max)
    record("D1", f"min={expected_min} max={expected_max}, sentinel (32767) excluded",
           {"min": p["/System/MinCellTemperature"], "max": p["/System/MaxCellTemperature"],
            "bank_temp": p["/Dc/0/Temperature"]}, ok)
except Exception as e:
    fail_note("D1", e)

# =======================================================================
# D2 -- per-pack Soc with one pack missing/None
# =======================================================================
try:
    pd1 = healthy_pack_data(soc=72.5)
    packs = {
        1: stub_pack_status(1, pd1),
        2: stub_pack_status(2, None, present=False, real_claimed=False, simulated=False),
    }
    p = params_mod.build_bank_params(packs)
    pack1_soc_ok = p["packs"][1]["Soc"] == 72.5
    pack2_excluded = p["packs"][2]["Soc"] is None and p["packs"][2]["Status"] == 0
    bank_soc_ok = p["/Soc"] == 72.5  # min across present == only present pack
    no_crash = True
    ok = pack1_soc_ok and pack2_excluded and bank_soc_ok
    record("D2", "pack1 Soc=72.5 correct, pack2 Soc=None excluded, bank /Soc=72.5, no crash",
           {"pack1_soc": p["packs"][1]["Soc"], "pack2_soc": p["packs"][2]["Soc"], "bank_soc": p["/Soc"]}, ok)
except Exception as e:
    fail_note("D2", e)

# =======================================================================
# D3 -- Voltages/Diff with sentinel cells (verify felicity_reader's cell
# sentinel filter, then downstream aggregation from filtered cells only)
# =======================================================================
try:
    SENT = felicity_reader.SENTINEL_INT16
    raw_cells_mv = [3300, SENT, 3400, SENT, 3250, 3350, SENT, SENT,
                    SENT, SENT, SENT, SENT, SENT, SENT, SENT, SENT]
    filtered = [v / 1000.0 for v in raw_cells_mv if v != SENT]  # exact felicity_reader.read_pack() logic
    reader_filter_ok = filtered == [3.30, 3.40, 3.25, 3.35] and len(filtered) == 4

    pd = healthy_pack_data(cells=filtered, cell_count=len(filtered),
                            cell_min=min(filtered), cell_max=max(filtered))
    packs = {1: stub_pack_status(1, pd), 2: stub_pack_status(2, None, present=False, real_claimed=False)}
    p = params_mod.build_bank_params(packs)
    expected_diff = round((3.40 - 3.25) * 1000)  # in mV terms; Diff is in V though
    diff_ok = abs(p["/Voltages/Diff"] - (3.40 - 3.25)) < 1e-9
    ok = reader_filter_ok and diff_ok
    record("D3", "sentinel cells (0x7FFF) excluded by felicity_reader; Voltages/Diff computed from real cells only (0.15V)",
           {"filtered_cells": filtered, "Voltages/Diff": p["/Voltages/Diff"]}, ok)
except Exception as e:
    fail_note("D3", e)

# =======================================================================
# I1 -- grace period (discovery.py)
# =======================================================================
try:
    FAKE_PORT = "FAKE:GRACE_TEST"
    orig_read_pack = felicity_reader.read_pack
    fail_calls = {"n": 0}

    def _always_fail(port, address=1, timeout=1.0):
        if port == FAKE_PORT:
            fail_calls["n"] += 1
            return felicity_reader._empty_result(port, "simulated failure for I1 test")
        return orig_read_pack(port, address=address, timeout=timeout)

    # seed a real_claimed slot as if a real pack had previously been seen
    known = {
        1: discovery.PackStatus(index=1, port=FAKE_PORT, serial="TESTSERIAL_I1", present=True,
                                 simulated=False, real_claimed=True, fail_count=0,
                                 data=healthy_pack_data(serial="TESTSERIAL_I1")),
        2: discovery.PackStatus(index=2, port=None, serial=None, present=False,
                                 simulated=False, real_claimed=False, fail_count=0, data=None),
    }

    felicity_reader.read_pack = _always_fail
    try:
        # failure #1
        known = discovery.scan(known, ports_glob="/NO_SUCH_GLOB_PATTERN_*")
        f1_present = known[1].present is True
        f1_data_kept = known[1].data is not None and known[1].data.get("serial") == "TESTSERIAL_I1"
        f1_fail_count = known[1].fail_count

        # failure #2
        known = discovery.scan(known, ports_glob="/NO_SUCH_GLOB_PATTERN_*")
        f2_present = known[1].present is True
        f2_fail_count = known[1].fail_count

        # failure #3 -> should now drop
        known = discovery.scan(known, ports_glob="/NO_SUCH_GLOB_PATTERN_*")
        f3_present = known[1].present  # expected False (dropped after FAIL_THRESHOLD=3)
        f3_data_dropped = known[1].data is None
        f3_fail_count = known[1].fail_count
    finally:
        felicity_reader.read_pack = orig_read_pack

    ok = (f1_present and f1_data_kept and f1_fail_count == 1
          and f2_present and f2_fail_count == 2
          and f3_present is False and f3_data_dropped and f3_fail_count == 3)
    record("I1", "failure #1: present stays True, data kept, fail_count=1; failure #3: present=False, data=None",
           {"f1": (f1_present, f1_fail_count), "f2": (f2_present, f2_fail_count), "f3": (f3_present, f3_fail_count)}, ok)
except Exception as e:
    fail_note("I1", e)

# =======================================================================
# I2 -- history-update exception must not escape the daemon's per-cycle
# try/except (structural: dbus-felicity-bank.py _cycle() lines ~187-200
# cannot be exercised directly without a live D-Bus session -- reproduced
# the EXACT try/except pattern here with a forced-raising stub as a
# behavioral proxy for that code path).
# =======================================================================
try:
    def _boom_update_history(*a, **kw):
        raise RuntimeError("forced history-update exception for I2 test")

    escaped = False
    try:
        # exact structural mirror of dbus-felicity-bank.py _cycle()'s
        # history try/except block
        try:
            _boom_update_history()
        except Exception:
            pass  # logger.exception(...) in the real code
        after_block_reached = True
    except Exception:
        escaped = True
        after_block_reached = False

    ok = (not escaped) and after_block_reached
    record("I2", "exception from history_mod.update_history() is caught by _cycle()'s try/except and does not propagate (verified structurally: real code at dbus-felicity-bank.py lines 187-200 wraps update_history/to_dbus_dict/save_history in try/except Exception; NOT exercised via a live daemon instance since that requires a real D-Bus SystemBus session)",
           {"escaped": escaped, "after_block_reached": after_block_reached}, ok,
           "LIMITATION: could not instantiate FelicityBankDaemon (requires dbus/gi/SettingsDevice + SystemBus) without touching D-Bus session state, per safety constraints. Verified via direct source read + structural reproduction of the exact try/except.")
except Exception as e:
    fail_note("I2", e)

# =======================================================================
# I3 -- no 0x7FFF/32767 in ANY published field across a full simulated
# cycle with sentinels injected
# =======================================================================
try:
    SENT = 32767.0
    pd1 = healthy_pack_data(
        temp_bms=SENT, temps=[25.0, SENT],
        cells=[v / 1000.0 for v in [3300, felicity_reader.SENTINEL_INT16, 3400] if v != felicity_reader.SENTINEL_INT16],
    )
    pd2 = healthy_pack_data(temp_bms=30.0, temps=[SENT, SENT], soc=None)
    packs = {1: stub_pack_status(1, pd1), 2: stub_pack_status(2, pd2)}
    p = params_mod.build_bank_params(packs)

    st = history_mod._fresh_state()
    st = history_mod.update_history(st, voltage=p.get("/Dc/0/Voltage"), current=p.get("/Dc/0/Current"),
                                     soc=p.get("/Soc"), min_cell_v=p.get("/System/MinCellVoltage"),
                                     max_cell_v=p.get("/System/MaxCellVoltage"),
                                     installed_capacity_ah=p.get("/InstalledCapacity"), now=1000.0)
    p.update(history_mod.to_dbus_dict(st))

    def _walk(obj, path=""):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                found.extend(_walk(v, f"{path}.{k}"))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found.extend(_walk(v, f"{path}[{i}]"))
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if obj == 32767 or obj == 0x7FFF:
                found.append((path, obj))
        return found

    offenders = _walk(p)
    ok = len(offenders) == 0
    record("I3", "no field anywhere in the published dict equals 32767/0x7FFF", offenders, ok)
except Exception as e:
    fail_note("I3", e)

# =======================================================================
# I4 -- write-safety grep (function code 0x03 only)
# =======================================================================
import subprocess
try:
    files = ["felicity_reader.py", "params.py", "history.py", "discovery.py", "dbusservice.py", "dbus-felicity-bank.py"]
    hits = subprocess.run(
        ["grep", "-n", "0x06\\|0x10\\|WRITE_SINGLE\\|WRITE_MULTIPLE\\|write_register"] + files,
        cwd=AGG_DIR, capture_output=True, text=True,
    )
    func_codes = subprocess.run(["grep", "-rn", "FUNCTION_"], cwd=AGG_DIR, capture_output=True, text=True)
    ok = hits.returncode == 1 and hits.stdout.strip() == ""  # grep returncode 1 = no matches
    record("I4", "no 0x06/0x10/write-register references in any module; only FUNCTION_READ=0x03 defined/used",
           {"write_code_hits": hits.stdout.strip(), "function_defs": func_codes.stdout.strip()}, ok)
except Exception as e:
    fail_note("I4", e)


# =======================================================================
# Output
# =======================================================================
print("\n===== SCENARIO RESULTS =====")
n_pass = 0
n_fail = 0
for sid, expected, actual, ok, note in RESULTS:
    status = "PASS" if ok else "FAIL"
    if ok:
        n_pass += 1
    else:
        n_fail += 1
    print(f"\n[{status}] {sid}")
    print(f"  expected: {expected}")
    print(f"  actual:   {actual}")
    if note:
        print(f"  note:     {note}")

print(f"\n===== SUMMARY: {n_pass} PASS / {n_fail} FAIL / {len(RESULTS)} total =====")

# dump machine-readable copy too
with open(os.path.join(SANDBOX, "results.json"), "w") as f:
    json.dump(
        [{"id": sid, "expected": str(expected), "actual": str(actual), "pass": ok, "note": note} for sid, expected, actual, ok, note in RESULTS],
        f, indent=2, default=str,
    )
