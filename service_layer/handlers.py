from domain.conversation import ConversationState
from domain.events import (
    PersonCountChanged, EmergencyStopPressed, DisplayTakeoverRequested,
)
from domain.pause_resume import PauseResumeController
from service_layer.bus import EventBus
from adapters.tts_piper import PiperTTSAdapter


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    pause_controller: PauseResumeController,
    tts: PiperTTSAdapter,
    status_display,
) -> None:
    tts.takeover_sink = status_display.append_text

    def on_person_count_changed(event: PersonCountChanged) -> None:
        if event.count > 1 and conversation.confidential:
            conversation.switch_modality("web")
            print(f"  [Handler] Modalitätswechsel zu 'web' "
                  f"(PersonCount={event.count}, vertraulich={conversation.confidential})")

    def on_emergency_stop(event: EmergencyStopPressed) -> None:
        if pause_controller.state.name == "NORMAL":
            pause_controller.on_first_press()
            tts.pause()
        elif pause_controller.state.name == "PAUSED":
            pause_controller.on_second_press()
            print("  [Handler] Rückfrage: fortsetzen oder verwerfen? "
                  "(Phase 1: noch nicht an Whisper/Web-UI angebunden)")

    def on_display_takeover(event: DisplayTakeoverRequested) -> None:
        if status_display.text_active:
            status_display.stop_text()
            print("  [Handler] Display-Textmodus beendet.")
        elif tts.request_takeover():
            print("  [Handler] Sprachausgabe unterbrochen, Antwort läuft auf dem Display weiter.")
        else:
            print("  [Handler] Keine laufende Antwort zum Umleiten.")

    bus.subscribe(PersonCountChanged, on_person_count_changed)
    bus.subscribe(EmergencyStopPressed, on_emergency_stop)
    bus.subscribe(DisplayTakeoverRequested, on_display_takeover)
