import asyncio
import logging
import queue
import threading
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from adapters.conversation_store import ConversationStore
from domain.conversation import ConversationState
from domain.events import (
    AssistantDeltaReceived,
    AssistantMessageCompleted,
    ListeningStateChanged,
    ModalitySwitched,
    RadarTargetsUpdated,
    SpeechTranscribed,
)
from service_layer.bus import EventBus

_RADAR_REQUEST_TIMEOUT_S = 2.0

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


class ChatBridgeAdapter:
    """Serves the chat web app and mirrors the conversation to it over a
    WebSocket, in both directions: bus events -> connected clients, and
    typed messages from a client -> the shared turn queue that main.py's
    turn loop consumes (alongside console-triggered voice turns).
    """

    def __init__(
        self,
        bus: EventBus,
        conversation: ConversationState,
        store: ConversationStore,
        turn_queue: "queue.Queue[str]",
        radar_url: str,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self.bus = bus
        self.conversation = conversation
        self.store = store
        self.turn_queue = turn_queue
        self.radar_url = radar_url.rstrip("/")
        self.host = host
        self.port = port

        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[WebSocket] = set()
        self._clients_lock = threading.Lock()

        self.app = FastAPI(title="ReadTheRoom Chat Bridge")
        self._register_routes()

        bus.subscribe(SpeechTranscribed, lambda e: self._broadcast({
            "type": "user_message", "text": e.text, "conversation_id": e.conversation_id,
        }))
        bus.subscribe(AssistantDeltaReceived, lambda e: self._broadcast({
            "type": "assistant_delta", "text": e.delta, "conversation_id": e.conversation_id,
        }))
        bus.subscribe(AssistantMessageCompleted, lambda e: self._on_assistant_message_completed(e))
        bus.subscribe(ListeningStateChanged,
                       lambda e: self._broadcast({"type": "listening", "value": e.listening}))
        bus.subscribe(ModalitySwitched,
                       lambda e: self._broadcast({"type": "modality", "value": e.to_modality, "reason": e.reason}))
        bus.subscribe(RadarTargetsUpdated,
                       lambda e: self._broadcast({"type": "radar_targets", "targets": e.targets}))

    def _register_routes(self) -> None:
        index_html = (_STATIC_DIR / "index.html").read_text()

        @self.app.get("/")
        async def index():
            return HTMLResponse(index_html)

        @self.app.get("/robot.png")
        async def robot_icon():
            return FileResponse(_STATIC_DIR / "robot.png")

        # Plain `def` (not `async def`): FastAPI runs sync route handlers in
        # its own threadpool, so the blocking `requests` call to the XIAO
        # doesn't stall the WebSocket/event loop. Same pattern as the sync
        # `speak()` route in entrypoints/mac_server.py.
        @self.app.get("/api/radar/zone")
        def get_zone():
            return self._radar_get("/zone")

        @self.app.post("/api/radar/zone")
        async def set_zone(request: Request):
            bounds = await request.json()
            return await run_in_threadpool(
                self._radar_post, "/zone/set", data={
                    "min_x_mm": bounds.get("min_x_mm"),
                    "max_x_mm": bounds.get("max_x_mm"),
                    "min_y_mm": bounds.get("min_y_mm"),
                    "max_y_mm": bounds.get("max_y_mm"),
                },
            )

        @self.app.post("/api/radar/zone/reset")
        def reset_zone():
            return self._radar_post("/zone/reset")

        @self.app.post("/api/radar/calibrate/start")
        def start_calibration(seconds: int | None = None):
            params = {"seconds": seconds} if seconds is not None else None
            return self._radar_post("/calibrate/start", params=params)

        @self.app.get("/api/radar/calibrate/status")
        def calibration_status():
            return self._radar_get("/calibrate/status")

        @self.app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            with self._clients_lock:
                self._clients.add(websocket)
            active_id = self.store.get_active_id()
            await websocket.send_json({
                "type": "conversations",
                "items": self.store.list_conversations(),
            })
            await websocket.send_json({
                "type": "conversation_selected",
                "id": active_id,
                "messages": self.store.get_history(active_id) if active_id else [],
                "modality": self.conversation.modality,
                "voice_enabled": self.conversation.voice_enabled,
            })
            try:
                while True:
                    data = await websocket.receive_json()
                    self._handle_client_message(data)
            except WebSocketDisconnect:
                pass
            finally:
                with self._clients_lock:
                    self._clients.discard(websocket)

    def _on_assistant_message_completed(self, event: AssistantMessageCompleted) -> None:
        self._broadcast({"type": "assistant_done", "text": event.text, "conversation_id": event.conversation_id})
        # Title may have just been set (first message) and recency order changed.
        self._broadcast({"type": "conversations", "items": self.store.list_conversations()})

    def _radar_get(self, path: str) -> JSONResponse:
        try:
            response = requests.get(self.radar_url + path, timeout=_RADAR_REQUEST_TIMEOUT_S)
            return JSONResponse(response.json(), status_code=response.status_code)
        except (requests.RequestException, ValueError) as e:
            return JSONResponse({"error": f"Radar nicht erreichbar: {e}"}, status_code=502)

    def _radar_post(self, path: str, data: dict | None = None, params: dict | None = None) -> JSONResponse:
        try:
            response = requests.post(
                self.radar_url + path, data=data, params=params, timeout=_RADAR_REQUEST_TIMEOUT_S,
            )
            return JSONResponse(response.json(), status_code=response.status_code)
        except (requests.RequestException, ValueError) as e:
            return JSONResponse({"error": f"Radar nicht erreichbar: {e}"}, status_code=502)

    def _handle_client_message(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "user_text":
            text = (data.get("text") or "").strip()
            if text:
                self.turn_queue.put(text)
        elif msg_type == "set_voice_enabled":
            self.conversation.voice_enabled = bool(data.get("value"))
            self._broadcast({"type": "voice_enabled", "value": self.conversation.voice_enabled})
        elif msg_type == "new_conversation":
            new_id = self.store.create_conversation()
            self.store.set_active_id(new_id)
            self._broadcast_active_conversation()
        elif msg_type == "select_conversation":
            conversation_id = data.get("id")
            if conversation_id:
                self.store.set_active_id(conversation_id)
                self._broadcast_active_conversation()
        elif msg_type == "delete_conversation":
            self._delete_conversation(data.get("id"))

    def _delete_conversation(self, conversation_id: str | None) -> None:
        if not conversation_id:
            return
        was_active = self.store.get_active_id() == conversation_id
        self.store.delete_conversation(conversation_id)

        if was_active:
            remaining = self.store.list_conversations()
            new_active = remaining[0]["id"] if remaining else self.store.create_conversation()
            self.store.set_active_id(new_active)

        self._broadcast_active_conversation()

    def _broadcast_active_conversation(self) -> None:
        active_id = self.store.get_active_id()
        self._broadcast({"type": "conversations", "items": self.store.list_conversations()})
        self._broadcast({
            "type": "conversation_selected",
            "id": active_id,
            "messages": self.store.get_history(active_id) if active_id else [],
            "modality": self.conversation.modality,
            "voice_enabled": self.conversation.voice_enabled,
        })

    def _broadcast(self, message: dict) -> None:
        if self._loop is None:
            return
        with self._clients_lock:
            clients = list(self._clients)
        for ws in clients:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, message), self._loop)

    @staticmethod
    async def _safe_send(ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_json(message)
        except Exception:
            pass

    def start(self) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            server_config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
            server = uvicorn.Server(server_config)
            loop.run_until_complete(server.serve())

        threading.Thread(target=run, daemon=True).start()
        logger.info("[ChatBridge] Server läuft auf http://%s:%s", self.host, self.port)
