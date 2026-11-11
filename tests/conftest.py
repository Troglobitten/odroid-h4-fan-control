"""Shared fixtures. All hardware I/O is faked; nothing touches real sysfs."""

import os
import sys

import pytest

# Ensure repo root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fan_control


@pytest.fixture(autouse=True)
def _reset_missing_cache():
    """Reset the module-level negative-result cache between tests."""
    fan_control._missing_path_cache.clear()
    fan_control._implausible_logged.clear()
    yield
    fan_control._missing_path_cache.clear()
    fan_control._implausible_logged.clear()


@pytest.fixture
def make_config():
    """Factory for a valid FanConfig."""

    def _make(**overrides):
        defaults = {
            "sleep": 2,
            "overheat": False,
            "curve": "lin",
            "pwm_min": 20,
            "pwm_max": 255,
            "tmin": 30,
            "tmax": 80,
            "overheat_threshold": 95,
            "pwm_hysteresis": 2,
            "verbose": False,
        }
        defaults.update(overrides)
        return fan_control.FanConfig(**defaults)

    return _make


@pytest.fixture(scope="session", autouse=True)
def _import_has_no_side_effects():
    """Session safety: importing fan_control must not parse args or open sysfs.

    The historical endless-loop bug came from import-time parse_args() + the
    infinite main() loop firing during pytest collection. Importing here must
    be a no-op: no argparse Namespace at module scope, no PWM_SYSFS probe.
    """
    import importlib

    importlib.reload(fan_control)
    assert not hasattr(fan_control, "args"), "module must not call parse_args() at import"
    assert not hasattr(fan_control, "PWM_SYSFS"), "module must not probe sysfs at import"
    assert fan_control._pwm_path_resolved is False, "PWM path must be lazy"