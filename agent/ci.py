"""GitHub Actions helpers: annotations and the job summary.

Annotations show on a run's page without signing in, so on a public repo the
results of every run (and every doctor check) can be read at a glance.
Never put secret values in them.
"""

from __future__ import annotations

import os


def in_actions() -> bool:
    return os.getenv("GITHUB_ACTIONS") == "true"


def _esc(text: str, prop: bool = False) -> str:
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def annotate(level: str, title: str, message: str) -> None:
    """level: notice | warning | error. No-op outside GitHub Actions."""
    if in_actions():
        print(f"::{level} title={_esc(title, prop=True)}::{_esc(message)[:900]}", flush=True)


def summary(markdown: str) -> None:
    """Append markdown to the run's summary page. No-op outside GitHub Actions."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if in_actions() and path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown.rstrip() + "\n\n")
