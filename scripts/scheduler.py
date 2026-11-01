"""Stage 11 / SPEC 16: automatic research mode.

  python -m scripts.scheduler status            # jobs, last run, next run
  python -m scripts.scheduler run               # run forever (one tick per minute)
  python -m scripts.scheduler once              # run due jobs once (for cron)
  python -m scripts.scheduler run-now JOB       # manual run of one job
  python -m scripts.scheduler pause | resume    # stop / continue scheduled runs
  python -m scripts.scheduler log [--n 20]      # job run journal
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from backend.core.config import load_config
from backend.automation.scheduler import Scheduler, jobs_from_config
from backend.database import schema as S


def _t(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "run", "once", "run-now", "pause", "resume", "log"])
    ap.add_argument("job", nargs="?")
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    cfg = load_config()
    sc = cfg.lab["scheduler"]
    engine = S.make_engine(cfg.lab["database"]["url"])
    s = Scheduler(engine, jobs_from_config(cfg), max_parallel_heavy=sc["max_parallel_heavy_jobs"])
    if args.action == "pause":
        s.pause()
        print("paused: scheduled jobs will not start (manual run-now still works)")
    elif args.action == "resume":
        s.resume()
        print("resumed")
    elif args.action == "run-now":
        if args.job not in s.jobs:
            raise SystemExit(f"unknown job {args.job!r}; known: {', '.join(s.jobs)}")
        print(args.job, s.run_now(args.job))
    elif args.action == "once":
        print(s.tick() or ("paused" if s.paused else "nothing due"))
    elif args.action == "run":
        print(f"scheduler running (tick {sc['tick_seconds']} s); Ctrl+C to stop")
        try:
            s.loop(sc["tick_seconds"])
        except KeyboardInterrupt:
            print("stopped")
    elif args.action == "log":
        with engine.connect() as c:
            for r in c.execute(select(S.job_runs).order_by(S.job_runs.c.id.desc()).limit(args.n)):
                dur = (r.finished_at - r.started_at) if r.finished_at else None
                tail = (r.log or "").strip().splitlines()[-1:] if r.log else []
                print(f"{_t(r.started_at)}  {r.job:16s} {r.trigger:8s} {r.status:8s} "
                      f"{'' if dur is None else f'{dur}s':>6}  {tail[0][:90] if tail else ''}")
    print(f"\n{'paused' if s.paused else 'active'}")
    for row in s.status():
        print(f"  {row['job']:16s} every {row['every_minutes']:>5} min {'heavy' if row['heavy'] else '     '}"
              f"  last: {row['last_status'] or '—':8s} {_t(row['last_started'])}  next in {row['next_due_in_minutes']} min")


if __name__ == "__main__":
    main()
