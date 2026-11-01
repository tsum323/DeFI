"""Stage 10: run the dashboard (read-only API + built frontend).

  (cd frontend && npm ci && npm run build)   # once, or after frontend changes
  python -m scripts.dashboard [--port 8000]  # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("backend.api.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
