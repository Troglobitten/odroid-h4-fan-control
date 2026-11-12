"""Import-safety regression: fan_control must import with zero side effects.

The historical endless-pytest-loop bug was caused by import-time parse_args()
plus the infinite main() loop firing during collection. This test guards the
fix: importing the module must not parse argv, open sysfs, or start a loop.
"""

import os
import subprocess
import sys

# Repo root, so the fresh-interpreter subprocess import works regardless of
# the directory pytest was invoked from (L1).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_import_has_no_side_effects():
    # Fresh interpreter: importing must print nothing, exit 0, and return fast.
    # We deliberately do NOT pass -h or any argv that would trigger argparse.
    result = subprocess.run(
        [sys.executable, "-c", "import fan_control"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"import produced output: {result.stdout!r}"
    assert result.stderr == "", f"import produced errors: {result.stderr!r}"


def test_module_exposes_expected_api():
    import fan_control

    for name in (
        "FanConfig",
        "SensorSnapshot",
        "LoopDecision",
        "LoopDeps",
        "LoopState",
        "build_parser",
        "parse_cli",
        "decide",
        "run_loop_iteration",
        "scale_temp",
        "compute_pwm_linear",
        "compute_pwm_log",
        "compute_pwm_exp",
        "compute_pwm",
        "compute_pwm_from_temp",
        "read_cpu_temp",
        "read_nvme_temp",
        "read_drive_temp",
        "find_pwm_path",
        "get_pwm_path",
        "write_pwm",
        "ensure_manual_pwm_mode",
        "main",
    ):
        assert hasattr(fan_control, name), f"missing {name}"


def test_no_removed_globals():
    import fan_control

    for name in (
        "args",
        "parser",
        "SLEEP_DURATION",
        "PWM_MIN",
        "PWM_MAX",
        "TMIN",
        "TMAX",
        "OVERHEAT_THRESHOLD",
        "OVERHEAT_MARGIN",
        "PWM_SYSFS",
    ):
        assert not hasattr(fan_control, name), f"stale module-level {name} present"