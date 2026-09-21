import importlib.util
import logging
import os

logger = logging.getLogger(__name__)

ADDON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_addons")


class LocalAddons:
    """Add-ons that live on one machine only. Every *_addon.py in
    local_addons/ (git-ignored, next to the code) that defines setup(ctx) is
    loaded at start; setup() returns an object, or None to stay inactive.
    That object may implement:

        reply_to(text, source) -> str | None
            Called before a turn reaches the language model. A string is
            answered instead of the model's reply, "" drops the turn without
            answering or storing it, None leaves it to the model.
        holds_tracking() -> bool
            True while the head should not follow voices.

    An add-on that raises is logged and skipped. Without the folder this
    does nothing. is_set() lets it stand in for a threading.Event wherever
    head tracking takes a pause condition."""

    def __init__(self, ctx, directory: str = ADDON_DIR):
        self.addons = []
        self._failing = set()
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            if not name.endswith("_addon.py"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"local_addons.{name[:-3]}",
                                                              os.path.join(directory, name))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                setup = getattr(module, "setup", None)
                addon = setup(ctx) if callable(setup) else None
            except Exception:
                logger.exception("[Add-on] %s failed to load - skipped.", name)
                continue
            if addon is not None:
                self.addons.append(addon)
                logger.info("[Add-on] %s loaded.", name)

    def reply_to(self, text: str, source: str):
        for addon in self.addons:
            method = getattr(addon, "reply_to", None)
            if method is None:
                continue
            try:
                reply = method(text, source)
            except Exception:
                logger.exception("[Add-on] %s failed on a turn - left to the model.", type(addon).__name__)
                continue
            if reply is not None:
                return reply
        return None

    def is_set(self) -> bool:
        for addon in self.addons:
            method = getattr(addon, "holds_tracking", None)
            if method is None:
                continue
            try:
                if method():
                    return True
            except Exception:
                if id(addon) not in self._failing:
                    self._failing.add(id(addon))
                    logger.exception("[Add-on] %s failed in holds_tracking().", type(addon).__name__)
        return False


class AnyOf:
    """Reads as set while any of the given conditions is set."""

    def __init__(self, *conditions):
        self.conditions = conditions

    def is_set(self) -> bool:
        return any(c.is_set() for c in self.conditions)
