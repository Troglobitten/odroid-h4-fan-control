#!/usr/bin/env python3
"""Render the fan curve of the shipped service configuration as SVG.

The flags are read from ``fancontrol.service`` and the points are computed with
the real curve functions, so the picture cannot drift from the code:

    python3 docs/make_fan_curve.py

writes ``docs/fan-curve-light.svg`` and ``docs/fan-curve-dark.svg``.
"""

from __future__ import annotations

import pathlib
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fan_control

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "border": "#e1e0d9",
        "text": "#0b0b0b",
        "text2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": "#2a78d6",
    },
    "dark": {
        "surface": "#1a1a19",
        "border": "#2c2c2a",
        "text": "#f0efec",
        "text2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": "#3987e5",
    },
}

WIDTH, HEIGHT = 720, 400
LEFT, RIGHT, TOP, BOTTOM = 64, 696, 96, 338
T_MIN, T_MAX = 20, 100  # x domain in °C
P_MAX = 270  # y domain 0..P_MAX (PWM tops out at 255)
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def service_config() -> tuple[fan_control.FanConfig, str]:
    unit = (ROOT / "fancontrol.service").read_text(encoding="utf-8")
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    argv = shlex.split(line[len("ExecStart=") :])[2:]
    flags = " ".join(a for a in argv if a != "-v")
    return fan_control.parse_cli(argv), flags


def x(temp: float) -> float:
    return LEFT + (temp - T_MIN) / (T_MAX - T_MIN) * (RIGHT - LEFT)


def y(pwm: float) -> float:
    return BOTTOM - pwm / P_MAX * (BOTTOM - TOP)


def pwm_at(temp: int, cfg: fan_control.FanConfig) -> int:
    """PWM the daemon applies at ``temp`` (the same decision the loop takes)."""
    snap = fan_control.SensorSnapshot(cpu=temp, drive=None, nvme=None)
    return fan_control.decide(snap, cfg, last_pwm=None).pwm


def render(theme: dict[str, str], cfg: fan_control.FanConfig, flags: str) -> str:
    c = theme
    hot = cfg.overheat_threshold if cfg.overheat else None

    # One vertex per degree; at the overheat threshold the curve steps up.
    pts: list[tuple[float, float]] = []
    for temp in range(T_MIN, T_MAX + 1):
        if hot is not None and temp == hot:
            pts.append((x(temp), y(pwm_at(temp - 1, cfg))))
        pts.append((x(temp), y(pwm_at(temp, cfg))))
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = f"{x(T_MIN):.1f},{y(0):.1f} {line} {x(T_MAX):.1f},{y(0):.1f}"

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="{FONT}" role="img" '
        f'aria-label="Fan curve of the shipped service configuration">'
    )
    out.append(
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="8" '
        f'fill="{c["surface"]}" stroke="{c["border"]}"/>'
    )
    out.append(
        f'<text x="24" y="36" font-size="16" font-weight="600" fill="{c["text"]}">'
        "Fan curve of the shipped service</text>"
    )
    out.append(f'<text x="24" y="58" font-size="12" fill="{c["text2"]}">{flags}</text>')

    # Recessive grid + tick labels.
    for pwm in range(0, 251, 50):
        out.append(
            f'<line x1="{LEFT}" x2="{RIGHT}" y1="{y(pwm):.1f}" y2="{y(pwm):.1f}" '
            f'stroke="{c["axis"] if pwm == 0 else c["grid"]}"/>'
        )
        out.append(
            f'<text x="{LEFT - 8}" y="{y(pwm) + 4:.1f}" font-size="11" text-anchor="end" '
            f'fill="{c["muted"]}">{pwm}</text>'
        )
    for temp in range(T_MIN, T_MAX + 1, 10):
        out.append(
            f'<text x="{x(temp):.1f}" y="{BOTTOM + 18}" font-size="11" text-anchor="middle" '
            f'fill="{c["muted"]}">{temp}</text>'
        )
    out.append(
        f'<text x="{LEFT - 8}" y="{TOP - 14}" font-size="11" text-anchor="end" '
        f'fill="{c["muted"]}">PWM</text>'
    )
    out.append(
        f'<text x="{(LEFT + RIGHT) / 2:.1f}" y="{BOTTOM + 42}" font-size="11" '
        f'text-anchor="middle" fill="{c["muted"]}">'
        "Hottest temperature of CPU / NVMe / drive (°C)</text>"
    )

    # The series: 10 % wash, 2 px line, ringed markers at the breakpoints.
    out.append(f'<polygon points="{area}" fill="{c["series"]}" fill-opacity="0.10"/>')
    out.append(
        f'<polyline points="{line}" fill="none" stroke="{c["series"]}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    labels = [
        (cfg.tmin, cfg.pwm_min, f"PWM {cfg.pwm_min} up to {cfg.tmin} °C", -8, -12),
        (cfg.tmax, cfg.pwm_max, f"PWM {cfg.pwm_max} from {cfg.tmax} °C", -10, -10),
    ]
    if hot is not None:
        labels.append(
            (hot, fan_control.PWM_FULL, f"PWM {fan_control.PWM_FULL} from {hot} °C", -12, 4)
        )
    for temp, pwm, text, dx, dy in labels:
        out.append(
            f'<circle cx="{x(temp):.1f}" cy="{y(pwm):.1f}" r="5" fill="{c["series"]}" '
            f'stroke="{c["surface"]}" stroke-width="2"/>'
        )
        out.append(
            f'<text x="{x(temp) + dx:.1f}" y="{y(pwm) + dy:.1f}" font-size="12" '
            f'font-weight="600" text-anchor="end" fill="{c["text"]}">{text}</text>'
        )
    if hot is not None:
        out.append(
            f'<text x="{x(hot) - 12:.1f}" y="{y(fan_control.PWM_FULL) + 20:.1f}" font-size="11" '
            f'text-anchor="end" fill="{c["text2"]}">overheat: full speed, shutdown after '
            f"{cfg.overheat_confirm} readings in a row</text>"
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    cfg, flags = service_config()
    for name, theme in THEMES.items():
        target = ROOT / "docs" / f"fan-curve-{name}.svg"
        target.write_text(render(theme, cfg, flags), encoding="utf-8", newline="\n")
        print(f"wrote {target.relative_to(ROOT)}")
    print("table:", {t: pwm_at(t, cfg) for t in (30, 40, 50, 60, 70, 80, 90, 95)})


if __name__ == "__main__":
    main()
