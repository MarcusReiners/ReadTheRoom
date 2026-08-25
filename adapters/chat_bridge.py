import asyncio
import logging
import queue
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from adapters.conversation_store import ConversationStore
from adapters.hardware.servo_calibration import save_home_offset
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
        radar,
        turntable,
        servo_calibration_path: str,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self.bus = bus
        self.conversation = conversation
        self.store = store
        self.turn_queue = turn_queue
        self.radar = radar
        self.turntable = turntable
        self.servo_calibration_path = servo_calibration_path
        self.host = host
        self.port = port

        # In-progress calibration position on the servo's true hardware range,
        # unclamped by the cable-safety window (see calibrate_servo_home.py) -
        # nudges move this and the servo directly; only "save" commits it as
        # the new home_offset_degrees.
        # Note: does not itself move the servo - it's just clamped bookkeeping
        # in sync with whatever position ServoTurntableAdapter's own __init__
        # already drove it to via the (still cable-safe) home_offset_degrees.
        self._calibration_raw_angle = self.turntable.clamp_raw_angle(
            self.turntable.raw_angle_for_offset(self.turntable.home_offset_degrees)
        )

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

        # The radar adapter owns the actual link to the sensor (ESP-NOW via
        # a USB-serial bridge) and keeps the latest zone/calibration state
        # cached in memory, so these just read/write that directly - no
        # network call, and no need for a threadpool hop.
        @self.app.get("/api/radar/zone")
        def get_zone():
            return JSONResponse(self.radar.get_zone())

        @self.app.post("/api/radar/zone")
        async def set_zone(request: Request):
            bounds = await request.json()
            self.radar.send_command({
                "cmd": "set_zone",
                "min_x_mm": bounds.get("min_x_mm"),
                "max_x_mm": bounds.get("max_x_mm"),
                "min_y_mm": bounds.get("min_y_mm"),
                "max_y_mm": bounds.get("max_y_mm"),
            })
            return JSONResponse({
                "valid": True,
                "min_x_mm": bounds.get("min_x_mm"),
                "max_x_mm": bounds.get("max_x_mm"),
                "min_y_mm": bounds.get("min_y_mm"),
                "max_y_mm": bounds.get("max_y_mm"),
            })

        @self.app.post("/api/radar/zone/reset")
        def reset_zone():
            self.radar.send_command({"cmd": "reset_zone"})
            return JSONResponse({"valid": False})

        @self.app.post("/api/radar/calibrate/start")
        def start_calibration(seconds: int | None = None):
            self.radar.send_command({"cmd": "start_calibration", "seconds": seconds or 20})
            return JSONResponse({"calibrating": True})

        @self.app.get("/api/radar/calibrate/status")
        def calibration_status():
            return JSONResponse(self.radar.get_calibration_status())

        # The servo's home offset compensates for wherever the 360-degree
        # servo's horn actually ended up mounted. Nudge moves the servo
        # directly on its true hardware range (unclamped by the cable-safety
        # window that bounds normal conversation-driven movement - this is a
        # supervised calibration session, it shouldn't fight the operator);
        # save converts wherever that landed into a home_offset_degrees and
        # commits it, same two-step flow as calibrate_servo_home.py.
        @self.app.get("/api/servo/calibration")
        def get_servo_calibration():
            offset = self.turntable.offset_for_raw_angle(self._calibration_raw_angle)
            return JSONResponse({"home_offset_degrees": offset})

        @self.app.post("/api/servo/nudge")
        async def nudge_servo(request: Request):
            body = await request.json()
            delta = float(body.get("delta", 0))
            self._calibration_raw_angle = self.turntable.set_raw_angle(self._calibration_raw_angle + delta)
            offset = self.turntable.offset_for_raw_angle(self._calibration_raw_angle)
            return JSONResponse({"home_offset_degrees": offset})

        @self.app.post("/api/servo/save")
        def save_servo_calibration():
            offset = self.turntable.offset_for_raw_angle(self._calibration_raw_angle)
            self.turntable.set_home_offset(offset)
            save_home_offset(self.servo_calibration_path, offset)
            return JSONResponse({"saved": True, "home_offset_degrees": offset})

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
                "private_mode": self.conversation.confidential,
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

    def _handle_client_message(self, data: dict) -> None:
        msg_type = data.get("type")
        if msg_type == "user_text":
            text = (data.get("text") or "").strip()
            if text:
                self.turn_queue.put(text)
        elif msg_type == "set_voice_enabled":
            self.conversation.voice_enabled = bool(data.get("value"))
            self._broadcast({"type": "voice_enabled", "value": self.conversation.voice_enabled})
        elif msg_type == "set_private_mode":
            self.conversation.set_private_mode(bool(data.get("value")))
            self._broadcast({"type": "private_mode", "value": self.conversation.confidential})
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
            "private_mode": self.conversation.confidential,
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
