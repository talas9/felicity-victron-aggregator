#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
felicity_reader.py -- data layer for reading Felicity FLA-series LiFePO4 packs
over RS485 Modbus RTU (function code 0x03 only).

This module is READ-ONLY toward the hardware: it never issues a write function
code. It builds Modbus frames by hand (struct + a local CRC-16/MODBUS
implementation) and does not depend on pymodbus.

Protocol constants below are taken verbatim from the dbus-serialbattery
`felicity.py` driver (register addresses, scaling factors) -- see README.md
for the register table and the reasoning behind the sentinel-filtering and
temperature-register decisions.

This module does NOT publish to D-Bus. It only reads packs and returns plain
dicts. The D-Bus publishing layer is a separate, parallel piece of work.
"""

from __future__ import annotations

import struct
import time
import random
from typing import Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - reported at call time, not import time
    serial = None


# --------------------------------------------------------------------------
# Protocol constants (from felicity.py; register addresses are decimal here,
# frame-building uses the raw big-endian register/count bytes as the source
# of truth -- see README.md "4921 vs 4929" section for why these values were
# picked over the driver's own inline comments).
# --------------------------------------------------------------------------

SLAVE_ADDRESS_DEFAULT = 1
FUNCTION_READ = 0x03  # the ONLY function code this module will ever send

REG_CELL_VOLTAGES = 4906        # 16 registers, mV as uint16, /1000 = V
REG_CELL_VOLTAGES_COUNT = 16

REG_TOTAL_V_I = 4870            # 2 registers: V (uint16/100), I (int16/10, sign-flipped)
REG_TOTAL_V_I_COUNT = 2

REG_SOC = 4875                  # 1 register, raw uint16 (%)
REG_SOC_COUNT = 1

REG_STATUS = 4866               # 3 registers, status/fault bitmasks
REG_STATUS_COUNT = 3

# --------------------------------------------------------------------------
# Register 4866/4868 decode (status/fault bitmap). Source: FAULT-DECODE.md
# in this directory, itself sourced verbatim from the maintained
# dbus-serialbattery `bms/felicity.py` driver. Reg 4866 (status, offset 0
# of this 3-register/6-byte block) carries the BMS's own MOSFET-enable
# flags; reg 4868 (fault, offset 4) carries named hard-fault bits.
#
# MATERIAL CAVEAT (see FAULT-DECODE.md "safe_to_implement_now" /
# "uncertainty"): felicity.py was verified against model LPBF48250 fw418.
# This box's pack is FLA12100 -- a different Felicity model. Same-family
# register reuse is plausible but UNVERIFIED. That is why fet_charge/
# fet_discharge below are surfaced as OBSERVATIONAL data only (never wired
# into /Io/AllowToCharge or /Io/AllowToDischarge -- see params.py). The 7
# named fault bits are considered higher-confidence (verbatim from the
# merged driver source) and ARE wired into /Alarms/* (annunciate-only,
# never gates charge/discharge), per the FAULT-DECODE.md recommendation.
#
# Unknown bits (reg 4867 entirely; reg 4868 bits 0,1,7,10-15) are never
# asserted as a fault -- fail-safe direction is silence, not a spurious
# alarm or a false do-not-charge.
REG_STATUS_FET_CHARGE_BIT = 0b0000000000000001      # reg 4866 bit0
REG_STATUS_FET_DISCHARGE_BIT = 0b0000000000000100   # reg 4866 bit2

FAULT_BIT_MAP = {
    # fault_flags key                    reg 4868 bit
    "high_cell_voltage": 0b0000000000000100,          # bit2
    "low_cell_voltage": 0b0000000000001000,           # bit3
    "high_charge_current": 0b0000000000010000,        # bit4
    "high_discharge_current": 0b0000000000100000,     # bit5
    "high_internal_temperature": 0b0000000001000000,  # bit6 (BMS board temp)
    "high_charge_temperature": 0b0000000100000000,    # bit8
    "low_charge_temperature": 0b0000001000000000,     # bit9
}


def _decode_status(status_data: bytes) -> dict:
    """Decode the 6-byte (3-register) 4866 status/fault block into
    observational fields. Never raises -- a malformed/short buffer yields
    all-unknown (None/all-False) rather than an exception, since this is
    diagnostic data and must never be able to take down a read_pack() call.

    Sentinel handling (0x7FFF / SENTINEL_INT16 -- "register not populated",
    same convention filtered elsewhere in this file for cells/temps/DVCC):
    a sentinel is NOT real status data and must never be bitmasked into an
    alarm or FET observation. 0x7FFF has bits 2,3,4,5,6,8,9 ALL set, which
    happen to be exactly the bits FAULT_BIT_MAP treats as named hard faults
    -- bitmasking it directly would fire all 7 /Alarms/* at once and latch
    them, even though this is a SUCCESSFUL read of unpopulated data (so it
    bypasses the CRC/timeout/short-frame guard entirely). If reg 4868
    (fault) reads as sentinel it is treated as "no fault reported"
    (fault_int forced to 0 before bitmasking, all /Alarms/* clear). If reg
    4866 (status) reads as sentinel, the FET bits are UNKNOWN, not "FETs
    off" or "FETs on" -- fet_charge_observed/fet_discharge_observed stay
    None rather than being inferred from a sentinel. This sentinel path is
    deliberately separate from the except-path below: that path handles a
    failed/short READ (struct.unpack error), this path handles a
    successful read that legitimately came back "not populated"."""
    try:
        status_int = _u16(status_data, 0)   # reg 4866
        fault_int = _u16(status_data, 4)    # reg 4868 (3rd register of the block)
    except Exception:
        return {
            "fet_charge_observed": None,
            "fet_discharge_observed": None,
            "fault_flags": {name: False for name in FAULT_BIT_MAP},
        }

    if status_int == SENTINEL_INT16:
        fet_charge_observed = None
        fet_discharge_observed = None
    else:
        fet_charge_observed = bool(status_int & REG_STATUS_FET_CHARGE_BIT)
        fet_discharge_observed = bool(status_int & REG_STATUS_FET_DISCHARGE_BIT)

    if fault_int == SENTINEL_INT16:
        # A sentinel must NEVER produce an alarm -- force to "no fault".
        fault_int = 0

    return {
        "fet_charge_observed": fet_charge_observed,
        "fet_discharge_observed": fet_discharge_observed,
        "fault_flags": {name: bool(fault_int & bit) for name, bit in FAULT_BIT_MAP.items()},
    }

REG_DVCC = 4892                 # 4 registers: maxV, minV (/100), maxChg, maxDis (/10)
REG_DVCC_COUNT = 4

REG_BMS_MOS_TEMP = 4874         # 1 register, int16, no scaling (deg C)
REG_BMS_MOS_TEMP_COUNT = 1

# Open question settled against the live unit (see README.md): the driver's
# byte literal b"\x13\x39\x00\x05" decodes to register 4921, NOT the 4929
# claimed in its comment. Reading against the live pack confirms 4921 is
# correct: offsets 1 and 2 of this 5-register block (registers 4922, 4923)
# yield plausible ~37 C readings that match the driver's own published
# MOSTemperature; offset 3 (register 4924) returns the 0x7FFF sentinel,
# consistent with this being a 3rd temperature sensor that isn't physically
# wired on a pack that only breaks out 2 of them. Register 4921 itself
# (offset 0) is read but never assigned by the driver -- an apparent driver
# bug/leftover, not evidence for 4929.
REG_TEMPS_1_3 = 4921            # 5 registers; index 0 unused by driver, 1/2/3 = temp1/2/3
REG_TEMPS_1_3_COUNT = 5

REG_FIRMWARE = 63499            # 1 register
REG_FIRMWARE_COUNT = 1

REG_SERIAL = 63492              # 5 registers, concatenated as decimal string
REG_SERIAL_COUNT = 5

SENTINEL_INT16 = 0x7FFF         # "register not populated" for this pack variant


# --------------------------------------------------------------------------
# CRC-16/MODBUS (poly 0xA001, init 0xFFFF), appended little-endian.
# --------------------------------------------------------------------------

def _crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)


def _build_frame(address: int, register: int, count: int) -> bytes:
    """Build a Modbus RTU read-holding-registers (0x03) request frame."""
    body = struct.pack(">BBHH", address & 0xFF, FUNCTION_READ, register, count)
    return body + _crc16_modbus(body)


class _ModbusError(Exception):
    pass


def _read_registers(ser, address: int, register: int, count: int, timeout: float) -> bytes:
    """
    Send one 0x03 read request and return the raw data payload bytes
    (the register data only, CRC already validated and stripped).
    Raises _ModbusError with a descriptive message on any failure.
    Never sends anything other than function code 0x03.
    """
    frame = _build_frame(address, register, count)
    ser.reset_input_buffer()
    ser.write(frame)

    expected_len = 3 + (count * 2) + 2  # addr + func + bytecount + data + crc
    deadline = time.monotonic() + timeout
    resp = b""
    while len(resp) < expected_len and time.monotonic() < deadline:
        chunk = ser.read(expected_len - len(resp))
        if not chunk:
            continue
        resp += chunk

    if len(resp) < 3:
        raise _ModbusError(
            f"timeout/short response reading register {register} "
            f"(got {len(resp)} bytes, wanted {expected_len})"
        )

    resp_addr, resp_func, byte_count = struct.unpack(">BBB", resp[:3])

    if resp_addr != (address & 0xFF):
        raise _ModbusError(f"address mismatch: sent {address}, got {resp_addr}")

    if resp_func != FUNCTION_READ:
        if resp_func & 0x80:
            raise _ModbusError(f"Modbus exception response (func=0x{resp_func:02x}) reading register {register}")
        raise _ModbusError(f"unexpected function code 0x{resp_func:02x} reading register {register}")

    if len(resp) < 3 + byte_count + 2:
        raise _ModbusError(
            f"incomplete frame reading register {register}: "
            f"declared {byte_count} data bytes but only got {len(resp) - 5} usable"
        )

    data = resp[3:3 + byte_count]
    crc_received = resp[3 + byte_count: 3 + byte_count + 2]
    crc_calc = _crc16_modbus(resp[0:3 + byte_count])

    if crc_received != crc_calc:
        raise _ModbusError(
            f"CRC mismatch reading register {register}: "
            f"received {crc_received.hex()}, calculated {crc_calc.hex()}"
        )

    if byte_count != count * 2:
        raise _ModbusError(
            f"byte count mismatch reading register {register}: "
            f"expected {count * 2}, got {byte_count}"
        )

    return data


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def _empty_result(port: str, error: str) -> dict:
    return {
        "ok": False,
        "error": error,
        "port": port,
        "cells": [],
        "cell_count": 0,
        "cell_min": None,
        "cell_max": None,
        "cell_spread_mv": None,
        "voltage": None,
        "current": None,
        "soc": None,
        "temp_bms": None,
        "temps": [],
        "serial": None,
        "firmware": None,
        "dvcc_max_v": None,
        "dvcc_min_v": None,
        "dvcc_max_charge_current": None,
        "dvcc_max_discharge_current": None,
        "fet_charge_observed": None,
        "fet_discharge_observed": None,
        "fault_flags": {name: False for name in FAULT_BIT_MAP},
        "raw": {},
        "simulated": False,
    }


def read_pack(port: str, address: int = 1, timeout: float = 1.0) -> dict:
    """
    Read one Felicity pack over RS485 Modbus and return a combined dict.

    Never raises. Never hangs past `timeout` per register read. Any failure
    (open error, timeout, CRC mismatch, malformed frame) results in
    ok=False with a descriptive error and empty/None data fields.

    Only function code 0x03 (read holding registers) is ever sent.
    """
    if port == "SIM" or port.startswith("sim:"):
        return _simulate_pack(port)

    if serial is None:
        return _empty_result(port, "pyserial not installed (import serial failed)")

    ser = None
    raw: dict = {}
    try:
        try:
            ser = serial.Serial(
                port=port,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=timeout,
            )
        except Exception as exc:
            return _empty_result(port, f"failed to open serial port: {exc}")

        try:
            cell_data = _read_registers(ser, address, REG_CELL_VOLTAGES, REG_CELL_VOLTAGES_COUNT, timeout)
            raw[REG_CELL_VOLTAGES] = cell_data.hex()

            raw_cells_mv = [_u16(cell_data, i * 2) for i in range(REG_CELL_VOLTAGES_COUNT)]
            cells = [v / 1000.0 for v in raw_cells_mv if v != SENTINEL_INT16]
            cell_count = len(cells)

            vi_data = _read_registers(ser, address, REG_TOTAL_V_I, REG_TOTAL_V_I_COUNT, timeout)
            raw[REG_TOTAL_V_I] = vi_data.hex()
            voltage = _u16(vi_data, 0) / 100.0
            current = (_s16(vi_data, 2) / 10.0) * -1

            soc_data = _read_registers(ser, address, REG_SOC, REG_SOC_COUNT, timeout)
            raw[REG_SOC] = soc_data.hex()
            soc = _u16(soc_data, 0)

            status_data = _read_registers(ser, address, REG_STATUS, REG_STATUS_COUNT, timeout)
            raw[REG_STATUS] = status_data.hex()
            status_decoded = _decode_status(status_data)

            temp_bms_data = _read_registers(ser, address, REG_BMS_MOS_TEMP, REG_BMS_MOS_TEMP_COUNT, timeout)
            raw[REG_BMS_MOS_TEMP] = temp_bms_data.hex()
            temp_bms = float(_s16(temp_bms_data, 0))

            temps_data = _read_registers(ser, address, REG_TEMPS_1_3, REG_TEMPS_1_3_COUNT, timeout)
            raw[REG_TEMPS_1_3] = temps_data.hex()
            # offset 0 (register 4921) is read but not used by the reference
            # driver either -- kept in raw[] for inspection, not surfaced.
            raw_temps = [_s16(temps_data, i * 2) for i in (1, 2, 3)]
            temps = [float(t) for t in raw_temps if t != SENTINEL_INT16]

            # DVCC limits (register 4892, 4 registers): maxV/minV are pack-level
            # total-voltage limits (/100, same scaling as REG_TOTAL_V_I's voltage
            # field), maxChg/maxDis are current limits (/10). Added for the bank
            # aggregator (dbus-felicity-bank.py) so it can publish real
            # /Info/MaxChargeCurrent, /Info/MaxDischargeCurrent, /Info/MaxChargeVoltage,
            # /Info/BatteryLowVoltage instead of the reference driver's sentinel-
            # poisoned zeros. Assumed unsigned (uint16) per the register comment --
            # the comment does not call out a sign flip the way REG_TOTAL_V_I's
            # current field does, so no sign flip is applied here.
            dvcc_data = _read_registers(ser, address, REG_DVCC, REG_DVCC_COUNT, timeout)
            raw[REG_DVCC] = dvcc_data.hex()
            _raw_max_v = _u16(dvcc_data, 0)
            _raw_min_v = _u16(dvcc_data, 2)
            _raw_max_chg = _u16(dvcc_data, 4)
            _raw_max_dis = _u16(dvcc_data, 6)
            dvcc_max_v = None if _raw_max_v == SENTINEL_INT16 else _raw_max_v / 100.0
            dvcc_min_v = None if _raw_min_v == SENTINEL_INT16 else _raw_min_v / 100.0
            dvcc_max_charge_current = None if _raw_max_chg == SENTINEL_INT16 else _raw_max_chg / 10.0
            dvcc_max_discharge_current = None if _raw_max_dis == SENTINEL_INT16 else _raw_max_dis / 10.0

            fw_data = _read_registers(ser, address, REG_FIRMWARE, REG_FIRMWARE_COUNT, timeout)
            raw[REG_FIRMWARE] = fw_data.hex()
            firmware = str(_u16(fw_data, 0))

            serial_data = _read_registers(ser, address, REG_SERIAL, REG_SERIAL_COUNT, timeout)
            raw[REG_SERIAL] = serial_data.hex()
            serial_number = "".join(str(_u16(serial_data, i * 2)) for i in range(REG_SERIAL_COUNT))

        except _ModbusError as exc:
            return _empty_result(port, str(exc))
        except Exception as exc:  # belt-and-braces: never raise out of read_pack
            return _empty_result(port, f"unexpected error: {exc}")

    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    if cell_count == 0:
        return _empty_result(port, "all cell registers read as sentinel (0x7FFF) -- no populated cells")

    cell_min = min(cells)
    cell_max = max(cells)

    return {
        "ok": True,
        "error": None,
        "port": port,
        "cells": cells,
        "cell_count": cell_count,
        "cell_min": cell_min,
        "cell_max": cell_max,
        "cell_spread_mv": round((cell_max - cell_min) * 1000, 1),
        "voltage": voltage,
        "current": current,
        "soc": float(soc),
        "temp_bms": temp_bms,
        "temps": temps,
        "serial": serial_number,
        "firmware": firmware,
        "dvcc_max_v": dvcc_max_v,
        "dvcc_min_v": dvcc_min_v,
        "dvcc_max_charge_current": dvcc_max_charge_current,
        "dvcc_max_discharge_current": dvcc_max_discharge_current,
        "fet_charge_observed": status_decoded["fet_charge_observed"],
        "fet_discharge_observed": status_decoded["fet_discharge_observed"],
        "fault_flags": status_decoded["fault_flags"],
        "raw": raw,
        "simulated": False,
    }


def read_all(ports: list[str], address: int = 1, timeout: float = 1.0) -> list[dict]:
    """
    Read every pack in `ports`, in order. One bad/absent port never fails
    the whole call -- that entry gets ok=False, the rest still read.
    """
    results = []
    for port in ports:
        try:
            results.append(read_pack(port, address=address, timeout=timeout))
        except Exception as exc:  # read_pack should never raise, but be paranoid
            results.append(_empty_result(port, f"read_all caught unexpected exception: {exc}"))
    return results


# --------------------------------------------------------------------------
# Simulation mode
# --------------------------------------------------------------------------

# Baseline captured non-invasively from the live pack on ttyUSB0 via its
# running dbus-serialbattery D-Bus service (com.victronenergy.battery.ttyUSB0)
# on 2026-07-20, 16:2x -- NOT read directly off the serial bus (that port is
# held by dbus-serialbattery and was not disturbed). Used only to seed
# realistic SIM values.
_SIM_BASELINE = {
    "cells": [3.343, 3.425, 3.376, 3.378],
    "voltage": 13.52,
    "current": -0.1,
    "soc": 99.0,
    "temp_bms": 37.0,
    "temps": [37.0, 37.0],  # 3rd sensor unpopulated (sentinel) on this pack too
    "firmware": "20250729",
    "serial": "SIM0000000000001",
    # DVCC limits were never read from the live pack for this baseline (that
    # register was added to the reader after the baseline capture) -- these
    # are plausible placeholder values for a 4S 100Ah LiFePO4 pack, not a
    # captured reading. Clearly simulated=True downstream either way.
    "dvcc_max_v": 14.6,
    "dvcc_min_v": 12.0,
    "dvcc_max_charge_current": 50.0,
    "dvcc_max_discharge_current": 50.0,
}


def _simulate_pack(port: str) -> dict:
    """
    Return realistic synthetic pack data for development/testing before the
    second RS485 adapter arrives. Clearly flagged simulated=True everywhere
    it matters so it can never be mistaken for a real reading.
    """
    rng = random.Random(port)  # deterministic per SIM port label, still varies run-to-run via time jitter
    jitter = random.Random()

    cells = [round(v + jitter.uniform(-0.006, 0.006), 3) for v in _SIM_BASELINE["cells"]]
    cell_min = min(cells)
    cell_max = max(cells)
    voltage = round(sum(cells) + jitter.uniform(-0.05, 0.05), 2)
    current = round(_SIM_BASELINE["current"] + jitter.uniform(-0.3, 0.3), 2)
    soc = max(0.0, min(100.0, round(_SIM_BASELINE["soc"] + jitter.uniform(-1.5, 1.5), 1)))
    temp_bms = round(_SIM_BASELINE["temp_bms"] + jitter.uniform(-1.0, 1.0), 1)
    temps = [round(t + jitter.uniform(-1.0, 1.0), 1) for t in _SIM_BASELINE["temps"]]

    suffix = port.split(":", 1)[1] if ":" in port else "A"

    return {
        "ok": True,
        "error": None,
        "port": port,
        "cells": cells,
        "cell_count": len(cells),
        "cell_min": cell_min,
        "cell_max": cell_max,
        "cell_spread_mv": round((cell_max - cell_min) * 1000, 1),
        "voltage": voltage,
        "current": current,
        "soc": soc,
        "temp_bms": temp_bms,
        "temps": temps,
        "serial": f"SIM-{suffix}-0000000000001",
        "firmware": "SIM-0.0.0",
        "dvcc_max_v": _SIM_BASELINE["dvcc_max_v"],
        "dvcc_min_v": _SIM_BASELINE["dvcc_min_v"],
        "dvcc_max_charge_current": _SIM_BASELINE["dvcc_max_charge_current"],
        "dvcc_max_discharge_current": _SIM_BASELINE["dvcc_max_discharge_current"],
        # A simulated pack is always "healthy": both FETs observed enabled,
        # no fault bits set. Never real 4866/4868 register data.
        "fet_charge_observed": True,
        "fet_discharge_observed": True,
        "fault_flags": {name: False for name in FAULT_BIT_MAP},
        "raw": {"__simulated__": "no real registers were read"},
        "simulated": True,  # <-- unmistakable flag
    }


# --------------------------------------------------------------------------
# __main__: read given ports (default: real port + SIM) and pretty-print
# both packs side by side.
# --------------------------------------------------------------------------

def _fmt(v, width=7, prec=3):
    if v is None:
        return " " * width
    return f"{v:>{width}.{prec}f}"


def _print_packs(results: list[dict]):
    max_cells = max((r["cell_count"] for r in results), default=0)

    print()
    header = "  ".join(f"{('SIM ' if r['simulated'] else '') + r['port']:<28}" for r in results)
    print(header)
    print("-" * len(header))

    for r in results:
        if not r["ok"]:
            print(f"[{r['port']}] ERROR: {r['error']}")

    print()
    for i in range(max_cells):
        row = []
        for r in results:
            if r["ok"] and i < len(r["cells"]):
                row.append(f"Cell{i+1}: {_fmt(r['cells'][i])} V")
            else:
                row.append(" " * 18)
        print("  ".join(row))

    print()
    fields = [
        ("Cell count", lambda r: str(r["cell_count"])),
        ("Cell min (V)", lambda r: _fmt(r["cell_min"])),
        ("Cell max (V)", lambda r: _fmt(r["cell_max"])),
        ("Spread (mV)", lambda r: _fmt(r["cell_spread_mv"], prec=1)),
        ("Pack V", lambda r: _fmt(r["voltage"], prec=2)),
        ("Current (A)", lambda r: _fmt(r["current"], prec=2)),
        ("SoC (%)", lambda r: _fmt(r["soc"], prec=1)),
        ("Temp BMS (C)", lambda r: _fmt(r["temp_bms"], prec=1)),
        ("Temps (C)", lambda r: str(r["temps"])),
        ("Serial", lambda r: str(r["serial"])),
        ("Firmware", lambda r: str(r["firmware"])),
        ("Simulated", lambda r: str(r["simulated"])),
    ]
    for label, fn in fields:
        row = "  ".join(f"{(fn(r) if r['ok'] else '-'):<28}" for r in results)
        print(f"{label:<14} {row}")
    print()


if __name__ == "__main__":
    import sys

    argv_ports = sys.argv[1:]
    if not argv_ports:
        argv_ports = ["/dev/ttyUSB0", "SIM"]

    packs = read_all(argv_ports)
    _print_packs(packs)
