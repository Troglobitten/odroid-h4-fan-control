"""Log gating: forced on PWM/fail-safe state change, 30 s heartbeat,
one ERROR on entering fail-safe, rate-limited shutdown-failure errors."""

import fan_control


def _deps(reads, clock_times, shutdown=None):
    """LoopDeps with scripted temperature readings and a scripted clock.

    The clock is consumed exactly once per run_loop_iteration call.
    """
    reads_iter = iter(reads)
    clock_iter = iter(clock_times)
    return fan_control.LoopDeps(
        read_cpu=lambda: next(reads_iter),
        read_nvme=lambda: None,
        read_drive=lambda: None,
        write_pwm_fn=lambda v, c, p: True,
        shutdown_fn=shutdown or (lambda: True),
        sleep_fn=lambda s: None,
        clock=lambda: next(clock_iter),
        pwm_path="/fake/pwm1",
    )


def _run(deps, config, n):
    state = fan_control.LoopState()
    for _ in range(n):
        state = fan_control.run_loop_iteration(deps, state, config)
    return state


def test_status_heartbeats_are_rate_limited(make_config, caplog):
    cfg = make_config()
    # 16 iterations 2 s apart (t = 0, 2, ... 30): only the first iteration
    # and the 30 s heartbeat produce a status line.
    times = [float(t) for t in range(0, 32, 2)]
    deps = _deps([55] * len(times), times)
    with caplog.at_level("INFO"):
        _run(deps, cfg, len(times))
    status_lines = [r for r in caplog.records if "CPU:" in r.message]
    assert len(status_lines) == 2


def test_status_line_forced_on_pwm_change(make_config, caplog):
    cfg = make_config(pwm_hysteresis=2)
    # 45 C -> pwm ~90; jump to 75 C -> pwm ~231: far beyond hysteresis,
    # so the change must be logged immediately (well before 30 s).
    reads = [45, 45, 75, 75]
    times = [0.0, 2.0, 4.0, 6.0]
    deps = _deps(reads, times)
    with caplog.at_level("INFO"):
        _run(deps, cfg, 4)
    status_lines = [r for r in caplog.records if "CPU:" in r.message]
    assert len(status_lines) == 2  # initial line + forced transition line


def test_fail_safe_error_logged_once_not_per_iteration(make_config, caplog):
    cfg = make_config()
    reads = [45, None, None, None, None]
    times = [0.0, 2.0, 4.0, 6.0, 32.0]
    deps = _deps(reads, times)
    with caplog.at_level("INFO"):
        _run(deps, cfg, 5)
    errors = [
        r for r in caplog.records if r.levelname == "ERROR" and "fail-safe" in r.message
    ]
    assert len(errors) == 1  # exactly one ERROR on entering fail-safe (M1)
    # the heartbeat still carries the fail-safe marker afterwards (t = 32 s)
    heartbeat = [
        r
        for r in caplog.records
        if r.levelname == "INFO" and "fail-safe" in r.message
    ]
    assert len(heartbeat) >= 2  # transition line + 30 s heartbeat


def test_no_status_lines_when_short_lived(make_config, caplog):
    # Two quiet iterations inside the heartbeat window: only the initial log.
    cfg = make_config()
    deps = _deps([45, 45], [0.0, 2.0])
    with caplog.at_level("INFO"):
        _run(deps, cfg, 2)
    status_lines = [r for r in caplog.records if "CPU:" in r.message]
    assert len(status_lines) == 1


def test_shutdown_failure_error_rate_limited(make_config, caplog):
    cfg = make_config(overheat=True, overheat_threshold=50)
    times = [0.0, 2.0, 4.0, 6.0]  # 4 iterations, 2 s apart
    deps = _deps([96, 96, 96, 96], times, shutdown=lambda: False)
    with caplog.at_level("INFO"):
        _run(deps, cfg, 4)
    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    errors = [
        r for r in caplog.records if r.levelname == "ERROR" and "shutdown attempt" in r.message
    ]
    assert len(criticals) == 1  # logged once on the first attempt
    assert len(errors) == 1  # failure spam limited to one per 30 s
