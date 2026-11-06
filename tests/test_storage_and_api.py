from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyango_lfs.api import ApiClient
from cyango_lfs.errors import ApiError, ConfigurationError
from cyango_lfs.models import JobStatus, UploadedPart, UploadJob, utc_now
from cyango_lfs.storage import JobStore

from tests.fake_cyango import TOKEN, FakeCyango


def _job(store: JobStore, job_id: str, status: JobStatus, size: int = 5) -> UploadJob:
    staged = store.staging_path(job_id, "spz")
    staged.write_bytes(b"x" * size)
    now = utc_now()
    job = UploadJob(
        id=job_id,
        created_at=now,
        updated_at=now,
        status=status,
        file_path=str(staged),
        source_format="spz",
        title=job_id,
        description="",
        folder_id="",
        node_names=["Splat"],
        sh_degree=3,
        base_url="https://api.example.test",
        file_size=size if status != JobStatus.EXPORTING else 0,
        uploaded_parts=[UploadedPart(1, '"etag"', 5)],
    )
    store.save(job)
    return job


class JobStoreTests(unittest.TestCase):
    def test_round_trip_resume_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            failed = _job(store, "failed", JobStatus.FAILED)
            _job(store, "stale-export", JobStatus.EXPORTING)
            _job(store, "done", JobStatus.COMPLETE)

            loaded = store.load("failed")
            self.assertEqual(loaded.uploaded_parts, failed.uploaded_parts)
            self.assertEqual(store.latest_resumable().id, "failed")

            store.cleanup()
            self.assertEqual([job.id for job in store.list_jobs()], ["failed"])
            self.assertEqual(sorted(path.name for path in store.staging_dir.iterdir()), ["failed.spz"])

    def test_delete_never_touches_files_outside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "cache")
            outside = Path(directory) / "user.ply"
            outside.write_bytes(b"keep")
            job = _job(store, "j", JobStatus.FAILED)
            job.file_path = str(outside)
            store.delete(job)
            self.assertTrue(outside.exists())


class ApiClientTests(unittest.TestCase):
    def test_token_and_url_checks(self) -> None:
        with self.assertRaises(ConfigurationError):
            ApiClient("")
        with self.assertRaises(ConfigurationError):
            ApiClient("pc_token")
        with self.assertRaises(ConfigurationError):
            ApiClient(TOKEN, "http://api.example.test/external-publish/v1")
        ApiClient(TOKEN, "http://localhost:8080/external-publish/v1")

    def test_me_folders_and_error_message(self) -> None:
        with FakeCyango() as fake:
            client = ApiClient(TOKEN, fake.base_url)
            self.assertEqual(client.get_me()["user"]["email"], "maker@example.com")
            self.assertEqual(client.list_folders(), [{"id": "f1", "name": "Scans"}])
            with self.assertRaises(ApiError) as caught:
                client.sign_parts("unknown", [1])
            self.assertEqual(caught.exception.status, 404)
            self.assertEqual(caught.exception.code, "not_found")
            self.assertEqual(str(caught.exception), "Unknown or expired upload_id.")
            self.assertFalse(caught.exception.transient)


if __name__ == "__main__":
    unittest.main()
