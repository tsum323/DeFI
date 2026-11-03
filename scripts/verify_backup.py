"""Backup integrity: rebuild a fresh database ONLY from the committed snapshots and compare
it with the working database (content hashes of the pool events up to the snapshot's last
block, row counts of every research table).

  python -m scripts.verify_backup [--tmp DIR]
"""
import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from backend.data_engine.snapshot import restore_pool
from backend.database import schema as S
from backend.database.research_snapshot import TABLES, restore_research

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--tmp", default=None)
args = ap.parse_args()
tmp = Path(args.tmp or tempfile.mkdtemp()) / "restored.sqlite"
tmp.unlink(missing_ok=True)
t0 = time.time()
eng = S.make_engine(f"sqlite:///{tmp}")
print("pool:", restore_pool(eng, ROOT / "data_snapshots" / "wbtc_usdc_3000"))
print("research:", restore_research(eng, ROOT / "data_snapshots" / "research_db"))
last = json.loads((ROOT / "data_snapshots/wbtc_usdc_3000/manifest.json").read_text())["coverage"]["last_block"]
a, b = sqlite3.connect(ROOT / "data/lab.sqlite"), sqlite3.connect(tmp)
def h(conn, sql):
    m = hashlib.sha256()
    for r in conn.execute(sql):
        m.update(repr(r).encode())
    return m.hexdigest()[:16]
ok = True
checks = [("raw_logs", f"select block_number, log_index, tx_hash, topics, data from raw_logs where block_number <= {last} order by block_number, log_index"),
          ("ev_swap", f"select * from ev_swap where block_number <= {last} order by block_number, log_index"),
          ("ev_mint", f"select * from ev_mint where block_number <= {last} order by block_number, log_index"),
          ("ev_burn", f"select * from ev_burn where block_number <= {last} order by block_number, log_index"),
          ("blocks", f"select number, hash, timestamp, base_fee_per_gas from blocks where number <= {last} order by number")]
for name, sql in checks:
    try:
        x, y = h(a, sql), h(b, sql)
    except sqlite3.Error as e:
        print(name, "ERR", e); ok = False; continue
    print(f"{name:12s} working={x} restored={y} {'OK' if x == y else 'MISMATCH'}"); ok &= x == y
for t in TABLES:
    x = a.execute(f"select count(*) from {t}").fetchone()[0]; y = b.execute(f"select count(*) from {t}").fetchone()[0]
    print(f"{t:24s} working={x} restored={y} {'OK' if x == y else 'DIFF'}"); ok &= x == y
print("RESULT:", "BACKUP VERIFIED" if ok else "BACKUP INCOMPLETE", f"({time.time() - t0:.0f}s)")
