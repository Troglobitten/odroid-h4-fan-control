"""The shipped systemd unit must stay in step with the script and the README."""

import pathlib
import shlex

import fan_control

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNIT = (ROOT / "fancontrol.service").read_text(encoding="utf-8")


def _exec_start():
    lines = [line for line in UNIT.splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    return shlex.split(lines[0][len("ExecStart=") :])


def test_unit_exec_start_is_accepted_by_the_cli():
    argv = _exec_start()
    assert argv[:2] == ["/usr/bin/python3", "/opt/odroid-fan/fan_control.py"]
    cfg = fan_control.parse_cli(argv[2:])  # exits with code 2 on any bad flag
    assert cfg.overheat is True


def test_readme_test_run_uses_the_unit_flags():
    # Step 5 promises "exactly the flags the service uses".
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    flags = " ".join(_exec_start()[2:])
    assert f"sudo python3 /opt/odroid-fan/fan_control.py {flags}\n" in readme


def test_unit_starts_after_module_loading_and_limits_restarts():
    assert "After=systemd-modules-load.service" in UNIT
    assert "StartLimitBurst=" in UNIT
    assert "WantedBy=multi-user.target" in UNIT


def test_fan_curve_pictures_match_the_unit_file():
    # README embeds docs/fan-curve-*.svg; they are generated from the unit's
    # flags and the real curve code and must be refreshed when either changes.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_fan_curve", ROOT / "docs" / "make_fan_curve.py"
    )
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    cfg, flags = gen.service_config()
    for name, theme in gen.THEMES.items():
        committed = (ROOT / "docs" / f"fan-curve-{name}.svg").read_text(encoding="utf-8")
        assert committed == gen.render(theme, cfg, flags), (
            f"docs/fan-curve-{name}.svg is stale: run python3 docs/make_fan_curve.py"
        )
