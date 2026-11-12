"""Temperature readers: millidegree parsing, label preference, hottest core,
thermal_zone fallback, sensors parsing, NVMe smart-log, drive smartctl."""

import fan_control


def test_scale_temp_millidegrees():
    assert fan_control.scale_temp(45000, millidegrees=True) == 45
    assert fan_control.scale_temp(1000, millidegrees=True) == 1
    assert fan_control.scale_temp(999, millidegrees=True) == 0
    assert fan_control.scale_temp(-5000, millidegrees=True) == -5


def test_scale_temp_whole_degrees_passthrough():
    assert fan_control.scale_temp(45, millidegrees=False) == 45
    assert fan_control.scale_temp(-3, millidegrees=False) == -3


def _hwmon_glob(hw, inputs):
    def fake_glob(pattern):
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(hw)]
        if pattern.endswith("temp*_input"):
            return [str(hw / f"temp{i}_input") for i in inputs]
        return []

    return fake_glob


def test_read_cpu_hwmon_millidegrees(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("45000\n")
    (hw / "temp1_label").write_text("Package id 0\n")
    monkeypatch.setattr(fan_control.glob, "glob", _hwmon_glob(hw, [1]))
    # 45000 millidegrees -> 45 °C by source contract (always //1000)
    assert fan_control.read_cpu_temp() == 45


def test_read_cpu_exact_1000_millidegrees(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("1000\n")
    (hw / "temp1_label").write_text("Package id 0\n")
    monkeypatch.setattr(fan_control.glob, "glob", _hwmon_glob(hw, [1]))
    # 1000 millidegrees -> exactly 1 °C, not the raw 1000
    assert fan_control.read_cpu_temp() == 1


def test_read_cpu_negative_value(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("-5000\n")
    (hw / "temp1_label").write_text("Package id 0\n")
    monkeypatch.setattr(fan_control.glob, "glob", _hwmon_glob(hw, [1]))
    assert fan_control.read_cpu_temp() == -5


def test_read_cpu_hottest_core(monkeypatch, tmp_path):
    # finding #13: return MAX across matching labeled inputs
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("40000\n")
    (hw / "temp1_label").write_text("Core 0\n")
    (hw / "temp2_input").write_text("55000\n")
    (hw / "temp2_label").write_text("Core 1\n")
    monkeypatch.setattr(fan_control.glob, "glob", _hwmon_glob(hw, [1, 2]))
    assert fan_control.read_cpu_temp() == 55


def test_read_cpu_thermal_zone_fallback(monkeypatch, tmp_path):
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "temp").write_text("47000\n")

    def fake_glob(pattern):
        if pattern.startswith("/sys/class/hwmon"):
            return []
        if pattern.startswith("/sys/class/thermal"):
            return [str(zone / "temp")]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.read_cpu_temp() == 47


def test_read_cpu_thermal_zone_takes_hottest(monkeypatch, tmp_path):
    # L3: fallback must use the hottest zone, not whichever sorts first.
    z0 = tmp_path / "thermal_zone0"
    z0.mkdir()
    (z0 / "temp").write_text("40000\n")
    z1 = tmp_path / "thermal_zone1"
    z1.mkdir()
    (z1 / "temp").write_text("47000\n")

    def fake_glob(pattern):
        if pattern.startswith("/sys/class/hwmon"):
            return []
        if pattern.startswith("/sys/class/thermal"):
            return [str(z0 / "temp"), str(z1 / "temp")]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.read_cpu_temp() == 47


def test_read_cpu_unlabeled_fallback_takes_hottest(monkeypatch, tmp_path):
    # L3: hwmon entry matches by name but has no label files — the unlabeled
    # fallback must pick the hottest input, not the first one.
    hw = tmp_path / "hwmon0"
    hw.mkdir()
    (hw / "name").write_text("coretemp\n")
    (hw / "temp1_input").write_text("40000\n")
    (hw / "temp2_input").write_text("57000\n")
    monkeypatch.setattr(fan_control.glob, "glob", _hwmon_glob(hw, [1, 2]))
    assert fan_control.read_cpu_temp() == 57


def test_read_cpu_sensors_parsing(monkeypatch):
    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])
    output = (
        "coretemp-isa-0000\n"
        "Adapter: ISA adapter\n"
        "Package id 0:  +52.0°C  (high = +80.0°C, crit = +100.0°C)\n"
    )

    def fake_check_output(cmd, **kwargs):
        assert cmd == ["sensors"]
        assert kwargs.get("timeout") == fan_control.SUBPROCESS_TIMEOUT
        return output

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake_check_output)
    assert fan_control.read_cpu_temp() == 52


def test_read_cpu_no_source_returns_none(monkeypatch):
    monkeypatch.setattr(fan_control.glob, "glob", lambda pattern: [])

    def fail(*args, **kwargs):
        raise FileNotFoundError("no sensors")

    monkeypatch.setattr(fan_control.subprocess, "check_output", fail)
    assert fan_control.read_cpu_temp() is None


def test_read_nvme_smart_log(monkeypatch):
    def fake_glob(pattern):
        if pattern == "/dev/nvme*n1":
            return ["/dev/nvme0n1"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)

    def fake_check_output(cmd, **kwargs):
        assert cmd[:2] == ["nvme", "smart-log"]
        assert kwargs.get("timeout") == fan_control.SUBPROCESS_TIMEOUT
        return "Temperature:                        44 Celsius\n"

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake_check_output)
    assert fan_control.read_nvme_temp() == 44


def test_read_nvme_max_across_namespaces(monkeypatch):
    def fake_glob(pattern):
        if pattern == "/dev/nvme*n1":
            return ["/dev/nvme0n1", "/dev/nvme1n1"]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    temps = {"/dev/nvme0n1": 40, "/dev/nvme1n1": 51}

    def fake_check_output(cmd, **kwargs):
        return f"Temperature: {temps[cmd[2]]} Celsius\n"

    monkeypatch.setattr(fan_control.subprocess, "check_output", fake_check_output)
    assert fan_control.read_nvme_temp() == 51