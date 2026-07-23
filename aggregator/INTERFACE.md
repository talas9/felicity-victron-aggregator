# INTERFACE.md — Module Contracts (params.py / discovery.py / dbusservice.py)

Grounded in the working reference implementation
`/data/rs485-cells/aggregator/dbus-felicity-bank.py` (already running as
`com.victronenergy.battery.felicity_bank`, DeviceInstance 514) and
`felicity_reader.py`. These signatures let the three modules be authored in
parallel without drift. See PARAM-SPEC.md for the derivation of every value
named here.

All modules build on `felicity_reader.read_pack(port, address=1,
timeout=1.0) -> dict` / `read_all(ports, ...) -> list[dict]` — read that
module first, do not duplicate its register logic.

---

## discovery.py

```python
from dataclasses import dataclass

MAPPING_FILE = "/data/rs485-cells/aggregator/pack_mapping.json"
SIMULATE_UNCLAIMED_SLOTS: bool  # module-level toggle, default True; mirrors
                                 # dbus-felicity-bank.py's SIMULATE_PACK2 —
                                 # once a slot is real_claimed it is NEVER
                                 # sim-padded again, even if it later drops out.

@dataclass
class PackStatus:
    index: int              # 1-based, matches /Battery/<n>. Bank is 2S only: 1 or 2.
    port: str | None        # e.g. "/dev/ttyUSB0" (real) or "sim:A"/"sim:B" (simulated); None if never seen.
    serial: str | None      # from felicity_reader's register 63492 read (5 regs, decimal-concat string).
    present: bool           # True if this poll cycle got ok=True data (real or, if enabled, simulated).
    simulated: bool         # True only when present via SIMULATE_UNCLAIMED_SLOTS, never once real_claimed.
    real_claimed: bool      # True forever once a real pack has been identified on this slot.
    fail_count: int         # consecutive failed reads since last success; daemon marks present=False at 3.
    data: dict | None       # the felicity_reader.read_pack() dict for this cycle, or None if not present.

def read_mapping(path: str = MAPPING_FILE) -> dict[str, int]:
    """Load serial -> 1-based slot-index map. Missing file -> {}. Never raises."""

def write_mapping(mapping: dict[str, int], path: str = MAPPING_FILE) -> None:
    """Atomic write (tmp file + os.replace). Never raises; logs and no-ops on failure."""

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

def rescan(known: dict[int, PackStatus], **kwargs) -> dict[int, PackStatus]:
    """
    Entry point for the /Redetect button and the periodic rescan timer.
    Identical contract to scan() -- kept as a separate name only so the
    daemon's onchangecallback has an obviously-named thing to call; it is
    not a different algorithm.
    """
```

---

## params.py

```python
def build_bank_params(packs: dict[int, "discovery.PackStatus"]) -> dict:
    """
    Input: discovery.py's PackStatus dict (indices 1, 2).
    Output: ONE normalized dict, ready for dbusservice.py.update(). Keys
    for bank-level fields are the EXACT D-Bus path strings (leading '/')
    the daemon writes verbatim via `srv[key] = value`. Values are None
    where nothing can currently be computed (dbusservice.py must publish
    None as-is, not coerce to 0 -- see PARAM-SPEC.md /Info/* rows).

    {
      "/Connected": int,                        # 1 if >=1 pack present else 0
      "/System/NrOfBatteries": int,              # count of present packs (0-2)
      "/System/NrOfCellsPerBattery": int,        # max(cell_count) across present packs
      "/System/MinCellVoltage": float | None,    # V, real cells only, across BOTH packs
      "/System/MaxCellVoltage": float | None,    # V, real cells only, across BOTH packs
      "/System/MinVoltageCellId": str | None,    # "B{pack}C{cell}", 1-based both
      "/System/MaxVoltageCellId": str | None,
      "/Dc/0/Voltage": float | None,              # V, SUM across present packs (series)
      "/Dc/0/Current": float | None,              # A, AVERAGE across present packs
      "/Dc/0/Power": float | None,                 # W, Voltage * Current
      "/Dc/0/Temperature": float | None,           # degC, MAX of sentinel-filtered temps, both packs
      "/Soc": float | None,                        # %, MIN across present packs
      "/Capacity": float | None,                   # Ah remaining = InstalledCapacity * Soc / 100
      "/InstalledCapacity": float,                  # Ah, constant 100.0 (bank = one pack's capacity)
      "/Info/MaxChargeVoltage": float | None,       # V, SUM of dvcc_max_v across present packs
      "/Info/BatteryLowVoltage": float | None,      # V, SUM of dvcc_min_v across present packs
      "/Info/MaxChargeCurrent": float | None,       # A, MIN of dvcc_max_charge_current
      "/Info/MaxDischargeCurrent": float | None,    # A, MIN of dvcc_max_discharge_current
      "/Io/AllowToCharge": int,                     # 1 if NrOfBatteries > 0 else 0 (undecoded, see spec)
      "/Io/AllowToDischarge": int,                  # same rule as AllowToCharge
      "packs": {
        1: {                                        # key = slot index, both 1 and 2 always present
          "Serial": str | None,
          "Voltage": float | None,                  # V
          "Current": float | None,                  # A
          "Temperature": float | None,               # degC, max(temp_bms, temps[]) for THIS pack only
          "Capacity": float,                          # Ah, constant 100.0 (nameplate)
          "FwVersion": str | None,
          "Status": int,                              # 1 = responding normally, 0 = no data (NOT a decoded BMS fault)
          "Cells": list[float],                        # real cells only, sentinel-filtered, index 0 = Cell/1
          "simulated": bool,
        },
        2: { ... same shape ... },
      },
    }

    Pure function: no D-Bus, no I/O, no hardware access -- takes the dict
    discovery.py already read this cycle and does math only. Never raises;
    a pack with data=None simply contributes nothing to the cross-pack
    aggregates (its slot's "packs" entry is still present, values None).
    """
```

---

## dbusservice.py

```python
from typing import Callable

class FelicityBankDbusService:
    def __init__(
        self,
        bus,                                  # dbus.SystemBus()
        service_name: str,                    # "com.victronenergy.battery.felicity_bank"
        device_instance: int,                 # from Settings, NOT < 512, NOT 276/512/513
        product_id: int,
        product_name: str,
        process_version: str,
        mgmt_connection: str,
        max_cells: int,                       # upper bound for pre-created Cell/<i>/Voltage paths (16)
        on_redetect: Callable[[], None],
        on_request_id: Callable[[int], bool],
    ):
        """
        Wraps velib_python VeDbusService. Creates every path in the schema
        above (see PARAM-SPEC.md for the full path list) with a sane
        initial value (None or 0), THEN calls service.register(). Does not
        read hardware or call discovery.py/params.py itself -- the daemon
        supplies data via update() below.

        Registers exactly two SETTABLE paths:
          /Redetect              (initial 0) -> on_redetect callback
          /Battery/Request/Id    (initial 1) -> on_request_id callback
        """

    def update(self, params: dict) -> None:
        """
        params = the exact dict returned by params.build_bank_params().
        Writes every bank-level key directly: `srv[path] = params[path]`
        for each of the fixed keys listed in params.py's schema. For each
        slot idx in params["packs"], writes `srv[f"/Battery/{idx}/{suffix}"]
        = value` for every scalar key, and
        `srv[f"/Battery/{idx}/Cell/{i}/Voltage"] = v` for i, v in
        enumerate(params["packs"][idx]["Cells"], start=1) (unpopulated
        cell indices up to max_cells keep their prior/None value -- never
        write a sentinel-derived value here, params.py already filtered).
        Wraps all writes in one `with self.service as srv:` block so a GUI
        read never observes a half-updated bank. Never raises out of a
        normal call -- catches and logs any single bad value rather than
        aborting the whole poll cycle.
        """

    def reset_redetect(self) -> None:
        """Sets /Redetect back to 0. The daemon calls this via
        GLib.idle_add(...) AFTER on_redetect() has finished -- never call
        it synchronously from inside the onchangecallback itself, or the
        write re-enters mid-transaction (see dbus-felicity-bank.py history:
        an earlier version had a busy-guard here that could leave /Redetect
        stuck at 1; the fix is idle_add + unconditional reset, not a guard)."""
```

### Callback hook signatures the daemon supplies

```python
def on_redetect() -> None:
    """No args. Daemon triggers an immediate discovery.rescan() + poll
    cycle, then MUST schedule dbusservice.reset_redetect() via
    GLib.idle_add (not called inline). Exceptions are caught and logged by
    the daemon, not by dbusservice.py."""

def on_request_id(new_id: int) -> bool:
    """Return True to accept the write (VeDbusService stores it and echoes
    it back on the next GetValue); return False to reject (value not
    stored). Daemon validates new_id is 1 or 2 (bank is 2S) before
    accepting -- this is a display selector only, it must NOT change any
    aggregation or charge-authority behavior."""
```

---

## Daemon wiring (how the three fit together, for reference only)

```
packs = discovery.scan({})                 # first call, empty prior state
svc = dbusservice.FelicityBankDbusService(..., on_redetect=..., on_request_id=...)
loop:
    packs = discovery.scan(packs)          # or rescan(packs) on the slower timer / on /Redetect
    params = build_bank_params(packs)
    svc.update(params)
```
