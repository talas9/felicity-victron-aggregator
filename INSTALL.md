# Installation guide

This walks you through installing the Felicity bank aggregator on a
Victron Cerbo GX (or any Venus OS GX device), from an empty out-of-the-box
device to a running, reboot-proof service. It assumes you have never used
SSH before but are comfortable following steps carefully. Every command
below is generic — replace placeholders like `<your-device-ip>` with your
own values. Nothing here contains anyone else's IP address, password, or
hardware serial number; those are yours to fill in.

If you get stuck, re-read the step slowly before moving on — most
problems here come from skipping a step, not from something being wrong
with your hardware.

## 1. What you'll need

- A **Victron Cerbo GX**, or any other Venus OS GX device (Cerbo GX,
  Cerbo-S GX, Venus GX, Ekrano GX, a Raspberry Pi running Venus OS, etc).
  This guide uses "the GX device" throughout.
- Venus OS installed and the device already on your network (Ethernet or
  WiFi), showing up normally in the VRM Portal or on its own local
  display/Remote Console. Any reasonably current Venus OS release works;
  this project was built and tested on a current 2026 release.
- **Two** USB-to-RS485 adapters — one per battery pack. This project was
  built and tested with the **DSD TECH SH-U11F** (FTDI FT232R chipset);
  any similar FTDI-based USB-RS485 adapter should work the same way.
  You need two separate adapters, not one adapter shared between packs
  (see below for why).
- The RJ45 wiring described in `PINOUT.md` in this repository — a custom
  cable per pack that breaks out pins 5/6 (RS485-A/B) to your USB-RS485
  adapter, alongside whatever CAN wiring you already have to the GX
  device. Read `PINOUT.md` before you wire anything; it documents the
  full pin table and the wiring gotchas (no dedicated ground pin, single
  shared communication socket, etc).
- Both Felicity packs, each wired to its **own** USB-RS485 adapter.

### Why each pack needs its own adapter

Both Felicity packs answer to the same fixed Modbus address, `0x01` —
there's no DIP switch or setting to change it. Two devices on one RS485
bus that both claim the same address can't be told apart, so they can't
share a single bus or a single adapter. Giving each pack its own
adapter (and therefore its own bus) sidesteps the addressing conflict
entirely — the aggregator talks to each adapter separately and combines
the two readings in software.

## 2. Enable SSH / root access on Venus OS

Venus OS ships locked down: by default there's no SSH access and no
root password. You unlock this from the GX device's own screen (or its
Remote Console), not remotely. The steps below are Victron's current
official procedure — see
[Venus OS: Root Access](https://www.victronenergy.com/live/ccgx:root_access)
for the source; don't skip ahead based on memory of an older Venus OS
version, the exact button sequence matters.

1. On the GX device (or in Remote Console), go to **Settings → General**
   and set **Access Level** to **User and installer**. The password for
   this level is `ZZZ`.
2. Back out to the **General** page so that **Access Level** is
   highlighted but not opened (don't go into the Access Level selection
   screen itself — just have it highlighted on the General page).
3. Unlock Superuser:
   - **Physical GX device with a button pad:** press and hold the
     **right button** of the center pad until Access Level changes to
     **Superuser**.
   - **Remote Console, classic UI:** use the right arrow key on your
     keyboard the same way.
   - **Remote Console / touchscreen, new UI:** select, drag down, and
     hold down the entire Access & Security menu list for **five
     seconds** until it changes to Superuser.
4. With Superuser unlocked, go to **Settings → General → Set root
   password** and create your own root password (this is the password
   you'll use to SSH in — pick something you'll remember, it's local to
   this device). Note: a firmware update resets this password, so you
   may need to set it again after updating Venus OS.
5. Still in **Settings → General**, enable **SSH on LAN**. (On Venus OS
   versions older than v2.40, this option is called "Remote Support"
   instead — enabling it also enables SSH.)
6. Find the device's IP address: go to **Settings → Ethernet** (or
   **Settings → WiFi** if it's connected wirelessly) and note the IP
   address shown for the active connection. If you can't find it there,
   the VRM Portal's device page also shows it, or check your router's
   list of connected devices.

You only need to do this once per device — the SSH/root setup persists
across reboots (though, as noted above, a firmware update will reset the
root password specifically, not the SSH-enabled setting).

## 3. Connect over SSH

From a computer on the same network as the GX device, open a terminal
and run:

```sh
ssh root@<your-device-ip>
```

Replace `<your-device-ip>` with the address you found in step 2.6. The
first time you connect you'll be asked to confirm the device's SSH key
fingerprint — type `yes`. Then enter the root password you set in step
2.4.

You should land at a root shell prompt on the GX device, something like
`root@einstein:~#` (the name after `@` is your device's own hostname,
not a fixed value).

Everything from here on happens inside this SSH session, unless a step
says otherwise.

**Why `/data`:** Venus OS's writable filesystem is split — most of it is
overwritten wholesale by every firmware update, but `/data` is the one
partition that survives updates untouched. That's why this whole
install lives under `/data` rather than somewhere like `/opt` — a
firmware update would otherwise silently delete it.

## 4. Installing the aggregator

### 4.1 Get the code onto the device

If your GX device has internet access and `git` available:

```sh
mkdir -p /data/rs485-cells
cd /data/rs485-cells
git clone https://github.com/talas9/felicity-victron-aggregator.git .
```

If the device has no internet access, or `git` isn't available on your
Venus OS version, copy the repository over from your own computer
instead, from a second terminal (not the SSH session):

```sh
scp -r /path/to/felicity-victron-aggregator/. root@<your-device-ip>:/data/rs485-cells/
```

Either way, you should end up with `/data/rs485-cells/aggregator/`
containing `dbus-felicity-bank.py` and the other files from this
repository.

### 4.2 Plug in the two USB-RS485 adapters

Plug both USB-RS485 adapters into the GX device's USB ports, each wired
to one Felicity pack per `PINOUT.md`. Confirm both are detected:

```sh
dmesg | tail -20
```

You should see two new `ttyUSB` devices appear (typically
`/dev/ttyUSB0` and `/dev/ttyUSB1`, though the exact numbers can vary).
You can also list them directly:

```sh
ls -l /dev/ttyUSB*
```

### 4.3 Stop Venus OS from claiming the adapters (the udev rule)

By default, Venus OS's `serial-starter` service inspects every new
serial device and tries to claim it for its own auto-detection (as a
VE.Direct device, a GPS, another supported battery driver, and so on).
Since these two adapters are for the aggregator, not for Venus OS's own
drivers, you need to tell `serial-starter` to leave them alone. This is
done with a udev rule that tags each adapter (by its own unique USB
serial number) with `VE_SERVICE=ignore`.

First, find each adapter's USB serial number:

```sh
udevadm info -a -n /dev/ttyUSB0 | grep -m1 '{serial}'
udevadm info -a -n /dev/ttyUSB1 | grep -m1 '{serial}'
```

Each will print something like `ATTRS{serial}=="A50285BI"` — write down
both values (they'll be different for each adapter — that's how the
rule tells them apart).

Create the udev rule file:

```sh
nano /etc/udev/rules.d/99-felicity-rs485.rules
```

Add one line per adapter, using the serial numbers you just found (this
example uses two placeholder serials — replace both with yours):

```
SUBSYSTEM=="tty", ATTRS{serial}=="ADAPTER_1_SERIAL", ENV{VE_SERVICE}="ignore"
SUBSYSTEM=="tty", ATTRS{serial}=="ADAPTER_2_SERIAL", ENV{VE_SERVICE}="ignore"
```

Save and exit (in `nano`: Ctrl+O, Enter, then Ctrl+X), then reload the
rules and reconnect the adapters so they pick it up:

```sh
udevadm control --reload-rules
udevadm trigger
```

Unplugging and replugging both adapters (or rebooting) guarantees the
new rule is applied cleanly.

### 4.4 Work out which pack is on which adapter

You don't need to hardcode this — the aggregator identifies each pack
by its own serial number (via `discovery.py`) and remembers the
mapping in `aggregator/pack_mapping.json`, which starts out empty
(`{}`) and fills in automatically the first time each pack is seen.
`/dev/ttyUSB0` and `/dev/ttyUSB1` are allowed to swap around on reboot
— the mapping is keyed on pack serial, not port name, so this doesn't
matter.

If you want to sanity-check readings from each adapter before starting
the service proper, you can run the reader module directly:

```sh
cd /data/rs485-cells/aggregator
python3 felicity_reader.py /dev/ttyUSB0 /dev/ttyUSB1
```

This prints both packs' live readings side by side (per-cell voltages,
pack voltage/current/SoC, temperatures) without touching D-Bus — useful
for confirming both adapters are actually talking to a pack before you
move on. Press Ctrl+C to stop it.

## 5. Setting up the service

Venus OS uses **daemontools** to supervise long-running services: each
service is a directory containing a `run` script that daemontools
executes and restarts if it exits. We create that structure once,
inside `/data/rs485-cells` (so it survives firmware updates), then
point Venus OS at it.

### 5.1 Check the service directory structure

The `run` scripts daemontools needs are already shipped in this
repository — `service/run` and `service/log/run` — so once you've
cloned or copied the repo onto the device (4.1) there's nothing to
create here. `service/run` starts the aggregator daemon; `service/log/run`
mirrors the standard daemontools/multilog pattern used by Venus OS
services in general and by the upstream `dbus-serialbattery` project
this aggregator builds on (see `CREDITS.md`).

Confirm both are present and point at the path you actually cloned
into:

```sh
cat /data/rs485-cells/service/run
cat /data/rs485-cells/service/log/run
```

If you cloned or copied the repo somewhere other than
`/data/rs485-cells`, edit `service/run`'s `cd` line to match before
continuing.

Both scripts ship executable already, but git can lose the executable
bit depending on how you transferred the repo (e.g. some `scp`/zip
flows). Verify and fix it if needed:

```sh
ls -l /data/rs485-cells/service/run /data/rs485-cells/service/log/run
chmod +x /data/rs485-cells/service/run /data/rs485-cells/service/log/run
```

### 5.2 Link it into `/service` — and why that alone isn't enough

`/service` is where daemontools' `svscan` looks for services to
supervise. On Venus OS, `/service` is a **tmpfs** — a RAM-backed
filesystem that starts empty on every boot and gets rebuilt from the
`/opt/victronenergy/service` registry that ships with the firmware.
That registry doesn't know about our aggregator, so if we only create a
symlink by hand right now, it works immediately but **disappears on the
next reboot**, along with any manual edit to `/opt` (which firmware
updates overwrite anyway).

Create the symlink now, so the service starts immediately without
waiting for a reboot:

```sh
ln -s /data/rs485-cells/service /service/dbus-felicity-bank
```

Within a few seconds `svscan` should notice the new symlink and start
supervising it.

### 5.3 Make it survive a reboot: the `/data/rc.local` hook

`/data/rc.local` is a script Venus OS runs late in its own boot
sequence — specifically, after `svscan` is already running — every
single boot, and (like everything else under `/data`) it survives
firmware updates. We use it to recreate the `/service` symlink every
time the device starts, since the symlink itself gets wiped along with
the rest of `/service`'s tmpfs on every boot.

Create or edit `/data/rc.local`:

```sh
nano /data/rc.local
```

Add these lines (if the file already has other content, append to it,
don't replace it):

```sh
#!/bin/sh
ln -sf /data/rs485-cells/service /service/dbus-felicity-bank
chmod +x /data/rs485-cells/service/run
chmod +x /data/rs485-cells/service/log/run
```

Make it executable:

```sh
chmod +x /data/rc.local
```

Now a reboot re-creates the symlink automatically, `svscan` picks it up
again, and the service comes back up on its own — no manual step
needed after a reboot or a firmware update.

## 6. Verifying it's working

### 6.1 Check the service is running

```sh
svstat /service/dbus-felicity-bank
```

A healthy service reports something like `up (pid 1234) 45 seconds`. If
it instead shows a very low uptime that keeps resetting (e.g. `up ...
0 seconds` every time you check), it's crash-looping — see
Troubleshooting below.

You can also tail its log directly:

```sh
tail -f /var/log/dbus-felicity-bank/current
```

Press Ctrl+C to stop watching.

### 6.2 Check both packs are being read

```sh
dbus -y com.victronenergy.battery.felicity_bank / GetValue
```

This dumps every path the aggregator publishes. Look for `/Dc/0/Voltage`,
`/Dc/0/Current`, `/Soc`, and the per-pack paths under `/Battery/0/...`
and `/Battery/1/...` — with both adapters connected and both packs
responding, you should see two distinct, sane sets of readings (not one
pack's data duplicated, and not placeholder/simulated values — those
are only used when a slot has never had a real pack connected).

You can also query a single value directly, e.g.:

```sh
dbus -y com.victronenergy.battery.felicity_bank /Soc GetValue
```

### 6.3 Check it shows up on the GX device and in VRM

On the GX device's own display (or Remote Console), go to the device
list — you should see a new battery entry, "Felicity Bank (2S)"
(or similar), alongside your other connected devices (inverter/charger,
etc). Give it a few minutes and check the VRM Portal online — the same
battery should appear there too, with live voltage/current/SoC.

## 7. Troubleshooting

**An adapter isn't showing up as `/dev/ttyUSB*` at all**
Try a different USB port, and check `dmesg | tail -20` right after
plugging it in — a completely silent `dmesg` usually means a cable or
port problem rather than a software one. Confirm it's genuinely an
FTDI-chipset adapter (or at least one whose driver is already built
into Venus OS) — some cheap RS485 adapters use chipsets Venus OS
doesn't have a driver for.

**One pack is missing (readings for only one `/Battery/N/...` present, or one is stuck on simulated data)**
- Re-check the RJ45 wiring for that specific pack against `PINOUT.md` —
  RS485-A/B (pins 5/6) is easy to get swapped or miswired given there's
  no dedicated ground pin.
- Run `python3 felicity_reader.py /dev/ttyUSB0 /dev/ttyUSB1` directly
  (see 4.4) to see if that pack answers at all outside of the full
  service — this isolates whether it's a wiring/adapter problem or a
  service/discovery problem.
- Double check the udev rule serial numbers (4.3) actually match the
  adapter that pack is wired to — if you mixed up which serial belongs
  to which physical adapter, `serial-starter` may still be grabbing the
  port for its own use instead of leaving it free for the aggregator.

**Both packs read fine manually, but the service shows nothing / crashes**
Check the log: `tail -50 /var/log/dbus-felicity-bank/current`. Common
causes: a typo in `service/run` (re-check the exact content in 5.1), the
`run` script not being executable (`chmod +x` again), or the repository
path not actually being `/data/rs485-cells/aggregator` (if you cloned
or copied it somewhere else, `service/run`'s `cd` line needs to match).

**Service works now, but is gone after a reboot**
This means the `/data/rc.local` hook (5.3) either isn't present, isn't
executable, or has a typo. After a reboot, check:

```sh
ls -l /service/dbus-felicity-bank
```

If that symlink is missing, `/data/rc.local` didn't run correctly —
re-check `chmod +x /data/rc.local` and the exact file content. You can
also test it without rebooting by running `/data/rc.local` by hand and
then checking `svstat /service/dbus-felicity-bank` again.

**SSH stopped working after a firmware update**
Expected — a firmware update resets the root password (noted in step
2.4). Repeat step 2 (Superuser unlock → set root password → confirm SSH
on LAN is still enabled) and you're back in. Your `/data` install itself
is untouched by the update.
