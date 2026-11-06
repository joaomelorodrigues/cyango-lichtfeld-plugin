from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cyango_lfs.controller import PluginController
from cyango_lfs.errors import ConfigurationError
from cyango_lfs.models import JobStatus
from cyango_lfs.storage import JobStore

from tests.fake_cyango import TOKEN, FakeCyango


class _Settings:
    def __init__(self) -> None:
        self.values: dict = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value

    def update(self, values) -> None:
        self.values.update(values)


class _Plugins:
    def __init__(self) -> None:
        self.value = _Settings()

    def settings(self, _name):
        return self.value


class _Splat:
    def active_sh_degree(self) -> int:
        return 3


class _Node:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.gaussian_count = count

    def splat_data(self):
        return _Splat()


class _Scene:
    def is_valid(self) -> bool:
        return True

    def get_nodes(self):
        return [_Node("Chapel", 1000), _Node("Garden", 500)]

    def get_visible_nodes(self):
        return [_Node("Chapel", 1000)]


class _Ui:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def request_redraw(self) -> None:
        pass

    def open_url(self, url: str) -> None:
        self.opened.append(url)

    def get_export_state(self):
        return {}

    def cancel_export(self) -> None:
        pass


class _Lfs:
    """The parts of the lichtfeld module the controller uses. Exports write random bytes."""

    def __init__(self, export_size: int = 700) -> None:
        self.plugins = _Plugins()
        self.ui = _Ui()
        self.export_size = export_size
        self.exports: list[tuple] = []

    def get_scene(self):
        return _Scene()

    def export_scene(self, code, path, names, sh_degree) -> None:
        self.exports.append((code, path, list(names), sh_degree))
        Path(path).write_bytes(os.urandom(self.export_size))


class _Signal:
    value: dict = {}

    def subscribe(self, _callback):
        return lambda: None


class _RuntimeState:
    export_progress_state = _Signal()


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


class PluginControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = JobStore(Path(self.dir.name))

    def _controller(self, lf: _Lfs, base_url: str) -> PluginController:
        lf.plugins.value.set("base_url", base_url)
        with patch("cyango_lfs.controller.load_token", return_value=""):
            controller = PluginController(lf, _RuntimeState(), self.store, auto_connect=False)
        self.addCleanup(controller.shutdown)
        return controller

    def test_connect_loads_the_account_and_folders(self) -> None:
        with FakeCyango() as fake:
            controller = self._controller(_Lfs(), fake.base_url)
            with patch("cyango_lfs.controller.clear_stored_token"):
                controller.set_token(TOKEN, remember=False)
            controller.test_connection()
            _wait_for(lambda: not controller.snapshot().active)
            state = controller.snapshot()
            self.assertEqual(state.error, "")
            self.assertEqual(state.account_text, "maker@example.com · Studio")
            self.assertEqual(state.folders, (("f1", "Scans"),))
            self.assertEqual(state.token_source, "this LichtFeld session")

    def test_export_then_upload_publishes_the_story(self) -> None:
        with FakeCyango() as fake:
            lf = _Lfs(export_size=900)
            controller = self._controller(lf, fake.base_url)
            with patch("cyango_lfs.controller.clear_stored_token"):
                controller.set_token(TOKEN, remember=False)
            controller.start_upload(
                scope="visible",
                source_format="spz",
                title="",
                description="",
                folder_id="f1",
                sh_degree=3,
                concurrency=2,
            )
            code, path, names, sh_degree = lf.exports[0]
            self.assertEqual((code, names, sh_degree), (2, ["Chapel"], 3))
            self.assertEqual(controller.snapshot().phase, "export")

            controller._on_export_state({"active": True, "progress": 0.5, "stage": "Writing", "path": path})
            self.assertEqual(controller.snapshot().progress, 0.5)
            controller._on_export_state({"active": False, "stage": "Complete", "path": path})
            # A second end event must not start a second upload.
            controller._on_export_state({"active": False, "stage": "Complete", "path": path})
            _wait_for(lambda: controller.snapshot().phase == "complete")

            state = controller.snapshot()
            self.assertEqual(state.story_url, "https://studio.example/editor/?storyId=s1")
            self.assertEqual(len(fake.completed), 1)
            self.assertEqual(fake.completed[0]["title"], "Chapel")
            self.assertEqual(fake.completed[0]["folder_id"], "f1")
            self.assertFalse(Path(path).exists(), "the staged export is deleted after success")
            self.assertEqual(self.store.list_jobs(), [])

    def test_failed_upload_can_be_resumed(self) -> None:
        with FakeCyango() as fake:
            lf = _Lfs(export_size=900)
            controller = self._controller(lf, fake.base_url)
            with patch("cyango_lfs.controller.clear_stored_token"):
                controller.set_token(TOKEN, remember=False)
            fake.fail["/uploads/complete"] = [400]
            controller.start_upload(
                scope="all", source_format="ply", title="Two", description="", folder_id="", sh_degree=3, concurrency=2
            )
            path = lf.exports[0][1]
            controller._on_export_state({"active": False, "stage": "Complete", "path": path})
            _wait_for(lambda: controller.snapshot().phase == "failed")
            state = controller.snapshot()
            self.assertIn("Injected 400", state.error)
            self.assertTrue(state.resume_available)
            self.assertEqual(self.store.latest_resumable().status, JobStatus.FAILED)

            controller.resume_latest(2)
            _wait_for(lambda: controller.snapshot().phase == "complete")
            self.assertEqual(len(fake.completed), 1)
            self.assertFalse(controller.snapshot().resume_available)

    def test_export_error_is_shown(self) -> None:
        with FakeCyango() as fake:
            lf = _Lfs()
            controller = self._controller(lf, fake.base_url)
            with patch("cyango_lfs.controller.clear_stored_token"):
                controller.set_token(TOKEN, remember=False)
            controller.start_upload(
                scope="all", source_format="sog", title="T", description="", folder_id="", sh_degree=3, concurrency=1
            )
            path = lf.exports[0][1]
            controller._on_export_state({"active": True, "progress": 0.1, "path": path})
            controller._on_export_state({"active": False, "stage": "Failed", "error": "Out of memory", "path": path})
            state = controller.snapshot()
            self.assertEqual(state.phase, "failed")
            self.assertEqual(state.error, "Out of memory")
            self.assertFalse(state.resume_available)

    def test_upload_needs_a_token(self) -> None:
        controller = self._controller(_Lfs(), "https://api.example.test/external-publish/v1")
        with self.assertRaises(ConfigurationError):
            controller.start_upload(
                scope="all", source_format="spz", title="", description="", folder_id="", sh_degree=3, concurrency=1
            )

    def test_remembered_token_is_loaded_and_falls_back_when_no_store(self) -> None:
        lf = _Lfs()
        lf.plugins.value.set("remember_token", True)
        with patch("cyango_lfs.controller.load_token", return_value=TOKEN):
            controller = PluginController(lf, _RuntimeState(), self.store, auto_connect=False)
        self.addCleanup(controller.shutdown)
        self.assertEqual(controller.snapshot().token_source, "secure credential store")

        with patch("cyango_lfs.controller.save_token", return_value=False):
            controller.set_token("cyp_other", remember=True)
        self.assertEqual(controller.snapshot().token_source, "this LichtFeld session")
        self.assertFalse(lf.plugins.value.get("remember_token"))


if __name__ == "__main__":
    unittest.main()
