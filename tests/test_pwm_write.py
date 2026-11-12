"""find_pwm_path preference/fallback and write_pwm behavior."""

import fan_control


def _tree(tmp_path, name, with_pwm=True):
    hw = tmp_path / name
    hw.mkdir()
    (hw / "name").write_text(name + "\n")
    if with_pwm:
        (hw / "pwm1").write_text("128\n")
    return hw


def test_find_pwm_path_prefers_preferred_name(monkeypatch, tmp_path):
    generic = _tree(tmp_path, "otherchip")
    preferred = _tree(tmp_path, "it87")
    real_glob_fn = fan_control.glob.glob  # original, before patching

    def fake_glob(pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(generic), str(preferred)]
        return real_glob_fn(pattern)

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.find_pwm_path() == str(preferred / "pwm1")


def test_find_pwm_path_fallback(monkeypatch, tmp_path):
    generic = _tree(tmp_path, "otherchip")
    real_glob_fn = fan_control.glob.glob

    def fake_glob(pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(generic)]
        return real_glob_fn(pattern)

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.find_pwm_path() == str(generic / "pwm1")


def test_find_pwm_path_none(monkeypatch):
    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])
    assert fan_control.find_pwm_path() is None


def test_write_pwm_success(make_config, tmp_path):
    cfg = make_config(pwm_min=30, pwm_max=128)
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")
    assert fan_control.write_pwm(100, cfg, str(pwm)) is True
    assert pwm.read_text() == "100"


def test_write_pwm_clamps_to_range(make_config, tmp_path):
    cfg = make_config(pwm_min=30, pwm_max=128)
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")

    fan_control.write_pwm(999, cfg, str(pwm))
    assert pwm.read_text() == "128"

    fan_control.write_pwm(-50, cfg, str(pwm))
    assert pwm.read_text() == "30"


def test_write_pwm_none_path_returns_true(make_config):
    # no path: log-only mode, treated as success so the loop advances cache
    assert fan_control.write_pwm(50, make_config(), None) is True


def test_write_pwm_permission_error_returns_false(make_config, tmp_path, monkeypatch):
    cfg = make_config()
    pwm = tmp_path / "pwm1"
    pwm.write_text("0")

    import builtins

    def deny(*args, **kwargs):
        raise PermissionError("EACCES")

    # open is a builtin; patch it in builtins (module-level lookup finds it there)
    monkeypatch.setattr(builtins, "open", deny)
    assert fan_control.write_pwm(50, cfg, str(pwm)) is False


def test_write_pwm_file_not_found_returns_false(make_config, tmp_path, monkeypatch):
    cfg = make_config()
    import builtins

    real_open = builtins.open

    def vanish(path, *args, **kwargs):
        raise FileNotFoundError("ENOENT")

    monkeypatch.setattr(builtins, "open", vanish)
    assert fan_control.write_pwm(50, cfg, str(tmp_path / "gone")) is False
    monkeypatch.setattr(builtins, "open", real_open)


def test_write_failure_does_not_advance_cache(make_config, monkeypatch):
    # finding #7: run_loop_iteration must not cache failed writes
    cfg = make_config(pwm_min=20, pwm_max=255, tmin=30, tmax=80)
    writes = []

    def failing_writer(value, config, path):
        writes.append(value)
        return False

    deps = fan_control.LoopDeps(
        read_cpu=lambda: 70,
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=failing_writer,
        shutdown_fn=lambda: None,
        sleep_fn=lambda s: None,
        clock=lambda: 100.0,
        pwm_path="/fake/pwm1",
    )
    state = fan_control.LoopState()
    state = fan_control.run_loop_iteration(deps, state, cfg)
    assert writes, "writer should have been attempted"
    assert state.last_pwm is None, "failed write must not advance last_pwm"

    # success path advances the cache
    def ok_writer(value, config, path):
        writes.append(value)
        return True

    deps.write_pwm_fn = ok_writer
    state = fan_control.run_loop_iteration(deps, state, cfg)
    assert state.last_pwm == writes[-1]