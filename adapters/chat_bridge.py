import asyncio
import logging
import queue
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from adapters.app_settings import save_app_settings
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
        llm=None,
        tts=None,
        app_settings_path: str = "app_settings.json",
        voices: list | None = None,
        active_voice_id: str | None = None,
        calibration_mode: threading.Event | None = None,
        composing_mode: threading.Event | None = None,
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
        # For the settings tab's system-prompt/voice-library editors -
        # llm.system_prompt and tts.voice_id (where the provider has one) are
        # mutated directly and persisted to app_settings_path, same
        # read-live-mutate-then-persist pattern as the servo calibration flow
        # above. voices/active_voice_id are this class's own in-memory copy
        # of what main.py loaded from app_settings_path at startup (each
        # entry: {"id", "name", "voice_id"}) - add_voice/delete_voice/
        # select_voice below mutate this list and re-save the whole thing.
        self.llm = llm
        self.tts = tts
        self.app_settings_path = app_settings_path
        self.voices = voices if voices else [{"id": str(uuid.uuid4()), "name": "Default", "voice_id": ""}]
        self.active_voice_id = active_voice_id or self.voices[0]["id"]
        # Set for as long as a client has the settings tab open - start_doa_tracking()
        # pauses on this (it was fighting live servo-calibration nudges with its own
        # DOA-driven moves otherwise) and LedEyesAdapter shows a wrench instead of the
        # eyes. Shared with main.py's construction of both; a fresh Event() here if
        # main.py didn't pass one (USE_SERVO/USE_LED_MATRIX both off) so this class
        # always has something to .set()/.clear() without needing to know why not.
        self.calibration_mode = calibration_mode if calibration_mode is not None else threading.Event()
        # Set for as long as a client has the chat text box focused -
        # vad_input_loop pauses on this too (composing_mode param), so typing
        # a message doesn't also get picked up as a separate spoken turn.
        self.composing_mode = composing_mode if composing_mode is not None else threading.Event()
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
                "system_prompt": self._get_system_prompt(),
                "voices": self.voices,
                "active_voice_id": self.active_voice_id,
                "volume": self._get_volume(),
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
                # A dropped connection (tab closed mid-typing, network blip)
                # shouldn't leave voice input silently disabled forever with
                # no on-device indicator that anything's wrong - unlike
                # calibration_mode (which shows a wrench), there's no visual
                # cue that composing_mode is stuck.
                if not self._clients:
                    self.composing_mode.clear()

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
            if not self.conversation.voice_enabled and self.tts is not None:
                self.tts.request_takeover()  # cuts off whatever's playing right now, not just future turns
            self._broadcast({"type": "voice_enabled", "value": self.conversation.voice_enabled})
        elif msg_type == "set_private_mode":
            self.conversation.set_private_mode(bool(data.get("value")))
            self._broadcast({"type": "private_mode", "value": self.conversation.confidential})
        elif msg_type == "set_calibration_mode":
            if bool(data.get("value")):
                self.calibration_mode.set()
                if self.tts is not None:
                    self.tts.request_takeover()  # settings tab shouldn't have to wait out an in-progress reply
            else:
                self.calibration_mode.clear()
        elif msg_type == "set_composing":
            if bool(data.get("value")):
                self.composing_mode.set()
            else:
                self.composing_mode.clear()
        elif msg_type == "set_volume":
            value = data.get("value")
            if value is not None and self.tts is not None and hasattr(self.tts, "set_volume"):
                self.tts.set_volume(float(value))
                save_app_settings(
                    self.app_settings_path, self._get_system_prompt(), self.voices, self.active_voice_id,
                    self._get_volume(),
                )
                self._broadcast({"type": "volume", "value": self._get_volume()})
        elif msg_type == "set_system_prompt":
            text = (data.get("value") or "").strip()
            if text and self.llm is not None:
                self.llm.system_prompt = text
                save_app_settings(
                    self.app_settings_path, text, self.voices, self.active_voice_id, self._get_volume(),
                )
                self._broadcast({"type": "system_prompt", "value": text})
        elif msg_type == "add_voice":
            name = (data.get("name") or "").strip()
            voice_id = (data.get("voice_id") or "").strip()
            if name and voice_id:
                entry = {"id": str(uuid.uuid4()), "name": name, "voice_id": voice_id}
                self.voices.append(entry)
                self._save_voices()
                self._broadcast_voices()
        elif msg_type == "delete_voice":
            self._delete_voice(data.get("id"))
        elif msg_type == "select_voice":
            voice_id = data.get("id")
            if voice_id:
                self._select_voice(voice_id)
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
            "system_prompt": self._get_system_prompt(),
            "voices": self.voices,
            "active_voice_id": self.active_voice_id,
            "volume": self._get_volume(),
        })

    def _get_system_prompt(self) -> str:
        return getattr(self.llm, "system_prompt", "") or ""

    def _get_volume(self) -> float:
        return getattr(self.tts, "volume", 1.0)

    def _select_voice(self, voice_id: str) -> None:
        entry = next((v for v in self.voices if v["id"] == voice_id), None)
        if entry is None:
            return
        self.active_voice_id = voice_id
        if self.tts is not None and hasattr(self.tts, "voice_id"):
            self.tts.voice_id = entry["voice_id"]
        self._save_voices()
        self._broadcast_voices()

    def _delete_voice(self, voice_id: str | None) -> None:
        if not voice_id or len(self.voices) <= 1:
            # Always keep at least one voice around - nothing left to fall
            # back to (and nothing for tts.voice_id to point at) otherwise.
            return
        self.voices = [v for v in self.voices if v["id"] != voice_id]
        if self.active_voice_id == voice_id:
            self._select_voice(self.voices[0]["id"])  # also saves+broadcasts
        else:
            self._save_voices()
            self._broadcast_voices()

    def _save_voices(self) -> None:
        save_app_settings(
            self.app_settings_path, self._get_system_prompt(), self.voices, self.active_voice_id,
            self._get_volume(),
        )

    def _broadcast_voices(self) -> None:
        self._broadcast({"type": "voices", "voices": self.voices, "active_voice_id": self.active_voice_id})

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
