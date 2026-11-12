"""Temperature readers, part 2: NVMe fallback/caching, drive smartctl."""

import fan_control


def test_read_nvme_hwmon_fallback(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon1"
    hw.mkdir()
    (hw / "name").write_text("nvme\n")
    (hw / "temp1_input").write_text("41000\n")

    def fake_glob(pattern):
        if pattern == "/dev/nvme*n1":
            return []
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(hw)]
        if pattern.endswith("temp*_input"):
            return [str(hw / "temp1_input")]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def fail(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(fan_control.subprocess, "check_output", fail)
    assert fan_control.read_nvme_temp() == 41


def test_read_nvme_all_missing_returns_none(monkeypatch):
    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])
    assert fan_control.read_nvme_temp() is None


def test_read_nvme_negative_result_cached(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])
    assert fan_control.read_nvme_temp() is None
    assert "nvme_devices" in fan_control._missing_path_cache
    assert fan_control._missing_path_cache["nvme_devices"] > time_mod.monotonic()
    # a second call still short-circuits the /dev/nvme probe
    assert fan_control.read_nvme_temp() is None


def test_read_drive_smartctl(monkeypatch):
    def fake_glob(pattern):
        if pattern == "/dev/sd[a-z]":
            return ["/dev/sda"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def fake_check_output(cmd, **kwargs):
        assert cmd[:2] == ["smartctl", "-A"]
        assert kwargs.get("timeout") == fan_control.SUBPROCESS_TIMEOUT
        return (
            "194 Temperature_Celsius     0x0022   100   100   000    "
            "Old_age   Always       -       38\n"
        )

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake_check_output)
    assert fan_control.read_drive_temp() == 38


def test_read_drive_smartctl_missing_returns_none(monkeypatch):
    def fake_glob(pattern):
        if pattern == "/dev/sd[a-z]":
            return ["/dev/sda"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def fail(cmd, **kwargs):
        raise FileNotFoundError("smartctl not installed")

    monkeypatch.setattr(fan_control.subprocess, "check_output", fail)
    assert fan_control.read_drive_temp() is None
    assert "smartctl" in fan_control._missing_path_cache


def test_read_drive_no_devices_returns_none(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])
    assert fan_control.read_drive_temp() is None
    assert "sd_devices" in fan_control._missing_path_cache
    assert fan_control._missing_path_cache["sd_devices"] > time_mod.monotonic()


def test_missing_cache_expires_and_reprobes(monkeypatch):
    # M2: an expired entry must be re-probed, not cached for the lifetime
    # of the process (e.g. a drive that appears after startup).
    import time as time_mod

    fan_control._missing_path_cache["sd_devices"] = time_mod.monotonic() - 1
    probed = {"n": 0}

    def counting_glob(pattern):
        probed["n"] += 1
        return []

    monkeypatch.setattr(fan_control.glob, "glob", counting_glob)
    assert fan_control.read_drive_temp() is None
    assert probed["n"] >= 1  # expired entry -> /dev probed again
    # and re-marked with a fresh expiry
    assert fan_control._missing_path_cache["sd_devices"] > time_mod.monotonic()


def test_read_drive_timeout_cached_per_device(monkeypatch):
    # M3: a hung SATA device must not cost SUBPROCESS_TIMEOUT every loop.
    import subprocess as sp

    def fake_glob(pattern):
        if pattern == "/dev/sd[a-z]":
            return ["/dev/sda"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def hang(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, fan_control.SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(fan_control.subprocess, "check_output", hang)
    assert fan_control.read_drive_temp() is None
    assert "sd:/dev/sda" in fan_control._missing_path_cache

    calls = {"n": 0}

    def counting(cmd, **kwargs):
        calls["n"] += 1
        raise sp.TimeoutExpired(cmd, fan_control.SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(fan_control.subprocess, "check_output", counting)
    assert fan_control.read_drive_temp() is None
    assert calls["n"] == 0  # per-device TTL cache prevents respawning


def test_read_nvme_timeout_cached_per_device(monkeypatch):
    import subprocess as sp

    def fake_glob(pattern):
        if pattern == "/dev/nvme*n1":
            return ["/dev/nvme0n1"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def hang(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, fan_control.SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(fan_control.subprocess, "check_output", hang)
    assert fan_control.read_nvme_temp() is None
    assert "nvme:/dev/nvme0n1" in fan_control._missing_path_cache

    calls = {"n": 0}

    def counting(cmd, **kwargs):
        calls["n"] += 1
        raise sp.TimeoutExpired(cmd, fan_control.SUBPROCESS_TIMEOUT)

    monkeypatch.setattr(fan_control.subprocess, "check_output", counting)
    assert fan_control.read_nvme_temp() is None
    assert calls["n"] == 0