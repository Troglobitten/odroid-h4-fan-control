"""Manual-mode setup (pwmN_enable) and lazy PWM path re-resolution (M4/L2)."""

import builtins

import fan_control


def test_ensure_manual_mode_writes_enable(tmp_path):
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")
    (tmp_path / "pwm1_enable").write_text("2\n")
    assert fan_control.ensure_manual_pwm_mode(str(pwm)) is True
    assert (tmp_path / "pwm1_enable").read_text() == "1"


def test_ensure_manual_mode_already_manual(tmp_path):
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")
    enable = tmp_path / "pwm1_enable"
    enable.write_text("1\n")
    assert fan_control.ensure_manual_pwm_mode(str(pwm)) is True
    assert enable.read_text() == "1\n"  # untouched


def test_ensure_manual_mode_no_enable_file(tmp_path):
    # EC/BIOS-managed boards may not expose pwmN_enable: nothing to do.
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")
    assert fan_control.ensure_manual_pwm_mode(str(pwm)) is True


def test_ensure_manual_mode_write_failure_returns_false(tmp_path, monkeypatch):
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")
    enable = tmp_path / "pwm1_enable"
    enable.write_text("2\n")

    real_open = builtins.open

    def deny_write(path, mode="r", *args, **kwargs):
        if "w" in mode and str(path).endswith("_enable"):
            raise PermissionError("EACCES")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_write)
    assert fan_control.ensure_manual_pwm_mode(str(pwm)) is False
    assert enable.read_text() == "2\n"  # unchanged


def test_get_pwm_path_re_resolves_when_vanished(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    pwm = hw / "pwm1"
    pwm.write_text("128\n")
    (hw / "name").write_text("it87\n")

    def fake_glob(pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(hw)] if pwm.exists() else []
        if pattern.endswith("pwm[0-9]*"):
            return [str(pwm)] if pwm.exists() else []
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    monkeypatch.setattr(fan_control, "_pwm_path_cache", None)
    monkeypatch.setattr(fan_control, "_pwm_path_resolved", False)

    first = fan_control.get_pwm_path()
    assert first == str(pwm)
    pwm.unlink()
    # stale path -> immediate re-resolution (None here; retried next call)
    assert fan_control.get_pwm_path() is None


def test_loop_refreshes_vanished_pwm_path(make_config, tmp_path):
    # L2: when the detected node vanishes, the real wiring re-resolves it
    # and the write goes to the new node.
    old_path = tmp_path / "old_pwm1"  # never created -> does not exist
    new_path = tmp_path / "new_pwm1"
    new_path.write_text("0")
    written = []

    deps = fan_control.LoopDeps(
        read_cpu=lambda: 55,
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=lambda v, c, p: written.append(p) or True,
        shutdown_fn=lambda: True,
        sleep_fn=lambda s: None,
        clock=lambda: 0.0,
        pwm_path=str(old_path),
        refresh_path=lambda: str(new_path),
    )
    state = fan_control.LoopState()
    state = fan_control.run_loop_iteration(deps, state, make_config())
    assert written and written[0] == str(new_path)
