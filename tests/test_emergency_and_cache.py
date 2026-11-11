"""Emergency full speed (PWM 255 regardless of --pwm-max) and the 30 s cache
for the subprocess-based drive/NVMe readers."""

import fan_control

SERVICE_LIKE = {"pwm_min": 30, "pwm_max": 128, "tmin": 40, "tmax": 80}


def _deps(reads, writer, **extra):
    it = iter(reads)
    return fan_control.LoopDeps(
        read_cpu=lambda: next(it),
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=writer,
        shutdown_fn=lambda: True,
        sleep_fn=lambda s: None,
        clock=lambda: 0.0,
        pwm_path=extra.pop("pwm_path", "/fake/pwm1"),
        **extra,
    )


# ------------------------------------------------------------ emergency: decide


def test_fail_safe_is_full_speed_not_pwm_max(make_config):
    cfg = make_config(**SERVICE_LIKE)
    d = fan_control.decide(fan_control.SensorSnapshot(None, None, None), cfg, last_pwm=128)
    assert d.pwm == 255 and d.full_speed is True
    assert d.should_write is True  # 128 on the fan is not good enough
    assert "PWM: 255 (fail-safe" in d.status


def test_overheat_is_full_speed_not_pwm_max(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95, **SERVICE_LIKE)
    d = fan_control.decide(fan_control.SensorSnapshot(96, None, None), cfg, last_pwm=128)
    assert d.pwm == 255 and d.full_speed is True and d.should_write is True
    assert d.should_shutdown is True
    assert "overheat" in d.status


def test_hot_but_overheat_disabled_stays_on_curve(make_config):
    # without -o the threshold is ignored by design: normal curve maximum
    cfg = make_config(overheat=False, overheat_threshold=95, **SERVICE_LIKE)
    d = fan_control.decide(fan_control.SensorSnapshot(96, None, None), cfg, last_pwm=None)
    assert d.pwm == 128 and d.full_speed is False


def test_below_threshold_stays_within_pwm_max(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95, **SERVICE_LIKE)
    d = fan_control.decide(fan_control.SensorSnapshot(94, None, None), cfg, last_pwm=None)
    assert d.pwm == 128 and d.full_speed is False


# -------------------------------------------------------------- emergency: loop


def test_loop_returns_to_curve_after_fail_safe(make_config):
    cfg = make_config(**SERVICE_LIKE)
    writes = []
    deps = _deps([50, None, None, 50], lambda v, c, p: writes.append(v) or True)
    fan_control.main(cfg, max_iterations=4, deps=deps)
    curve = fan_control.compute_pwm_linear(50, cfg)
    assert writes == [curve, 255, curve]  # one write per state, back to normal


def test_overheat_spike_goes_full_speed_without_shutdown(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95, **SERVICE_LIKE)
    writes = []
    shutdowns = []
    deps = _deps([60, 99, 60], lambda v, c, p: writes.append(v) or True)
    deps.shutdown_fn = lambda: shutdowns.append(1) or True
    fan_control.main(cfg, max_iterations=3, deps=deps)
    assert 255 in writes  # fan reacts on the first over-threshold reading
    assert shutdowns == []  # but one reading does not power the board off


def test_real_wiring_bypasses_pwm_max(make_config, tmp_path):
    cfg = make_config(**SERVICE_LIKE)
    pwm = tmp_path / "pwm2"
    pwm.write_text("0")
    deps = _deps(
        [None],
        fan_control.write_pwm,
        pwm_path=str(pwm),
        write_full_fn=fan_control.write_pwm_full,
    )
    fan_control.run_loop_iteration(deps, fan_control.LoopState(), cfg)
    assert pwm.read_text() == "255"
    # the normal writer still honours --pwm-max
    fan_control.write_pwm(255, cfg, str(pwm))
    assert pwm.read_text() == "128"


def test_real_deps_use_the_full_speed_writer(monkeypatch):
    monkeypatch.setattr(fan_control, "get_pwm_path", lambda: None)
    assert fan_control.LoopDeps.real().write_full_fn is fan_control.write_pwm_full


# ----------------------------------------------------------------------- cache


def _sda_only(monkeypatch):
    monkeypatch.setattr(
        fan_control.glob, "glob", lambda pat: ["/dev/sda"] if pat == "/dev/sd[a-z]" else []
    )


def _smartctl(temp, calls):
    def fake(cmd, **kwargs):
        calls.append(cmd)
        return (
            "194 Temperature_Celsius     0x0022   100   100   000    Old_age   "
            f"Always       -       {temp}\n"
        )

    return fake


def test_drive_temp_is_cached_for_ttl(monkeypatch):
    _sda_only(monkeypatch)
    now = {"t": 1000.0}
    monkeypatch.setattr(fan_control.time, "monotonic", lambda: now["t"])
    calls = []
    monkeypatch.setattr(fan_control.subprocess, "check_output", _smartctl(38, calls))

    for _ in range(10):  # 10 loop iterations inside the TTL
        assert fan_control.read_drive_temp() == 38
        now["t"] += 2
    assert len(calls) == 1  # smartctl spawned once, not per iteration

    monkeypatch.setattr(fan_control.subprocess, "check_output", _smartctl(45, calls))
    now["t"] = 1000.0 + fan_control.DRIVE_TEMP_CACHE_TTL
    assert fan_control.read_drive_temp() == 45  # refreshed after expiry
    assert len(calls) == 2


def test_drive_none_result_is_cached_too(monkeypatch):
    # e.g. disk in standby: do not respawn smartctl every 2 s to find out
    _sda_only(monkeypatch)
    calls = []

    def no_temp(cmd, **kwargs):
        calls.append(cmd)
        return "Device is in STANDBY mode, exit(2)\n"

    monkeypatch.setattr(fan_control.subprocess, "check_output", no_temp)
    assert fan_control.read_drive_temp() is None
    assert fan_control.read_drive_temp() is None
    assert len(calls) == 1


def test_nvme_cli_is_cached_for_ttl(monkeypatch):
    monkeypatch.setattr(
        fan_control.glob, "glob", lambda pat: ["/dev/nvme0n1"] if pat == "/dev/nvme*n1" else []
    )
    now = {"t": 500.0}
    monkeypatch.setattr(fan_control.time, "monotonic", lambda: now["t"])
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return "temperature : 41 C (314 Kelvin)\n"

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake)
    for _ in range(5):
        assert fan_control.read_nvme_temp() == 41
        now["t"] += 2
    assert len(calls) == 1
    now["t"] = 500.0 + fan_control.DRIVE_TEMP_CACHE_TTL
    assert fan_control.read_nvme_temp() == 41
    assert len(calls) == 2


def test_nvme_hwmon_fallback_is_read_live_while_cli_is_cached(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon1"
    hw.mkdir()
    (hw / "name").write_text("nvme\n")
    temp = hw / "temp1_input"
    temp.write_text("40000\n")

    def fake_glob(pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(hw)]
        if pattern.endswith("temp*_input"):
            return [str(temp)]
        return []  # no /dev/nvme*n1 -> nvme-cli yields None

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.read_nvme_temp() == 40
    temp.write_text("55000\n")
    assert fan_control.read_nvme_temp() == 55  # sysfs is cheap: never cached
