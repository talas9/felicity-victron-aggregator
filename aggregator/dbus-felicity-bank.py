#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbus-felicity-bank.py -- daemon entry point for the Felicity bank aggregator.

Wires together the three modules documented in INTERFACE.md:
  - discovery.py   : port autodiscovery + serial-based pack identity + SIM padding
  - params.py      : pure aggregation math -> normalized D-Bus-path-keyed dict
  - dbusservice.py : VeDbusService wrapper, path schema, /Redetect + Request/Id

This file owns NO hardware/register/aggregation logic of its own -- it is
pure wiring, per INTERFACE.md's "Daemon wiring" section:

    packs = discovery.scan({})
    svc = dbusservice.FelicityBankDbusService(..., on_redetect=..., on_request_id=...)
    loop:
        packs = discovery.scan(packs)
        params = build_bank_params(packs)
        svc.update(params)

discovery.scan() both re-reads already-claimed slots AND probes free slots
for new packs in one call (see discovery.py), so a single ~2s poll timer is
sufficient -- there is no separate "probe" cadence to maintain. /Redetect
calls discovery.rescan() (a documented alias of scan(), kept separate only
so the callback has an obviously-named thing to call) for an out-of-cycle
immediate refresh, then resets the button via GLib.idle_add per
dbusservice.py's reset_redetect() contract (never called synchronously from
inside the onchangecallback itself).

Simulation of pack 2 while only one RS485 adapter is physically connected
is controlled entirely inside discovery.py (SIMULATE_UNCLAIMED_SLOTS,
default True). No sim-related state or switch lives in this file -- see
discovery.py's module docstring for the one-line switch and the "nothing
to change here" note for when the second adapter is plugged in.

Never crashes on a read/aggregation/publish error -- discovery.py,
params.py, and dbusservice.py each already catch and log internally; this
file adds one more top-level guard per cycle as defense in depth so a
genuinely unexpected exception can never kill the GLib poll timer.

Exits cleanly on SIGTERM/SIGINT (daemontools sends SIGTERM on `svc -d`).
"""

from __future__ import annotations

import logging
import os
import signal
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(1, _HERE)
sys.path.insert(1, os.path.join(_HERE, "ext", "velib_python"))

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402

from settingsdevice import SettingsDevice  # noqa: E402

import discovery  # noqa: E402
import params as params_mod  # noqa: E402
import dbusservice as dbusservice_mod  # noqa: E402
import history as history_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVICE_NAME = "com.victronenergy.battery.felicity_bank"
PRODUCT_NAME = "Felicity Bank (2S)"
PROCESS_VERSION = "2.0.0"

# Not a Victron-issued product code (there isn't one for a hand-rolled
# aggregator) -- matches dbus-serialbattery's own placeholder for
# unregistered/community battery drivers on this box.
PRODUCT_ID = 0xBA77

# Default DeviceInstance the very first time this service ever runs on this
# box; the settings service (com.victronenergy.settings) is the source of
# truth thereafter, persisted at
# /Settings/Devices/felicity_bank/ClassAndVrmInstance. Must stay >= 512 and
# distinct from vebus (276, the battery monitor -- must stay so) and the
# two CAN battery services (512, 513, must stay healthy). 514 was verified
# free on this box before first use.
DEVICE_INSTANCE_DEFAULT = 514

MAX_CELLS = 16  # upper bound on pre-created Cell/<i>/Voltage paths per pack

POLL_INTERVAL_MS = 2000

logger = logging.getLogger("dbus-felicity-bank")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class FelicityBankDaemon:
    def __init__(self):
        self.bus = dbus.SystemBus()
        self.instance = self._acquire_device_instance()
        self.packs: dict[int, discovery.PackStatus] = {}

        # Group C: persistent coulomb/energy counter + history state.
        # Loaded once at startup (corruption-safe -- see history.py), then
        # advanced every cycle and persisted no more than once every
        # history.SAVE_INTERVAL_S, plus a forced flush on shutdown (see
        # shutdown() below).
        self.history_state = history_mod.load_history()

        # Group B: last-logged decoded FET-vs-heuristic divergence per
        # pack, so _log_fet_observation() only logs on a state CHANGE
        # (not every 2s poll) -- keeps this a validation signal, not log
        # spam, while still capturing every transition for later review.
        self._last_fet_log: dict[int, tuple] = {}

        self.svc = dbusservice_mod.FelicityBankDbusService(
            bus=self.bus,
            service_name=SERVICE_NAME,
            device_instance=self.instance,
            product_id=PRODUCT_ID,
            product_name=PRODUCT_NAME,
            process_version=PROCESS_VERSION,
            mgmt_connection="Modbus RTU RS485 (aggregated, 2 packs)",
            max_cells=MAX_CELLS,
            on_redetect=self._on_redetect,
            on_request_id=self._on_request_id,
        )

        # Initial synchronous discovery + publish so the first values (and
        # cell count) reflect reality as soon as the service is registered,
        # rather than leaving mandatory-but-still-None values until the
        # first 2s timer tick.
        self._cycle()

        GLib.timeout_add(POLL_INTERVAL_MS, self._poll_cb)

    # -- device instance -------------------------------------------------

    def _acquire_device_instance(self) -> int:
        settings = SettingsDevice(
            self.bus,
            {
                "instance": [
                    "/Settings/Devices/felicity_bank/ClassAndVrmInstance",
                    "battery:%d" % DEVICE_INSTANCE_DEFAULT,
                    0,
                    0,
                ],
            },
            eventCallback=None,
        )
        self._settings = settings  # keep a reference alive for the life of the process
        raw = settings["instance"]
        try:
            return int(str(raw).split(":", 1)[1])
        except Exception:
            logger.warning(
                "could not parse persisted ClassAndVrmInstance %r, using default %d",
                raw, DEVICE_INSTANCE_DEFAULT,
            )
            return DEVICE_INSTANCE_DEFAULT

    # -- discovery / aggregate / publish ----------------------------------

    def _cycle(self):
        """One discovery -> aggregate -> publish cycle. Never raises."""
        try:
            self.packs = discovery.scan(self.packs)
        except Exception:
            logger.exception("discovery.scan() raised (it is documented to self-guard internally)")

        try:
            p = params_mod.build_bank_params(self.packs)
        except Exception:
            logger.exception("build_bank_params() raised -- skipping publish this cycle")
            return

        # Group C: advance the persistent history/coulomb counter with
        # this cycle's bank values, merge its /History/* (+/ConsumedAmphours
        # override) paths on top of params.py's output, then persist
        # (internally throttled to >=60s between writes -- see
        # history.SAVE_INTERVAL_S). Isolated in its own try/except so a
        # history bug can never prevent the (already-computed) normal
        # params from being published.
        try:
            self.history_state = history_mod.update_history(
                self.history_state,
                voltage=p.get("/Dc/0/Voltage"),
                current=p.get("/Dc/0/Current"),
                soc=p.get("/Soc"),
                min_cell_v=p.get("/System/MinCellVoltage"),
                max_cell_v=p.get("/System/MaxCellVoltage"),
                installed_capacity_ah=p.get("/InstalledCapacity") or params_mod.BANK_CAPACITY_AH,
            )
            p.update(history_mod.to_dbus_dict(self.history_state))
            history_mod.save_history(self.history_state)
        except Exception:
            logger.exception("history update/persist failed -- publishing this cycle without /History/* update")

        self._log_fet_observation(p)

        try:
            self.svc.update(p)
        except Exception:
            logger.exception("dbusservice.update() raised (it is documented to self-guard internally)")

    def _log_fet_observation(self, p: dict) -> None:
        """Group B safety validation aid: log (at WARNING, since it is the
        interesting/actionable case) whenever a pack's DECODED FET state
        (observational, register 4866 -- see FAULT-DECODE.md) disagrees
        with the CURRENT AUTHORITATIVE heuristic-based /Io/AllowToCharge or
        /Io/AllowToDischarge. This does not change behavior -- the
        heuristic remains authoritative (see params.py) -- it only gives a
        way to review, over days, whether the decode would have agreed
        with the heuristic before it is ever trusted on its own (model
        mismatch LPBF48250 vs FLA12100 is UNVERIFIED). Logs only on
        state CHANGE per pack, not every 2s poll, to stay a signal rather
        than log spam."""
        try:
            allow_charge = p.get("/Io/AllowToCharge")
            allow_discharge = p.get("/Io/AllowToDischarge")
            for idx, pack in (p.get("packs") or {}).items():
                fet_c = pack.get("ChargeFetObserved")
                fet_d = pack.get("DischargeFetObserved")
                key = (fet_c, fet_d, allow_charge, allow_discharge)
                if self._last_fet_log.get(idx) == key:
                    continue
                self._last_fet_log[idx] = key
                if fet_c is None and fet_d is None:
                    continue  # pack not present/ok this cycle -- nothing to compare
                diverges = (fet_c is not None and fet_c != allow_charge) or (
                    fet_d is not None and fet_d != allow_discharge
                )
                log_fn = logger.warning if diverges else logger.info
                log_fn(
                    "Battery %s FET decode (observational): charge_fet=%s discharge_fet=%s "
                    "vs heuristic Io/AllowToCharge=%s Io/AllowToDischarge=%s%s",
                    idx, fet_c, fet_d, allow_charge, allow_discharge,
                    " -- DIVERGES (heuristic remains authoritative, not gated on decode)" if diverges else "",
                )
        except Exception:
            logger.exception("_log_fet_observation() failed -- non-fatal, purely diagnostic")

    def _poll_cb(self):
        self._cycle()
        return True  # keep the GLib timer running

    def shutdown(self) -> None:
        """Called once from main()'s signal handler before the mainloop
        exits. Forces a final history.json flush (bypassing the normal
        60s throttle) so the last few seconds of accumulation since the
        previous periodic save are never silently lost on a clean
        `svc -d` stop. Never raises."""
        try:
            history_mod.save_history(self.history_state, force=True)
        except Exception:
            logger.exception("final history flush on shutdown failed")

    # -- D-Bus write handlers ---------------------------------------------

    def _on_redetect(self):
        # Called synchronously from dbusservice's onchangecallback (itself
        # already serialized by GLib's single-threaded mainloop). Must NOT
        # call reset_redetect() inline -- schedule it via GLib.idle_add so
        # it runs after this handler (and the D-Bus write that triggered
        # it) has fully returned. See dbusservice.py's reset_redetect()
        # docstring for the history of why a busy-guard here was wrong.
        logger.info("Redetect requested via D-Bus -- triggering immediate rescan")
        try:
            self.packs = discovery.rescan(self.packs)
            p = params_mod.build_bank_params(self.packs)
            self.svc.update(p)
        except Exception:
            logger.exception("Redetect-triggered rescan failed")
        finally:
            GLib.idle_add(self.svc.reset_redetect)

    def _on_request_id(self, new_id: int) -> bool:
        if new_id not in (1, 2):
            return False
        logger.info(
            "GUI selected Battery/Request/Id = %d (display selector only, no aggregation/charge-authority change)",
            new_id,
        )
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    DBusGMainLoop(set_as_default=True)
    mainloop = GLib.MainLoop()

    # Held in a local variable (previously discarded) so the SIGTERM/SIGINT
    # handler below can call daemon.shutdown() to force a final
    # history.json flush -- see FelicityBankDaemon.shutdown()'s docstring.
    # The GLib timer already kept the object alive via a bound-method
    # closure either way; this is purely for the shutdown() access.
    daemon = FelicityBankDaemon()

    def _quit(*_args):
        logger.info("signal received, shutting down")
        daemon.shutdown()
        mainloop.quit()
        return False

    # GLib.unix_signal_add integrates the signal with the glib mainloop
    # itself (a plain Python signal handler is not reliably serviced while
    # blocked in GLib's C-level poll).
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _quit)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _quit)

    logger.info("entering main loop")
    mainloop.run()
    logger.info("exited cleanly")


if __name__ == "__main__":
    main()
