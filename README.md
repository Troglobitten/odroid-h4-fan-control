# ODROID H4 Fan Control

A Python script that controls the fan on an **ODROID H4**. It reads CPU, drive, and NVMe
temperatures, maps the hottest reading through a configurable fan curve, and writes the
result to the fan controller's PWM sysfs node — with fail-safe behavior, optional
overheat shutdown, and bounded journal logging.

---

## Features

- Controls fan speed from **CPU**, **Drive** (best-effort, needs `smartctl`), and **NVMe**
  temperatures — always uses the **hottest** available reading.
- Three fan curves: **linear**, **logarithmic**, **exponential** (`-c lin|log|exp`).
- **Fail-safe:** if *all* sensors are unavailable, the fan runs at **maximum** speed —
  never off — and one `ERROR` is logged on entering that state.
- **Manual mode at startup:** the script sets `pwmN_enable = 1` when the node exists, so
  PWM writes actually take effect instead of being ignored by automatic/thermal control.
- **Overheat protection** (opt-in via `-o`): shuts down at `--overheat-threshold`
  (default **95 °C**, exact comparison, no hidden margin) once it has been reached on
  `--overheat-confirm` consecutive readings (default **3**), so a single glitched
  sensor value cannot power the board off. If the shutdown command fails, the script
  **keeps running** (fan held at max) and retries every cycle.
- **Plausibility filter:** readings outside **−40…150 °C** (uninitialised sensors,
  packed SMART raw values) are discarded with a rate-limited `WARNING`.
- **Hysteresis:** PWM is only rewritten when it changes by more than `--pwm-hysteresis`
  (default **2**), reducing sysfs churn.
- **Bounded logging:** a status line on every PWM/fail-safe state change, plus a
  **30-second heartbeat** while quiet — no per-iteration log spam.
- **Robust sensor probing:** absent or hung sources/devices are cached for **60 s** and
  re-probed afterwards (a dead NVMe or SATA drive can't stall the loop forever).
- Auto-detects the PWM sysfs node (prefers `it8613`/`it87`, re-resolves if it vanishes).
- **Import-safe & testable:** no side effects at import time; **127** pytest cases, all
  hardware I/O mocked; CI runs on every push.

---

## Requirements

- **ODROID H4** with the fan set to **software mode** in the BIOS.
- **Python 3.8+** (`python3`).
- **root** for writing `/sys/class/hwmon` (the systemd unit runs as root; manual runs
  need `sudo`).
- Optional kernel module **it87** if your board needs it (see step 1).
- Optional **nvme-cli** for NVMe temperatures via NVMe SMART.
- Optional **smartmontools** (`smartctl`) for SATA drive temperatures.

> **Note:** Some ODROID H4 boards use the Hardkernel EC instead of `it87`. The script
> auto-detects the PWM node and its `*_enable` file; if your board exposes different
> sysfs paths, see [Troubleshooting](#troubleshooting).

---

## Installation

### Step 1 — Optional: hardware support tools

Only needed if you want NVMe / drive temperatures, or if your board requires the `it87`
module:

```bash
# NVMe + SATA drive temperatures
sudo apt update
sudo apt install nvme-cli smartmontools

# it87 kernel module (only if no hwmon PWM node is present out of the box).
# DKMS install is preferred: the module is rebuilt automatically on every
# kernel update, so fan control survives reboots and upgrades.
sudo apt install dkms build-essential git linux-headers-$(uname -r)
git clone https://github.com/frankcrawford/it87.git
cd it87
sudo ./dkms-install.sh
sudo modprobe it87

# Auto-load at boot (modprobe alone only lasts until the next reboot).
# Without DKMS use `make && sudo make install` instead — but then re-run it
# after every kernel update before modprobe will work again.
echo it87 | sudo tee /etc/modules-load.d/it87.conf
```

### Step 2 — Clone the repository to the service directory

The systemd unit in step 4 expects the script at `/opt/odroid-fan/fan_control.py`, so
clone there:

```bash
sudo mkdir -p /opt/odroid-fan
sudo git clone https://github.com/reserve85/odroid-h4-fan-control.git /opt/odroid-fan
sudo chmod +x /opt/odroid-fan/fan_control.py
```

> Prefer a different location (e.g. your home directory)? Clone anywhere you like and
> adjust `WorkingDirectory=` and `ExecStart=` in the systemd unit from step 4 to match.

### Step 3 — Verify the script manually (before installing the service)

```bash
# 1. Show all options (works without root)
python3 /opt/odroid-fan/fan_control.py --help

# 2. Short test run with EXACTLY the flags the service will use (Ctrl+C stops it).
#    Include -o here: overheat shutdown is only active with -o, so a test run
#    without it cannot reveal a sensor that would power the board off.
sudo python3 /opt/odroid-fan/fan_control.py -v -o -c lin --tmin 40 --tmax 80 --pwm-min 30 --pwm-max 128 --overheat-threshold 95 --pwm-hysteresis 2
```

A healthy run looks like this:

```text
Using PWM sysfs path: /sys/class/hwmon/hwmon2/pwm1
Set /sys/class/hwmon/hwmon2/pwm1_enable = 1 (manual mode; was 2)
fan_control.py started (sleep=2s, curve=lin, overheat=False, ...)
CPU: 44°C  Drive: N/A  NVMe: 38°C  PWM: 74
```

What to check:

| Line | Meaning |
|---|---|
| `Using PWM sysfs path: ...` | A fan controller node was found. |
| `Set ..._enable = 1 (manual mode; ...)` | The script switched the channel to manual control. |
| `No PWM sysfs path found` | No PWM node detected — see [Troubleshooting](#troubleshooting). |
| `CPU: ... PWM: ...` | A status line; PWM should rise when the CPU heats up. |
| `Ignoring implausible temperature ...` | A sensor returns garbage; it is ignored. Note which one. |
| `Overheat reading 1/3 ...` | A reading hit the threshold — **do not install the service** until you know why. |

If everything looks right, press `Ctrl+C` and continue.

### Step 4 — Install the systemd service

Create `/etc/systemd/system/fancontrol.service`:

```ini
[Unit]
Description=ODROID H4 Fan Control Python Service
# Start only after /etc/modules-load.d (it87) has been processed and udev has
# created the hwmon devices; the PWM path is resolved once at startup.
After=systemd-modules-load.service systemd-udev-settle.service local-fs.target
Wants=systemd-modules-load.service
# Stop restarting after 5 failures in 60 s instead of looping forever.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/opt/odroid-fan
ExecStart=/usr/bin/python3 /opt/odroid-fan/fan_control.py -v -o -c lin --tmin 40 --tmax 80 --pwm-min 30 --pwm-max 128 --overheat-threshold 95 --pwm-hysteresis 2
Restart=always
RestartSec=5
# Fan control must write to /sys/class/hwmon, which requires root.
# Additional hardening below restricts everything else.
User=root
ProtectSystem=strict
ReadWritePaths=/sys/class/hwmon
PrivateTmp=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=fancontrol

[Install]
WantedBy=multi-user.target
```

> If you cloned the repository somewhere other than `/opt/odroid-fan`, update
> `WorkingDirectory=` and `ExecStart=` accordingly.

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fancontrol.service
sudo systemctl start fancontrol.service
```

### Step 5 — Verify the service

```bash
# status should be "active (running)"
systemctl status fancontrol.service

# follow the logs
sudo journalctl -u fancontrol.service -f -o short-iso-precise

# watch the PWM value respond to load (in another shell)
watch -n1 'cat /sys/class/hwmon/hwmon*/pwm1'
```

To force the fan up, put some load on the board, e.g.:

```bash
sudo apt install stress-ng
stress-ng --cpu 4 --timeout 60
```

The `pwm1` value should climb while the load runs and fall back afterwards.

### Updating

```bash
sudo systemctl stop fancontrol.service
sudo git -C /opt/odroid-fan pull
sudo systemctl start fancontrol.service
```

(Run `sudo systemctl daemon-reload` first if the unit file changed.)

### Uninstalling

```bash
sudo systemctl disable --now fancontrol.service
sudo rm /etc/systemd/system/fancontrol.service
sudo systemctl daemon-reload
sudo rm -rf /opt/odroid-fan
```

---

## Command Line Options

| Option | Description |
|---|---|
| `-v`, `--verbose` | Enable verbose (DEBUG) logging. |
| `-t`, `--sleep <seconds>` | Sleep interval between checks (default **2**, must be > 0). |
| `-o`, `--overheat` | Enable overheat protection (shutdown when threshold reached). |
| `--overheat-threshold <°C>` | Shutdown threshold (default **95**). Triggers **exactly** at this temperature; must be ≥ 0. |
| `--overheat-confirm <n>` | Consecutive over-threshold readings required before shutdown (default **3**, must be ≥ 1). |
| `--pwm-hysteresis <value>` | Only rewrite PWM when it changes by more than this (default **2**, must be ≥ 0). |
| `-c`, `--curve <lin\|log\|exp>` | Fan curve type (default `lin`). |
| `--pwm-min <value>` | Minimum PWM value (default `20`, range `0..255`). |
| `--pwm-max <value>` | Maximum PWM value (default `255`, range `0..255`). |
| `--tmin <°C>` / `--tmax <°C>` | Temperature range for the curve (default `30`/`80`, `tmin` must be < `tmax`). |

Invalid values (e.g. `--tmin >= --tmax`, `--sleep <= 0`, PWM outside `0..255`,
negative hysteresis or threshold) are rejected with a clean error and **exit code 2**.

---

## Examples

Manual run with custom settings:

```bash
sudo /usr/bin/python3 /opt/odroid-fan/fan_control.py -v -o -c lin --tmin 40 --tmax 80 --pwm-min 30 --pwm-max 128 --overheat-threshold 95
```

Service management and logs:

```bash
sudo systemctl start fancontrol.service
sudo systemctl stop fancontrol.service
sudo journalctl -u fancontrol.service -f -o short-iso-precise
```

## Behavior Notes

- **Temperature sources** (in order): coretemp/hwmon labels `package`/`core` → thermal
  zones (hottest wins) → `sensors` output. NVMe uses `nvme smart-log` with an hwmon
  fallback; drives use `smartctl -A` (best-effort, reported as `N/A` if unavailable).
- **Aggregation:** the hottest reading across CPU/Drive/NVMe drives the curve. If *all*
  sensors are unavailable the script fails safe to **maximum** PWM.
- **PWM write only on change:** the value is rewritten only when the delta exceeds
  `--pwm-hysteresis`; failed writes are retried on the next cycle (the cache only
  advances on success).
- **Manual mode:** at startup the script writes `1` to `<pwm-path>_enable` if that file
  exists and isn't already `1`, logging the change — automatic/thermal firmware control
  would otherwise silently ignore PWM writes.
- **Logging cadence:** a status line on every PWM/fail-safe state change, plus a
  heartbeat every **30 s** while nothing changes; entering fail-safe logs a single
  `ERROR`. With the default 2 s interval, journal volume stays bounded.
- **Sensor probing:** missing binaries/devices and hung probes are cached for **60 s**
  (per device), then re-probed — so a drive added after startup is picked up, and a
  hanging `smartctl`/`nvme` call can't stall every loop.
- **Overheat shutdown** only happens with `-o` set. The first attempt logs `CRITICAL`;
  if the `shutdown` command fails, the loop keeps the fan at max and retries, logging at
  most one `ERROR` per 30 s.
- **Loop robustness:** `sleep` is the last call of every iteration; unhandled
  exceptions are logged and backed off (`max(sleep, 5)` seconds); `Ctrl+C` exits
  cleanly.

---

## Development / Testing

The full `pytest` suite lives under `tests/` — **all hardware I/O (sysfs, subprocess)
is mocked**, so it runs anywhere (Linux, Windows, CI).

```bash
# install the package in editable mode with dev tools (pytest + ruff)
python3 -m pip install -e ".[dev]"

# run the suite (127 tests, ~0.2 s; a 30 s faulthandler guard fails hangs fast)
python3 -m pytest

# lint
ruff check fan_control.py tests/
```

Notable test guarantees:

- `tests/test_import_safety.py` — importing `fan_control` performs **no**
  argparse/sysfs work (the historical cause of endless pytest runs).
- `tests/test_logging.py` — log cadence: forced on state change, 30 s heartbeat,
  exactly one fail-safe `ERROR`.
- `tests/test_pwm_enable.py` — manual-mode setup and PWM path re-resolution.

CI (`.github/workflows/ci.yml`) runs ruff + pytest on every push and pull request
against Python 3.8, 3.11, and 3.13.

## Troubleshooting

**Board powers off / "crashes" during boot as soon as the service is enabled, but the
manual test was fine** — almost always the overheat shutdown (`-o`) firing on a bogus
sensor value at boot; a manual run without `-o` cannot trigger it. Recover by booting
into recovery/maintenance mode and running `systemctl disable fancontrol.service`,
then look at the log of the boot that went down:

```bash
# previous boot (needs a persistent journal: sudo mkdir -p /var/log/journal)
sudo journalctl -b -1 -u fancontrol.service -o short-iso-precise
# was it a clean shutdown or a real crash?
last -x | head
```

`Overheat threshold reached: shutting down (...)` names every sensor value at that
moment. Current versions ignore readings outside −40…150 °C and need
`--overheat-confirm` (default 3) consecutive hits, so a single glitch no longer powers
the board off. Update (`git pull`), re-run step 3 **with `-o`**, then re-enable the
service. If the log shows no fancontrol lines at all before the hang, it is a kernel
problem (usually `it87` with `force_id`): check `journalctl -b -1 -k -p err`.

**`No PWM sysfs path found` in the log** — no fan controller node was detected.
List what the system exposes:

```bash
for h in /sys/class/hwmon/hwmon*; do echo -n "$h: "; cat $h/name 2>/dev/null || echo "(no name)"; ls $h | grep -E '^pwm|^fan' 2>/dev/null || true; done
```

- No `pwm*` entries at all → install/load the `it87` module (step 1), or check that
  the BIOS fan is set to **software mode**.
- Worked before, but no `pwm*` after a reboot or kernel update → `modprobe it87` only
  lasts until reboot unless `/etc/modules-load.d/it87.conf` exists, and a plain
  `make install` module must be rebuilt for each new kernel (the DKMS install in
  step 1 handles both automatically). If `modprobe it87` fails with `No such device`,
  the in-tree driver could not claim the chip — install the DKMS module from step 1,
  or force detection with `sudo modprobe it87 force_id=0x<ID>` using the chip ID
  reported by `sudo sensors-detect`. Always load the module **before** restarting
  the service — the PWM path is resolved once at startup.
- Entries exist but the wrong chip is picked → adjust the `preferred_names` tuple in
  `find_pwm_path()` (`fan_control.py`).

**PWM path found, writes succeed, but the fan speed never changes** — the channel is
likely still under automatic/thermal control. The script forces manual mode at startup:

```text
Set /sys/class/hwmon/hwmon2/pwm1_enable = 1 (manual mode; was 2)
```

If you instead see `Could not set ... to manual mode`, set it manually to see why:

```bash
echo 1 | sudo tee /sys/class/hwmon/hwmon*/pwm1_enable
```

Also verify the BIOS fan setting is **software mode**.

**CPU temperature looks too low / like chassis air** — you are probably on an ACPI
thermal zone instead of `coretemp`. Ensure the `coretemp` driver is loaded
(`cat /sys/class/hwmon/hwmon*/name`) — the script prefers it automatically.

**Drive temperatures always show `N/A`** — install `smartmontools`
(`sudo apt install smartmontools`) and make sure `smartctl` is on `PATH` and the script
runs as root (the service does).

**NVMe temperatures always show `N/A`** — install `nvme-cli` (`sudo apt install nvme-cli`).
Remember missing sources are re-probed after at most **60 s**, not immediately.

**Log says `fail-safe: no readings`** — *every* sensor source is unavailable. The fan is
held at maximum PWM as a precaution. Check the individual sources (previous points) and
run with `-v` for detail.

**Overheat shutdown never happens** — the feature is opt-in: pass `-o`
(`--overheat`). Without it the threshold is ignored by design.

**`Overheat shutdown attempt ... failed`** — the `shutdown` command was not available
or returned an error. The script keeps running with the fan at max and retries; fix the
underlying issue (e.g. systemd/shutdown not present).

---

## Credits

Based on the ODROID wiki article *Fan Speed Control with Temperature* and the original
shell script. Ported to Python and adapted locally. YMMV.
