"""PWM curve math: boundaries, monotonicity, div-by-zero guard, clamping."""

import math

import fan_control


def test_linear_none_returns_pwm_min(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_linear(None, cfg) == cfg.pwm_min


def test_linear_below_tmin(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_linear(cfg.tmin - 1, cfg) == cfg.pwm_min


def test_linear_at_tmin(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_linear(cfg.tmin, cfg) == cfg.pwm_min


def test_linear_at_tmax(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_linear(cfg.tmax, cfg) == cfg.pwm_max


def test_linear_above_tmax(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_linear(cfg.tmax + 1, cfg) == cfg.pwm_max


def test_log_none_returns_pwm_min(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_log(None, cfg) == cfg.pwm_min


def test_log_boundaries(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_log(cfg.tmin, cfg) == cfg.pwm_min
    assert fan_control.compute_pwm_log(cfg.tmax, cfg) == cfg.pwm_max


def test_exp_none_returns_pwm_min(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_exp(None, cfg) == cfg.pwm_min


def test_exp_boundaries(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm_exp(cfg.tmin, cfg) == cfg.pwm_min
    assert fan_control.compute_pwm_exp(cfg.tmax, cfg) == cfg.pwm_max


def test_div_by_zero_guard_tmin_eq_tmax(make_config):
    # build an invalid config directly (bypass validate) to exercise the
    # defensive guard inside each curve.
    cfg = fan_control.FanConfig(
        sleep=2, overheat=False, curve="lin", pwm_min=20, pwm_max=255, tmin=50, tmax=50
    )
    assert fan_control.compute_pwm_linear(50, cfg) == cfg.pwm_min
    assert fan_control.compute_pwm_log(50, cfg) == cfg.pwm_min
    assert fan_control.compute_pwm_exp(50, cfg) == cfg.pwm_min


def test_monotonic_sweep(make_config):
    cfg = make_config()
    for fn in (
        fan_control.compute_pwm_linear,
        fan_control.compute_pwm_log,
        fan_control.compute_pwm_exp,
    ):
        prev = -1
        for t in range(cfg.tmin - 5, cfg.tmax + 6):
            v = fn(t, cfg)
            assert cfg.pwm_min <= v <= cfg.pwm_max, f"{fn.__name__}({t})={v} out of range"
            assert v >= prev, f"{fn.__name__} not monotonic at t={t}"
            prev = v


def test_clamped_to_range(make_config):
    cfg = make_config(pwm_min=30, pwm_max=128)
    for fn in (
        fan_control.compute_pwm_linear,
        fan_control.compute_pwm_log,
        fan_control.compute_pwm_exp,
    ):
        for t in (0, 1000):
            v = fn(t, cfg)
            assert 30 <= v <= 128


def test_compute_pwm_uses_max_reading(make_config):
    cfg = make_config()
    # max of (40, None, 60) = 60
    assert fan_control.compute_pwm(40, None, 60, "lin", cfg) == fan_control.compute_pwm_linear(
        60, cfg
    )


def test_compute_pwm_all_none_returns_pwm_min(make_config):
    cfg = make_config()
    assert fan_control.compute_pwm(None, None, None, "lin", cfg) == cfg.pwm_min


def test_compute_pwm_dispatches_method(make_config):
    cfg = make_config()
    t = 55
    assert fan_control.compute_pwm(t, None, None, "lin", cfg) == fan_control.compute_pwm_linear(
        t, cfg
    )
    assert fan_control.compute_pwm(t, None, None, "log", cfg) == fan_control.compute_pwm_log(
        t, cfg
    )
    assert fan_control.compute_pwm(t, None, None, "exp", cfg) == fan_control.compute_pwm_exp(
        t, cfg
    )


def test_log_curve_shape(make_config):
    # sanity: log curve at midpoint should be > linear midpoint? No — log1p
    # mapping means mid maps to log1p(4.5)/log1p(9) ≈ 0.7, so above linear 0.5.
    cfg = make_config()
    mid = (cfg.tmin + cfg.tmax) // 2
    lin = fan_control.compute_pwm_linear(mid, cfg)
    log = fan_control.compute_pwm_log(mid, cfg)
    assert log >= lin
    assert math.isclose(lin, (cfg.pwm_min + cfg.pwm_max) // 2, abs_tol=2)