# Credits

This project builds on the work of the following open-source projects.

## [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)

The Felicity Modbus register map and protocol constants used by
`aggregator/felicity_reader.py` were derived from this driver's
`bms/felicity.py`. Several bugs found in that driver's Felicity
handling (unfiltered sentinel register values, an unfiltered
temperature average, DVCC current limits collapsing under a poisoned
protection state machine) are documented and fixed in this project's
own independent implementation — see `aggregator/PARAM-SPEC.md`.

## [Louisvdw/dbus-serialbattery](https://github.com/Louisvdw/dbus-serialbattery)

The original project that `mr-manuel/venus-os_dbus-serialbattery` is
maintained from, and the origin of the overall `dbus-serialbattery`
architecture (per-BMS driver modules, a shared `battery.py` base
class, D-Bus publishing conventions) this project's protocol layer was
informed by.

## Victron Energy — velib_python

`aggregator/ext/velib_python/` vendors helper modules
(`vedbus.py`, `settingsdevice.py`, `ve_utils.py`, `logger.py`) from
Victron Energy's `velib_python`, used unmodified to publish a
standards-compliant Victron D-Bus battery service on Venus OS.
