"""Entry point: ``python3 -m conductor`` (or ``./conductor.sh``).

Starts the supervisor thread and the web UI. Ctrl-C / SIGTERM stops the
conductor but deliberately leaves running agents alone: they write to files,
not to us, and are re-attached on the next start.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .config import Config
from .db import Store
from .server import serve
from .service import Service
from .supervisor import Supervisor


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("Conductor needs Python 3.11 or newer (it reads policy files with tomllib); "
              f"this is {sys.version.split()[0]}.", file=sys.stderr)
        return 2
    cfg = Config()
    ap = argparse.ArgumentParser(prog="conductor", description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--home", default=str(cfg.home), help="data directory (CONDUCTOR_HOME)")
    ap.add_argument("--max-parallel", type=int, default=cfg.max_parallel)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    from pathlib import Path
    cfg.home = Path(args.home).expanduser()
    cfg.host, cfg.port, cfg.max_parallel = args.host, args.port, args.max_parallel
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("conductor")

    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    sup = Supervisor(cfg, store)
    svc = Service(cfg, store, sup)
    try:
        httpd = serve(svc, cfg.host, cfg.port)
    except OSError as e:
        log.error("cannot listen on %s:%s: %s", cfg.host, cfg.port, e)
        return 1
    sup.start()
    log.info("conductor ready: http://%s:%s  (data in %s)", cfg.host, cfg.port, cfg.home)

    def shutdown(signum, _frame):
        log.info("shutting down (agents keep running; they will be re-attached)")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        sup.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
