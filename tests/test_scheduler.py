"""Stage 11: scheduler (SPEC 16) with a fake runner and a fake clock (no subprocesses)."""

from __future__ import annotations

import os

from sqlalchemy import select

from backend.automation.scheduler import Job, Scheduler
from backend.database import schema as S


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, runner, jobs=None):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    jobs = jobs or [Job("sync", 15, ["x"]), Job("evaluate", 60, ["y"], after=("sync",)),
                    Job("heavy", 1440, ["z"], heavy=True), Job("ai", 1440, ["w"], heavy=True, needs_env="NO_SUCH_ENV_X")]
    clock = Clock()
    return Scheduler(engine, jobs, runner=runner, clock=clock), engine, clock


def test_schedule_order_log_and_intervals(tmp_path):
    calls = []
    s, engine, clock = make(tmp_path, lambda j: (calls.append(j.name), (0, f"{j.name} ok"))[1])
    r = s.tick()
    assert calls == ["sync", "evaluate", "heavy"] and r["ai"] == "skipped"
    assert s.tick() == {}  # nothing due a moment later
    clock.t += 16 * 60
    assert s.tick() == {"sync": "ok"}
    clock.t += 50 * 60
    assert set(s.tick()) == {"sync", "evaluate"}
    with engine.connect() as c:
        runs = c.execute(select(S.job_runs)).all()
    assert all(r.finished_at is not None for r in runs)
    assert {r.status for r in runs} == {"ok", "skipped"}


def test_failed_dependency_blocks_dependents_and_failure_is_logged(tmp_path):
    s, engine, _ = make(tmp_path, lambda j: (1, "boom") if j.name == "sync" else (0, "ok"))
    r = s.tick()
    assert r["sync"] == "failed" and "evaluate" not in r
    assert s.last_run("sync").log == "boom" and s.last_run("sync").exit_code == 1


def test_pause_resume_and_manual_run(tmp_path):
    calls = []
    s, _, _ = make(tmp_path, lambda j: (calls.append(j.name), (0, ""))[1])
    s.pause()
    assert s.tick() == {} and calls == []
    assert s.run_now("sync") == "ok" and calls == ["sync"]  # manual run works while paused
    s.resume()
    assert "evaluate" in s.tick()


def test_heavy_jobs_never_overlap(tmp_path):
    s, engine, _ = make(tmp_path, lambda j: (0, ""))
    # another live process holds the heavy lock (pid 1 always exists)
    s._set("heavy_lock", "validation:1")
    assert s.run("heavy") == "skipped"
    assert s.last_run("heavy").log == "another heavy job is running"
    # a lock left by a dead process is released
    s._set("heavy_lock", "validation:99999999")
    assert s.run("heavy") == "ok" and (s._get("heavy_lock") or "") == ""
    # while a heavy job runs, a nested heavy job is refused
    inner = []

    def runner(job):
        if job.name == "heavy":
            inner.append(s.run("heavy2"))
        return 0, ""
    s2, _, _ = make(tmp_path / "b", runner, [Job("heavy", 1, ["a"], heavy=True), Job("heavy2", 1, ["b"], heavy=True)])
    s = s2
    assert s2.run("heavy") == "ok" and inner == ["skipped"]
    assert os.getpid()  # sanity


def test_heavy_research_reused_unless_study_outdated(tmp_path, monkeypatch):
    import backend.validation.freshness as F
    calls = []
    s, engine, clock = make(tmp_path, lambda j: (calls.append(j.name), (0, "ok"))[1],
                            [Job("validation", 10080, ["v"], heavy=True, only_if="study_outdated")])
    monkeypatch.setattr(F, "study_outdated", lambda now=None: None)
    assert s.run("validation") == "skipped" and calls == []          # current study: reused
    assert s.run("validation", trigger="manual") == "ok"             # a manual run is allowed
    monkeypatch.setattr(F, "study_outdated", lambda now=None: "result code changed")
    assert s.run("validation") == "ok" and calls == ["validation", "validation"]
    with engine.connect() as c:
        logs = [r.log for r in c.execute(select(S.job_runs).where(S.job_runs.c.status == "skipped"))]
    assert "reused, not recomputed" in logs[0]


def test_study_outdated_rules(tmp_path, monkeypatch):
    import json

    import backend.validation.freshness as F
    d = tmp_path / "data_snapshots" / "validation"
    d.mkdir(parents=True)
    monkeypatch.setattr(F, "ROOT", tmp_path)
    monkeypatch.setattr("backend.strategies.mass_test.code_version", lambda rev="HEAD": "sim-a")
    assert F.study_outdated() == "no stored study"
    (d / "validation_x.json").write_text(json.dumps({"code_version": "sim-a", "segments": [{"end_ts": 1000}]}))
    assert F.study_outdated(now=1000 + 90 * 86400) is None
    assert "new 91-day segment" in F.study_outdated(now=1000 + 91 * 86400)
    monkeypatch.setattr("backend.strategies.mass_test.code_version", lambda rev="HEAD": "sim-b")
    assert "result code changed" in F.study_outdated(now=1000)
