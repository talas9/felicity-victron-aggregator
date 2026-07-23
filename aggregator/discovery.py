#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discovery.py -- port autodiscovery + serial-based stable pack identification
for the Felicity bank aggregator.

Contract: see /data/rs485-cells/aggregator/INTERFACE.md ("discovery.py"
section). Builds on felicity_reader.read_pack() -- does not duplicate its
register logic. Pure-ish: the only I/O is probing serial ports via
felicity_reader and reading/writing the small JSON mapping file; never
touches D-Bus.

Identity is by SERIAL (register 63492, read via felicity_reader), never by
port name or arrival order -- /dev/ttyUSB* device nodes can and do renumber
across reboots or reconnects. Once a serial is seen, its bank slot (1 or 2)
is persisted to MAPPING_FILE and never reassigned.

SIMULATE_UNCLAIMED_SLOTS mirrors dbus-felicity-bank.py's SIMULATE_PACK2:
while a slot has never had a real pack identified on it, scan() pads it
with felicity_reader's SIM mode (sim:A / sim:B) so the full bank/pack/cell
hierarchy is exercised before the second RS485 adapter physically arrives.
The instant a real pack is identified on a slot, that slot is permanently
"real_claimed" and is never sim-padded again, even if it later drops out.

Today (see pack_mapping.json on the box): Battery 1 is REAL, serial
<PACK_A_SERIAL>, on /dev/ttyUSB0. Battery 2 has never been real_claimed, so
it is simulated as sim:B whenever SIMULATE_UNCLAIMED_SLOTS is True.

TO SWITCH ON THE REAL SECOND ADAPTER: nothing needs to change in this file
at all. Plug the second Felicity pack's RS485 adapter in; the next scan()
cycle will glob it as a new /dev/ttyUSB* candidate, read its serial, find
it unmapped, assign it the lowest free slot (2, since 1 is already taken),
persist that to pack_mapping.json, and mark it real_claimed -- permanently
displacing the sim:B padding from that point on. No manual edit required.

If you ever want to HARD-DISABLE all sim-padding (e.g. before final
hand-off, so an unpopulated slot just shows disconnected instead of fake
data), flip the one line below:

    SIMULATE_UNCLAIMED_SLOTS = True   ->   SIMULATE_UNCLAIMED_SLOTS = False
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass

import felicity_reader

logger = logging.getLogger("discovery")

_HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(_HERE, "pack_mapping.json")

# See module docstring: the one-line switch to turn all sim-padding off.
SIMULATE_UNCLAIMED_SLOTS = False

FAIL_THRESHOLD = 3  # consecutive failed reads on a real_claimed slot before present flips False

_SLOT_INDICES = (1, 2)  # bank is hard 2S


@dataclass
class PackStatus:
    index: int              # 1-based, matches /Battery/<n>. Bank is 2S only: 1 or 2.
    port: str | None        # e.g. "/dev/ttyUSB0" (real) or "sim:A"/"sim:B" (simulated); None if never seen.
    serial: str | None      # from felicity_reader's register 63492 read (5 regs, decimal-concat string).
    present: bool           # True if this poll cycle got ok=True data (real or, if enabled, simulated).
    simulated: bool         # True only when present via SIMULATE_UNCLAIMED_SLOTS, never once real_claimed.
    real_claimed: bool      # True forever once a real pack has been identified on this slot.
    fail_count: int         # consecutive failed reads since last success; present flips False at 3.
    data: dict | None       # the felicity_reader.read_pack() dict; last-known-good is carried
                             # forward through a real_claimed slot's grace period (see
                             # _carry_forward), so this can be a *previous* cycle's reading
                             # while fail_count is nonzero. None only once dropped (never
                             # claimed and unfilled, or fail_count reached FAIL_THRESHOLD).


# --------------------------------------------------------------------------
# Mapping persistence
# --------------------------------------------------------------------------

def read_mapping(path: str = MAPPING_FILE) -> dict[str, int]:
    """Load serial -> 1-based slot-index map. Missing file -> {}. Never raises."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
        logger.warning("mapping file %s did not contain a JSON object -- ignoring", path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("could not read mapping file %s (%s) -- treating as empty", path, exc)
    return {}


def write_mapping(mapping: dict[str, int], path: str = MAPPING_FILE) -> None:
    """Atomic write (tmp file + os.replace). Never raises; logs and no-ops on failure."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(mapping, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("failed to persist pack mapping to %s: %s", path, exc)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------

def _carry_forward(known: dict[int, PackStatus]) -> dict[int, PackStatus]:
    """Build this cycle's working dict from the prior cycle's, preserving
    the identity/permanence fields (real_claimed, serial, port, fail_count)
    and resetting `present` to its neutral per-cycle starting point.

    `data` is DELIBERATELY carried forward (last-known-good), not reset to
    None here -- this is the grace-period fix (see _refresh_claimed_slots):
    a transient read failure must not blank a real_claimed slot's data
    before the fail-threshold check runs, or the pack silently drops out
    of every bank aggregate on the very first bad poll. _refresh_claimed_
    slots() is the only place that ever nulls `data` again, and only once
    fail_count reaches FAIL_THRESHOLD. For never-claimed slots this carried
    -forward value is harmless -- _fill_unclaimed_with_sim() overwrites it
    unconditionally every cycle since `present` is always False here."""
    out: dict[int, PackStatus] = {}
    for idx in _SLOT_INDICES:
        prev = known.get(idx)
        if prev is not None:
            out[idx] = PackStatus(
                index=idx,
                port=prev.port,
                serial=prev.serial,
                present=False,
                simulated=False,
                real_claimed=prev.real_claimed,
                fail_count=prev.fail_count,
                data=prev.data,
            )
        else:
            out[idx] = PackStatus(
                index=idx, port=None, serial=None, present=False,
                simulated=False, real_claimed=False, fail_count=0, data=None,
            )
    return out


def _sim_port_for(idx: int) -> str:
    return f"sim:{'A' if idx == 1 else 'B'}"


def _refresh_claimed_slots(result: dict[int, PackStatus], address: int, timeout: float) -> None:
    """Re-read each already real_claimed slot on its known port. Tracks
    fail_count across calls; only flips present False once fail_count
    reaches FAIL_THRESHOLD (mirrors dbus-felicity-bank.py's connected/
    FAIL_THRESHOLD behavior) so a single transient read glitch doesn't
    falsely report a pack as gone -- last-known data is kept during the
    grace period."""
    for slot in result.values():
        if not slot.real_claimed or not slot.port:
            continue
        r = felicity_reader.read_pack(slot.port, address=address, timeout=timeout)
        if r.get("ok"):
            slot.data = r
            slot.serial = r.get("serial") or slot.serial
            slot.present = True
            slot.simulated = False
            slot.fail_count = 0
        else:
            slot.fail_count += 1
            was_present = slot.fail_count < FAIL_THRESHOLD
            logger.warning(
                "Battery %d (%s) read failed (%d/%d): %s",
                slot.index, slot.port, slot.fail_count, FAIL_THRESHOLD, r.get("error"),
            )
            if was_present:
                # Grace period: slot.data is already last-known-good here
                # (carried forward by _carry_forward, not reset to None),
                # so just flip present back True -- a single bad poll must
                # not flap the slot to "disconnected" or drop it from the
                # bank aggregates. FAIL_THRESHOLD (module-level, currently
                # 3) consecutive failures are required before that happens.
                slot.present = True
            else:
                logger.warning(
                    "Battery %d (%s) marked DISCONNECTED after %d consecutive failures",
                    slot.index, slot.port, slot.fail_count,
                )
                slot.present = False
                slot.data = None


def _probe_for_new_packs(
    result: dict[int, PackStatus],
    mapping: dict[str, int],
    ports_glob: str,
    address: int,
    timeout: float,
) -> None:
    """Probe every /dev/ttyUSB* candidate not already claimed by a
    real_claimed slot's port. Identifies packs by serial, never by port
    order. Assigns/persists a new mapping entry immediately on first
    sighting of an unseen serial."""
    free_slots = [idx for idx in _SLOT_INDICES if not result[idx].real_claimed]
    if not free_slots:
        return  # bank already fully real -- nothing left to discover

    claimed_ports = {s.port for s in result.values() if s.real_claimed and s.port}

    try:
        candidates = sorted(glob.glob(ports_glob))
    except Exception as exc:
        logger.warning("failed to glob %s: %s", ports_glob, exc)
        candidates = []

    for port in candidates:
        if port in claimed_ports:
            continue

        r = felicity_reader.read_pack(port, address=address, timeout=timeout)
        if not r.get("ok"):
            logger.debug("probe %s: no response (%s)", port, r.get("error"))
            continue

        serial_number = r.get("serial")
        if not serial_number:
            logger.debug("probe %s: responded but returned no serial -- ignoring", port)
            continue

        slot_index = mapping.get(serial_number)
        if slot_index is None:
            used = set(mapping.values())
            free = [i for i in _SLOT_INDICES if i not in used]
            if not free:
                logger.warning(
                    "discovered pack serial %s on %s but both bank slots are already "
                    "assigned to other serials -- ignoring (bank is hard 2S)",
                    serial_number, port,
                )
                continue
            slot_index = free[0]
            mapping[serial_number] = slot_index
            write_mapping(mapping)
            logger.info(
                "adopted new pack serial %s on %s as Battery %d (persisted to %s)",
                serial_number, port, slot_index, MAPPING_FILE,
            )

        slot = result.get(slot_index)
        if slot is None:
            continue
        if slot.real_claimed:
            # Already claimed by a different serial within this same cycle
            # (race between two ports both mapping-hitting the same slot).
            # Should not happen in practice -- mapping is 1:1 -- but never
            # silently overwrite an existing claim.
            logger.warning(
                "serial %s maps to Battery %d, but that slot is already claimed by "
                "serial %s this cycle -- ignoring", serial_number, slot_index, slot.serial,
            )
            continue

        slot.port = port
        slot.serial = serial_number
        slot.data = r
        slot.present = True
        slot.simulated = False
        slot.real_claimed = True
        slot.fail_count = 0
        claimed_ports.add(port)
        logger.info("pack serial %s on %s is Battery %d", serial_number, port, slot_index)


def _fill_unclaimed_with_sim(result: dict[int, PackStatus], address: int, timeout: float) -> None:
    """Any slot still not real_claimed and not already present (i.e. not
    freshly claimed this same cycle) gets simulated data when
    SIMULATE_UNCLAIMED_SLOTS is True, else present=False/data=None."""
    for idx, slot in result.items():
        if slot.real_claimed or slot.present:
            continue
        if SIMULATE_UNCLAIMED_SLOTS:
            sim_port = _sim_port_for(idx)
            r = felicity_reader.read_pack(sim_port, address=address, timeout=timeout)
            slot.port = sim_port
            slot.serial = r.get("serial")
            slot.data = r
            slot.present = bool(r.get("ok"))
            slot.simulated = True
            slot.fail_count = 0
        else:
            slot.port = None
            slot.serial = None
            slot.data = None
            slot.present = False
            slot.simulated = False
            slot.fail_count = 0


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def scan(
    known: dict[int, PackStatus],
    ports_glob: str = "/dev/ttyUSB*",
    address: int = 1,
    timeout: float = 1.0,
) -> dict[int, PackStatus]:
    """
    Probe every candidate port NOT already claimed by a real_claimed slot.
    Identify each responding pack by its SERIAL register (63492), not port
    order or arrival sequence -- ports can and do renumber across reboots.
    Look the serial up in read_mapping(); if unseen, assign the lowest free
    slot index (1 then 2) and persist via write_mapping() immediately. If
    both slots are already claimed by other serials, log and ignore the new
    pack (bank is hard 2S).

    Returns an updated dict[int, PackStatus] keyed 1 and 2, always both
    keys present. Slots with no real pack claimed get simulated data
    (sim:A / sim:B) when SIMULATE_UNCLAIMED_SLOTS is True, else
    present=False, data=None. `known` is the previous cycle's return value
    (or {} on first call) -- required so real_claimed permanence and
    fail_count survive across calls. Never raises.
    """
    try:
        known = known or {}
        result = _carry_forward(known)
        mapping = read_mapping()

        _refresh_claimed_slots(result, address=address, timeout=timeout)
        _probe_for_new_packs(result, mapping, ports_glob=ports_glob, address=address, timeout=timeout)
        _fill_unclaimed_with_sim(result, address=address, timeout=timeout)

        return result
    except Exception:
        logger.exception("scan() failed unexpectedly -- returning prior known state unchanged")
        # Never raise: hand back whatever we had (carried-forward if we got
        # that far, else the caller's own prior state) so the daemon's poll
        # loop keeps running instead of crashing.
        try:
            return result  # type: ignore[possibly-undefined]
        except NameError:
            return known or {}


def rescan(known: dict[int, PackStatus], **kwargs) -> dict[int, PackStatus]:
    """
    Entry point for the /Redetect button and the periodic rescan timer.
    Identical contract to scan() -- kept as a separate name only so the
    daemon's onchangecallback has an obviously-named thing to call; it is
    not a different algorithm.
    """
    return scan(known, **kwargs)


# --------------------------------------------------------------------------
# __main__: print discovered packs
# --------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    state: dict[int, PackStatus] = {}
    state = scan(state)

    print()
    for idx in sorted(state):
        slot = state[idx]
        if slot.real_claimed:
            tag = "REAL"
        elif slot.simulated:
            tag = "SIM"
        else:
            tag = "none"
        print(
            f"Battery {idx}: present={slot.present!s:<5} [{tag:<4}] "
            f"port={slot.port!s:<12} serial={slot.serial!s:<24} fail_count={slot.fail_count}"
        )
        d = slot.data
        if d and d.get("ok"):
            print(
                f"    voltage={d['voltage']}V current={d['current']}A soc={d['soc']}% "
                f"cells={d['cell_count']} spread={d['cell_spread_mv']}mV"
            )
        elif d and not d.get("ok"):
            print(f"    error: {d.get('error')}")
    print()
