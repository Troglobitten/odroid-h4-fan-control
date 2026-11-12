"""Regressions from the second review pass.

- hysteresis must be measured against the value last *written* (slow drifts
  used to be swallowed completely: the fan stayed at pwm_min while the
  temperature climbed degree by degree to tmax)
- a PWM node that appears after startup must be adopted
- smartctl output must be used even when the exit status is non-zero
- the fan must be released (auto mode / full speed) when the daemon stops
- an unhandled loop exception must not leave the fan at a stale speed
"""

import subprocess

import pytest

import fan_control


def _deps(read_cpu, writer, **extra):
    return fan_control.LoopDeps(
        read_cpu=read_cpu,
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=writer,
        shutdown_fn=lambda: True,
        sleep_fn=lambda s: None,
        clock=lambda: 0.0,
        pwm_path=extra.pop("pwm_path", "/fake/pwm1"),
        **extra,
    )


# ------------------------------------------------------------------ hysteresis


def test_slow_temperature_ramp_is_followed(make_config):
    # 1 PWM step per degree, hysteresis 2: every single step is inside the
    # band, but the accumulated drift must still reach the fan.
    cfg = make_config(pwm_min=30, pwm_max=80, tmin=40, tmax=90, pwm_hysteresis=2)
    temps = iter(range(40, 91))
    writes = []
    deps = _deps(lambda: next(temps), lambda v, c, p: writes.append(v) or True)
    fan_control.main(cfg, max_iterations=51, deps=deps)
    assert writes[0] == 30
    assert writes[-1] == 80  # fan reaches pwm_max at tmax
    assert len(writes) > 10  # followed all the way up, not just once
    # the fan never lags the curve by more than the hysteresis band
    steps = [b - a for a, b in zip(writes, writes[1:])]
    assert max(steps) <= cfg.pwm_hysteresis + 1


def test_slow_ramp_down_is_followed(make_config):
    cfg = make_config(pwm_min=30, pwm_max=80, tmin=40, tmax=90, pwm_hysteresis=2)
    temps = iter(range(90, 39, -1))
    writes = []
    deps = _deps(lambda: next(temps), lambda v, c, p: writes.append(v) or True)
    fan_control.main(cfg, max_iterations=51, deps=deps)
    assert writes[0] == 80
    assert writes[-1] == 30  # back at pwm_min at tmin


def test_decide_reports_held_value_inside_band(make_config):
    cfg = make_config(tmin=30, tmax=80, pwm_hysteresis=10)
    snap = fan_control.SensorSnapshot(cpu=50, drive=None, nvme=None)
    target = fan_control.compute_pwm_linear(50, cfg)
    d = fan_control.decide(snap, cfg, last_pwm=target - 5)
    assert d.should_write is False
    assert d.pwm == target - 5  # status shows what is really on the fan
    assert f"PWM: {target - 5}" in d.status


def test_decide_snaps_to_curve_end_points(make_config):
    cfg = make_config(pwm_min=20, pwm_max=255, tmin=30, tmax=80, pwm_hysteresis=10)
    hot = fan_control.SensorSnapshot(cpu=85, drive=None, nvme=None)
    d = fan_control.decide(hot, cfg, last_pwm=250)  # within the band of 255
    assert d.should_write is True and d.pwm == 255
    cold = fan_control.SensorSnapshot(cpu=25, drive=None, nvme=None)
    d = fan_control.decide(cold, cfg, last_pwm=24)
    assert d.should_write is True and d.pwm == 20


# -------------------------------------------------------------------- PWM path


def test_get_pwm_path_keeps_probing_until_found(monkeypatch):
    found = {"path": None}
    monkeypatch.setattr(fan_control, "find_pwm_path", lambda: found["path"])
    monkeypatch.setattr(fan_control, "_pwm_path_cache", None)
    monkeypatch.setattr(fan_control, "_pwm_path_resolved", False)
    monkeypatch.setattr(fan_control.os.path, "exists", lambda p: True)

    assert fan_control.get_pwm_path() is None
    found["path"] = "/sys/class/hwmon/hwmon3/pwm2"  # driver loaded later
    assert fan_control.get_pwm_path() == "/sys/class/hwmon/hwmon3/pwm2"


def test_loop_adopts_pwm_path_that_appears_after_startup(make_config, tmp_path):
    pwm = tmp_path / "pwm2"
    enable = tmp_path / "pwm2_enable"
    available = {"yes": False}
    written = []

    def refresh():
        return str(pwm) if available["yes"] else None

    deps = _deps(
        lambda: 55,
        lambda v, c, p: written.append((p, v)) or True,
        pwm_path=None,
        refresh_path=refresh,
    )
    cfg = make_config()
    state = fan_control.LoopState()
    state = fan_control.run_loop_iteration(deps, state, cfg)
    assert written == [(None, written[0][1])]  # nothing to write to yet

    pwm.write_text("0")
    enable.write_text("2\n")
    available["yes"] = True
    state = fan_control.run_loop_iteration(deps, state, cfg)
    # same temperature, but the new node gets manual mode and a forced write
    assert written[-1][0] == str(pwm)
    assert enable.read_text() == "1"
    assert deps.pwm_path == str(pwm)


def test_manual_mode_is_reasserted_on_heartbeat(make_config, tmp_path):
    pwm = tmp_path / "pwm2"
    pwm.write_text("0")
    enable = tmp_path / "pwm2_enable"
    enable.write_text("1\n")
    writes = []
    clock = {"t": 0.0}
    deps = _deps(
        lambda: 55,
        lambda v, c, p: writes.append(v) or True,
        pwm_path=str(pwm),
        refresh_path=lambda: str(pwm),
    )
    deps.clock = lambda: clock["t"]
    cfg = make_config()
    state = fan_control.run_loop_iteration(deps, fan_control.LoopState(), cfg)
    assert len(writes) == 1

    enable.write_text("2\n")  # e.g. resume from suspend
    clock["t"] = fan_control.LOG_REPORT_INTERVAL + 1
    state = fan_control.run_loop_iteration(deps, state, cfg)
    assert enable.read_text() == "1"
    state = fan_control.run_loop_iteration(deps, state, cfg)
    assert len(writes) == 2  # PWM rewritten after manual mode came back


# -------------------------------------------------------------------- smartctl


def _one_sda(monkeypatch):
    monkeypatch.setattr(
        fan_control.glob, "glob", lambda pat: ["/dev/sda"] if pat == "/dev/sd[a-z]" else []
    )


def test_smartctl_nonzero_exit_status_still_yields_temperature(monkeypatch):
    _one_sda(monkeypatch)
    table = (
        "194 Temperature_Celsius     0x0022   100   100   000    Old_age   "
        "Always       -       41\n"
    )

    def fake(cmd, **kwargs):
        raise subprocess.CalledProcessError(64, cmd, output=table)  # bit 6 set

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake)
    assert fan_control.read_drive_temp() == 41


def test_smartctl_does_not_wake_sleeping_disks(monkeypatch):
    _one_sda(monkeypatch)
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        raise subprocess.CalledProcessError(
            2, cmd, output="Device is in STANDBY mode, exit(2)\n"
        )

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake)
    assert fan_control.read_drive_temp() is None
    assert seen["cmd"] == ["smartctl", "-A", "-n", "standby", "/dev/sda"]


# ------------------------------------------------------------- release on exit


@pytest.fixture
def pwm_node(tmp_path):
    fan_control._pwm_original_enable.clear()
    pwm = tmp_path / "pwm2"
    pwm.write_text("40")
    enable = tmp_path / "pwm2_enable"
    yield pwm, enable
    fan_control._pwm_original_enable.clear()


def test_release_restores_automatic_mode(pwm_node):
    pwm, enable = pwm_node
    enable.write_text("2\n")
    fan_control.ensure_manual_pwm_mode(str(pwm))
    assert enable.read_text() == "1"
    fan_control.release_pwm(str(pwm))
    assert enable.read_text() == "2"
    assert pwm.read_text() == "40"  # PWM untouched, the chip is in charge again


def test_release_goes_full_speed_when_channel_was_already_manual(pwm_node):
    pwm, enable = pwm_node
    enable.write_text("1\n")
    fan_control.ensure_manual_pwm_mode(str(pwm))
    fan_control.release_pwm(str(pwm))
    assert pwm.read_text() == "255"


def test_release_full_speed_after_overheat_shutdown(pwm_node):
    pwm, enable = pwm_node
    enable.write_text("2\n")
    fan_control.ensure_manual_pwm_mode(str(pwm))
    fan_control.release_pwm(str(pwm), full_speed=True)
    assert pwm.read_text() == "255"
    assert enable.read_text() == "1"  # stays manual so 255 is honoured


def test_release_without_path_is_a_noop():
    fan_control.release_pwm(None)


def test_main_calls_release_on_normal_end_and_on_interrupt(make_config):
    calls = []
    deps = _deps(lambda: 45, lambda v, c, p: True, release_fn=calls.append)
    fan_control.main(make_config(), max_iterations=2, deps=deps)
    assert calls == [False]

    def interrupted():
        raise KeyboardInterrupt  # what the SIGTERM handler raises

    calls.clear()
    deps = _deps(interrupted, lambda v, c, p: True, release_fn=calls.append)
    fan_control.main(make_config(), max_iterations=5, deps=deps)
    assert calls == [False]


def test_main_releases_at_full_speed_after_overheat(make_config):
    calls = []
    deps = _deps(lambda: 99, lambda v, c, p: True, release_fn=calls.append)
    cfg = make_config(overheat=True, overheat_threshold=95, overheat_confirm=1)
    fan_control.main(cfg, max_iterations=5, deps=deps)
    assert calls == [True]


def test_sigterm_handler_unwinds_like_ctrl_c():
    with pytest.raises(KeyboardInterrupt):
        fan_control._raise_keyboard_interrupt(15, None)


# ------------------------------------------------------------ exception path


def test_unhandled_exception_drives_fan_to_full_speed(make_config):
    cfg = make_config(pwm_min=30, pwm_max=128)
    writes = []

    def boom():
        raise RuntimeError("sensor exploded")

    deps = _deps(boom, lambda v, c, p: writes.append(v) or True)
    state = fan_control.run_loop_iteration(deps, fan_control.LoopState(last_pwm=30), cfg)
    assert writes == [255]  # full speed, not --pwm-max
    assert state.last_pwm == 255


def test_exception_in_fail_safe_write_is_swallowed(make_config):
    def boom():
        raise RuntimeError("sensor exploded")

    def bad_writer(v, c, p):
        raise OSError("sysfs gone")

    deps = _deps(boom, bad_writer)
    fan_control.run_loop_iteration(deps, fan_control.LoopState(), make_config())  # no raise
