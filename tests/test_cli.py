"""CLI parser: defaults, choices, new flags, clean validation errors."""

import pytest

import fan_control


def test_build_parser_returns_parser():
    p = fan_control.build_parser()
    assert p.prog


def test_parse_cli_defaults():
    cfg = fan_control.parse_cli([])
    assert cfg.sleep == 2
    assert cfg.overheat is False
    assert cfg.curve == "lin"
    assert cfg.pwm_min == 20
    assert cfg.pwm_max == 255
    assert cfg.tmin == 30
    assert cfg.tmax == 80
    assert cfg.overheat_threshold == 95
    assert cfg.pwm_hysteresis == 2
    assert cfg.verbose is False
    cfg.validate()  # defaults must be valid


def test_parse_cli_all_flags():
    cfg = fan_control.parse_cli(
        [
            "-v",
            "-t",
            "5",
            "-o",
            "-c",
            "log",
            "--pwm-min",
            "30",
            "--pwm-max",
            "128",
            "--tmin",
            "40",
            "--tmax",
            "70",
            "--overheat-threshold",
            "90",
            "--pwm-hysteresis",
            "4",
        ]
    )
    assert cfg.verbose is True
    assert cfg.sleep == 5
    assert cfg.overheat is True
    assert cfg.curve == "log"
    assert (cfg.pwm_min, cfg.pwm_max) == (30, 128)
    assert (cfg.tmin, cfg.tmax) == (40, 70)
    assert cfg.overheat_threshold == 90
    assert cfg.pwm_hysteresis == 4


def test_invalid_curve_rejected():
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["-c", "quadratic"])
    assert exc.value.code == 2


def test_overheat_threshold_accepted():
    cfg = fan_control.parse_cli(["--overheat-threshold", "88"])
    assert cfg.overheat_threshold == 88


def test_pwm_hysteresis_accepted():
    cfg = fan_control.parse_cli(["--pwm-hysteresis", "7"])
    assert cfg.pwm_hysteresis == 7


def test_validation_error_clean_exit_code_2(capsys):
    # tmin >= tmax -> parser.error -> SystemExit(2), clean message, no traceback
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--tmin", "80", "--tmax", "30"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "tmin" in err
    assert "Traceback" not in err


def test_sleep_validation_error_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--sleep", "0"])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_pwm_range_validation_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--pwm-min", "0", "--pwm-max", "300"])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_negative_hysteresis_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--pwm-hysteresis", "-1"])
    assert exc.value.code == 2
    assert "hysteresis" in capsys.readouterr().err


def test_negative_overheat_threshold_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        fan_control.parse_cli(["--overheat-threshold", "-5"])
    assert exc.value.code == 2
    assert "overheat-threshold" in capsys.readouterr().err