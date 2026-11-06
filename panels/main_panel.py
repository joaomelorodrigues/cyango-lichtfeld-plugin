"""Retained-mode Cyango publish panel."""

from __future__ import annotations

import html
from pathlib import Path

import lichtfeld as lf

from ..cyango_lfs.errors import CyangoError
from ..cyango_lfs.progress import format_bytes
from ..cyango_lfs.runtime import get_controller

SCOPES = ("all", "visible")
FORMATS = ("spz", "ply", "sog")
FORMAT_NOTES = {
    "spz": "Compact, near-lossless. Best for most scenes.",
    "ply": "Lossless, but the largest upload.",
    "sog": "Smallest upload, lossy compression.",
}
STUDIO_URL = "https://studio.cyango.com"
MODEL = "cyango"


def _unescape(value) -> str:
    return html.unescape(str(value or ""))


class CyangoPanel(lf.ui.Panel):
    id = "cyango.main"
    label = "Cyango"
    space = lf.ui.PanelSpace.MAIN_PANEL_TAB
    order = 211
    template = str(Path(__file__).resolve().with_name("main_panel.rml"))
    height_mode = lf.ui.PanelHeightMode.FILL
    update_interval_ms = 100
    update_policy = "interval"

    def __init__(self) -> None:
        controller = get_controller()
        scope_index = int(controller.preference("scope_index", 0))
        format_name = str(controller.preference("format", "spz"))
        self.scope = SCOPES[scope_index] if scope_index in range(len(SCOPES)) else SCOPES[0]
        self.source_format = format_name if format_name in FORMATS else FORMATS[0]
        self.sh_degree = str(max(0, min(int(controller.preference("sh_degree", 3)), 3)))
        self.concurrency = str(max(1, min(int(controller.preference("concurrency", 4)), 8)))
        self.folder_id = str(controller.preference("folder_id", ""))
        self.title = controller.suggested_title(self.scope)
        self.description = ""
        self.token_entry = ""
        self.remember_token = bool(controller.preference("remember_token", False))

        state = controller.snapshot()
        self._credentials_open = not state.token_configured
        self._advanced_open = False
        self._selection_valid = False
        self._selection_title = "Checking the scene..."
        self._selection_note = ""
        self._selection_metadata = ""
        self._last_scope = ""
        self._last_state_key = None
        self._last_folders = None
        self._auth_pending = False
        self._dismissed_story_id = ""
        self._handle = None
        self._refresh_selection(force=True)

    # -- Data model -----------------------------------------------------

    def on_bind_model(self, ctx) -> None:
        model = ctx.create_data_model(MODEL)
        if model is None:
            return

        for name, getter, setter in (
            ("scope", lambda: self.scope, self._set_scope),
            ("source_format", lambda: self.source_format, self._set_source_format),
            ("sh_degree", lambda: self.sh_degree, self._set_sh_degree),
            ("concurrency", lambda: self.concurrency, self._set_concurrency),
            ("folder_id", lambda: self.folder_id, self._set_folder_id),
            ("title", lambda: self.title, self._set_title),
            ("description", lambda: self.description, self._set_description),
            ("token_entry", lambda: self.token_entry, self._set_token_entry),
        ):
            model.bind(name, getter, setter)
        model.bind_record_list("folders")

        snapshot = lambda: get_controller().snapshot()  # noqa: E731
        for name, getter in (
            ("show_form", self._show_form),
            ("show_progress", self._show_progress),
            ("show_success", self._show_success),
            ("show_account", self._show_account),
            ("show_credentials", lambda: not self._show_account()),
            ("show_cancel_credentials", lambda: snapshot().token_configured),
            ("can_connect", self._can_connect),
            ("account_title", self._account_title),
            ("account_source", self._account_source),
            ("remember_token", lambda: self.remember_token),
            ("advanced_open", lambda: self._advanced_open),
            ("advanced_marker", lambda: "v" if self._advanced_open else ">"),
            ("format_note", lambda: FORMAT_NOTES.get(self.source_format, "")),
            ("selection_valid", lambda: self._selection_valid),
            ("selection_title", lambda: self._selection_title),
            ("selection_note", lambda: self._selection_note),
            ("can_upload", self._can_upload),
            ("show_credentials_error", self._show_credentials_error),
            ("show_upload_error", self._show_upload_error),
            ("form_error", lambda: snapshot().error),
            ("show_resume", lambda: snapshot().resume_available and not snapshot().active),
            ("resume_title", lambda: snapshot().resumable_title),
            ("resume_size", lambda: format_bytes(snapshot().resumable_size)),
            ("progress_heading", lambda: "Exporting scene" if snapshot().phase == "export" else "Uploading scene"),
            ("progress_status", lambda: snapshot().status_text),
            ("progress_value", lambda: str(max(0.0, min(snapshot().progress, 1.0)))),
            ("progress_pct", lambda: f"{snapshot().progress * 100:.0f}%"),
            ("progress_detail", lambda: snapshot().progress_text),
            ("success_title", lambda: self.title.strip() or "LichtFeld scene"),
            ("success_metadata", lambda: self._selection_metadata),
            ("has_story_url", lambda: bool(snapshot().story_url)),
        ):
            model.bind_func(name, getter)

        for name, handler in (
            ("change_credentials", self._on_change_credentials),
            ("cancel_credentials", self._on_cancel_credentials),
            ("connect", self._on_connect),
            ("open_token_page", lambda *_: get_controller().open_url(STUDIO_URL)),
            ("forget_token", self._on_forget_token),
            ("toggle_remember", self._on_toggle_remember),
            ("toggle_advanced", self._on_toggle_advanced),
            ("refresh_folders", self._on_refresh_folders),
            ("start_upload", self._on_start_upload),
            ("cancel_upload", lambda *_: get_controller().cancel()),
            ("resume_upload", self._on_resume_upload),
            ("discard_resume", self._on_discard_resume),
            ("open_story", self._on_open_story),
            ("copy_link", self._on_copy_link),
            ("upload_another", self._on_upload_another),
        ):
            model.bind_event(name, handler)

        self._handle = model.get_handle()
        self._last_folders = None
        self._sync_folders(get_controller().snapshot().folders)

    # -- Lifecycle ------------------------------------------------------

    def on_mount(self, doc) -> None:
        super().on_mount(doc)
        self._refresh_selection(force=True)
        self._dirty()

    def on_update(self, doc) -> bool:
        del doc
        controller = get_controller()
        controller.poll_export_state()
        state = controller.snapshot()

        dirty = self._refresh_selection()
        if self._sync_folders(state.folders):
            dirty = True
        state_key = tuple(vars(state).values())
        if state_key != self._last_state_key:
            self._last_state_key = state_key
            self._dirty()
            dirty = True
        if self._auth_pending and not state.active:
            self._auth_pending = False
            if state.token_configured and not state.error:
                self._credentials_open = False
                self.token_entry = ""
            self._dirty()
            dirty = True
        return dirty

    def on_scene_changed(self, doc) -> None:
        del doc
        self._last_scope = ""

    def on_unmount(self, doc) -> None:
        doc.remove_data_model(MODEL)
        self._handle = None

    # -- Setters --------------------------------------------------------

    def _set_scope(self, value) -> None:
        value = str(value)
        if value in SCOPES and value != self.scope:
            self.scope = value
            if not self.title.strip():
                self.title = get_controller().suggested_title(value)
            self._refresh_selection(force=True)
            self._dirty()

    def _set_source_format(self, value) -> None:
        value = str(value).lower()
        if value in FORMATS and value != self.source_format:
            self.source_format = value
            self._refresh_selection(force=True)
            self._dirty("source_format", "format_note", "success_metadata")

    def _set_sh_degree(self, value) -> None:
        try:
            self.sh_degree = str(max(0, min(int(float(value)), 3)))
        except (TypeError, ValueError):
            pass

    def _set_concurrency(self, value) -> None:
        try:
            self.concurrency = str(max(1, min(int(float(value)), 8)))
        except (TypeError, ValueError):
            pass

    def _set_folder_id(self, value) -> None:
        self.folder_id = str(value or "")

    def _set_title(self, value) -> None:
        self.title = _unescape(value)

    def _set_description(self, value) -> None:
        self.description = _unescape(value)

    def _set_token_entry(self, value) -> None:
        self.token_entry = _unescape(value)
        self._dirty("can_connect")

    # -- View helpers ---------------------------------------------------

    def _show_form(self) -> bool:
        return not self._show_progress() and not self._show_success()

    @staticmethod
    def _show_progress() -> bool:
        state = get_controller().snapshot()
        return state.active and state.phase in {"export", "upload"}

    def _show_success(self) -> bool:
        state = get_controller().snapshot()
        return state.phase == "complete" and bool(state.story_id) and state.story_id != self._dismissed_story_id

    def _show_account(self) -> bool:
        return get_controller().snapshot().token_configured and not self._credentials_open

    @staticmethod
    def _account_title() -> str:
        account = get_controller().snapshot().account_text
        return f"Connected as {account}" if account else "Connected to Cyango"

    @staticmethod
    def _account_source() -> str:
        source = get_controller().snapshot().token_source
        return {
            "secure credential store": "Token stored securely",
            "this LichtFeld session": "Token kept for this session",
        }.get(source, source)

    def _can_upload(self) -> bool:
        state = get_controller().snapshot()
        return state.token_configured and self._selection_valid and not state.active

    def _can_connect(self) -> bool:
        return bool(self.token_entry.strip()) and not get_controller().snapshot().active

    def _show_credentials_error(self) -> bool:
        state = get_controller().snapshot()
        return bool(state.error and not state.active and self._credentials_open)

    def _show_upload_error(self) -> bool:
        state = get_controller().snapshot()
        return bool(state.error and not state.active and not self._credentials_open)

    def _refresh_selection(self, *, force: bool = False) -> bool:
        if not force and self.scope == self._last_scope:
            return False
        previous = (self._selection_valid, self._selection_title, self._selection_note, self._selection_metadata)
        self._last_scope = self.scope
        try:
            selection = get_controller().inspect_scope(self.scope)
            count = len(selection.node_names)
            self._selection_valid = True
            self._selection_title = f"{count} splat {'node' if count == 1 else 'nodes'} ready"
            self._selection_note = f"{selection.gaussian_count:,} Gaussians"
            self._selection_metadata = f"{self.source_format.upper()} - {selection.gaussian_count:,} Gaussians"
        except CyangoError as error:
            self._selection_valid = False
            self._selection_title = {"all": "No splats in the scene", "visible": "No visible splats"}.get(
                self.scope, "The scene is not ready"
            )
            self._selection_note = str(error)
            self._selection_metadata = ""
        current = (self._selection_valid, self._selection_title, self._selection_note, self._selection_metadata)
        if current == previous:
            return False
        self._dirty("selection_valid", "selection_title", "selection_note", "can_upload", "success_metadata")
        return True

    def _sync_folders(self, folders) -> bool:
        if not self._handle or folders == self._last_folders:
            return False
        self._last_folders = folders
        self._handle.update_record_list("folders", [{"id": folder_id, "name": name} for folder_id, name in folders])
        if self.folder_id and folders and self.folder_id not in {folder_id for folder_id, _ in folders}:
            self.folder_id = ""
            self._dirty("folder_id")
        return True

    # -- Events ---------------------------------------------------------

    def _on_change_credentials(self, *_args) -> None:
        self._credentials_open = True
        self._dirty()

    def _on_cancel_credentials(self, *_args) -> None:
        if get_controller().snapshot().token_configured:
            self._credentials_open = False
            self.token_entry = ""
            self._dirty()

    def _on_connect(self, *_args) -> None:
        if not self._can_connect():
            return
        controller = get_controller()
        controller.set_token(self.token_entry, remember=self.remember_token)
        self.token_entry = ""
        self._auth_pending = True
        self._call(controller.test_connection)
        self._dirty()

    def _on_forget_token(self, *_args) -> None:
        get_controller().clear_token()
        self._credentials_open = True
        self.token_entry = ""
        self._dirty()

    def _on_toggle_remember(self, _handle, event, _args) -> None:
        checkbox = event.current_target() if event is not None else None
        if checkbox is None:
            return
        enabled = checkbox.has_attribute("checked")
        if enabled != self.remember_token:
            self.remember_token = enabled
            get_controller().set_remember_token(enabled)
            self._dirty("account_source")

    def _on_toggle_advanced(self, *_args) -> None:
        self._advanced_open = not self._advanced_open
        self._dirty("advanced_open", "advanced_marker")

    def _on_refresh_folders(self, *_args) -> None:
        self._call(get_controller().test_connection)

    def _on_start_upload(self, *_args) -> None:
        if not self._can_upload():
            return
        controller = get_controller()
        controller.save_preferences(
            {
                "scope_index": SCOPES.index(self.scope),
                "format": self.source_format,
                "sh_degree": int(self.sh_degree),
                "concurrency": int(self.concurrency),
                "folder_id": self.folder_id,
            }
        )
        self._dismissed_story_id = ""
        self._call(
            lambda: controller.start_upload(
                scope=self.scope,
                source_format=self.source_format,
                title=self.title,
                description=self.description,
                folder_id=self.folder_id,
                sh_degree=int(self.sh_degree),
                concurrency=int(self.concurrency),
            )
        )
        self._dirty()

    def _on_resume_upload(self, *_args) -> None:
        self._call(lambda: get_controller().resume_latest(int(self.concurrency)))

    def _on_discard_resume(self, *_args) -> None:
        get_controller().discard_resume()
        self._dirty()

    def _on_open_story(self, *_args) -> None:
        url = get_controller().snapshot().story_url
        if url:
            get_controller().open_url(url)

    def _on_copy_link(self, *_args) -> None:
        url = get_controller().snapshot().story_url
        if url:
            get_controller().copy_text(url)

    def _on_upload_another(self, *_args) -> None:
        self._dismissed_story_id = get_controller().snapshot().story_id
        self._refresh_selection(force=True)
        self._dirty()

    def _dirty(self, *names: str) -> None:
        if not self._handle:
            return
        if not names:
            self._handle.dirty_all()
            return
        for name in names:
            self._handle.dirty(name)

    @staticmethod
    def _call(operation) -> None:
        try:
            operation()
        except Exception as error:
            lf.log.error(f"Cyango: {error}")
            get_controller().report_error(error)
