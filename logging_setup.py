import logging
import os
from logging.handlers import RotatingFileHandler

_configured = False


def configure_logging(cfg) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    log_path = os.path.join(cfg.LOG_DIR, cfg.LOG_FILE)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_path, maxBytes=cfg.LOG_MAX_BYTES, backupCount=cfg.LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(cfg.LOG_LEVEL)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
