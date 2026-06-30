"""
Handler: verbinden Events mit Domain-Logik (ADR-001 Abschnitt 5).

Diese Funktionen werden beim Bus registriert und reagieren auf Events,
ohne dass die Adapter, die diese Events ausloesen, voneinander wissen.
"""

from domain.conversation import ConversationState
from domain.events import (
    PersonCountChanged, EmergencyStopPressed, SpeechPlaybackEnded,
)
from domain.pause_resume import PauseResumeController
from service_layer.bus import EventBus
from adapters.tts_piper import PiperTTSAdapter


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    pause_controller: PauseResumeController,
    tts: PiperTTSAdapter,
) -> None:
    """Zentrale Registrierung aller Event-Handler (Wiring)."""

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

    bus.subscribe(PersonCountChanged, on_person_count_changed)
    bus.subscribe(EmergencyStopPressed, on_emergency_stop)
