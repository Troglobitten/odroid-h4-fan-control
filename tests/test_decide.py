"""Pure decide(): fail-safe, overheat threshold, hysteresis, mixed None."""

import fan_control


def test_all_none_fail_safe_pwm(make_config):
    # finding #3: all sensors None -> pwm_max, not pwm_min
    cfg = make_config(pwm_min=20, pwm_max=255)
    snap = fan_control.SensorSnapshot(cpu=None, drive=None, nvme=None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.pwm == cfg.pwm_max
    assert d.fail_safe is True
    assert d.should_write is True
    assert d.should_shutdown is False
    assert "fail-safe" in d.status


def test_all_none_does_not_rewrite_when_already_at_max(make_config):
    cfg = make_config(pwm_max=255)
    snap = fan_control.SensorSnapshot(None, None, None)
    d = fan_control.decide(snap, cfg, last_pwm=255)
    assert d.should_write is False


def test_mixed_none_and_int(make_config):
    cfg = make_config(tmin=30, tmax=80)
    snap = fan_control.SensorSnapshot(cpu=None, drive=None, nvme=60)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.fail_safe is False
    assert d.pwm == fan_control.compute_pwm_linear(60, cfg)
    assert "N/A" in d.status


def test_overheat_exact_threshold_triggers(make_config):
    # finding #2: shutdown exactly at threshold, no hidden margin
    cfg = make_config(overheat=True, overheat_threshold=95)
    snap = fan_control.SensorSnapshot(cpu=95, drive=None, nvme=None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.should_shutdown is True


def test_overheat_threshold_minus_one_does_not_trigger(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95)
    snap = fan_control.SensorSnapshot(cpu=94, drive=None, nvme=None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.should_shutdown is False


def test_overheat_flag_off_never_shuts_down(make_config):
    cfg = make_config(overheat=False, overheat_threshold=50)
    snap = fan_control.SensorSnapshot(cpu=120, drive=None, nvme=None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.should_shutdown is False


def test_shutdown_not_requested_in_fail_safe(make_config):
    # even with overheat on, no readings -> no shutdown (cannot measure)
    cfg = make_config(overheat=True, overheat_threshold=0)
    snap = fan_control.SensorSnapshot(None, None, None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.should_shutdown is False


def test_hysteresis_blocks_small_change(make_config):
    # finding #14: delta <= hysteresis -> no write
    cfg = make_config(tmin=30, tmax=80, pwm_hysteresis=10)
    snap = fan_control.SensorSnapshot(cpu=50, drive=None, nvme=None)
    pwm = fan_control.compute_pwm_linear(50, cfg)
    # last_pwm within hysteresis of pwm
    d = fan_control.decide(snap, cfg, last_pwm=pwm - 5)
    assert d.should_write is False
    # delta > hysteresis -> write
    d2 = fan_control.decide(snap, cfg, last_pwm=pwm - 50)
    assert d2.should_write is True


def test_hysteresis_default_two(make_config):
    cfg = make_config(tmin=30, tmax=80)  # pwm_hysteresis=2 default
    snap = fan_control.SensorSnapshot(cpu=50, drive=None, nvme=None)
    pwm = fan_control.compute_pwm_linear(50, cfg)
    # delta == 2 exactly is NOT > 2 -> no write
    d = fan_control.decide(snap, cfg, last_pwm=pwm - 2)
    assert d.should_write is False
    # delta == 3 > 2 -> write
    d2 = fan_control.decide(snap, cfg, last_pwm=pwm - 3)
    assert d2.should_write is True


def test_first_write_always_happens(make_config):
    cfg = make_config()
    snap = fan_control.SensorSnapshot(cpu=45, drive=None, nvme=None)
    d = fan_control.decide(snap, cfg, last_pwm=None)
    assert d.should_write is True


def test_snapshot_max_temp():
    assert fan_control.SensorSnapshot(None, None, None).max_temp is None
    assert fan_control.SensorSnapshot(40, 55, None).max_temp == 55
    assert fan_control.SensorSnapshot(None, None, -5).max_temp == -5