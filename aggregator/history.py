#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
history.py -- persistent coulomb/energy counter + history state for the
Felicity bank aggregator (Group C: NEEDS ACCUMULATION paths, per
FEATURE-MAP.md).

New module, added alongside felicity_reader.py / discovery.py / params.py.
Owns the ONLY stateful, time-integrated aggregate in this codebase --
everything in params.py is a pure per-cycle function with no memory across
polls. This module deliberately keeps that distinction: it is NOT a pure
function (it needs the previous cycle's timestamp to integrate V*I over
time), but it still does no D-Bus and no hardware I/O -- its only I/O is
reading/writing its own JSON state file on disk.

State is persisted to history.json (same directory) via atomic
tmp-file + os.replace, exactly like discovery.py's pack_mapping.json.
Load is corruption-safe: any missing/unparsable/malformed file yields a
fresh state and a logged warning -- never a crash. Save is throttled to at
most once every SAVE_INTERVAL_S (flash-wear limiting) unless force=True
(used on SIGTERM).

State is fixed-size (a small number of scalar fields) -- it never grows
per pack, per cell, or per poll, so no unbounded-growth guard is needed
beyond "don't add a new field that's an ever-growing list", which this
module does not do.

Sign convention (see felicity_reader.py's REG_TOTAL_V_I comment,
"sign-flipped"): current is Victron-standard, positive = charging into the
battery, negative = discharging. This module's charge/discharge
attribution follows that convention.

Values start near-zero and grow over the life of the service -- this is
expected and correct per the task spec, not a bug. ChargedEnergy/
DischargedEnergy/TotalAhDrawn/ChargeCycles/DeepestDischarge are lifetime
cumulative and never reset (except by deleting history.json). Only
ConsumedAmphours (the real coulomb-counted since-last-full-charge counter)
resets, on a detected full-charge event.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("dbus-felicity-bank.history")

_HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(_HERE, "history.json")

# Flash-wear limiting: never write more often than this, regardless of how
# often update_history() is called (every ~2s per the daemon's poll timer).
SAVE_INTERVAL_S = 60.0

# Full-charge detection thresholds (SoC %). ARM_THRESHOLD is the level a
# charge cycle must be detected at; REARM_THRESHOLD is the level SoC must
# drop back below before the NEXT full-charge can be counted again -- the
# gap between the two is hysteresis so sitting at/near 100% does not
# increment ChargeCycles on every poll.
FULL_CHARGE_SOC_THRESHOLD = 99.5
FULL_CHARGE_REARM_SOC = 97.0

# Any single inter-cycle gap (dt) larger than this is treated as a
# clock jump / service restart / long outage, not a real elapsed
# interval -- integration is skipped for that step (timestamp still
# advances) rather than integrating a huge, corrupt dt. 30s is 15x the
# daemon's normal 2s poll interval, generous headroom for a slow cycle
# without accepting a multi-minute/hour gap as if it were continuous.
MAX_REASONABLE_DT_S = 30.0

_FRESH_STATE_TEMPLATE = {
    "charged_energy_kwh": 0.0,
    "discharged_energy_kwh": 0.0,
    "total_ah_drawn": 0.0,
    "consumed_amphours": None,  # seeded from the Group A SoC-derived approximation on first real update
    "min_voltage": None,
    "max_voltage": None,
    "min_cell_voltage": None,
    "max_cell_voltage": None,
    "deepest_discharge_ah": 0.0,
    "last_discharge_ah": 0.0,
    "charge_cycles": 0,
    "full_charge_armed": True,       # True = ready to count the next full-charge event
    "last_full_charge_epoch": None,  # wall-clock time.time() of the last detected full charge
    "created_epoch": None,           # wall-clock time.time() this state was first created (fresh install)
    "last_update_epoch": None,       # wall-clock time.time() of the last processed update_history() call
}

# Module-level (single daemon process per box) save throttle -- kept
# outside the persisted state dict so it never gets written to disk or
# corrupted by a malformed history.json.
_last_saved_monotonic = 0.0


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------

def _fresh_state() -> dict:
    state = dict(_FRESH_STATE_TEMPLATE)
    state["created_epoch"] = time.time()
    return state


def load_history(path: str = HISTORY_FILE) -> dict:
    """Load persisted history state. Missing file, unparsable JSON, or a
    JSON value that isn't the expected object -> fresh state, logged as a
    warning (except plain "file does not exist yet", which is the normal
    first-run case and logged at INFO). Never raises."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.info("no history file at %s yet -- starting fresh", path)
        return _fresh_state()
    except Exception as exc:
        logger.warning("history file %s unreadable/corrupt (%s) -- starting fresh", path, exc)
        return _fresh_state()

    if not isinstance(raw, dict):
        logger.warning("history file %s did not contain a JSON object -- starting fresh", path)
        return _fresh_state()

    # Merge onto the template so a fresh field added by a future version
    # (or a field missing from a corrupt/partial write) is never a KeyError
    # -- unknown/stale keys in the file are silently dropped.
    state = dict(_FRESH_STATE_TEMPLATE)
    for key in state:
        if key in raw:
            state[key] = raw[key]
    if state.get("created_epoch") is None:
        state["created_epoch"] = time.time()
    return state


def save_history(state: dict, path: str = HISTORY_FILE, force: bool = False) -> None:
    """Atomic write (tmp file + os.replace), throttled to at most once
    every SAVE_INTERVAL_S unless force=True (used on SIGTERM shutdown so
    the last few seconds of accumulation are never silently lost). Never
    raises; logs and no-ops on failure."""
    global _last_saved_monotonic
    now_mono = time.monotonic()
    if not force and (now_mono - _last_saved_monotonic) < SAVE_INTERVAL_S:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _last_saved_monotonic = now_mono
    except Exception as exc:
        logger.warning("failed to persist history to %s: %s", path, exc)


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------

def update_history(
    state: dict,
    *,
    voltage: float | None,
    current: float | None,
    soc: float | None,
    min_cell_v: float | None,
    max_cell_v: float | None,
    installed_capacity_ah: float,
    now: float | None = None,
) -> dict:
    """
    Advance history state by one poll cycle. Returns a NEW state dict
    (does not mutate the input in place, though callers may treat it as
    disposable either way). No I/O here -- persistence is a separate step
    via save_history().

    voltage/current/soc/min_cell_v/max_cell_v are this cycle's
    params.build_bank_params() bank-level values (may be None if no pack
    is currently present/ok -- in that case only bookkeeping timestamps
    advance, no integration happens for this cycle, and no min/max/soc
    tracking updates happen either).
    """
    state = dict(state)
    now = time.time() if now is None else now

    last_update = state.get("last_update_epoch")
    dt_s = None
    if last_update is not None:
        candidate = now - last_update
        if 0 < candidate <= MAX_REASONABLE_DT_S:
            dt_s = candidate
        # else: negative (clock stepped backward) or too large (restart /
        # long outage / clock jump) -- skip integration this cycle, but
        # still advance last_update_epoch below so the NEXT cycle's dt is
        # measured from now, not from the stale prior timestamp.
    state["last_update_epoch"] = now

    if voltage is not None and current is not None:
        if state.get("consumed_amphours") is None and soc is not None:
            # Seed the real coulomb counter from the Group A SoC-derived
            # approximation on the very first update this counter has ever
            # seen (fresh install, or an old history.json predating this
            # field) -- per the task spec, "keep the approximation as the
            # seed". After this, consumed_amphours is coulomb-counted, not
            # re-derived from Soc.
            state["consumed_amphours"] = installed_capacity_ah * (1.0 - soc / 100.0)

        if dt_s is not None:
            dt_hours = dt_s / 3600.0
            power_w = voltage * current
            energy_kwh = (power_w * dt_hours) / 1000.0
            if current > 0:
                # Charging convention (positive current): energy flowing
                # into the bank, and the since-last-full-charge counter
                # decreases (bounded at 0 -- integration noise must never
                # push it negative).
                state["charged_energy_kwh"] = state.get("charged_energy_kwh", 0.0) + energy_kwh
                if state.get("consumed_amphours") is not None:
                    state["consumed_amphours"] = max(
                        0.0, state["consumed_amphours"] - current * dt_hours
                    )
            elif current < 0:
                # Discharging convention (negative current).
                state["discharged_energy_kwh"] = state.get("discharged_energy_kwh", 0.0) + abs(energy_kwh)
                ah_drawn = abs(current) * dt_hours
                state["total_ah_drawn"] = state.get("total_ah_drawn", 0.0) + ah_drawn
                if state.get("consumed_amphours") is not None:
                    # Sanity-clamp to nameplate capacity -- integration
                    # drift must never report more Ah consumed than the
                    # bank can physically hold.
                    state["consumed_amphours"] = min(
                        installed_capacity_ah, state["consumed_amphours"] + ah_drawn
                    )

        state["min_voltage"] = voltage if state.get("min_voltage") is None else min(state["min_voltage"], voltage)
        state["max_voltage"] = voltage if state.get("max_voltage") is None else max(state["max_voltage"], voltage)

    if min_cell_v is not None:
        state["min_cell_voltage"] = (
            min_cell_v if state.get("min_cell_voltage") is None else min(state["min_cell_voltage"], min_cell_v)
        )
    if max_cell_v is not None:
        state["max_cell_voltage"] = (
            max_cell_v if state.get("max_cell_voltage") is None else max(state["max_cell_voltage"], max_cell_v)
        )

    # Full-charge / cycle detection (SoC-based, hysteresis-armed).
    if soc is not None:
        if soc >= FULL_CHARGE_SOC_THRESHOLD and state.get("full_charge_armed", True):
            consumed = state.get("consumed_amphours")
            last_discharge = consumed if consumed is not None else 0.0
            state["last_discharge_ah"] = last_discharge
            state["deepest_discharge_ah"] = max(state.get("deepest_discharge_ah", 0.0), last_discharge)
            state["charge_cycles"] = state.get("charge_cycles", 0) + 1
            state["consumed_amphours"] = 0.0
            state["last_full_charge_epoch"] = now
            state["full_charge_armed"] = False
        elif soc < FULL_CHARGE_REARM_SOC:
            state["full_charge_armed"] = True

    return state


def to_dbus_dict(state: dict) -> dict:
    """Map history state -> the /History/* (+/ConsumedAmphours override)
    D-Bus paths. Pure, no I/O."""
    now = time.time()
    if state.get("last_full_charge_epoch") is not None:
        time_since_full_charge = max(0, int(now - state["last_full_charge_epoch"]))
    elif state.get("created_epoch") is not None:
        # No full-charge observed yet since this history was created --
        # report time-since-daemon-first-tracked-this instead of a bare
        # None, per the task spec ("start near-zero and populate over
        # time" -- this is the near-zero starting point).
        time_since_full_charge = max(0, int(now - state["created_epoch"]))
    else:
        time_since_full_charge = None

    out = {
        "/History/ChargedEnergy": state.get("charged_energy_kwh"),
        "/History/DischargedEnergy": state.get("discharged_energy_kwh"),
        "/History/TotalAhDrawn": state.get("total_ah_drawn"),
        "/History/MinimumVoltage": state.get("min_voltage"),
        "/History/MaximumVoltage": state.get("max_voltage"),
        "/History/MinimumCellVoltage": state.get("min_cell_voltage"),
        "/History/MaximumCellVoltage": state.get("max_cell_voltage"),
        "/History/DeepestDischarge": state.get("deepest_discharge_ah"),
        "/History/LastDischarge": state.get("last_discharge_ah"),
        "/History/ChargeCycles": state.get("charge_cycles"),
        "/History/TimeSinceLastFullCharge": time_since_full_charge,
    }
    # /ConsumedAmphours: once the real coulomb counter has a value (i.e.
    # it has been seeded at least once by update_history()), it supersedes
    # params.py's SoC-derived approximation for the SAME key -- the daemon
    # applies this dict on top of params.build_bank_params()'s output.
    if state.get("consumed_amphours") is not None:
        out["/ConsumedAmphours"] = state["consumed_amphours"]
    return out


# --------------------------------------------------------------------------
# __main__: quick self-test -- simulate a charge/discharge/full-charge
# cycle in memory (no real file writes) and print the resulting state.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    st = _fresh_state()
    t = 1000.0
    # Discharge for a while.
    for _ in range(5):
        t += 2.0
        st = update_history(
            st, voltage=27.0, current=-5.0, soc=80.0,
            min_cell_v=3.30, max_cell_v=3.35, installed_capacity_ah=100.0, now=t,
        )
    # Recharge to full.
    for soc in (85.0, 92.0, 99.6):
        t += 2.0
        st = update_history(
            st, voltage=28.5, current=8.0, soc=soc,
            min_cell_v=3.40, max_cell_v=3.45, installed_capacity_ah=100.0, now=t,
        )
    print(json.dumps(st, indent=2))
    print(json.dumps(to_dbus_dict(st), indent=2))
