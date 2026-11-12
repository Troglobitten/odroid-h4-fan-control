"""Bounded main-loop runs: sleep-last, exception backoff, KeyboardInterrupt,
shutdown only with flag. No test may leave a loop running."""

import threading

import fan_control


def _make_deps(read_cpu=None, writer=None, shutdown=None, sleep=None, clock=None):
    return fan_control.LoopDeps(
        read_cpu=read_cpu or (lambda: 45),
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=writer or (lambda v, c, p: True),
        shutdown_fn=shutdown or (lambda: True),
        sleep_fn=sleep or (lambda s: None),
        clock=clock or (lambda: 100.0),
        pwm_path="/fake/pwm1",
    )


def test_bounded_loop_runs_exactly_max_iterations(make_config):
    cfg = make_config()
    sleeps = []
    deps = _make_deps(sleep=sleeps.append)
    fan_control.main(cfg, max_iterations=3, deps=deps)
    assert len(sleeps) == 3
    # sleep is the last call in each iteration — it must have happened
    assert all(s == cfg.sleep for s in sleeps)


def test_stop_event_halts_loop(make_config):
    cfg = make_config()
    ev = threading.Event()
    calls = {"n": 0}

    def sleep_and_stop(s):
        calls["n"] += 1
        ev.set()

    deps = _make_deps(sleep=sleep_and_stop)
    fan_control.main(cfg, stop_event=ev, deps=deps)
    assert calls["n"] == 1


def test_exception_backoff_uses_configured_sleep(make_config):
    cfg = make_config(sleep=2)
    sleeps = []

    def boom():
        raise RuntimeError("sensor exploded")

    deps = _make_deps(read_cpu=boom, sleep=sleeps.append)
    fan_control.main(cfg, max_iterations=2, deps=deps)
    # backoff = max(config.sleep, 5) = 5
    assert sleeps == [5, 5]


def test_keyboard_interrupt_clean_exit(make_config, caplog):
    cfg = make_config()

    def interrupted():
        raise KeyboardInterrupt

    deps = _make_deps(read_cpu=interrupted)
    with caplog.at_level("INFO"):
        fan_control.main(cfg, max_iterations=5, deps=deps)  # must not raise
    assert any("interrupted" in r.message.lower() for r in caplog.records)


def test_shutdown_never_invoked_without_flag(make_config):
    cfg = make_config(overheat=False, overheat_threshold=0)
    shutdown_calls = {"n": 0}
    deps = _make_deps(read_cpu=lambda: 150, shutdown=lambda: shutdown_calls.__setitem__("n", shutdown_calls["n"] + 1))
    fan_control.main(cfg, max_iterations=3, deps=deps)
    assert shutdown_calls["n"] == 0


def test_shutdown_invoked_once_with_flag(make_config):
    cfg = make_config(overheat=True, overheat_threshold=95)
    shutdown_calls = {"n": 0}

    def do_shutdown():
        shutdown_calls["n"] += 1
        return True

    deps = _make_deps(read_cpu=lambda: 96, shutdown=do_shutdown)
    fan_control.main(cfg, max_iterations=10, deps=deps)
    # loop must break after shutdown; only one invocation (#2 no double shutdown)
    assert shutdown_calls["n"] == 1


def test_shutdown_failure_retries_and_loop_keeps_running(make_config, caplog):
    # H2: a failed shutdown must NOT stop the daemon — it retries every
    # cycle while the fan stays under control at pwm_max.
    cfg = make_config(overheat=True, overheat_threshold=95, overheat_confirm=1)
    shutdown_calls = {"n": 0}

    def fail_shutdown():
        shutdown_calls["n"] += 1
        return False

    deps = _make_deps(read_cpu=lambda: 96, shutdown=fail_shutdown)
    with caplog.at_level("INFO"):
        fan_control.main(cfg, max_iterations=3, deps=deps)
    assert shutdown_calls["n"] == 3  # loop did not exit early
    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(criticals) == 1  # critical logged once, not per attempt


def test_successful_write_advances_cache_and_skips_repeat(make_config):
    cfg = make_config(tmin=30, tmax=80)
    writes = []
    deps = _make_deps(
        read_cpu=lambda: 55, writer=lambda v, c, p: writes.append(v) or True
    )
    fan_control.main(cfg, max_iterations=5, deps=deps)
    # with a stable 55 °C reading, hysteresis means only the first write happens
    assert len(writes) == 1