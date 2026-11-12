"""Boot-crash regression: a bogus sensor value must never power the board off.

Running as a service with ``-o`` used to shut the board down immediately at
boot when a single reading was garbage (packed SMART raw value, last column =
Max, uninitialised sensor). These tests pin the three defences: plausibility
filter, correct smartctl/nvme parsing, and the overheat confirmation streak.
"""

import pytest

import fan_control

# ---------------------------------------------------------------- plausibility


@pytest.mark.parametrize("value", [-40, 0, 45, 150])
def test_plausible_keeps_real_temperatures(value):
    assert fan_control.plausible(value, "x") == value


@pytest.mark.parametrize("value", [-41, 151, 255, 65261, 25769803812])
def test_plausible_rejects_garbage(value, caplog):
    with caplog.at_level("WARNING"):
        assert fan_control.plausible(value, "x") is None
    assert any("implausible" in r.message for r in caplog.records)


def test_plausible_warning_is_rate_limited(caplog):
    with caplog.at_level("WARNING"):
        for _ in range(5):
            fan_control.plausible(9999, "same-source")
    assert len([r for r in caplog.records if "implausible" in r.message]) == 1


def test_bogus_hwmon_nvme_sensor_is_ignored(monkeypatch, tmp_path):
    hw = tmp_path / "hwmon1"
    hw.mkdir()
    (hw / "name").write_text("nvme\n")
    (hw / "temp1_input").write_text("41000\n")
    (hw / "temp2_input").write_text("65261850\n")  # broken sensor 2

    def fake_glob(pattern):
        if pattern == "/dev/nvme*n1":
            return []
        if pattern == "/sys/class/hwmon/hwmon*":
            return [str(hw)]
        if pattern.endswith("temp*_input"):
            return [str(hw / "temp1_input"), str(hw / "temp2_input")]
        return []

    monkeypatch.setattr(fan_control.glob, "glob", fake_glob)
    assert fan_control.read_nvme_temp() == 41


# ------------------------------------------------------------- smartctl parse


def test_smartctl_uses_raw_column_not_max():
    out = (
        "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      "
        "UPDATED  WHEN_FAILED RAW_VALUE\n"
        "194 Temperature_Celsius     0x0022   036   045   000    Old_age   "
        "Always       -       36 (Min/Max 20/98)\n"
    )
    assert fan_control._parse_smartctl_temp(out) == 36


def test_smartctl_packed_raw_value_is_not_returned_as_temperature():
    out = (
        "194 Temperature_Celsius     0x0022   100   100   000    Old_age   "
        "Always       -       25769803812\n"
    )
    # the parser returns the number; plausible() then rejects it
    assert fan_control.plausible(fan_control._parse_smartctl_temp(out), "sda") is None


def test_smartctl_prefers_194_over_190():
    out = (
        "190 Airflow_Temperature_Cel 0x0032   064   045   040    Old_age   "
        "Always       -       33 (Min/Max 22/55)\n"
        "194 Temperature_Celsius     0x0022   100   100   000    Old_age   "
        "Always       -       38\n"
    )
    assert fan_control._parse_smartctl_temp(out) == 38


def test_smartctl_scsi_style():
    out = "Current Drive Temperature:     31 C\nDrive Trip Temperature:        65 C\n"
    assert fan_control._parse_smartctl_temp(out) == 31


def test_smartctl_no_temperature():
    assert fan_control._parse_smartctl_temp("SMART support is: Unavailable\n") is None


# ----------------------------------------------------------------- nvme parse


@pytest.mark.parametrize(
    "out, expected",
    [
        ("critical_warning : 0\ntemperature : 38 C (311 Kelvin)\n", 38),
        ("temperature\t\t\t\t: 37 °C (310 K)\n", 37),
        ("temperature : 311 K\n", 38),
        ("Warning Temperature Time : 99\ntemperature : 40 C\n", 40),
        ("Temperature Sensor 1 : 120 C\n", None),
        # nvme-cli 2.13 under LANG=en_US.UTF-8, verbatim from the ODROID H4.
        # Read as °C this was 105/118 "degrees" -> instant overheat shutdown.
        (
            (
                "critical_warning\t\t\t: 0\n"
                "temperature\t\t\t\t: 105 °F (314 K)\n"
                "Temperature Sensor 1\t\t\t: 105 °F (314 K)\n"
            ),
            41,
        ),
        ("temperature : 118 °F\n", 48),  # Fahrenheit only, no Kelvin
        ("temperature : 104 F\n", 40),
    ],
)
def test_nvme_parse(out, expected):
    assert fan_control._parse_nvme_temp(out) == expected


# ----------------------------------------------------------- overheat debounce


def _deps(read_cpu, shutdown):
    return fan_control.LoopDeps(
        read_cpu=read_cpu,
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=lambda v, c, p: True,
        shutdown_fn=shutdown,
        sleep_fn=lambda s: None,
        clock=lambda: 100.0,
        pwm_path="/fake/pwm1",
    )


def test_single_spike_does_not_shut_down(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95)  # default confirm=3
    readings = iter([45, 120, 45, 120, 120, 45, 45, 45])
    calls = {"n": 0}

    def shutdown():
        calls["n"] += 1
        return True

    fan_control.main(cfg, max_iterations=8, deps=_deps(lambda: next(readings), shutdown))
    assert calls["n"] == 0


def test_sustained_overheat_shuts_down_after_confirm(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95, overheat_confirm=3)
    calls = {"iter_at_shutdown": None}
    it = {"n": 0}

    def read():
        it["n"] += 1
        return 99

    def shutdown():
        calls["iter_at_shutdown"] = it["n"]
        return True

    fan_control.main(cfg, max_iterations=10, deps=_deps(read, shutdown))
    assert calls["iter_at_shutdown"] == 3


def test_overheat_confirm_cli_and_validation(capsys):
    assert fan_control.parse_cli([]).overheat_confirm == 3
    assert fan_control.parse_cli(["--overheat-confirm", "5"]).overheat_confirm == 5
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--overheat-confirm", "0"])
    assert exc.value.code == 2
