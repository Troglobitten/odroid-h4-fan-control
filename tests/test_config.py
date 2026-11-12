"""FanConfig.validate() unit tests."""

import pytest


def test_valid_defaults(make_config):
    cfg = make_config()
    assert cfg.validate() is cfg


def test_tmin_equals_tmax_raises(make_config):
    cfg = make_config(tmin=50, tmax=50)
    with pytest.raises(ValueError, match="tmin"):
        cfg.validate()


def test_tmin_greater_than_tmax_raises(make_config):
    cfg = make_config(tmin=80, tmax=30)
    with pytest.raises(ValueError, match="tmin"):
        cfg.validate()


def test_pwm_min_greater_than_pwm_max_raises(make_config):
    cfg = make_config(pwm_min=200, pwm_max=100)
    with pytest.raises(ValueError, match="pwm"):
        cfg.validate()


def test_sleep_zero_raises(make_config):
    cfg = make_config(sleep=0)
    with pytest.raises(ValueError, match="sleep"):
        cfg.validate()


def test_sleep_negative_raises(make_config):
    cfg = make_config(sleep=-1)
    with pytest.raises(ValueError, match="sleep"):
        cfg.validate()


def test_pwm_min_negative_raises(make_config):
    cfg = make_config(pwm_min=-1, pwm_max=255)
    with pytest.raises(ValueError, match="0\\.\\.255"):
        cfg.validate()


def test_pwm_max_above_255_raises(make_config):
    cfg = make_config(pwm_min=0, pwm_max=300)
    with pytest.raises(ValueError, match="0\\.\\.255"):
        cfg.validate()


def test_pwm_boundaries_ok(make_config):
    make_config(pwm_min=0, pwm_max=255).validate()
    make_config(pwm_min=0, pwm_max=0).validate()


def test_negative_pwm_hysteresis_raises(make_config):
    cfg = make_config(pwm_hysteresis=-1)
    with pytest.raises(ValueError, match="hysteresis"):
        cfg.validate()


def test_negative_overheat_threshold_raises(make_config):
    cfg = make_config(overheat_threshold=-1)
    with pytest.raises(ValueError, match="overheat-threshold"):
        cfg.validate()


def test_zero_hysteresis_and_threshold_ok(make_config):
    make_config(pwm_hysteresis=0, overheat_threshold=0).validate()