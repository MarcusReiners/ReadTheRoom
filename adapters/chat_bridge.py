import asyncio
import logging
import queue
import threading
import time
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
    AssistantTurnCancelled,
    ListeningStateChanged,
    ModalitySwitched,
    RadarTargetsUpdated,
    VoiceDucked,
    SpeechTranscribed,
)
from service_layer.bus import EventBus

# Must match entrypoints/main.py's PRIORITY_CHAT - lower priority number is
# served first out of turn_queue (a queue.PriorityQueue of (priority,
# monotonic_ns, text)), so a chat-typed message always jumps ahead of
# anything still waiting from voice/console input.
_PRIORITY_CHAT = 0

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
        turn_queue: "queue.PriorityQueue",
        radar,
        turntable,
        servo_calibration_path: str,
        llm=None,
        tts=None,
        stt=None,
        app_settings_path: str = "app_settings.json",
        voices: list | None = None,
        active_voice_id: str | None = None,
        doa=None,
        calibration_mode: threading.Event | None = None,
        assistant_speaking: threading.Event | None = None,
        study_dir: str = "study",
        composing_mode: threading.Event | None = None,
        cancel_current_turn: threading.Event | None = None,
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
        self.stt = stt
        self.app_settings_path = app_settings_path
        self.voices = voices if voices else [{"id": str(uuid.uuid4()), "name": "Default", "voice_id": ""}]
        self.active_voice_id = active_voice_id or self.voices[0]["id"]
        # Set for as long as a client has the settings tab open - start_doa_tracking()
        # pauses on this (it was fighting live servo-calibration nudges with its own
        # DOA-driven moves otherwise) and LedEyesAdapter shows a wrench instead of the
        # eyes. Shared with main.py's construction of both; a fresh Event() here if
        # main.py didn't pass one (USE_SERVO/USE_LED_MATRIX both off) so this class
        # always has something to .set()/.clear() without needing to know why not.
        self.doa = doa
        self.calibration_mode = calibration_mode if calibration_mode is not None else threading.Event()
        self.assistant_speaking = assistant_speaking if assistant_speaking is not None else threading.Event()
        self.study_dir = study_dir
        self._doa_calibration = None
        # Set for as long as a client has the chat text box focused -
        # vad_input_loop pauses on this too (composing_mode param), so typing
        # a message doesn't also get picked up as a separate spoken turn.
        self.composing_mode = composing_mode if composing_mode is not None else threading.Event()
        # Set the instant a chat message is sent (user_text handler below) -
        # main.py's turn loop clears it before each handle_turn() call and
        # handle_turn() checks it while streaming, so a still-in-progress
        # voice-triggered reply gets cut short rather than finishing first.
        self.cancel_current_turn = cancel_current_turn if cancel_current_turn is not None else threading.Event()
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
            "type": "user_message", "text": e.text, "conversation_id": e.conversation_id, "replaced": e.replaced,
        }))
        bus.subscribe(AssistantDeltaReceived, lambda e: self._broadcast({
            "type": "assistant_delta", "text": e.delta, "conversation_id": e.conversation_id,
        }))
        bus.subscribe(AssistantMessageCompleted, lambda e: self._on_assistant_message_completed(e))
        bus.subscribe(AssistantTurnCancelled, lambda e: self._broadcast({
            "type": "assistant_cancelled", "conversation_id": e.conversation_id,
        }))
        bus.subscribe(ListeningStateChanged,
                       lambda e: self._broadcast({"type": "listening", "value": e.listening}))
        bus.subscribe(ModalitySwitched,
                       lambda e: self._broadcast({"type": "modality", "value": e.to_modality, "reason": e.reason}))
        bus.subscribe(VoiceDucked, lambda e: self._broadcast({"type": "voice_ducked", "value": e.active}))
        self._last_radar_broadcast = 0.0
        bus.subscribe(RadarTargetsUpdated, self._on_radar_targets)

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

        # The door zone lives only on the Pi (the sensor keeps just the room
        # zone): entries and exits are decided there, and someone standing in
        # it during a conversation makes the voice quieter.
        @self.app.get("/api/radar/door_zone")
        def get_door_zone():
            getter = getattr(self.radar, "get_door_zone", None)
            return JSONResponse(getter() if getter else {"valid": False})

        @self.app.post("/api/radar/door_zone")
        async def set_door_zone(request: Request):
            setter = getattr(self.radar, "set_door_zone", None)
            if setter is None:
                return JSONResponse({"error": "this radar has no door zone support"}, status_code=400)
            zone = setter(await request.json())
            if not zone.get("valid"):
                return JSONResponse({"error": "the door zone must be at least 10 cm on each side"},
                                    status_code=400)
            self._save_settings()
            return JSONResponse(zone)

        @self.app.delete("/api/radar/door_zone")
        def delete_door_zone():
            setter = getattr(self.radar, "set_door_zone", None)
            if setter is not None:
                setter(None)
                self._save_settings()
            return JSONResponse({"valid": False})

        # Where the zone rectangle is enforced: 0 = in the XIAO's software
        # (module reports everything, out-of-zone targets stay visible on the
        # radar view), 1 = the module's own Detection zone (reports only
        # what's inside), 2 = the module's Filter zone (reports everything
        # except what's inside - for boxing off a noise source).
        @self.app.post("/api/radar/zone/mode")
        async def set_zone_mode(request: Request):
            body = await request.json()
            mode = int(body.get("mode", 0))
            if mode not in (0, 1, 2):
                return JSONResponse({"error": "mode muss 0, 1 oder 2 sein"}, status_code=400)
            self.radar.send_command({"cmd": "set_zone_mode", "mode": mode})
            return JSONResponse({"mode": mode})

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
                "llm_model": self._get_llm_model(),
                "reasoning_effort": self._get_reasoning_effort(),
                "stt_language": self._get_stt_language(),
                "voices": self.voices,
                "active_voice_id": self.active_voice_id,
                "volume": self._get_volume(),
                "servo_range": self._get_servo_range(),
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
                # Chat always has priority: interrupt whatever's currently
                # playing/generating (a harmless no-op if nothing is) and
                # jump this ahead of anything still waiting from voice/
                # console input - see main.py's PRIORITY_CHAT/handle_turn().
                self.cancel_current_turn.set()
                if self.tts is not None:
                    self.tts.request_takeover()
                self.turn_queue.put((_PRIORITY_CHAT, time.monotonic_ns(), text))
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
        elif msg_type == "start_doa_calibration":
            cal = self._ensure_doa_calibration()
            if cal is None:
                self._broadcast({"type": "doa_calibration",
                                 "status": {"running": False, "step": "unavailable",
                                            "message": "No servo or microphone array on this machine."}})
            elif not cal.start(distance_m=data.get("distance_m"), angles=data.get("angles")):
                self._broadcast({"type": "doa_calibration", "status": cal.status()})
        elif msg_type == "cancel_doa_calibration":
            if self._doa_calibration is not None:
                self._doa_calibration.cancel()
        elif msg_type == "set_composing":
            if bool(data.get("value")):
                self.composing_mode.set()
            else:
                self.composing_mode.clear()
        elif msg_type == "set_volume":
            value = data.get("value")
            if value is not None and self.tts is not None and hasattr(self.tts, "set_volume"):
                self.tts.set_volume(float(value))
                self._save_settings()
                self._broadcast({"type": "volume", "value": self._get_volume()})
        elif msg_type == "set_system_prompt":
            text = (data.get("value") or "").strip()
            if text and self.llm is not None:
                self.llm.system_prompt = text
                self._save_settings()
                self._broadcast({"type": "system_prompt", "value": text})
        elif msg_type == "set_llm_model":
            model = (data.get("value") or "").strip()
            if model and self.llm is not None:
                self.llm.model = model
                self._save_settings()
                self._broadcast({"type": "llm_model", "value": model})
        elif msg_type == "set_reasoning_effort":
            # "" is meaningful: leave the provider's own default alone rather
            # than forcing a level. Note Gemini 3.x cannot fully disable
            # thinking - "disable"/"none" land on its lowest level, not off.
            value = (data.get("value") or "").strip()
            if self.llm is not None:
                self.llm.reasoning_effort = value or None
                self._save_settings()
                self._broadcast({"type": "reasoning_effort", "value": value})
        elif msg_type == "set_stt_language":
            # "" means let Scribe auto-detect - the right setting when more
            # than one language gets spoken at the assistant. A fixed code is
            # more accurate when the language really is fixed, and a WRONG
            # fixed code returns mangled transcripts rather than an error.
            value = (data.get("value") or "").strip()
            if self.stt is not None and hasattr(self.stt, "language_code"):
                self.stt.language_code = value or None
                self._save_settings()
                self._broadcast({"type": "stt_language", "value": value})
        elif msg_type == "set_servo_range":
            self._set_servo_range(data.get("min"), data.get("max"))
        elif msg_type == "add_voice":
            name = (data.get("name") or "").strip()
            voice_id = (data.get("voice_id") or "").strip()
            if name and voice_id:
                entry = {"id": str(uuid.uuid4()), "name": name, "voice_id": voice_id}
                self.voices.append(entry)
                self._save_settings()
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
            "llm_model": self._get_llm_model(),
            "reasoning_effort": self._get_reasoning_effort(),
            "stt_language": self._get_stt_language(),
            "voices": self.voices,
            "active_voice_id": self.active_voice_id,
            "volume": self._get_volume(),
            "servo_range": self._get_servo_range(),
        })

    def _get_system_prompt(self) -> str:
        return getattr(self.llm, "system_prompt", "") or ""

    def _get_llm_model(self) -> str:
        return getattr(self.llm, "model", "") or ""

    def _get_reasoning_effort(self) -> str:
        return getattr(self.llm, "reasoning_effort", "") or ""

    def _get_stt_language(self) -> str:
        return getattr(self.stt, "language_code", "") or ""

    def _get_volume(self) -> float:
        return getattr(self.tts, "volume", 1.0)

    def _get_servo_range(self) -> dict:
        return {
            "min": getattr(self.turntable, "safe_min_angle", 0.0),
            "max": getattr(self.turntable, "safe_max_angle", 180.0),
        }

    def _set_servo_range(self, min_angle, max_angle) -> None:
        if min_angle is None or max_angle is None:
            return
        try:
            self.turntable.set_safe_range(float(min_angle), float(max_angle))
        except (ValueError, TypeError) as e:
            # Rejected (inverted, or wider than the servo can physically
            # sweep) - tell the client why and re-send the range still in
            # force, so its inputs snap back instead of showing a value the
            # head isn't actually honouring.
            self._broadcast({
                "type": "servo_range", "error": str(e), **self._get_servo_range(),
            })
            return
        self._save_settings()
        self._broadcast({"type": "servo_range", **self._get_servo_range()})

    def _select_voice(self, voice_id: str) -> None:
        entry = next((v for v in self.voices if v["id"] == voice_id), None)
        if entry is None:
            return
        self.active_voice_id = voice_id
        if self.tts is not None and hasattr(self.tts, "voice_id"):
            self.tts.voice_id = entry["voice_id"]
        self._save_settings()
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
            self._save_settings()
            self._broadcast_voices()

    def _save_settings(self) -> None:
        """Snapshots the whole settings file from the live adapters, rather
        than tracking a parallel copy of each value. The adapters are the
        source of truth (llm.model is what the next turn actually uses), so
        there's nothing here that can drift out of sync with them."""
        servo = self._get_servo_range()
        door_getter = getattr(self.radar, "get_door_zone", None)
        save_app_settings(self.app_settings_path, {
            "system_prompt": self._get_system_prompt(),
            "llm_model": self._get_llm_model(),
            "reasoning_effort": self._get_reasoning_effort(),
            "stt_language_code": self._get_stt_language(),
            "volume": self._get_volume(),
            "servo_min_angle": servo["min"],
            "servo_max_angle": servo["max"],
            "voices": self.voices,
            "active_voice_id": self.active_voice_id,
            "door_zone": door_getter() if door_getter else None,
        })

    def _broadcast_voices(self) -> None:
        self._broadcast({"type": "voices", "voices": self.voices, "active_voice_id": self.active_voice_id})

    def _ensure_doa_calibration(self):
        if self._doa_calibration is not None:
            return self._doa_calibration
        doa = getattr(self, "doa", None)
        if doa is None or self.turntable is None or self.tts is None:
            return None
        from service_layer.doa_calibration import DOACalibration
        self._doa_calibration = DOACalibration(
            doa=doa, turntable=self.turntable, tts=self.tts,
            assistant_speaking=self.assistant_speaking,
            calibration_mode=self.calibration_mode,
            study_dir=self.study_dir,
            on_progress=lambda st: self._broadcast({"type": "doa_calibration", "status": st}),
        )
        return self._doa_calibration

    def _on_radar_targets(self, event) -> None:
        """At most 10 radar updates a second reach the browser. Each one is a
        fire-and-forget send per client, so at sensor rate a slow phone or
        laptop queued them up and delayed every other message behind them -
        including the one that shows the switch to text."""
        now = time.monotonic()
        if now - self._last_radar_broadcast < 0.1:
            return
        self._last_radar_broadcast = now
        self._broadcast({"type": "radar_targets", "targets": event.targets})

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
