import plistlib

from agent.schedule import PREFIX, describe, jobs, plist


def test_default_schedule(make_settings):
    s = make_settings()
    j = jobs(s)
    stocks = j[f"{PREFIX}.stocks"]
    assert stocks["args"] == ["run", "--scope", "stocks", "--wait", "120"]
    assert {w["Weekday"] for w in stocks["when"]} == {2, 3, 4, 5, 6}   # Tue..Sat
    assert all((w["Hour"], w["Minute"]) == (8, 30) for w in stocks["when"])

    crypto = j[f"{PREFIX}.crypto"]["when"]
    assert [(w["Hour"], w["Minute"]) for w in crypto] == [
        (3, 15), (7, 15), (11, 15), (15, 15), (19, 15), (23, 15)]      # every 4h, 7 days
    assert all("Weekday" not in w for w in crypto)

    [weekly] = j[f"{PREFIX}.weekly"]["when"]
    assert weekly == {"Weekday": 5, "Hour": 18, "Minute": 0}           # Friday 6pm
    assert "Crypto: every 4h" in describe(s)[1]


def test_plist_is_valid(make_settings, tmp_path):
    s = make_settings()
    for label, job in jobs(s).items():
        data = plistlib.loads(plist(label, job["args"], job["when"], project=tmp_path).encode())
        assert data["Label"] == label
        assert data["ProgramArguments"][:2] == ["/bin/bash", f"{tmp_path}/run.sh"]
        assert data["ProgramArguments"][2:] == job["args"]
        assert data["StartCalendarInterval"] == job["when"]


def test_hourly_crypto(make_settings, tmp_path):
    s = make_settings()
    s.crypto_every_hours = 1
    crypto = jobs(s)[f"{PREFIX}.crypto"]["when"]
    assert len(crypto) == 24


def test_github_workflows_match_config(make_settings):
    """Cron lines in .github/workflows agree with config.yaml and call the shared job."""
    from pathlib import Path

    import yaml

    from agent.settings import load_settings

    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows"
    if not wf.exists():
        wf = root / "ci" / "workflows"   # before scripts/move-to-github.sh installs them
    s = load_settings(project_dir=root)
    crypto = yaml.safe_load((wf / "crypto.yml").read_text())
    cron = crypto[True]["schedule"][0]["cron"]              # PyYAML reads `on:` as True
    assert cron.split()[1] == f"*/{s.crypto_every_hours}"
    for name, task in (("crypto", "crypto"), ("stocks", "stocks"), ("weekly", "weekly")):
        doc = yaml.safe_load((wf / f"{name}.yml").read_text())
        [job] = doc["jobs"].values()
        assert job["uses"] == "./.github/workflows/_run.yml"
        assert job["with"]["task"] == task and job["secrets"] == "inherit"
    stocks = yaml.safe_load((wf / "stocks.yml").read_text())[True]["schedule"][0]["cron"]
    minute, hour, _, _, dow = stocks.split()
    assert (int(hour), dow) == (22, "1-5")                  # after the US close, Mon-Fri US
    shared = yaml.safe_load((wf / "_run.yml").read_text())
    assert shared["jobs"]["run"]["concurrency"]["group"] == "trading-agent"
