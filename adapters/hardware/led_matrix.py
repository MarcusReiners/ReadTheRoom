import logging
import threading
import time

logger = logging.getLogger(__name__)


class LedMatrix:
    def __init__(
        self,
        rows: int = 48,
        cols: int = 96,
        chain: int = 2,
        brightness: int = 60,
        gpio_slowdown: int = 4,
        fps: int = 20,
        limit_refresh_rate_hz: int = 120,
        pwm_bits: int = 7,
    ) -> None:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        options = RGBMatrixOptions()
        options.rows = rows
        options.cols = cols
        options.chain_length = chain
        options.brightness = brightness
        options.gpio_slowdown = gpio_slowdown
        options.hardware_mapping = "regular"
        options.drop_privileges = False
        options.limit_refresh_rate_hz = limit_refresh_rate_hz
        # Lower pwm_bits trades color depth (still 128 levels/channel) for a
        # meaningfully higher hardware refresh rate, i.e. less visible flicker.
        # Worth spending here since nothing else on the Pi competes for CPU
        # now that STT/LLM/TTS run in the cloud.
        options.pwm_bits = pwm_bits

        self.matrix = RGBMatrix(options=options)
        self.width = cols * chain
        self.height = rows
        self._frame_time = 1.0 / fps
        self._renderers: list = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def add_renderer(self, renderer) -> None:
        self._renderers.append(renderer)

    def start(self) -> None:
        self._thread.start()
        logger.info("[LedMatrix] %sx%s gestartet.", self.width, self.height)

    def _loop(self) -> None:
        canvas = self.matrix.CreateFrameCanvas()
        t0 = time.monotonic()
        while True:
            canvas.Clear()
            t = time.monotonic() - t0
            for renderer in self._renderers:
                renderer.render(canvas, t)
            canvas = self.matrix.SwapOnVSync(canvas)
            time.sleep(self._frame_time)
