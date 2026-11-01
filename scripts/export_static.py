"""Export the dashboard as a static snapshot (no server needed).

  (cd frontend && VITE_STATIC=1 npx vite build --base=./ --outDir dist-static)
  python -m scripts.export_static [--out DIR] [--details 30] [--site]

--site also copies the result to site/, which GitHub Pages publishes from main
(.github/workflows/pages.yml) at https://<owner>.github.io/<repo>/.

Writes every API response as data/*.json next to the static build, with strategy
charts for the most robust, the promising and the forward-tested strategies.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import app

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "frontend" / "dist-static"))
    ap.add_argument("--details", type=int, default=30)
    ap.add_argument("--site", action="store_true", help="copy to site/ for GitHub Pages")
    args = ap.parse_args()
    out = Path(args.out)
    if not (out / "index.html").exists():
        raise SystemExit(f"{out}: build the static frontend first (see docstring)")
    data = out / "data"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    c = TestClient(app)

    def get(path):
        r = c.get(path)
        r.raise_for_status()
        return r.json()
    docs = {n: get(f"/api/{n}") for n in ("overview", "strategies", "research", "signals", "portfolio",
                                          "datasources")}
    rows = docs["strategies"]["rows"]
    names = {r["name"]: r["spec_hash"] for r in rows}
    want = [r["spec_hash"] for r in rows[:args.details]]
    want += [names[n] for n in docs["overview"]["promising_names"] if n in names]
    for f in docs["portfolio"]["forward_tests"]:
        if f["result"] and f["result"].get("strategy") in names:
            want.append(names[f["result"]["strategy"]])
    want += [r["spec_hash"] for r in rows if r["name"] == "passive_full_range"]
    want = list(dict.fromkeys(want))
    t0 = time.monotonic()
    for i, h in enumerate(want, 1):
        (data / f"strategy_{h}.json").write_text(json.dumps(get(f"/api/strategies/{h}")))
        print(f"  details {i}/{len(want)} {time.monotonic() - t0:.0f}s", flush=True)
    have = set(want)
    for r in rows:
        r["has_details"] = r["spec_hash"] in have
    for n, d in docs.items():
        (data / f"{n}.json").write_text(json.dumps(d))
    ds = docs["datasources"]
    (data / "snapshot.json").write_text(json.dumps({"exported_at": int(time.time()),
                                                    "last_block": ds["last_block"],
                                                    "last_block_ts": ds["last_block_ts"]}))
    # the artifact host rejects files containing U+FFFD; Plotly has it only inside string
    # literals, where the escape sequence is equivalent
    for js in (out / "assets").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        if "\ufffd" in text:
            js.write_text(text.replace("\ufffd", "\\ufffd"), encoding="utf-8")
    size = sum(p.stat().st_size for p in data.iterdir())
    print(f"exported {len(list(data.iterdir()))} files, {size / 1e6:.1f} MB -> {data}")
    if args.site:
        site = ROOT / "site"
        shutil.rmtree(site, ignore_errors=True)
        shutil.copytree(out, site)
        (site / ".nojekyll").write_text("")  # serve files as they are
        print(f"copied to {site.relative_to(ROOT)}/ (published by GitHub Pages from main)")


if __name__ == "__main__":
    main()
