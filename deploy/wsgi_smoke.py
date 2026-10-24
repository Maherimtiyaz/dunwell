"""Serve the WSGI entry point with the standard library, for verification.

    python deploy/wsgi_smoke.py --port 8997

Why this exists: PythonAnywhere imports ``dunwell.wsgi:application`` instead of
running ``python -m dunwell.serve``. That is a different transport, and a
transport bug is invisible to tests that call the application object directly.
This runs the *same* callable against a real socket, so the production path can
be exercised without a PythonAnywhere account.

Configuration is whatever the environment already holds; nothing is
overridden here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from wsgiref.simple_server import make_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dunwell.wsgi import application  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8997)
    args = parser.parse_args(argv)

    with make_server(args.host, args.port, application) as httpd:
        print(f"WSGI app on http://{args.host}:{args.port}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
