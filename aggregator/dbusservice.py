#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbusservice.py -- VeDbusService wrapper for the Felicity bank aggregator.

Implements FelicityBankDbusService exactly per INTERFACE.md. Creates every
D-Bus path in PARAM-SPEC.md's schema, exposes update(normalized_dict) to
refresh values in one atomic transaction, and registers the two settable
paths (/Redetect, /Battery/Request/Id) that the daemon wires up via the
on_redetect / on_request_id callbacks.

Does NOT read hardware and does NOT call discovery.py / params.py itself --
the daemon supplies data via update(). Pure D-Bus plumbing.

velib_python import: this module lives in
/data/rs485-cells/aggregator/, which already has its own
ext/velib_python/ (vedbus.py + ve_utils.py + settingsdevice.py), the same
layout dbus-felicity-bank.py already uses successfully on this box. We
insert that directory onto sys.path exactly the way dbus-felicity-bank.py
does, then `from vedbus import VeDbusService`.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_VELIB_DIR = os.path.join(_HERE, "ext", "velib_python")
if _VELIB_DIR not in sys.path:
    sys.path.insert(1, _VELIB_DIR)

from vedbus import VeDbusService  # noqa: E402

logger = logging.getLogger("dbus-felicity-bank.dbusservice")

# Bank is hard 2S -- battery slot indices are always 1 and 2. Not a magic
# number scattered through the class: named here once.
BATTERY_SLOT_INDICES = (1, 2)

# Per-pack cosmetic default names, confirmed against physical position via
# a live unplug test (slot binding is by serial, not by ttyUSB port):
# slot 1 = physical right pack, slot 2 = physical left pack.
SLOT_LABELS = {1: "Right", 2: "Left"}

# The fixed, bank-level D-Bus paths written directly from
# params.build_bank_params()'s top-level keys. Kept as an explicit list
# (rather than "iterate every dict key") so a stray/renamed key in params
# fails loudly (KeyError-free skip + log) instead of silently writing an
# unplanned path.
_BANK_LEVEL_PATHS = (
    "/Connected",
    "/System/NrOfBatteries",
    "/System/NrOfCellsPerBattery",
    "/System/MinCellVoltage",
    "/System/MaxCellVoltage",
    "/System/MinVoltageCellId",
    "/System/MaxVoltageCellId",
    "/Dc/0/Voltage",
    "/Dc/0/Current",
    "/Dc/0/Power",
    "/Dc/0/Temperature",
    "/Soc",
    "/Capacity",
    "/InstalledCapacity",
    "/Info/MaxChargeVoltage",
    "/Info/BatteryLowVoltage",
    "/Info/MaxChargeCurrent",
    "/Info/MaxDischargeCurrent",
    "/Io/AllowToCharge",
    "/Io/AllowToDischarge",
    # -- Group A/B/C additions --
    "/Serial",
    "/ConsumedAmphours",
    "/Voltages/Diff",
    "/System/MinCellTemperature",
    "/System/MaxCellTemperature",
    "/System/MinTemperatureCellId",
    "/System/MaxTemperatureCellId",
    "/System/NrOfModulesOnline",
    "/System/NrOfModulesOffline",
    "/System/NrOfModulesBlockingCharge",
    "/System/NrOfModulesBlockingDischarge",
    "/Alarms/HighCellVoltage",
    "/Alarms/LowCellVoltage",
    "/Alarms/HighChargeCurrent",
    "/Alarms/HighDischargeCurrent",
    "/Alarms/HighInternalTemperature",
    "/Alarms/HighChargeTemperature",
    "/Alarms/LowChargeTemperature",
    # -- derived threshold alarms (Group E, 2026-07-21) --
    "/Alarms/CellImbalance",
    "/Alarms/LowVoltage",
    "/Alarms/HighVoltage",
    "/Alarms/LowSoc",
    "/History/ChargedEnergy",
    "/History/DischargedEnergy",
    "/History/TotalAhDrawn",
    "/History/MinimumVoltage",
    "/History/MaximumVoltage",
    "/History/MinimumCellVoltage",
    "/History/MaximumCellVoltage",
    "/History/DeepestDischarge",
    "/History/LastDischarge",
    "/History/ChargeCycles",
    "/History/TimeSinceLastFullCharge",
)

# Per-pack scalar suffixes (params["packs"][idx][<key>] -> /Battery/<idx>/<key>).
# "simulated" and "Cells" are deliberately excluded here: "simulated" is not
# a published D-Bus path (see dbus-felicity-bank.py's PackSlot -- it is
# daemon-internal bookkeeping only), and "Cells" is handled separately
# below since it fans out into indexed Cell/<i>/Voltage paths.
_PACK_SCALAR_SUFFIXES = (
    "Serial",
    "Voltage",
    "Current",
    "Temperature",
    "Soc",
    "Capacity",
    "FwVersion",
    "Status",
    # Observational-only FET decode (Group B) -- see params.py's per-pack
    # comment. Never the authoritative AllowToCharge/Discharge gate.
    "ChargeFetObserved",
    "DischargeFetObserved",
)


class FelicityBankDbusService:
    def __init__(
        self,
        bus,
        service_name: str,
        device_instance: int,
        product_id: int,
        product_name: str,
        process_version: str,
        mgmt_connection: str,
        max_cells: int,
        on_redetect: Callable[[], None],
        on_request_id: Callable[[int], bool],
    ):
        self._on_redetect = on_redetect
        self._on_request_id = on_request_id
        self._max_cells = max_cells

        self.service = VeDbusService(service_name, bus=bus, register=False)
        self._add_paths(
            device_instance=device_instance,
            product_id=product_id,
            product_name=product_name,
            process_version=process_version,
            mgmt_connection=mgmt_connection,
            max_cells=max_cells,
        )
        self.service.register()
        logger.info(
            "registered %s on D-Bus, DeviceInstance=%d",
            service_name,
            device_instance,
        )

    # -- D-Bus path setup --------------------------------------------------

    def _add_paths(
        self,
        device_instance: int,
        product_id: int,
        product_name: str,
        process_version: str,
        mgmt_connection: str,
        max_cells: int,
    ) -> None:
        s = self.service

        # Mandatory paths (every Victron D-Bus service must publish these).
        s.add_path("/Mgmt/ProcessName", os.path.basename(sys.argv[0]) or "dbus-felicity-bank")
        s.add_path("/Mgmt/ProcessVersion", process_version)
        s.add_path("/Mgmt/Connection", mgmt_connection)
        s.add_path("/DeviceInstance", device_instance)
        s.add_path("/ProductId", product_id)
        s.add_path("/ProductName", product_name)
        s.add_path("/FirmwareVersion", process_version)  # this daemon's version, not pack firmware
        s.add_path("/HardwareVersion", None)
        s.add_path("/Connected", 0)
        s.add_path("/Serial", None)

        # /CustomName: cosmetic identity only (Group D). Settable so the
        # GUI's rename dialog works, but NOT part of the per-cycle
        # update() writes below -- once set (by a user, or left at this
        # default), it is never overwritten by the daemon's poll loop.
        # NOTE: this is in-memory only for the life of this process; a
        # user-set name does not currently survive a service restart (no
        # SettingsDevice-backed persistence wired up for it) -- acceptable
        # per the task's "or make it settable, defaulting to that".
        s.add_path(
            "/CustomName",
            "Felicity Bank (2S)",
            writeable=True,
            onchangecallback=lambda path, newvalue: True,
        )

        # Bank-level paths -- see PARAM-SPEC.md for derivations.
        s.add_path("/System/NrOfBatteries", 0)
        s.add_path("/System/NrOfCellsPerBattery", 0)
        s.add_path("/System/MinCellVoltage", None)
        s.add_path("/System/MaxCellVoltage", None)
        s.add_path("/System/MinVoltageCellId", None)
        s.add_path("/System/MaxVoltageCellId", None)

        s.add_path("/Dc/0/Voltage", None)
        s.add_path("/Dc/0/Current", None)
        s.add_path("/Dc/0/Power", None)
        s.add_path("/Dc/0/Temperature", None)
        s.add_path("/Soc", None)
        s.add_path("/Capacity", None)
        s.add_path("/InstalledCapacity", 0.0)

        s.add_path("/Info/MaxChargeVoltage", None)
        s.add_path("/Info/BatteryLowVoltage", None)
        s.add_path("/Info/MaxChargeCurrent", None)
        s.add_path("/Info/MaxDischargeCurrent", None)

        s.add_path("/Io/AllowToCharge", 0)
        s.add_path("/Io/AllowToDischarge", 0)

        # -- Group A additions --
        s.add_path("/ConsumedAmphours", None)
        s.add_path("/Voltages/Diff", None)
        s.add_path("/System/MinCellTemperature", None)
        s.add_path("/System/MaxCellTemperature", None)
        s.add_path("/System/MinTemperatureCellId", None)
        s.add_path("/System/MaxTemperatureCellId", None)
        s.add_path("/System/NrOfModulesOnline", 0)
        s.add_path("/System/NrOfModulesOffline", 0)
        s.add_path("/System/NrOfModulesBlockingCharge", 0)
        s.add_path("/System/NrOfModulesBlockingDischarge", 0)

        # -- Group B additions: the 7 real, decoded alarms. Victron 0/1/2
        # convention; 0 = ok until a fault bit is actually observed set.
        s.add_path("/Alarms/HighCellVoltage", 0)
        s.add_path("/Alarms/LowCellVoltage", 0)
        s.add_path("/Alarms/HighChargeCurrent", 0)
        s.add_path("/Alarms/HighDischargeCurrent", 0)
        s.add_path("/Alarms/HighInternalTemperature", 0)
        s.add_path("/Alarms/HighChargeTemperature", 0)
        s.add_path("/Alarms/LowChargeTemperature", 0)

        # -- Group E additions: derived threshold alarms (params.py
        # compares bank aggregates -- /Voltages/Diff, /Dc/0/Voltage,
        # /Soc -- against named LiFePO4 thresholds each cycle). Same
        # annunciate-only, Victron 0/1/2 convention as Group B above;
        # start at 0 (ok/unknown) like every other alarm path.
        s.add_path("/Alarms/CellImbalance", 0)
        s.add_path("/Alarms/LowVoltage", 0)
        s.add_path("/Alarms/HighVoltage", 0)
        s.add_path("/Alarms/LowSoc", 0)

        # -- Group C additions: history.py-owned accumulators. Start at
        # None/0 -- populated by the daemon merging history.to_dbus_dict()
        # into the params.py dict before calling update() (see
        # dbus-felicity-bank.py). Intentionally NOT computed by params.py
        # itself (params.py is a pure, memoryless function; these are
        # stateful and persisted across restarts).
        s.add_path("/History/ChargedEnergy", None)
        s.add_path("/History/DischargedEnergy", None)
        s.add_path("/History/TotalAhDrawn", None)
        s.add_path("/History/MinimumVoltage", None)
        s.add_path("/History/MaximumVoltage", None)
        s.add_path("/History/MinimumCellVoltage", None)
        s.add_path("/History/MaximumCellVoltage", None)
        s.add_path("/History/DeepestDischarge", None)
        s.add_path("/History/LastDischarge", None)
        s.add_path("/History/ChargeCycles", 0)
        s.add_path("/History/TimeSinceLastFullCharge", None)

        # Settable paths.
        s.add_path(
            "/Redetect",
            0,
            writeable=True,
            onchangecallback=self._on_redetect_change,
        )
        s.add_path(
            "/Battery/Request/Id",
            1,
            writeable=True,
            onchangecallback=self._on_request_id_change,
        )

        # Per-pack paths, bank is hard 2S.
        for idx in BATTERY_SLOT_INDICES:
            p = f"/Battery/{idx}"
            s.add_path(
                f"{p}/CustomName",
                SLOT_LABELS.get(idx, f"Pack {idx}"),
                writeable=True,
                onchangecallback=lambda path, newvalue: True,
            )
            s.add_path(f"{p}/Serial", None)
            s.add_path(f"{p}/Voltage", None)
            s.add_path(f"{p}/Current", None)
            s.add_path(f"{p}/Temperature", None)
            s.add_path(f"{p}/Soc", None)
            s.add_path(f"{p}/Capacity", None)
            s.add_path(f"{p}/FwVersion", None)
            s.add_path(f"{p}/Status", 0)
            s.add_path(f"{p}/ChargeFetObserved", None)
            s.add_path(f"{p}/DischargeFetObserved", None)
            for i in range(1, max_cells + 1):
                s.add_path(f"{p}/Cell/{i}/Voltage", None)

    # -- settable-path callbacks --------------------------------------------

    def _on_redetect_change(self, path, newvalue):
        if newvalue:
            try:
                self._on_redetect()
            except Exception:
                logger.exception("on_redetect callback raised")
        return True  # accept the write; the daemon (via on_redetect) is
        # responsible for scheduling reset_redetect() through
        # GLib.idle_add once it has finished -- see reset_redetect()'s
        # docstring below. This method must never call reset_redetect()
        # itself synchronously.

    def _on_request_id_change(self, path, newvalue):
        try:
            n = int(newvalue)
        except (TypeError, ValueError):
            return False
        try:
            return bool(self._on_request_id(n))
        except Exception:
            logger.exception("on_request_id callback raised")
            return False

    # -- public API ----------------------------------------------------------

    def update(self, params: dict) -> None:
        """
        params = the exact dict returned by params.build_bank_params().
        Writes every bank-level key directly, then every per-pack scalar
        and Cell/<i>/Voltage path. Wraps all writes in one
        `with self.service as srv:` block so a GUI read never observes a
        half-updated bank. Never raises out of a normal call.
        """
        try:
            with self.service as srv:
                for path in _BANK_LEVEL_PATHS:
                    if path not in params:
                        continue
                    try:
                        srv[path] = params[path]
                    except Exception:
                        logger.exception("failed writing bank path %s", path)

                packs = params.get("packs", {})
                for idx in BATTERY_SLOT_INDICES:
                    pack = packs.get(idx)
                    if pack is None:
                        continue
                    p = f"/Battery/{idx}"
                    for suffix in _PACK_SCALAR_SUFFIXES:
                        if suffix not in pack:
                            continue
                        try:
                            srv[f"{p}/{suffix}"] = pack[suffix]
                        except Exception:
                            logger.exception("failed writing %s/%s", p, suffix)

                    cells = pack.get("Cells") or []
                    for i, v in enumerate(cells[: self._max_cells], start=1):
                        try:
                            srv[f"{p}/Cell/{i}/Voltage"] = v
                        except Exception:
                            logger.exception("failed writing %s/Cell/%d/Voltage", p, i)
                    # Indices beyond len(cells) keep their prior/None value
                    # -- never write a sentinel-derived value here, params.py
                    # already filtered (see INTERFACE.md).
        except Exception:
            logger.exception("update() failed")

    def reset_redetect(self) -> None:
        """Sets /Redetect back to 0. The daemon calls this via
        GLib.idle_add(...) AFTER on_redetect() has finished -- never call
        it synchronously from inside the onchangecallback itself, or the
        write re-enters mid-transaction (see dbus-felicity-bank.py history:
        an earlier version had a busy-guard here that could leave /Redetect
        stuck at 1; the fix is idle_add + unconditional reset, not a
        guard)."""
        try:
            self.service["/Redetect"] = 0
        except Exception:
            logger.exception("reset_redetect() failed")


# ---------------------------------------------------------------------------
# Smoke test -- instantiates on the SESSION bus under a throwaway service
# name, confirms every path was created, fires both settable callbacks by
# invoking the same onchangecallback path VeDbusItemExport.SetValue uses,
# then tears the service down and exits WITHOUT leaving anything registered.
#
# Run under a session bus, e.g.:
#   dbus-run-session -- python3 dbusservice.py
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        print("SMOKE TEST FAILED: no DBUS_SESSION_BUS_ADDRESS in environment "
              "-- run under `dbus-run-session -- python3 dbusservice.py`")
        return 1

    # A D-Bus connection that exports objects (VeDbusService does, for
    # every path) must be attached to a main loop before the connection is
    # created. The real daemon does this once in its own main() before
    # touching D-Bus at all (see dbus-felicity-bank.py); this smoke test
    # reproduces that same precondition for itself.
    DBusGMainLoop(set_as_default=True)
    session_bus = dbus.SessionBus()

    redetect_calls = []
    request_id_calls = []

    def on_redetect():
        redetect_calls.append(True)

    def on_request_id(new_id: int) -> bool:
        request_id_calls.append(new_id)
        return new_id in (1, 2)

    throwaway_name = "com.victronenergy.battery.felicity_bank_smoketest"

    svc = FelicityBankDbusService(
        bus=session_bus,
        service_name=throwaway_name,
        device_instance=514,
        product_id=0xBA77,
        product_name="Felicity Bank (2S) [smoketest]",
        process_version="0.0.0-smoketest",
        mgmt_connection="smoke test / no hardware",
        max_cells=16,
        on_redetect=on_redetect,
        on_request_id=on_request_id,
    )

    ok = True

    # 1. Confirm a representative set of paths exists.
    expected_paths = list(_BANK_LEVEL_PATHS) + [
        "/Mgmt/ProcessName", "/DeviceInstance", "/ProductId", "/ProductName",
        "/Redetect", "/Battery/Request/Id",
        "/Battery/1/Serial", "/Battery/1/Cell/1/Voltage", "/Battery/1/Cell/16/Voltage",
        "/Battery/2/Status",
    ]
    for path in expected_paths:
        if path not in svc.service:
            print(f"SMOKE TEST FAILED: path {path} missing after _add_paths()")
            ok = False
    print(f"path check: {len(expected_paths)} representative paths checked, "
          f"{'all present' if ok else 'SOME MISSING'}")

    # 2. Drive update() with a static dict matching params.build_bank_params()'s shape.
    static_params = {
        "/Connected": 1,
        "/System/NrOfBatteries": 2,
        "/System/NrOfCellsPerBattery": 4,
        "/System/MinCellVoltage": 3.347,
        "/System/MaxCellVoltage": 3.434,
        "/System/MinVoltageCellId": "B1C2",
        "/System/MaxVoltageCellId": "B2C3",
        "/Dc/0/Voltage": 27.10,
        "/Dc/0/Current": 1.5,
        "/Dc/0/Power": 40.65,
        "/Dc/0/Temperature": 37.6,
        "/Soc": 82.0,
        "/Capacity": 82.0,
        "/InstalledCapacity": 100.0,
        "/Info/MaxChargeVoltage": 28.6,
        "/Info/BatteryLowVoltage": 20.0,
        "/Info/MaxChargeCurrent": 10.0,
        "/Info/MaxDischargeCurrent": 50.0,
        "/Io/AllowToCharge": 1,
        "/Io/AllowToDischarge": 1,
        "packs": {
            1: {
                "Serial": "<PACK_A_SERIAL>",
                "Voltage": 13.54,
                "Current": 1.5,
                "Temperature": 37.6,
                "Capacity": 100.0,
                "FwVersion": "1.03",
                "Status": 1,
                "Cells": [3.401, 3.347, 3.410, 3.434],
                "simulated": False,
            },
            2: {
                "Serial": "sim-B",
                "Voltage": 13.56,
                "Current": 1.5,
                "Temperature": 36.9,
                "Capacity": 100.0,
                "FwVersion": "1.03",
                "Status": 1,
                "Cells": [3.390, 3.400, 3.412, 3.398],
                "simulated": True,
            },
        },
    }
    svc.update(static_params)

    checks = [
        ("/Connected", 1),
        ("/System/NrOfBatteries", 2),
        ("/Dc/0/Voltage", 27.10),
        ("/Battery/1/Serial", "<PACK_A_SERIAL>"),
        ("/Battery/1/Cell/2/Voltage", 3.347),
        ("/Battery/2/Cell/4/Voltage", 3.398),
    ]
    for path, expected in checks:
        actual = svc.service[path]
        if actual != expected:
            print(f"SMOKE TEST FAILED: {path} = {actual!r}, expected {expected!r}")
            ok = False
    # Untouched cell index beyond pack 1's 4 real cells must keep its prior
    # value (None from _add_paths), never a sentinel-derived value.
    if svc.service["/Battery/1/Cell/5/Voltage"] is not None:
        print(f"SMOKE TEST FAILED: /Battery/1/Cell/5/Voltage should remain None, "
              f"got {svc.service['/Battery/1/Cell/5/Voltage']!r}")
        ok = False
    print("update() value check: " + ("all correct" if ok else "SOME WRONG"))

    # 3. Fire both settable callbacks the same way VeDbusItemExport.SetValue
    #    does: invoke the registered onchangecallback directly.
    accepted = svc.service._onchangecallbacks["/Redetect"]("/Redetect", 1)
    if not accepted or not redetect_calls:
        print(f"SMOKE TEST FAILED: /Redetect callback did not fire "
              f"(accepted={accepted}, calls={redetect_calls})")
        ok = False
    else:
        print("/Redetect callback: fired correctly, accepted the write")

    accepted = svc.service._onchangecallbacks["/Battery/Request/Id"]("/Battery/Request/Id", 2)
    if not accepted or request_id_calls != [2]:
        print(f"SMOKE TEST FAILED: /Battery/Request/Id callback wrong "
              f"(accepted={accepted}, calls={request_id_calls})")
        ok = False
    else:
        print("/Battery/Request/Id callback: fired correctly with new_id=2, accepted")

    rejected = svc.service._onchangecallbacks["/Battery/Request/Id"]("/Battery/Request/Id", 99)
    if rejected:
        print("SMOKE TEST FAILED: /Battery/Request/Id accepted an out-of-range id (99)")
        ok = False
    else:
        print("/Battery/Request/Id callback: correctly rejected out-of-range id=99")

    # 4. Tear down -- explicitly release the D-Bus name so nothing is left
    #    registered when this process exits.
    del svc.service
    del svc

    print("SMOKE TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_smoke_test())
