"""Automatic research mode (SPEC §16): scheduled jobs with a run log.

- Jobs come from config/lab.yaml (scheduler.jobs): interval, heavy flag, command
  (a `python -m` module of this project), optional dependencies (`after`) and a
  required environment variable (`needs_env`).
- Every run is recorded in `job_runs` (start, end, status, exit code, log tail).
- Heavy jobs take a lock in `scheduler_state`; with max_parallel_heavy_jobs = 1 no
  two heavy computations run at the same time, even across scheduler processes.
  A lock left by a dead process is released.
- Pause / resume (`scheduler_state.paused`) and manual runs (`run_now`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import insert, select, update

from backend.database import schema as S

ROOT = Path(__file__).resolve().parents[2]
LOG_TAIL = 20_000


@dataclass
class Job:
    name: str
    every_minutes: int
    cmd: list[str]
    heavy: bool = False
    after: tuple[str, ...] = ()
    needs_env: str | None = None
    only_if: str | None = None   # "study_outdated": run on schedule only when it is

    @property
    def timeout(self) -> int:
        return 6 * 3600 if self.heavy else 30 * 60


def jobs_from_config(cfg) -> list[Job]:
    out = []
    for name, j in cfg.lab["scheduler"]["jobs"].items():
        out.append(Job(name, int(j["every_minutes"]), list(j["cmd"]), bool(j.get("heavy", False)),
                       tuple(j.get("after", ())), j.get("needs_env"), j.get("only_if")))
    return out


def subprocess_runner(job: Job) -> tuple[int, str]:
    try:
        r = subprocess.run([sys.executable, "-m", *job.cmd], cwd=ROOT, capture_output=True, text=True,
                           timeout=job.timeout)
        return r.returncode, (r.stdout + r.stderr)[-LOG_TAIL:]
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout after {job.timeout}s\n{(exc.stdout or '')[-LOG_TAIL:]}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


class Scheduler:
    def __init__(self, engine, jobs: list[Job], runner: Callable[[Job], tuple[int, str]] = subprocess_runner,
                 clock: Callable[[], float] = time.time, max_parallel_heavy: int = 1):
        if max_parallel_heavy != 1:
            raise ValueError("only max_parallel_heavy_jobs = 1 is supported")
        self.engine, self.runner, self.clock = engine, runner, clock
        self.jobs = {j.name: j for j in jobs}
        for j in jobs:
            for dep in j.after:
                if dep not in self.jobs:
                    raise ValueError(f"{j.name}: unknown dependency {dep}")

    # ----------------------------------------------------------- state
    def _get(self, key: str) -> str | None:
        with self.engine.connect() as c:
            r = c.execute(select(S.scheduler_state.c.value).where(S.scheduler_state.c.key == key)).first()
        return r[0] if r else None

    def _set(self, key: str, value: str) -> None:
        now = int(self.clock())
        with self.engine.begin() as c:
            if c.execute(update(S.scheduler_state).where(S.scheduler_state.c.key == key)
                         .values(value=value, updated_at=now)).rowcount == 0:
                c.execute(insert(S.scheduler_state).values(key=key, value=value, updated_at=now))

    def pause(self) -> None:
        self._set("paused", "1")

    def resume(self) -> None:
        self._set("paused", "0")

    @property
    def paused(self) -> bool:
        return self._get("paused") == "1"

    def _acquire_heavy(self, job: Job) -> bool:
        holder = self._get("heavy_lock") or ""
        if holder:
            name, _, pid = holder.partition(":")
            if pid.isdigit() and _pid_alive(int(pid)) and int(pid) != os.getpid():
                return False
            if name and pid.isdigit() and int(pid) == os.getpid():
                return False  # this process already runs a heavy job
        now = int(self.clock())
        with self.engine.begin() as c:  # compare-and-set on the previous value
            q = update(S.scheduler_state).where(S.scheduler_state.c.key == "heavy_lock",
                                                S.scheduler_state.c.value == holder)
            if c.execute(q.values(value=f"{job.name}:{os.getpid()}", updated_at=now)).rowcount == 0:
                if holder:
                    return False
                try:
                    c.execute(insert(S.scheduler_state).values(key="heavy_lock",
                                                               value=f"{job.name}:{os.getpid()}",
                                                               updated_at=now))
                except Exception:  # noqa: BLE001 - lost the race
                    return False
        return True

    def _release_heavy(self) -> None:
        self._set("heavy_lock", "")

    # ----------------------------------------------------------- runs
    def last_run(self, name: str, ok_only: bool = False):
        q = select(S.job_runs).where(S.job_runs.c.job == name)
        if ok_only:
            q = q.where(S.job_runs.c.status == "ok")
        with self.engine.connect() as c:
            return c.execute(q.order_by(S.job_runs.c.id.desc()).limit(1)).first()

    def due(self, job: Job, now: float) -> bool:
        last = self.last_run(job.name)
        if last is None:
            return True
        if last.status == "running":
            return False
        return now - last.started_at >= job.every_minutes * 60

    def _record(self, job: Job, trigger: str, status: str, code: int | None, log: str,
                started: int, run_id: int | None = None) -> int:
        with self.engine.begin() as c:
            if run_id is None:
                return c.execute(insert(S.job_runs).values(
                    job=job.name, trigger=trigger, started_at=started,
                    finished_at=None if status == "running" else int(self.clock()),
                    status=status, exit_code=code, log=log)).inserted_primary_key[0]
            c.execute(update(S.job_runs).where(S.job_runs.c.id == run_id).values(
                finished_at=int(self.clock()), status=status, exit_code=code, log=log))
            return run_id

    def run(self, name: str, trigger: str = "schedule") -> str:
        job = self.jobs[name]
        started = int(self.clock())
        if job.needs_env and not os.environ.get(job.needs_env):
            self._record(job, trigger, "skipped", None, f"{job.needs_env} is not set", started)
            return "skipped"
        if job.only_if == "study_outdated" and trigger == "schedule":
            from backend.validation.freshness import study_outdated
            why = study_outdated()
            if why is None:
                self._record(job, trigger, "skipped", None,
                             "stored historical study is current: reused, not recomputed", started)
                return "skipped"
        if job.heavy and not self._acquire_heavy(job):
            self._record(job, trigger, "skipped", None, "another heavy job is running", started)
            return "skipped"
        rid = self._record(job, trigger, "running", None, "", started)
        try:
            code, log = self.runner(job)
        except Exception as exc:  # noqa: BLE001 - a crashed runner is a failed run
            code, log = 1, f"{type(exc).__name__}: {exc}"
        finally:
            if job.heavy:
                self._release_heavy()
        status = "ok" if code == 0 else "failed"
        self._record(job, trigger, status, code, log, started, rid)
        return status

    def tick(self) -> dict[str, str]:
        """Run every due job once, dependencies first. Nothing runs while paused."""
        if self.paused:
            return {}
        now = self.clock()
        results: dict[str, str] = {}
        for name in self._order():
            job = self.jobs[name]
            if any(results.get(d) == "failed" for d in job.after):
                continue  # do not act on data whose loading just failed
            if self.due(job, now):
                results[name] = self.run(name)
        return results

    def run_now(self, name: str) -> str:
        return self.run(name, trigger="manual")

    def _order(self) -> list[str]:
        seen, out = set(), []

        def visit(n):
            if n in seen:
                return
            seen.add(n)
            for d in self.jobs[n].after:
                visit(d)
            out.append(n)
        for n in self.jobs:
            visit(n)
        return out

    def status(self) -> list[dict]:
        now = self.clock()
        rows = []
        for name in self._order():
            job, last = self.jobs[name], self.last_run(name)
            nxt = (last.started_at + job.every_minutes * 60) if last else now
            rows.append({"job": name, "every_minutes": job.every_minutes, "heavy": job.heavy,
                         "last_status": last.status if last else None,
                         "last_started": last.started_at if last else None,
                         "next_due_in_minutes": max(0, round((nxt - now) / 60))})
        return rows

    def loop(self, tick_seconds: int = 60, max_ticks: int | None = None) -> None:
        n = 0
        while max_ticks is None or n < max_ticks:
            self.tick()
            n += 1
            if max_ticks is None or n < max_ticks:
                time.sleep(tick_seconds)
