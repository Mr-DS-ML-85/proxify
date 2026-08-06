"""
Proxify — Main Entry Point

Starts both the HTTP/HTTPS proxy server and the FastAPI REST/WebSocket API
concurrently using asyncio.

Usage:
    python main.py
"""

import asyncio
import logging
import signal
import sys
import os

# Add project root to path
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)

# Guard: the Lib++ package (installed as Lib_plus_plus / symlinked Lib__)
# contains its own bare `engine/`, `strategies/`, etc. directories that would
# SHADOW this project's top-level packages if Lib++ were ever added to sys.path
# ahead of us. Bare imports like `from engine.dom_cleaner import ...` MUST
# resolve to THIS project, not to Lib++'s engine. Pin that invariant up front:
# remove any Lib++ directory from sys.path and assert engine resolves locally.
def _protect_bare_packages() -> None:
    _lib_dir = os.path.join(_PROJECT_ROOT, "Lib++")
    for entry in list(sys.path):
        if entry and entry.rstrip("/\\") == _lib_dir:
            sys.path.remove(entry)
    if "Lib++" in sys.modules:
        sys.modules.pop("Lib++", None)
    import engine as _engine_pkg
    if os.path.dirname(os.path.abspath(_engine_pkg.__file__)) != _PROJECT_ROOT:
        raise RuntimeError(
            "engine resolved to a Lib++ copy (%s) instead of %s — sys.path is "
            "shadowed; refusing to start" % (_engine_pkg.__file__, _PROJECT_ROOT)
        )


_protect_bare_packages()

from config import config
from engine.decision_engine import DecisionEngine
from gateway.proxy_server import ProxyServer
from gateway.rest_api import create_rest_app
from gateway.websocket_api import register_websocket


def setup_logging() -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s │ %(levelname)-7s │ %(name)-30s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Suppress verbose asyncio SSL eof_received warnings
    class SuppressAsyncioSSLEOFWarning(logging.Filter):
        def filter(self, record):
            if "returning true from eof_received() has no effect when using ssl" in record.getMessage():
                return False
            return True
    
    logging.getLogger("asyncio").addFilter(SuppressAsyncioSSLEOFWarning())
    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def start_api_server(app, host: str, port: int) -> None:
    """Start the FastAPI server using uvicorn."""
    import uvicorn
    uv_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    await server.serve()


def ensure_gui_browser() -> None:
    """Launch the persistent HEADFUL GUI Chrome (Xvfb + CDP :9222) if it is
    not already running. Gives Google-class anti-bots a real desktop browser
    with persistent cookies/cache instead of a fresh headless launch."""
    import shutil
    import subprocess

    if not os.getenv("GUI_CHROME_ENABLED", "true").lower() == "true":
        return
    # Only run inside the container (Xvfb available)
    if not shutil.which("Xvfb"):
        return
    try:
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            return  # already up
        except Exception:
            pass
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "gui_browser.sh")
        if os.path.exists(script):
            subprocess.Popen(["bash", script],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            logging.getLogger("main").info(
                "GUI Chrome launcher spawned (Xvfb :99 + CDP :9222)"
            )
    except Exception as e:
        logging.getLogger("main").debug(f"ensure_gui_browser: {e}")


async def run() -> None:
    """Main async entry point."""
    logger = logging.getLogger("main")

    # Print banner
    print("""
╔══════════════════════════════════════════════════════════════════╗
║              🔱  PROXY ORCHESTRATOR  v1.0.0  🔱                ║
║         Anti-Bot Bypass Gateway & Universal Web Fetcher        ║
╠══════════════════════════════════════════════════════════════════╣
║  Proxy Server  : http://0.0.0.0:{:<26s}║
║  REST API      : http://0.0.0.0:{:<26s}║
║  WebSocket API : ws://0.0.0.0:{:<27s}║
║  Health Check  : http://0.0.0.0:{:<26s}║
║  Metrics       : http://0.0.0.0:{:<26s}║
╠══════════════════════════════════════════════════════════════════╣
║  Strategies    : {:<43s} ║
║  FlareSolverr  : {:<43s} ║
║  Redis L2      : {:<43s} ║
╚══════════════════════════════════════════════════════════════════╝
""".format(
        str(config.PROXY_PORT),
        str(config.API_PORT),
        f"{config.API_PORT}/ws/fetch",
        f"{config.API_PORT}/health",
        f"{config.API_PORT}/metrics",
        " → ".join(config.STRATEGY_ORDER),
        "Enabled" if config.FLARESOLVERR_ENABLED else "Disabled",
        "Enabled" if config.REDIS_ENABLED else "Disabled",
    ))

    # Launch the persistent headful GUI Chrome (Xvfb :99 + CDP :9222) — the
    # final backup strategy. Non-blocking; if it fails the pipeline still works.
    ensure_gui_browser()

    # Initialize Decision Engine
    engine = DecisionEngine()
    await engine.initialize()

    # Create proxy server
    proxy = ProxyServer(engine, port=config.PROXY_PORT)
    await proxy.start()

    # Create FastAPI app with REST + WebSocket
    app = create_rest_app(engine)
    register_websocket(app, engine)

    # Handle shutdown
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start API server. Whichever of these finishes first ends the run — if the
    # server dies, waiting on shutdown_event alone would hang forever, since
    # uvicorn replaces the SIGINT handler that sets it.
    server_task = asyncio.create_task(
        start_api_server(app, config.API_HOST, config.API_PORT)
    )
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        done, pending = await asyncio.wait(
            {server_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc:
                logger.error(f"API server exited with error: {exc}")
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down services...")
        await proxy.stop()
        await engine.shutdown()
        logger.info("Proxify stopped.")


def main():
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
