# Felicity Modbus register map (from driver source)

Source: `bms/felicity.py` in
`github.com/mr-manuel/venus-os_dbus-serialbattery`, the open-source
driver this project's protocol layer was originally derived from.

**This is an early survey of the driver source, not the final word —**
see `aggregator/PARAM-SPEC.md` and `aggregator/README.md` for the
confirmed, corrected register map (including the driver's own
temperature-register comment being wrong: the working code uses 4921,
not 4929-4931) and the fixes applied on top of it.

Protocol: Modbus RTU, function code `0x03` (read holding registers).
CRC: CRC-16, polynomial `0xA001`.

| Data | Register | Details |
|---|---|---|
| Cell voltages | `4906` | 16 registers × 2 bytes each, value ÷ 1000 (V). `cell_count` is hardcoded to 16 in the driver — not read dynamically from the pack. |
| Total pack voltage / current | `4870` | |
| SOC | `4875` | |
| Status / faults | `4866` | |
| DVCC limits | `4892` | |
| Temperatures | `4874`, and `4929`-`4931` | |
| Firmware / serial number | `\xf8\x0b` and `\xf8\x04` | Non-standard register addressing (raw command bytes), as implemented in the driver source, not translated to decimal here. |

## Confidence level — read this before trusting the map

This map is transcribed from working driver code that is used against
real Felicity hardware in the field, so it is credible. However:

- **No report exists for the FLA12100 specifically.** The community
  reports backing this driver's Felicity support cover a *different*
  Felicity SKU, not the FLA12100.
- The `cell_count = 16` hardcoding may or may not match the FLA12100's
  actual cell count — this needs to be checked against the FLA12100
  spec/nameplate before assuming 16 cell-voltage registers is correct
  for this pack.

**Bottom line: credible, but this survey alone does not confirm the
FLA12100 specifically.** The working `aggregator/` code and its own
verification against a live FLA12100 pack (see `PARAM-SPEC.md`) is the
real confirmation, not this document.
