"""
WebSocket API — Real-time streaming fetch endpoint for AI agents.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from engine.decision_engine import DecisionEngine
from strategies.base import FetchRequest

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections for real-time fetch streaming."""

    def __init__(self, engine: DecisionEngine) -> None:
        self._engine = engine
        self._active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection."""
        await websocket.accept()
        self._active_connections.append(websocket)
        logger.info(f"WebSocket connected. Active: {len(self._active_connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket."""
        if websocket in self._active_connections:
            self._active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Active: {len(self._active_connections)}")

    async def handle_connection(self, websocket: WebSocket) -> None:
        """
        Handle a WebSocket connection for the /ws/fetch endpoint.

        Protocol:
        Client sends:  {"url": "...", "method": "GET", "headers": {...}, "options": {...}}
        Server sends:  {"type": "start", "url": "..."}
                       {"type": "result", "success": true, "status_code": 200, "html": "...", ...}
                       {"type": "error", "error": "..."}
        """
        await self.connect(websocket)

        try:
            while True:
                # Receive fetch request
                raw = await websocket.receive_text()

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": f"Invalid JSON: {e}",
                    })
                    continue

                url = data.get("url")
                if not url:
                    await websocket.send_json({
                        "type": "error",
                        "error": "Missing 'url' field",
                    })
                    continue

                # Notify client we're starting
                await websocket.send_json({
                    "type": "start",
                    "url": url,
                })

                # Build fetch request
                options = data.get("options", {})
                request = FetchRequest(
                    url=url,
                    method=data.get("method", "GET"),
                    headers=data.get("headers", {}),
                    body=data.get("body", "").encode() if data.get("body") else None,
                    params=data.get("params"),
                    timeout=options.get("timeout", 30.0),
                    force_strategy=options.get("force_strategy"),
                    bypass_cache=options.get("bypass_cache", False),
                    session_id=options.get("session_id"),
                    force_new_session=options.get("force_new_session", False),
                )

                # Execute fetch
                try:
                    result = await self._engine.fetch(request)

                    await websocket.send_json({
                        "type": "result",
                        "success": result.success,
                        "status_code": result.status_code,
                        "url": result.url,
                        "final_url": result.final_url,
                        "headers": result.headers,
                        "html": result.html[:100000],  # Cap at 100KB for WS
                        "strategy_used": result.strategy_used,
                        "latency": round(result.latency, 4),
                        "retries": result.retries,
                        "cached": result.cached,
                        "error": result.error,
                        "metadata": result.metadata,
                    })

                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": str(e),
                    })

        except WebSocketDisconnect:
            self.disconnect(websocket)
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            self.disconnect(websocket)


def register_websocket(app, engine: DecisionEngine) -> None:
    """Register the WebSocket endpoint on the FastAPI app."""
    ws_manager = WebSocketManager(engine)

    @app.websocket("/ws/fetch")
    async def ws_fetch(websocket: WebSocket):
        """WebSocket endpoint for real-time fetch streaming."""
        await ws_manager.handle_connection(websocket)
