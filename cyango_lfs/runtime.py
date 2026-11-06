"""Controller singleton that survives hot reloads of the panel."""

from __future__ import annotations

import threading
from typing import Any

from .controller import PluginController

_lock = threading.Lock()
_controller: PluginController | None = None


def start_controller(lf: Any, runtime_state: Any) -> PluginController:
    global _controller
    with _lock:
        if _controller is None:
            _controller = PluginController(lf, runtime_state)
        return _controller


def get_controller() -> PluginController:
    if _controller is None:
        raise RuntimeError("The Cyango plugin controller is not running.")
    return _controller


def stop_controller() -> None:
    global _controller
    with _lock:
        controller, _controller = _controller, None
    if controller is not None:
        controller.shutdown()
