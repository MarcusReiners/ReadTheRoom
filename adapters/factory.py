from service_layer.bus import EventBus


def build_stt(cfg):
    if cfg.STT_PROVIDER == "elevenlabs":
        from adapters.stt.elevenlabs import ElevenLabsSTTAdapter
        return ElevenLabsSTTAdapter(
            api_key=cfg.ELEVENLABS_API_KEY,
            model_id=cfg.ELEVENLABS_STT_MODEL,
            language_code=cfg.ELEVENLABS_LANGUAGE_CODE,
        )
    if cfg.STT_PROVIDER == "remote":
        from adapters.stt.remote import RemoteSTTAdapter
        return RemoteSTTAdapter(server_url=cfg.MAC_SERVER_URL)
    if cfg.STT_PROVIDER == "local":
        from adapters.stt.local import LocalSTTAdapter
        return LocalSTTAdapter(
            model_path=cfg.WHISPER_MODEL,
            cpu_threads=cfg.WHISPER_THREADS,
            vad=cfg.WHISPER_VAD,
        )
    raise ValueError(f"Unbekannter STT_PROVIDER: {cfg.STT_PROVIDER!r}")


def build_tts(cfg, bus: EventBus):
    if cfg.TTS_PROVIDER == "elevenlabs":
        from adapters.tts.elevenlabs import ElevenLabsTTSAdapter
        return ElevenLabsTTSAdapter(
            bus=bus,
            api_key=cfg.ELEVENLABS_API_KEY,
            voice_id=cfg.ELEVENLABS_VOICE_ID,
            model_id=cfg.ELEVENLABS_TTS_MODEL,
            speaker_device=cfg.SPEAKER_DEVICE,
            language_code=cfg.ELEVENLABS_TTS_LANGUAGE_CODE,
        )
    if cfg.TTS_PROVIDER == "remote":
        from adapters.tts.remote import RemoteTTSAdapter
        return RemoteTTSAdapter(
            bus=bus,
            server_url=cfg.MAC_SERVER_URL,
            sample_rate=cfg.MAC_SERVER_TTS_SAMPLE_RATE,
            speaker_device=cfg.SPEAKER_DEVICE,
        )
    if cfg.TTS_PROVIDER == "local":
        from adapters.tts.local import LocalTTSAdapter
        return LocalTTSAdapter(
            bus=bus,
            model_path=cfg.PIPER_MODEL,
            speaker_device=cfg.SPEAKER_DEVICE,
        )
    raise ValueError(f"Unbekannter TTS_PROVIDER: {cfg.TTS_PROVIDER!r}")


def build_turntable(cfg, move_to_home_on_start: bool = True):
    if not cfg.USE_SERVO:
        from adapters.hardware.turntable import DummyTurntableAdapter
        return DummyTurntableAdapter()

    from adapters.hardware.turntable import ServoTurntableAdapter
    from adapters.hardware.servo_calibration import load_home_offset
    from adapters.app_settings import load_servo_range

    # The safe window is loaded here rather than applied by main.py after
    # construction, so the hardware scripts (track_audio, sweep_servo,
    # home_servo, ...) move through the same window the assistant does
    # instead of the config.py default.
    min_angle, max_angle = load_servo_range(
        cfg.APP_SETTINGS_PATH, cfg.SERVO_MIN_ANGLE, cfg.SERVO_MAX_ANGLE,
    )

    return ServoTurntableAdapter(
        pin=cfg.SERVO_GPIO_PIN,
        min_angle=min_angle,
        max_angle=max_angle,
        hardware_min_angle=cfg.SERVO_HARDWARE_MIN_ANGLE,
        hardware_max_angle=cfg.SERVO_HARDWARE_MAX_ANGLE,
        min_pulse_width=cfg.SERVO_MIN_PULSE_WIDTH,
        max_pulse_width=cfg.SERVO_MAX_PULSE_WIDTH,
        use_pigpio=cfg.SERVO_USE_PIGPIO,
        home_offset_degrees=load_home_offset(cfg.SERVO_CALIBRATION_PATH),
        move_to_home_on_start=move_to_home_on_start,
    )


def build_radar(cfg, bus: EventBus):
    if cfg.RADAR_PROVIDER == "ld2450":
        from adapters.hardware.radar_ld2450 import RadarLD2450Adapter
        return RadarLD2450Adapter(
            bus=bus,
            serial_port=cfg.RADAR_SERIAL_PORT,
            baud_rate=cfg.RADAR_SERIAL_BAUD,
        )
    if cfg.RADAR_PROVIDER == "dummy":
        from adapters.hardware.radar_ld2450 import DummyRadarAdapter
        return DummyRadarAdapter(bus=bus)
    raise ValueError(f"Unbekannter RADAR_PROVIDER: {cfg.RADAR_PROVIDER!r}")
