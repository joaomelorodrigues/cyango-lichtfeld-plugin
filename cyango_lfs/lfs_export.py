"""Main-thread adapter around the LichtFeld scene and export APIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

# lf.export_scene format codes.
EXPORT_FORMATS = {"ply": 0, "sog": 1, "spz": 2}


@dataclass(frozen=True)
class SceneSelection:
    node_names: list[str]
    gaussian_count: int
    maximum_sh_degree: int


class LfsExportAdapter:
    def __init__(self, lf: Any, runtime_state: Any) -> None:
        self.lf = lf
        self.runtime_state = runtime_state

    def resolve(self, scope: str) -> SceneSelection:
        scene = self.lf.get_scene()
        if scene is None or not scene.is_valid():
            raise ConfigurationError("Open a splat scene in LichtFeld first.")
        if scope == "visible":
            candidates = list(scene.get_visible_nodes())
        elif scope == "all":
            candidates = list(scene.get_nodes())
        else:
            raise ConfigurationError(f"Unknown export scope: {scope}.")

        names: list[str] = []
        gaussian_count = 0
        maximum_sh_degree = 0
        for node in candidates:
            try:
                splat = node.splat_data()
            except Exception:
                splat = None
            name = str(node.name)
            if splat is None or name in names:
                continue
            names.append(name)
            try:
                gaussian_count += int(node.gaussian_count)
            except Exception:
                pass
            try:
                maximum_sh_degree = max(maximum_sh_degree, int(splat.active_sh_degree()))
            except Exception:
                pass

        if not names:
            raise ConfigurationError(
                "The visible scene has no splats." if scope == "visible" else "The scene has no splats."
            )
        return SceneSelection(names, gaussian_count, maximum_sh_degree)

    def suggested_title(self, scope: str) -> str:
        try:
            names = self.resolve(scope).node_names
        except ConfigurationError:
            return "LichtFeld scene"
        return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"

    def start_export(self, source_format: str, path: Path, node_names: list[str], sh_degree: int) -> None:
        code = EXPORT_FORMATS.get(source_format)
        if code is None:
            raise ConfigurationError("The plugin exports SPZ, PLY or SOG.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lf.export_scene(code, str(path), node_names, int(sh_degree))

    def export_state(self) -> dict[str, Any]:
        try:
            value = self.runtime_state.export_progress_state.value
            if isinstance(value, dict):
                return dict(value)
        except Exception:
            pass
        value = self.lf.ui.get_export_state()
        return dict(value) if isinstance(value, dict) else {}

    def cancel_export(self) -> None:
        self.lf.ui.cancel_export()

    def open_url(self, url: str) -> None:
        if url:
            self.lf.ui.open_url(url)

    def copy_text(self, text: str) -> None:
        if text:
            self.lf.ui.set_clipboard_text(text)

    def request_redraw(self) -> None:
        try:
            self.lf.ui.request_redraw()
        except Exception:
            pass
