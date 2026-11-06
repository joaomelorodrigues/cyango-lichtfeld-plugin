from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from cyango_lfs.errors import ApiError, ConfigurationError, PartUploadError, UploadCancelled
from cyango_lfs.models import JobStatus, UploadJob, utc_now
from cyango_lfs.uploader import UploadCallbacks, UploadEngine, upload_file_name

from tests.fake_cyango import TOKEN, FakeCyango


def _job(path: Path, base_url: str, **fields) -> UploadJob:
    now = utc_now()
    values = dict(
        id="job1",
        created_at=now,
        updated_at=now,
        status=JobStatus.UPLOADING,
        file_path=str(path),
        source_format="spz",
        title="Chapel scan",
        description="North side",
        folder_id="f1",
        node_names=["Splat"],
        sh_degree=3,
        base_url=base_url,
        file_size=path.stat().st_size,
    )
    values.update(fields)
    return UploadJob(**values)


class UploadEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _file(self, size: int) -> tuple[Path, bytes]:
        data = os.urandom(size)
        path = Path(self.dir.name) / "scene.spz"
        path.write_bytes(data)
        return path, data

    def test_small_file_uses_one_put_and_creates_the_story(self) -> None:
        path, data = self._file(500)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            outcome = UploadEngine().upload(job, TOKEN, threading.Event())
            self.assertEqual(outcome.story_id, "s1")
            self.assertEqual(outcome.story_url, "https://studio.example/editor/?storyId=s1")
            self.assertEqual(fake.completed[0]["data"], data)
            body = fake.completed[0]["body"]
            self.assertEqual(body["file_name"], "Chapel-scan.spz")
            self.assertEqual(body["title"], "Chapel scan")
            self.assertEqual(body["description"], "North side")
            self.assertEqual(body["folder_id"], "f1")

    def test_large_file_uploads_parts_in_parallel_and_joins_them_in_order(self) -> None:
        path, data = self._file(256 * 9 + 17)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            saves = []
            UploadEngine(concurrency=4).upload(
                job, TOKEN, threading.Event(), UploadCallbacks(on_checkpoint=lambda: saves.append(1))
            )
            self.assertEqual(fake.completed[0]["data"], data)
            self.assertEqual(job.part_count, 10)
            self.assertEqual([part.part_number for part in job.uploaded_parts], list(range(1, 11)))
            self.assertGreaterEqual(len(saves), 10)

    def test_resume_uploads_only_the_missing_parts(self) -> None:
        path, data = self._file(256 * 4)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            cancel = threading.Event()

            def cancel_after_two(_snapshot=None) -> None:
                if len(job.uploaded_parts) >= 2:
                    cancel.set()

            with self.assertRaises(UploadCancelled):
                UploadEngine(concurrency=1).upload(job, TOKEN, cancel, UploadCallbacks(on_checkpoint=cancel_after_two))
            self.assertEqual([part.part_number for part in job.uploaded_parts], [1, 2])
            upload_id = job.upload_id

            UploadEngine(concurrency=2).upload(job, TOKEN, threading.Event())
            self.assertEqual(job.upload_id, upload_id)
            self.assertEqual(fake.put_count[f"{upload_id}/part/1"], 1)
            self.assertEqual(fake.put_count[f"{upload_id}/part/2"], 1)
            self.assertEqual(fake.completed[0]["data"], data)

    def test_failed_part_is_retried_with_a_new_url(self) -> None:
        path, data = self._file(256 * 3)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            # A 500, then a 403 (expired URL), on part 2.
            engine = UploadEngine(concurrency=1)
            engine._wait = lambda *_args, **_kw: None  # type: ignore[method-assign]

            def on_checkpoint() -> None:
                if job.upload_id and not job.uploaded_parts:
                    fake.fail[f"{job.upload_id}/part/2"] = [500, 403]

            engine.upload(job, TOKEN, threading.Event(), UploadCallbacks(on_checkpoint=on_checkpoint))
            self.assertEqual(fake.put_count[f"{job.upload_id}/part/2"], 3)
            self.assertEqual(fake.completed[0]["data"], data)

    def test_expired_ticket_restarts_the_upload_once(self) -> None:
        path, data = self._file(256 * 2)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            fake.fail["/uploads/complete"] = [404]
            UploadEngine().upload(job, TOKEN, threading.Event())
            self.assertEqual(sum(1 for call in fake.calls if call == ("POST", "/external-publish/v1/uploads")), 2)
            self.assertEqual(fake.completed[0]["data"], data)

    def test_old_session_is_replaced_without_calling_the_server(self) -> None:
        path, _data = self._file(256 * 2)
        with FakeCyango() as fake:
            job = _job(
                path,
                fake.base_url,
                upload_id="stale",
                upload_created_at="2020-01-01T00:00:00+00:00",
                mode="multipart",
                part_size=256,
                part_count=2,
            )
            UploadEngine().upload(job, TOKEN, threading.Event())
            self.assertNotEqual(job.upload_id, "stale")
            self.assertFalse(any("stale" in call[1] for call in fake.calls))

    def test_single_upload_resume_after_put_goes_straight_to_complete(self) -> None:
        path, _data = self._file(100)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)
            fake.fail["/uploads/complete"] = [500, 500, 500, 500]
            engine = UploadEngine()
            engine._wait = lambda *_args, **_kw: None  # type: ignore[method-assign]
            with self.assertRaises(ApiError):
                engine.upload(job, TOKEN, threading.Event())
            self.assertEqual(len(job.uploaded_parts), 1)
            engine.upload(job, TOKEN, threading.Event())
            self.assertEqual(fake.put_count[f"{job.upload_id}/single"], 1)

    def test_wrong_token_and_wrong_account_are_reported(self) -> None:
        path, _data = self._file(100)
        with FakeCyango() as fake:
            with self.assertRaises(ApiError) as caught:
                UploadEngine().upload(_job(path, fake.base_url), "cyp_wrong", threading.Event())
            self.assertEqual(caught.exception.status, 401)
            self.assertIn("not valid", str(caught.exception))
            with self.assertRaises(ConfigurationError):
                UploadEngine().upload(_job(path, fake.base_url, account_id="someone-else"), TOKEN, threading.Event())

    def test_changed_export_is_refused(self) -> None:
        path, _data = self._file(100)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url, file_size=99)
            with self.assertRaises(ConfigurationError):
                UploadEngine().upload(job, TOKEN, threading.Event())

    def test_part_error_is_not_retried_when_permanent(self) -> None:
        path, _data = self._file(256 * 2)
        with FakeCyango() as fake:
            job = _job(path, fake.base_url)

            def on_checkpoint() -> None:
                if job.upload_id and not job.uploaded_parts:
                    fake.fail[f"{job.upload_id}/part/1"] = [400]

            with self.assertRaises(PartUploadError):
                UploadEngine(concurrency=1).upload(
                    job, TOKEN, threading.Event(), UploadCallbacks(on_checkpoint=on_checkpoint)
                )

    def test_file_name_is_safe(self) -> None:
        self.assertEqual(upload_file_name("Église / nave #2", "ply"), "glise-nave-2.ply")
        self.assertEqual(upload_file_name("///", "sog"), "scene.sog")


if __name__ == "__main__":
    unittest.main()
