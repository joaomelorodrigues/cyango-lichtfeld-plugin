"""LichtFeld Studio plugin entry point: publish splat scenes to Cyango."""

import lichtfeld as lf
from lfs_plugins.ui import RuntimeState

from .cyango_lfs.runtime import start_controller, stop_controller
from .panels.main_panel import CyangoPanel

_PLUGIN_NAME = "cyango"
_CLASSES = [CyangoPanel]
__lfs_panel_classes__ = ["CyangoPanel"]
__lfs_panel_ids__ = [CyangoPanel.id]


def on_load() -> None:
    start_controller(lf, RuntimeState)
    for cls in _CLASSES:
        lf.register_class(cls)
        # A dev reload can keep a panel disabled; enable it so the tab shows.
        if hasattr(lf.ui, "set_panel_enabled"):
            lf.ui.set_panel_enabled(cls.id, True)
    if hasattr(lf.ui, "get_panel") and lf.ui.get_panel(CyangoPanel.id) is None:
        raise RuntimeError(f"LichtFeld did not register panel {CyangoPanel.id!r}")
    if hasattr(lf.ui, "request_redraw"):
        lf.ui.request_redraw()
    lf.log.info(f"{_PLUGIN_NAME} loaded")


def on_unload() -> None:
    for cls in reversed(_CLASSES):
        lf.unregister_class(cls)
    stop_controller()
    if hasattr(lf.ui, "request_redraw"):
        lf.ui.request_redraw()
    lf.log.info(f"{_PLUGIN_NAME} unloaded")
