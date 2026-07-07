from domain.conversation import ConversationState
from domain.events import PersonCountChanged, DisplayTakeoverRequested
from service_layer.bus import EventBus
from adapters.tts_piper import PiperTTSAdapter


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    tts: PiperTTSAdapter,
    status_display,
) -> None:
    tts.takeover_sink = status_display.set_text

    def on_person_count_changed(event: PersonCountChanged) -> None:
        if event.count > 1 and conversation.confidential:
            conversation.switch_modality("web")
            print(f"  [Handler] Modalitätswechsel zu 'web' "
                  f"(PersonCount={event.count}, vertraulich={conversation.confidential})")

    def on_display_takeover(event: DisplayTakeoverRequested) -> None:
        if status_display.text_active:
            status_display.stop_text()
            print("  [Handler] Display-Textmodus beendet.")
        elif tts.request_takeover():
            print("  [Handler] Sprachausgabe unterbrochen, Antwort läuft auf dem Display weiter.")
        else:
            print("  [Handler] Keine laufende Antwort zum Umleiten.")

    bus.subscribe(PersonCountChanged, on_person_count_changed)
    bus.subscribe(DisplayTakeoverRequested, on_display_takeover)
