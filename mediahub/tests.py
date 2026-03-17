from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import MediaAsset, ProcessingJob, UploadSession
from .services import (
    asset_storage_bytes,
    asset_tree_storage_bytes,
    create_processing_job,
    ffmpeg_time_to_seconds,
    create_upload_session,
    login_is_blocked,
    mark_job_failed,
    record_login_failure,
    snap_clip_boundaries,
    storage_relative,
)
from .templatetags.mediahub_extras import bitrate_mbps, filesize_human

User = get_user_model()


class ClipBoundaryTests(SimpleTestCase):
    def test_snap_clip_boundaries_moves_to_safe_keyframes(self):
        actual_start, actual_end = snap_clip_boundaries([0.0, 2.0, 4.0, 8.0, 12.0], 3.1, 7.5, 15.0)
        self.assertEqual(actual_start, 2.0)
        self.assertEqual(actual_end, 8.0)

    def test_ffmpeg_time_to_seconds_parses_progress_timestamp(self):
        self.assertEqual(ffmpeg_time_to_seconds("00:01:05.500000"), 65.5)

    def test_formatters_render_human_values(self):
        self.assertEqual(bitrate_mbps(4_500_000), "4.50 Mbps")
        self.assertEqual(filesize_human(3 * 1024 * 1024), "3.0 MB")


class LibraryAuthTests(TestCase):
    def test_healthz_reports_ok(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_library_requires_login(self):
        response = self.client.get(reverse("library"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


class LibraryGroupingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="password123")
        self.client = Client()
        self.client.force_login(self.user)

    def test_library_shows_clip_nested_under_parent_asset(self):
        parent = MediaAsset.objects.create(
            title="Parent Original",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            original_path="originals/demo/parent.mp4",
            original_name="parent.mp4",
        )
        MediaAsset.objects.create(
            title="Child Clip",
            asset_type=MediaAsset.AssetType.CLIP,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            parent=parent,
            original_path="originals/demo/child.mp4",
            original_name="child.mp4",
        )

        response = self.client.get(reverse("library"), {"asset_type": "clip"})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Parent Original", content)
        self.assertIn("Child Clip", content)
        self.assertIn("Child clip of Parent Original", content)
        self.assertLess(content.index("Parent Original"), content.index("Child Clip"))
        self.assertIn('data-asset-url="', content)
        self.assertNotIn(">Open<", content)

    def test_delete_clip_removes_asset_and_keeps_parent(self):
        parent = MediaAsset.objects.create(
            title="Parent Original",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            original_path="originals/demo/parent.mp4",
            original_name="parent.mp4",
        )
        clip = MediaAsset.objects.create(
            title="Child Clip",
            asset_type=MediaAsset.AssetType.CLIP,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            parent=parent,
            original_name="child.mp4",
            original_path="",
        )
        clip_dir = settings.ORIGINALS_ROOT / str(clip.public_id)
        clip_dir.mkdir(parents=True, exist_ok=True)
        clip_path = clip_dir / "child.mp4"
        clip_path.write_bytes(b"clip")
        clip.original_path = storage_relative(clip_path)
        clip.save(update_fields=["original_path", "updated_at"])

        response = self.client.post(reverse("asset-delete", args=[clip.public_id]), {"next": reverse("library")})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("library"))
        self.assertTrue(MediaAsset.objects.filter(pk=parent.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=clip.pk).exists())
        self.assertFalse(clip_dir.exists())

    def test_delete_parent_with_children_removes_entire_asset_tree(self):
        session = create_upload_session(
            user=self.user,
            file_name="parent.mp4",
            content_type="video/mp4",
            file_size=12,
            resume_key="parent.mp4:12:1",
        )
        parent = MediaAsset.objects.create(
            title="Parent Original",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            source_upload=session,
            original_name="parent.mp4",
            original_path="",
        )
        parent_dir = settings.ORIGINALS_ROOT / str(parent.public_id)
        parent_dir.mkdir(parents=True, exist_ok=True)
        parent_path = parent_dir / "parent.mp4"
        parent_path.write_bytes(b"parent")
        parent.original_path = storage_relative(parent_path)
        parent.save(update_fields=["original_path", "updated_at"])

        child = MediaAsset.objects.create(
            title="Child Clip",
            asset_type=MediaAsset.AssetType.CLIP,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            parent=parent,
            original_name="child.mp4",
            original_path="",
        )
        child_dir = settings.ORIGINALS_ROOT / str(child.public_id)
        child_dir.mkdir(parents=True, exist_ok=True)
        child_path = child_dir / "child.mp4"
        child_path.write_bytes(b"child")
        child.original_path = storage_relative(child_path)
        child.save(update_fields=["original_path", "updated_at"])

        response = self.client.post(reverse("asset-delete-tree", args=[parent.public_id]), {"next": reverse("library")})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("library"))
        self.assertFalse(MediaAsset.objects.filter(pk=parent.pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=child.pk).exists())
        self.assertFalse(UploadSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(parent_dir.exists())
        self.assertFalse(child_dir.exists())
        self.assertFalse((settings.STORAGE_ROOT / session.temp_path).exists())

    def test_delete_parent_only_keeps_child_clip(self):
        parent = MediaAsset.objects.create(
            title="Parent Original",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            original_path="originals/demo/parent.mp4",
            original_name="parent.mp4",
        )
        child = MediaAsset.objects.create(
            title="Child Clip",
            asset_type=MediaAsset.AssetType.CLIP,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            parent=parent,
            original_name="child.mp4",
            original_path="originals/demo/child.mp4",
        )

        response = self.client.post(reverse("asset-delete", args=[parent.public_id]), {"next": reverse("library")})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("library"))
        self.assertFalse(MediaAsset.objects.filter(pk=parent.pk).exists())
        child.refresh_from_db()
        self.assertIsNone(child.parent)

    def test_storage_footprint_counts_parent_child_and_dash_chunks(self):
        parent = MediaAsset.objects.create(
            title="Parent Original",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            original_name="parent.mp4",
            original_path="",
        )
        child = MediaAsset.objects.create(
            title="Child Clip",
            asset_type=MediaAsset.AssetType.CLIP,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            parent=parent,
            original_name="child.mp4",
            original_path="",
        )

        parent_original_dir = settings.ORIGINALS_ROOT / str(parent.public_id)
        parent_original_dir.mkdir(parents=True, exist_ok=True)
        (parent_original_dir / "parent.mp4").write_bytes(b"12345")

        parent_dash_dir = settings.DASH_ROOT / str(parent.public_id)
        parent_dash_dir.mkdir(parents=True, exist_ok=True)
        (parent_dash_dir / "chunk-1.m4s").write_bytes(b"1234567")

        child_original_dir = settings.ORIGINALS_ROOT / str(child.public_id)
        child_original_dir.mkdir(parents=True, exist_ok=True)
        (child_original_dir / "child.mp4").write_bytes(b"123")

        self.assertEqual(asset_storage_bytes(parent), 12)
        self.assertEqual(asset_storage_bytes(child), 3)
        self.assertEqual(asset_tree_storage_bytes(parent), 15)


class UploadWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="password123")
        self.client = Client()
        self.client.force_login(self.user)

    def test_upload_init_rejects_unsupported_extension(self):
        response = self.client.post(
            reverse("upload-init"),
            data='{"file_name":"notes.txt","file_size":123,"content_type":"text/plain","last_modified":"1"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported file type", response.json()["error"])

    def test_upload_page_allows_selecting_multiple_files(self):
        response = self.client.get(reverse("upload"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="file"', html=False)
        self.assertContains(response, "multiple", html=False)

    def test_upload_init_creates_distinct_sessions_for_different_files(self):
        response_one = self.client.post(
            reverse("upload-init"),
            data='{"file_name":"sample-a.mp4","file_size":100,"content_type":"video/mp4","last_modified":"1"}',
            content_type="application/json",
        )
        response_two = self.client.post(
            reverse("upload-init"),
            data='{"file_name":"sample-b.mp4","file_size":200,"content_type":"video/mp4","last_modified":"2"}',
            content_type="application/json",
        )

        self.assertEqual(response_one.status_code, 200)
        self.assertEqual(response_two.status_code, 200)
        self.assertNotEqual(response_one.json()["session_id"], response_two.json()["session_id"])
        self.assertEqual(UploadSession.objects.count(), 2)

    def test_create_upload_session_reuses_existing_active_session(self):
        session = create_upload_session(
            user=self.user,
            file_name="sample.mp4",
            content_type="video/mp4",
            file_size=100,
            resume_key="sample.mp4:100:1",
        )
        reused = create_upload_session(
            user=self.user,
            file_name="sample.mp4",
            content_type="video/mp4",
            file_size=100,
            resume_key="sample.mp4:100:1",
        )
        self.assertEqual(session.pk, reused.pk)
        self.assertTrue(Path(session.temp_path).as_posix().endswith("upload.bin"))

    @override_settings(LOGIN_RATE_LIMIT_ATTEMPTS=3, LOGIN_RATE_LIMIT_WINDOW=60)
    def test_login_failures_eventually_block(self):
        cache.clear()
        for _ in range(3):
            record_login_failure("reviewer", "127.0.0.1")
        self.assertTrue(login_is_blocked("reviewer", "127.0.0.1"))

    def test_clip_job_failure_does_not_mark_parent_asset_failed(self):
        asset = MediaAsset.objects.create(
            title="Clip Source",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.READY,
            uploaded_by=self.user,
            original_path="originals/demo/sample.mp4",
            original_name="sample.mp4",
        )
        job = ProcessingJob.objects.create(
            asset=asset,
            created_by=self.user,
            job_type=ProcessingJob.JobType.CREATE_CLIP,
        )

        mark_job_failed(job, "clip failed")
        asset.refresh_from_db()

        self.assertEqual(asset.current_status, MediaAsset.Status.READY)

    def test_process_job_failure_marks_asset_failed(self):
        asset = MediaAsset.objects.create(
            title="Process Source",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.PROCESSING,
            uploaded_by=self.user,
            original_path="originals/demo/sample.mp4",
            original_name="sample.mp4",
        )
        job = ProcessingJob.objects.create(
            asset=asset,
            created_by=self.user,
            job_type=ProcessingJob.JobType.PROCESS_ASSET,
        )

        mark_job_failed(job, "processing failed")
        asset.refresh_from_db()

        self.assertEqual(asset.current_status, MediaAsset.Status.FAILED)

    def test_create_processing_job_starts_with_queued_progress_state(self):
        asset = MediaAsset.objects.create(
            title="Queued Source",
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.UPLOADED,
            uploaded_by=self.user,
            original_path="originals/demo/sample.mp4",
            original_name="sample.mp4",
        )

        job = create_processing_job(asset=asset, created_by=self.user)

        self.assertEqual(job.progress_percent, 0)
        self.assertEqual(job.progress_stage, "Queued")
        self.assertEqual(job.progress_message, "Waiting for worker")
