#!/usr/bin/env python3
"""
fan_control.py - ODROID H4 Fan Control
Features:
- CLI params: -t/--sleep (default 2), -v, -o, -c (lin|log|exp), pwm/t thresholds,
  --overheat-threshold, --pwm-hysteresis
- Robust CPU/NVMe/Drive detection (thermal, hwmon, sensors, nvme-cli, smartctl);
  absent/hung sources are cached with a 60 s TTL and re-probed afterwards
- Auto-detect PWM sysfs (prefers it8613/it87); resolved lazily, not at import,
  re-resolved if the path vanishes; PWM channel forced to manual mode at startup
- Writes PWM only when changed (hysteresis measured against the last value
  actually written, so slow drifts are never lost)
- On exit (SIGTERM/Ctrl+C) the fan is handed back to automatic control or set
  to full speed - never left frozen at the last manual value
- Emergencies (all sensors unavailable, overheat threshold reached with -o,
  unhandled loop error) -> fan at full speed (PWM 255), ignoring --pwm-max
- smartctl / nvme-cli results are cached for 30 s (no process spawn per loop)
- Logging: status line on PWM/fail-safe state change + 30 s heartbeat;
  entering fail-safe logs one ERROR, not one per iteration
- Overheat shutdown: retried each cycle until the system actually goes down
- sleep() is last call in main loop
- Import-safe: no argparse/sysfs side effects at module import time
"""

from __future__ import annotations

import argparse
import glob
import logging
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, Literal, Optional, Sequence, Tuple

# -------------------------
# Types
# -------------------------
TempReading = Optional[int]
CurveMethod = Literal["lin", "log", "exp"]

LOG_REPORT_INTERVAL = 30.0  # seconds between heartbeat status lines
SUBPROCESS_TIMEOUT = 5  # seconds
MISSING_CACHE_TTL = 60.0  # seconds before an "absent source" is re-probed
# Drive/NVMe temperatures come from external tools (smartctl, nvme-cli) and
# change slowly; their results are reused for this long instead of spawning
# a process per device on every loop iteration.
DRIVE_TEMP_CACHE_TTL = 30.0
# Hardware maximum. Used for emergencies regardless of --pwm-max, which only
# limits the normal curve (typically for noise).
PWM_FULL = 255
# Readings outside this window are sensor glitches (e.g. a packed SMART raw
# value or an uninitialised sensor at boot), never real temperatures. They are
# discarded so a bogus value can neither drive the curve nor trigger an
# overheat shutdown.
TEMP_PLAUSIBLE_MIN = -40
TEMP_PLAUSIBLE_MAX = 150


@dataclass(frozen=True)
class FanConfig:
    sleep: int
    overheat: bool
    curve: CurveMethod
    pwm_min: int
    pwm_max: int
    tmin: int
    tmax: int
    overheat_threshold: int = 95
    pwm_hysteresis: int = 2
    verbose: bool = False
    overheat_confirm: int = 3

    def validate(self) -> FanConfig:
        if self.tmin >= self.tmax:
            raise ValueError(f"--tmin ({self.tmin}) must be < --tmax ({self.tmax})")
        if self.pwm_min > self.pwm_max:
            raise ValueError(f"--pwm-min ({self.pwm_min}) must be <= --pwm-max ({self.pwm_max})")
        if self.pwm_min < 0 or self.pwm_max > 255:
            raise ValueError(
                f"PWM values must be within 0..255 "
                f"(got pwm_min={self.pwm_min}, pwm_max={self.pwm_max})"
            )
        if self.sleep <= 0:
            raise ValueError(f"--sleep must be > 0 (got {self.sleep})")
        if self.pwm_hysteresis < 0:
            raise ValueError(f"--pwm-hysteresis must be >= 0 (got {self.pwm_hysteresis})")
        if self.overheat_threshold < 0:
            raise ValueError(
                f"--overheat-threshold must be >= 0 (got {self.overheat_threshold})"
            )
        if self.overheat_confirm < 1:
            raise ValueError(
                f"--overheat-confirm must be >= 1 (got {self.overheat_confirm})"
            )
        return self


@dataclass
class SensorSnapshot:
    cpu: TempReading = None
    drive: TempReading = None
    nvme: TempReading = None

    @classmethod
    def from_readings(
        cls, cpu: TempReading, drive: TempReading, nvme: TempReading
    ) -> SensorSnapshot:
        return cls(cpu=cpu, drive=drive, nvme=nvme)

    @property
    def max_temp(self) -> TempReading:
        """Hottest reading in °C, or None iff *all* sensors are unavailable."""
        readings = [r for r in (self.cpu, self.drive, self.nvme) if r is not None]
        return max(readings) if readings else None


@dataclass
class LoopDecision:
    pwm: int
    should_write: bool
    should_shutdown: bool
    status: str
    fail_safe: bool = False
    full_speed: bool = False  # emergency: write PWM_FULL, bypassing --pwm-max


# -------------------------
# Logging (no side effects until configured)
# -------------------------
logger = logging.getLogger("fancontrol")


# -------------------------
# CLI
# -------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ODROID H4 fan control")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "-t",
        "--sleep",
        type=int,
        default=2,
        help="Sleep duration between checks in seconds (default: 2)",
    )
    parser.add_argument("-o", "--overheat", action="store_true", help="Enable overheat protection")
    parser.add_argument(
        "-c",
        "--curve",
        type=str,
        default="lin",
        choices=["lin", "log", "exp"],
        help="PWM method (lin|log|exp)",
    )
    parser.add_argument("--pwm-min", type=int, default=20, help="Minimum PWM value")
    parser.add_argument("--pwm-max", type=int, default=255, help="Maximum PWM value")
    parser.add_argument(
        "--tmin", type=int, default=30, help="Temperature (°C) at which PWM starts increasing"
    )
    parser.add_argument(
        "--tmax", type=int, default=80, help="Temperature (°C) at which PWM reaches max"
    )
    parser.add_argument(
        "--overheat-threshold",
        type=int,
        default=95,
        help="Temperature (°C) at which overheat shutdown triggers (default: 95)",
    )
    parser.add_argument(
        "--overheat-confirm",
        type=int,
        default=3,
        help="Consecutive over-threshold readings required before shutdown (default: 3)",
    )
    parser.add_argument(
        "--pwm-hysteresis",
        type=int,
        default=2,
        help="Rewrite PWM only when the new value differs by more than this (default: 2)",
    )
    return parser


def parse_cli(argv: Optional[Sequence[str]] = None) -> FanConfig:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = FanConfig(
        sleep=args.sleep,
        overheat=args.overheat,
        curve=args.curve,
        pwm_min=args.pwm_min,
        pwm_max=args.pwm_max,
        tmin=args.tmin,
        tmax=args.tmax,
        overheat_threshold=args.overheat_threshold,
        pwm_hysteresis=args.pwm_hysteresis,
        verbose=args.verbose,
        overheat_confirm=args.overheat_confirm,
    )
    try:
        return config.validate()
    except ValueError as exc:
        parser.error(str(exc))  # exits with code 2 and a clean message
        raise  # unreachable; keeps type-checkers happy


# -------------------------
# Helpers: file read/write
# -------------------------
def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r") as f:
            s = f.read().strip()
            if not s:
                return None
            return int(float(s))
    except (OSError, ValueError):
        return None


# -------------------------
# Temperature readers
# -------------------------
def scale_temp(raw: int, *, millidegrees: bool) -> int:
    """Convert a raw sensor value to whole °C by source contract (#12).

    hwmon/thermal_zone inputs are always millidegrees → always divide by 1000.
    Sources that already report whole degrees (e.g. parsed `sensors` output)
    pass ``millidegrees=False``.
    """
    if millidegrees:
        return raw // 1000
    return raw


_implausible_logged: Dict[str, float] = {}  # source -> monotonic time of last warning


def plausible(value: TempReading, source: str) -> TempReading:
    """Return ``value`` if it is a physically plausible °C reading, else None.

    Implausible values are logged at WARNING at most once per
    LOG_REPORT_INTERVAL per source, so a permanently broken sensor is visible
    without flooding the journal.
    """
    if value is None:
        return None
    if TEMP_PLAUSIBLE_MIN <= value <= TEMP_PLAUSIBLE_MAX:
        return value
    now = time.monotonic()
    last = _implausible_logged.get(source)
    if last is None or now - last >= LOG_REPORT_INTERVAL:
        logger.warning(
            "Ignoring implausible temperature %s°C from %s (valid range %s..%s°C)",
            value,
            source,
            TEMP_PLAUSIBLE_MIN,
            TEMP_PLAUSIBLE_MAX,
        )
        _implausible_logged[source] = now
    return None


# Memoizes "path/source absent" probes so NVMe/sensors/smartctl aren't
# respawned every loop iteration (#9). Entries expire after MISSING_CACHE_TTL
# so a transient failure (or a device that appears later) is re-probed instead
# of being disabled for the process lifetime (M2/M3).
_missing_path_cache: Dict[str, float] = {}  # key -> monotonic expiry time


def _probe_missing(key: str) -> bool:
    """Return True if ``key`` is known-absent and its cache entry is fresh."""
    expiry = _missing_path_cache.get(key)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        del _missing_path_cache[key]
        return False
    return True


def _mark_missing(key: str) -> None:
    """Cache ``key`` as absent for MISSING_CACHE_TTL seconds."""
    _missing_path_cache[key] = time.monotonic() + MISSING_CACHE_TTL


# source key -> (monotonic expiry, reading). Holds the last result of the
# subprocess-based readers, including None, for DRIVE_TEMP_CACHE_TTL.
_reading_cache: Dict[str, Tuple[float, TempReading]] = {}


def _cached_reading(key: str, probe: Callable[[], TempReading]) -> TempReading:
    """Return the cached result for ``key`` or run ``probe`` and cache it."""
    now = time.monotonic()
    hit = _reading_cache.get(key)
    if hit is not None and now < hit[0]:
        return hit[1]
    value = probe()
    _reading_cache[key] = (now + DRIVE_TEMP_CACHE_TTL, value)
    return value


def read_cpu_temp() -> TempReading:
    """
    Prefer coretemp / hwmon CPU sensors (Package/Core). Fallbacks: thermal_zone,
    sensors output. Returns int °C or None.
    """
    # 1) prefer hwmon entries with name 'coretemp' or labels 'package'/'core'
    best: Optional[int] = None
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = ""
        try:
            with open(os.path.join(hw, "name")) as f:
                name = f.read().strip().lower()
        except OSError:
            name = ""
        if "coretemp" in name or "core" in name or "package" in name:
            # Hottest across all matching labeled inputs (#13).
            labeled_values = []
            for tfile in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
                label = ""
                label_file = tfile.replace("_input", "_label")
                try:
                    with open(label_file) as lf:
                        label = lf.read().strip().lower()
                except OSError:
                    label = ""
                if label and ("package" in label or "core" in label):
                    v = _read_int(tfile)
                    if v is None:
                        continue
                    t = plausible(scale_temp(v, millidegrees=True), tfile)
                    if t is not None:
                        labeled_values.append(t)
            if labeled_values:
                candidate = max(labeled_values)
                if best is None or candidate > best:
                    best = candidate
                continue
            # no labeled temps: fall back to the hottest temp*_input found
            for tfile in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
                v = _read_int(tfile)
                if v is None:
                    continue
                candidate = plausible(scale_temp(v, millidegrees=True), tfile)
                if candidate is None:
                    continue
                if best is None or candidate > best:
                    best = candidate

    if best is not None:
        return best

    # 2) fallback: thermal_zone (acpitz etc.) — hottest zone wins, so a cool
    #    chassis sensor cannot mask a hot package (L3)
    zone_best: Optional[int] = None
    for p in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        v = _read_int(p)
        if v is None:
            continue
        candidate = plausible(scale_temp(v, millidegrees=True), p)
        if candidate is None:
            continue
        if zone_best is None or candidate > zone_best:
            zone_best = candidate
    if zone_best is not None:
        return zone_best

    # 3) sensors command as last resort (whole degrees, not millidegrees)
    if not _probe_missing("sensors"):
        try:
            out = subprocess.check_output(
                ["sensors"], stderr=subprocess.DEVNULL, text=True, timeout=SUBPROCESS_TIMEOUT
            )
            for line in out.splitlines():
                if "package id" in line.lower() or re.search(r"core\s+\d+", line.lower()):
                    m = re.search(r"(-?\d+(\.\d+)?)\s*°C", line)
                    if m:
                        t = plausible(round(float(m.group(1))), "sensors")
                        if t is not None:
                            return t
            for line in out.splitlines():
                m = re.search(r"(-?\d+(\.\d+)?)\s*°C", line)
                if m:
                    t = plausible(round(float(m.group(1))), "sensors")
                    if t is not None:
                        return t
        except (OSError, subprocess.SubprocessError):
            _mark_missing("sensors")

    logger.debug("read_cpu_temp: no CPU temperature source found")
    return None


def _parse_nvme_temp(out: str) -> TempReading:
    """Extract the composite temperature in °C from ``nvme smart-log`` output.

    Only the line whose key is exactly ``temperature`` counts (not e.g.
    ``Warning Temperature Time``). Handles both ``38 C (311 Kelvin)`` and
    ``311 K (38 °C)`` styles by preferring an explicit Celsius value.
    """
    for line in out.splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != "temperature":
            continue
        m = re.search(r"(-?\d+)\s*°?\s*C\b", value)
        if m:
            return int(m.group(1))
        m = re.search(r"(-?\d+)\s*K(?:elvin)?\b", value)
        if m:
            return int(m.group(1)) - 273
        m = re.search(r"(-?\d+)", value)
        if m:
            return int(m.group(1))
    return None


def _parse_smartctl_temp(out: str) -> TempReading:
    """Extract a drive temperature in °C from ``smartctl -A`` output.

    ATA attribute rows look like::

        194 Temperature_Celsius 0x0022 036 045 000 Old_age Always - 36 (Min/Max 20/45)

    The temperature is the *first* integer of the RAW_VALUE column (field 10),
    not the last number on the line (that would be the Max, or a packed
    48-bit raw value of billions of °C). Attribute 194 is preferred over 190.
    SCSI/SAS output (``Current Drive Temperature: 36 C``) is also handled.
    """
    candidates: Dict[str, int] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[0].isdigit() and "temp" in fields[1].lower():
            m = re.match(r"(-?\d+)", fields[9])
            if m:
                candidates.setdefault(fields[0], int(m.group(1)))
            continue
        low = line.strip().lower()
        if low.startswith(("current drive temperature", "temperature:")):
            m = re.search(r"(-?\d+)", line.split(":", 1)[-1])
            if m:
                candidates.setdefault("text", int(m.group(1)))
    for key in ("194", "190", "text"):
        if key in candidates:
            return candidates[key]
    return next(iter(candidates.values()), None)


def _probe_nvme_cli_temp() -> TempReading:
    """Hottest composite temperature across all NVMe devices via nvme-cli."""
    temps = []

    # Try all NVMe namespaces via nvme-cli; cache absence of devices (#9) and
    # per-device timeouts so a hung device does not stall every loop (M3).
    if not _probe_missing("nvme_devices"):
        devs = sorted(glob.glob("/dev/nvme*n1"))
        if not devs:
            _mark_missing("nvme_devices")
        for dev in devs:
            if _probe_missing(f"nvme:{dev}"):
                continue
            try:
                out = subprocess.check_output(
                    ["nvme", "smart-log", dev],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT,
                )
                t = plausible(_parse_nvme_temp(out), f"nvme smart-log {dev}")
                if t is not None:
                    temps.append(t)
            except subprocess.TimeoutExpired:
                _mark_missing(f"nvme:{dev}")
            except (OSError, subprocess.SubprocessError):
                pass

    return max(temps) if temps else None


def read_nvme_temp() -> TempReading:
    """NVMe temperature: nvme-cli (cached for DRIVE_TEMP_CACHE_TTL), else hwmon."""
    cli = _cached_reading("nvme_cli", _probe_nvme_cli_temp)
    if cli is not None:
        return cli

    temps = []
    # Fallback: hwmon (always millidegrees per source contract)
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = ""
        try:
            with open(os.path.join(hw, "name")) as f:
                name = f.read().strip().lower()
        except OSError:
            name = ""

        if "nvme" not in name:
            continue

        for f in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            v = _read_int(f)
            if v is None:
                continue
            t = plausible(scale_temp(v, millidegrees=True), f)
            if t is not None:
                temps.append(t)

    return max(temps) if temps else None


def read_drive_temp() -> TempReading:
    """Drive temperature via smartctl, cached for DRIVE_TEMP_CACHE_TTL."""
    return _cached_reading("drive", _probe_drive_temp)


def _probe_drive_temp() -> TempReading:
    """Best-effort drive temperature via ``smartctl -A`` on /dev/sd* (#17).

    Returns None when smartctl is unavailable, no drive is present, or no
    temperature line is found. Absence of devices/missing binary is cached so
    the probe does not respawn every loop iteration (#9).
    """
    if _probe_missing("smartctl") or _probe_missing("sd_devices"):
        return None

    devs = sorted(glob.glob("/dev/sd[a-z]"))
    if not devs:
        _mark_missing("sd_devices")
        return None

    temps = []
    for dev in devs:
        if _probe_missing(f"sd:{dev}"):
            continue
        try:
            # -n standby: never spin up a sleeping disk just to read its
            # temperature (smartctl then exits 2 without attribute output).
            out = subprocess.check_output(
                ["smartctl", "-A", "-n", "standby", dev],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except FileNotFoundError:
            _mark_missing("smartctl")
            return None
        except subprocess.TimeoutExpired:
            _mark_missing(f"sd:{dev}")  # hung device: back off for one TTL (M3)
            continue
        except subprocess.CalledProcessError as exc:
            # smartctl's exit status is a bit mask: e.g. bit 6 ("error log
            # contains errors") is set permanently on many healthy-enough
            # drives although the attribute table was printed fine. Parse
            # whatever output there is instead of dropping the reading.
            out = exc.output if isinstance(exc.output, str) else ""
        except (OSError, subprocess.SubprocessError):
            continue

        t = plausible(_parse_smartctl_temp(out), f"smartctl {dev}")
        if t is not None:
            temps.append(t)

    return max(temps) if temps else None

# -------------------------
# PWM detection and write
# -------------------------
_pwm_path_cache: Optional[str] = None
_pwm_path_resolved = False


def get_pwm_path() -> Optional[str]:
    """Lazy accessor: resolve PWM sysfs path on first call, not at import (#5).

    Re-probes whenever no path is known yet (driver loaded after the daemon
    started, e.g. early in boot) or a previously detected path has vanished
    (driver reload), so neither case can disable fan control for the
    lifetime of the process (L2).
    """
    global _pwm_path_cache, _pwm_path_resolved
    if (
        not _pwm_path_resolved
        or _pwm_path_cache is None
        or not os.path.exists(_pwm_path_cache)
    ):
        _pwm_path_cache = find_pwm_path()
        _pwm_path_resolved = True
    return _pwm_path_cache


def find_pwm_path(
    preferred_names: Tuple[str, ...] = ("it8613", "it87", "w836", "nct", "ec", "odroid")
) -> Optional[str]:
    # prefer hwmon entries with matching name
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = ""
        try:
            with open(os.path.join(hw, "name")) as f:
                name = f.read().strip().lower()
        except OSError:
            name = ""
        if any(p in name for p in preferred_names):
            for pwm in sorted(glob.glob(os.path.join(hw, "pwm[0-9]*"))):
                if os.path.isfile(pwm):
                    return pwm
    # fallback: first pwm* anywhere
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        for pwm in sorted(glob.glob(os.path.join(hw, "pwm[0-9]*"))):
            if os.path.isfile(pwm):
                return pwm
    return None


# enable-file path -> value found before this daemon first touched it
_pwm_original_enable: Dict[str, int] = {}


def release_pwm(pwm_path: Optional[str], *, full_speed: bool = False) -> None:
    """Leave the fan in a safe state when the daemon stops.

    Without this the channel stays in manual mode frozen at the last PWM
    value (possibly pwm_min) with nothing controlling it any more. Preferred:
    restore the enable value found at startup (hand control back to the
    chip's automatic mode). If the channel was already manual, or the restore
    fails, or ``full_speed`` is requested (overheat shutdown), write PWM 255.
    """
    if not pwm_path:
        return
    enable_path = pwm_path + "_enable"
    original = _pwm_original_enable.get(enable_path)
    if not full_speed and original is not None and original != 1:
        try:
            with open(enable_path, "w") as f:
                f.write(str(original))
            logger.info("Restored %s = %s on exit", enable_path, original)
            return
        except OSError as exc:
            logger.warning("Could not restore %s = %s: %s", enable_path, original, exc)
    try:
        with open(pwm_path, "w") as f:
            f.write("255")
        logger.info("Set %s = 255 (full speed) on exit", pwm_path)
    except OSError as exc:
        logger.warning("Could not set %s to full speed on exit: %s", pwm_path, exc)


def ensure_manual_pwm_mode(pwm_path: str) -> bool:
    """Force the PWM channel into manual mode at startup (M4).

    Many chip drivers default a pwm channel to an automatic/thermal mode in
    which writes to ``pwmN`` are silently ignored. Best-effort: returns True
    when there is nothing to change (no enable file, already manual, or the
    current value is not readable); False only when a needed write failed.
    """
    enable_path = pwm_path + "_enable"
    current = _read_int(enable_path)
    if current is None:
        logger.debug(
            "No readable pwm enable file at %s; skipping manual-mode setup", enable_path
        )
        return True
    _pwm_original_enable.setdefault(enable_path, current)
    if current == 1:
        return True
    try:
        with open(enable_path, "w") as f:
            f.write("1")
        logger.info("Set %s = 1 (manual mode; was %s)", enable_path, current)
        return True
    except OSError as exc:
        logger.warning(
            "Could not set %s to manual mode (was %s): %s — "
            "fan may stay under firmware/thermal control",
            enable_path,
            current,
            exc,
        )
        return False


def write_pwm(value: int, config: FanConfig, path: Optional[str]) -> bool:
    """Write a PWM value clamped to --pwm-min..--pwm-max. Returns success (#7)."""
    return _write_pwm_raw(int(max(min(value, config.pwm_max), config.pwm_min)), path)


def write_pwm_full(path: Optional[str]) -> bool:
    """Emergency write: PWM_FULL, deliberately not limited by --pwm-max."""
    return _write_pwm_raw(PWM_FULL, path)


def _write_pwm_raw(v: int, path: Optional[str]) -> bool:
    if path:
        try:
            with open(path, "w") as f:
                f.write(str(v))
            logger.debug("Wrote PWM %s to %s", v, path)
            return True
        except PermissionError as exc:
            logger.warning("Permission denied writing PWM to %s: %s", path, exc)
            return False
        except FileNotFoundError as exc:
            logger.warning("PWM path vanished: %s: %s", path, exc)
            return False
        except OSError as exc:
            logger.warning("Failed to write PWM to %s: %s", path, exc)
            return False
    else:
        logger.debug("write_pwm(%s) called (no sysfs path)", v)
        return True


def _shutdown() -> bool:
    """Initiate system shutdown; returns True only if the command was
    accepted (H2). The loop keeps running (fan at pwm_max) and retries on
    failure instead of exiting while the board is still hot."""
    try:
        os.sync()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["shutdown", "-h", "now"], check=False, timeout=SUBPROCESS_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Failed to initiate shutdown: %s", exc)
        return False
    if result.returncode != 0:
        logger.error("shutdown command exited with code %s", result.returncode)
        return False
    return True

# -------------------------
# PWM curves
# -------------------------
def compute_pwm_linear(temp: Optional[int], config: FanConfig) -> int:
    if temp is None:
        return config.pwm_min
    if config.tmin >= config.tmax:  # defensive (#4)
        return config.pwm_min
    if temp <= config.tmin:
        return config.pwm_min
    if temp >= config.tmax:
        return config.pwm_max
    ratio = (temp - config.tmin) / float(config.tmax - config.tmin)
    return int(config.pwm_min + ratio * (config.pwm_max - config.pwm_min))


def compute_pwm_log(temp: Optional[int], config: FanConfig) -> int:
    if temp is None:
        return config.pwm_min
    if config.tmin >= config.tmax:  # defensive (#4)
        return config.pwm_min
    x = max(0.0, min(1.0, (temp - config.tmin) / float(config.tmax - config.tmin)))
    pwm = int(
        config.pwm_min + (math.log1p(x * 9) / math.log1p(9)) * (config.pwm_max - config.pwm_min)
    )
    return max(config.pwm_min, min(config.pwm_max, pwm))


def compute_pwm_exp(temp: Optional[int], config: FanConfig) -> int:
    if temp is None:
        return config.pwm_min
    if config.tmin >= config.tmax:  # defensive (#4)
        return config.pwm_min
    x = max(0.0, min(1.0, (temp - config.tmin) / float(config.tmax - config.tmin)))
    pwm = int(config.pwm_min + (x**2) * (config.pwm_max - config.pwm_min))
    return max(config.pwm_min, min(config.pwm_max, pwm))


def compute_pwm_from_temp(t: TempReading, method: CurveMethod, config: FanConfig) -> int:
    """Dispatch a single (already aggregated) temperature onto a curve (L5)."""
    if method == "log":
        return compute_pwm_log(t, config)
    if method == "exp":
        return compute_pwm_exp(t, config)
    return compute_pwm_linear(t, config)


def compute_pwm(
    cpu: TempReading, drive: TempReading, nvme: TempReading, method: CurveMethod, config: FanConfig
) -> int:
    readings = [r for r in (cpu, drive, nvme) if r is not None]
    t = max(readings) if readings else None
    return compute_pwm_from_temp(t, method, config)

# -------------------------
# Loop seam (testable)
# -------------------------
@dataclass
class LoopDeps:
    read_cpu: Callable[[], TempReading]
    read_nvme: Callable[[], TempReading]
    read_drive: Callable[[], TempReading]
    write_pwm_fn: Callable[[int, FanConfig, Optional[str]], bool]
    shutdown_fn: Callable[[], bool]
    sleep_fn: Callable[[float], None]
    clock: Callable[[], float]
    pwm_path: Optional[str]
    # Real wiring only (None in tests): re-resolves the sysfs path when the
    # detected node vanishes mid-run (L2).
    refresh_path: Optional[Callable[[], Optional[str]]] = None
    # Real wiring only: called once when the loop ends, with True when an
    # overheat shutdown was sent (-> full speed instead of automatic mode).
    release_fn: Optional[Callable[[bool], None]] = None
    # Emergency writer (PWM_FULL, not clamped to --pwm-max). When None, the
    # value PWM_FULL is passed to write_pwm_fn instead (test fakes).
    write_full_fn: Optional[Callable[[Optional[str]], bool]] = None

    def write_full(self, config: FanConfig, path: Optional[str]) -> bool:
        if self.write_full_fn is not None:
            return self.write_full_fn(path)
        return self.write_pwm_fn(PWM_FULL, config, path)

    @classmethod
    def real(cls) -> LoopDeps:
        return cls(
            read_cpu=read_cpu_temp,
            read_nvme=read_nvme_temp,
            read_drive=read_drive_temp,
            write_pwm_fn=write_pwm,
            shutdown_fn=_shutdown,
            sleep_fn=time.sleep,
            # monotonic: wall-clock jumps (NTP sync after boot, no RTC
            # battery) must not disturb heartbeat/rate-limit timing
            clock=time.monotonic,
            pwm_path=get_pwm_path(),
            refresh_path=get_pwm_path,
            release_fn=lambda full_speed: release_pwm(get_pwm_path(), full_speed=full_speed),
            write_full_fn=write_pwm_full,
        )


@dataclass
class LoopState:
    last_pwm: Optional[int] = None
    last_log_time: float = float("-inf")  # first iteration always logs
    last_state_key: Optional[Tuple[int, bool]] = None  # (pwm, fail_safe)
    shutdown_sent: bool = False
    shutdown_attempts: int = 0
    last_shutdown_error_log: float = float("-inf")
    overheat_streak: int = 0  # consecutive over-threshold iterations


def decide(snapshot: SensorSnapshot, config: FanConfig, *, last_pwm: Optional[int]) -> LoopDecision:
    """Pure decision function: fail-safe, curve, hysteresis, overheat (#2/#3/#14)."""
    max_temp = snapshot.max_temp
    cpu_str = f"{snapshot.cpu}°C" if snapshot.cpu is not None else "N/A"
    drive_str = f"{snapshot.drive}°C" if snapshot.drive is not None else "N/A"
    nvme_str = f"{snapshot.nvme}°C" if snapshot.nvme is not None else "N/A"

    if max_temp is None:
        # Fail-safe: all sensors unavailable -> full speed, not off (#3).
        # PWM_FULL, not --pwm-max: that limit is for noise in normal
        # operation, not for running blind.
        status = (
            f"CPU: {cpu_str}  Drive: {drive_str}  NVMe: {nvme_str}  "
            f"PWM: {PWM_FULL} (fail-safe: no readings)"
        )
        return LoopDecision(
            pwm=PWM_FULL,
            should_write=last_pwm != PWM_FULL,
            should_shutdown=False,
            status=status,
            fail_safe=True,
            full_speed=True,
        )

    pwm = compute_pwm_from_temp(max_temp, config.curve, config)

    # Overheat at the exact threshold — no hidden margin (#2).
    should_shutdown = config.overheat and max_temp >= config.overheat_threshold
    if should_shutdown:
        # Full speed from the first over-threshold reading on, i.e. already
        # while the shutdown is still waiting for --overheat-confirm.
        status = (
            f"CPU: {cpu_str}  Drive: {drive_str}  NVMe: {nvme_str}  "
            f"PWM: {PWM_FULL} (overheat: full speed)"
        )
        return LoopDecision(
            pwm=PWM_FULL,
            should_write=last_pwm != PWM_FULL,
            should_shutdown=True,
            status=status,
            fail_safe=False,
            full_speed=True,
        )

    # Hysteresis: rewrite only when the target differs from the value that
    # was last *written* by more than the threshold (#14). Inside the band
    # the applied value is held (and reported), so the reference never
    # drifts with unwritten targets. The curve end points always snap, so
    # the fan reliably reaches pwm_min / pwm_max.
    at_end_point = pwm in (config.pwm_min, config.pwm_max)
    if last_pwm is None or (at_end_point and pwm != last_pwm):
        should_write = True
    else:
        should_write = abs(pwm - last_pwm) > config.pwm_hysteresis
        if not should_write:
            pwm = last_pwm

    status = f"CPU: {cpu_str}  Drive: {drive_str}  NVMe: {nvme_str}  PWM: {pwm}"
    return LoopDecision(
        pwm=pwm,
        should_write=should_write,
        should_shutdown=should_shutdown,
        status=status,
        fail_safe=False,
    )


def run_loop_iteration(deps: LoopDeps, state: LoopState, config: FanConfig) -> LoopState:
    """One loop iteration: read -> decide -> act -> log (#6)."""
    try:
        # Resolve the sysfs node first: (re)probe while none is known (driver
        # loaded after startup) or when the known node vanished (driver
        # reload) (L2). A newly adopted node gets manual mode and a forced
        # write; nothing written to the old/absent node counts for it.
        path = deps.pwm_path
        if deps.refresh_path is not None and (path is None or not os.path.exists(path)):
            refreshed = deps.refresh_path()
            if refreshed and refreshed != path:
                logger.info("Using PWM sysfs path: %s", refreshed)
                ensure_manual_pwm_mode(refreshed)
                deps.pwm_path = path = refreshed
                state.last_pwm = None

        snapshot = SensorSnapshot.from_readings(
            deps.read_cpu(), deps.read_drive(), deps.read_nvme()
        )
        decision = decide(snapshot, config, last_pwm=state.last_pwm)

        # Only successful writes advance the cache (#7): after a failed write
        # the next iteration retries, and without a write last_pwm stays the
        # value that is actually on the fan.
        if decision.should_write:
            if decision.full_speed:
                written = deps.write_full(config, path)
            else:
                written = deps.write_pwm_fn(decision.pwm, config, path)
            if written:
                state.last_pwm = decision.pwm

        now = deps.clock()
        # Logging (#10): a status line is forced on PWM/fail-safe state
        # changes, plus a LOG_REPORT_INTERVAL heartbeat while the state is
        # quiet. The validated loop period (sleep >= 1 s) is the floor
        # between any two lines, so journal volume stays bounded.
        state_key = (decision.pwm, decision.fail_safe)
        state_changed = state_key != state.last_state_key
        heartbeat_due = (now - state.last_log_time) >= LOG_REPORT_INTERVAL

        # Fail-safe: exactly one ERROR when entering fail-safe; the periodic
        # status line (which carries the fail-safe marker) covers the rest.
        if decision.fail_safe and state_changed:
            logger.error("No temperature readings available; fail-safe: PWM=%s", decision.pwm)

        # Something else (resume from suspend, firmware) may flip the channel
        # back to automatic mode, where PWM writes are ignored. Check once per
        # heartbeat and force a rewrite if manual mode had to be re-asserted.
        if (
            heartbeat_due
            and path
            and deps.refresh_path is not None
            and _read_int(path + "_enable") not in (None, 1)
        ):
            ensure_manual_pwm_mode(path)
            state.last_pwm = None

        if state_changed or heartbeat_due:
            logger.info(decision.status)
            state.last_log_time = now
            state.last_state_key = state_key

        # Debounce: a single glitched reading (e.g. a sensor that is not yet
        # initialised during boot) must not power the board off; require
        # overheat_confirm consecutive over-threshold iterations.
        if decision.should_shutdown:
            state.overheat_streak += 1
            if state.overheat_streak < config.overheat_confirm:
                logger.warning(
                    "Overheat reading %s/%s (temp=%s°C): %s",
                    state.overheat_streak,
                    config.overheat_confirm,
                    snapshot.max_temp,
                    decision.status,
                )
        else:
            state.overheat_streak = 0
        overheat_confirmed = (
            decision.should_shutdown and state.overheat_streak >= config.overheat_confirm
        )

        if overheat_confirmed and not state.shutdown_sent:
            state.shutdown_attempts += 1
            if state.shutdown_attempts == 1:
                logger.critical(
                    "Overheat threshold reached: shutting down (temp=%s°C; %s)",
                    snapshot.max_temp,
                    decision.status,
                )
            if bool(deps.shutdown_fn()):
                state.shutdown_sent = True
            elif (now - state.last_shutdown_error_log) >= LOG_REPORT_INTERVAL:
                # Keep the loop (and the fan at pwm_max) alive and retry every
                # cycle, but do not spam the journal (H2).
                logger.error(
                    "Overheat shutdown attempt %s failed (temp=%s); "
                    "retrying, fan held at PWM=%s",
                    state.shutdown_attempts,
                    snapshot.max_temp,
                    decision.pwm,
                )
                state.last_shutdown_error_log = now

        deps.sleep_fn(config.sleep)  # sleep must be last call
        return state
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.exception("Unhandled exception in main loop")
        # Fail safe: while the loop is broken nothing tracks the temperature,
        # so do not leave the fan at a stale (possibly minimal) speed.
        try:
            if deps.write_full(config, deps.pwm_path):
                state.last_pwm = PWM_FULL
        except Exception:
            logger.debug("fail-safe PWM write failed", exc_info=True)
        deps.sleep_fn(max(config.sleep, 5))  # configured backoff
        return state


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    """SIGTERM handler: unwind like Ctrl+C so the exit path runs."""
    raise KeyboardInterrupt


def main(
    config: Optional[FanConfig] = None,
    *,
    max_iterations: Optional[int] = None,
    stop_event: Optional[object] = None,
    deps: Optional[LoopDeps] = None,
) -> None:
    if config is None:
        config = parse_cli()

    # Configure logging only here (never at import time) (#5).
    level = logging.DEBUG if config.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(message)s")

    if deps is None:
        deps = LoopDeps.real()
        # `systemctl stop` sends SIGTERM; Python's default action would kill
        # the process without running the exit path below.
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    # Startup diagnostics at INFO (#11).
    if deps.pwm_path:
        logger.info("Using PWM sysfs path: %s", deps.pwm_path)
        ensure_manual_pwm_mode(deps.pwm_path)  # pwm writes need manual mode (M4)
    else:
        logger.error("No PWM sysfs path found yet; will keep probing every cycle")

    logger.info(
        "fan_control.py started (sleep=%ss, curve=%s, overheat=%s, "
        "overheat_threshold=%s, overheat_confirm=%s, pwm_hysteresis=%s, verbose=%s, "
        "pwm_min=%s, pwm_max=%s, tmin=%s, tmax=%s)",
        config.sleep,
        config.curve,
        config.overheat,
        config.overheat_threshold,
        config.overheat_confirm,
        config.pwm_hysteresis,
        config.verbose,
        config.pwm_min,
        config.pwm_max,
        config.tmin,
        config.tmax,
    )

    state = LoopState()
    iterations = 0
    try:
        while True:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            state = run_loop_iteration(deps, state, config)
            iterations += 1
            if state.shutdown_sent:
                break
    except KeyboardInterrupt:
        logger.info("fan_control.py interrupted (Ctrl+C / SIGTERM); stopping")
    finally:
        if deps.release_fn is not None:
            deps.release_fn(state.shutdown_sent)


if __name__ == "__main__":
    main()
