"""Install / remove the macOS launchd jobs that keep the agent running.

Three jobs (times are your Mac's local time; set them in config.yaml > schedule):
- stocks: once a morning Tue–Sat, after the US close (US stocks + ASX signals)
- crypto: every N hours, every day
- weekly: the weekly summary, e.g. Friday 6pm
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from .settings import PROJECT_DIR, WEEKDAYS, Settings

PREFIX = "com.sumit.trading-agent"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
OLD_LABELS = [PREFIX]  # the single daily job from the first version


def _hm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def _launchd_weekday(day: str) -> int:
    return (WEEKDAYS.index(day) + 1) % 7  # launchd: 0 = Sunday, 1 = Monday ...


def jobs(s: Settings) -> dict[str, dict]:
    sh, sm = _hm(s.stocks_time)
    # Tue–Sat mornings in Melbourne follow the Mon–Fri US sessions
    stocks = [{"Weekday": d, "Hour": sh, "Minute": sm} for d in (2, 3, 4, 5, 6)]
    # crypto slots sit 75 minutes before the stocks start, so the two rarely overlap
    crypto = [{"Hour": h, "Minute": (sm + 45) % 60}
              for h in range((sh - 1) % s.crypto_every_hours, 24, s.crypto_every_hours)]
    wh, wm = _hm(s.weekly_time)
    weekly = [{"Weekday": _launchd_weekday(s.weekly_day), "Hour": wh, "Minute": wm}]
    return {
        f"{PREFIX}.stocks": {"args": ["run", "--scope", "stocks", "--wait", "120"],
                             "when": stocks},
        f"{PREFIX}.crypto": {"args": ["run", "--scope", "crypto", "--wait", "60"],
                             "when": crypto},
        f"{PREFIX}.weekly": {"args": ["weekly"], "when": weekly},
    }


def _dict(d: dict) -> str:
    return "<dict>" + "".join(f"<key>{k}</key><integer>{v}</integer>" for k, v in d.items()) + "</dict>"


def plist(label: str, args: list[str], when: list[dict], project: Path = PROJECT_DIR) -> str:
    name = label.rsplit(".", 1)[-1]
    argv = "".join(f"<string>{a}</string>" for a in ["/bin/bash", f"{project}/run.sh", *args])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{argv}</array>
  <key>WorkingDirectory</key><string>{project}</string>
  <key>StartCalendarInterval</key>
  <array>{''.join(_dict(w) for w in when)}</array>
  <key>StandardOutPath</key><string>{project}/logs/launchd-{name}.out.log</string>
  <key>StandardErrorPath</key><string>{project}/logs/launchd-{name}.err.log</string>
</dict>
</plist>
"""


def describe(s: Settings) -> list[str]:
    j = jobs(s)
    crypto_times = ", ".join(f"{w['Hour']:02d}:{w['Minute']:02d}" for w in j[f"{PREFIX}.crypto"]["when"])
    return [f"US stocks + ASX: Tue–Sat at {s.stocks_time}",
            f"Crypto: every {s.crypto_every_hours}h, every day ({crypto_times})",
            f"Weekly summary: {s.weekly_day} at {s.weekly_time}"]


def _bootout(label: str, path: Path) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)


def install(s: Settings) -> None:
    if platform.system() != "Darwin":
        raise SystemExit("install-schedule only works on macOS")
    (PROJECT_DIR / "logs").mkdir(exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for old in OLD_LABELS:
        p = AGENTS_DIR / f"{old}.plist"
        if p.exists():
            _bootout(old, p)
            p.unlink()
    for label, job in jobs(s).items():
        path = AGENTS_DIR / f"{label}.plist"
        _bootout(label, path)
        path.write_text(plist(label, job["args"], job["when"]), encoding="utf-8")
        r = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"launchctl failed for {label}: {r.stderr.strip()}")
    print("Schedules installed (your Mac's local time):")
    for line in describe(s):
        print(f"  - {line}")
    print("If the Mac is asleep at a scheduled time, that run starts when it wakes.\n"
          "If it is shut down, runs are skipped. Keep it on power and awake for 24/7 crypto.")


def installed() -> dict[str, bool]:
    return {label: (AGENTS_DIR / f"{label}.plist").exists()
            for label in [f"{PREFIX}.stocks", f"{PREFIX}.crypto", f"{PREFIX}.weekly", PREFIX]}


def uninstall() -> None:
    removed = 0
    for label in [f"{PREFIX}.stocks", f"{PREFIX}.crypto", f"{PREFIX}.weekly", *OLD_LABELS]:
        p = AGENTS_DIR / f"{label}.plist"
        if p.exists():
            _bootout(label, p)
            p.unlink()
            removed += 1
    print(f"Removed {removed} schedule(s)." if removed else "No schedules installed.")
