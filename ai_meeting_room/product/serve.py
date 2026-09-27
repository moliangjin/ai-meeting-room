"""Run the local Phase 2 product shell."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..persistence.sqlite_store import SQLiteStore
from ..brain.local_bridge import LocalBrainBridgeServer
from .. import __version__
from ..data_paths import ProductDataPathError, resolve_product_paths_from_environment
from .operations import ProductDataPaths, configure_product_logging
from .app import Phase2Application
from .server import make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-meeting-room-product")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    resolved_environment_paths = resolve_product_paths_from_environment()
    parser.add_argument("--db", default=str(resolved_environment_paths.database))
    parser.add_argument("--cao-url", default=os.environ.get("CAO_BASE_URL", "http://127.0.0.1:9889"))
    parser.add_argument("--bridge-port", type=int, default=9890)
    parser.add_argument("--no-legacy-extension-bridge", action="store_true")
    args = parser.parse_args(argv)
    database = Path(args.db).expanduser().resolve()
    if os.environ.get("AI_MEETING_ROOM_DATA_DIR") and database != resolved_environment_paths.database:
        raise ProductDataPathError("PRODUCT_PATH_ESCAPES_OVERRIDE_ROOT")
    paths = ProductDataPaths.from_database(database)
    paths.validate_containment()
    paths.ensure()
    logger = configure_product_logging(paths)
    logger.info("startup version=%s host=%s port=%s", __version__, args.host, args.port)
    app = Phase2Application(SQLiteStore(paths.database), cao_base_url=args.cao_url)
    # Reuse a healthy canonical CAO, or ask the Product-owned lifecycle manager
    # to start it before the first UI/provider readiness request. Recovery
    # failures are retained as structured blocked status; the Product Shell
    # still starts so users can see the cause and retry safely.
    app.initialize_startup_runtime()
    server = make_server(app, args.host, args.port)
    bridge_server = None
    if not args.no_legacy_extension_bridge:
        bridge_server = LocalBrainBridgeServer(app.local_brain_bridge, host="127.0.0.1", port=args.bridge_port)
    print(f"AI Meeting Room product shell: http://{args.host}:{args.port}", flush=True)
    try:
        if bridge_server is not None:
            bridge_server.start()
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        logger.info("shutdown requested")
        server.server_close()
        if bridge_server is not None:
            bridge_server.close()
        app.close()
        logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
