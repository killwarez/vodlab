from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from mediahub.models import AuditEvent, MediaAsset, ProcessingJob
from mediahub.services import audit, create_clip_job, create_processing_job, storage_relative
from mediahub.tasks import create_clip_task, process_asset_task


class Command(BaseCommand):
    help = "Import a local sample video into the demo library, process it, and optionally create a clip."

    def add_arguments(self, parser):
        parser.add_argument("source", nargs="?", default="sample.mp4", help="Path to the source video file.")
        parser.add_argument("--title", default="Sample Demo Asset", help="Title for the imported source asset.")
        parser.add_argument("--clip-title", default="Sample Demo Clip", help="Title for the generated clip asset.")
        parser.add_argument("--clip-start", type=float, default=12.0, help="Requested clip start in seconds.")
        parser.add_argument("--clip-end", type=float, default=36.0, help="Requested clip end in seconds.")
        parser.add_argument("--username", default=settings.DEMO_USERNAME or "reviewer", help="User that will own the imported assets.")
        parser.add_argument("--replace", action="store_true", help="Delete existing assets with the same title before importing.")
        parser.add_argument("--skip-clip", action="store_true", help="Only import and process the source asset.")

    def handle(self, *args, **options):
        source = Path(options["source"]).resolve()
        if not source.exists():
            raise CommandError(f"Source file not found: {source}")

        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User not found: {options['username']}") from exc

        title = options["title"]
        clip_title = options["clip_title"]

        if options["replace"]:
            for existing_asset in MediaAsset.objects.filter(title__in=[title, clip_title]).only("public_id"):
                for root in (settings.ORIGINALS_ROOT, settings.DERIVED_ROOT, settings.DASH_ROOT, settings.THUMBS_ROOT):
                    shutil.rmtree(root / str(existing_asset.public_id), ignore_errors=True)
            MediaAsset.objects.filter(title__in=[title, clip_title]).delete()

        asset = MediaAsset.objects.create(
            title=title,
            asset_type=MediaAsset.AssetType.ORIGINAL,
            current_status=MediaAsset.Status.UPLOADED,
            uploaded_by=user,
            original_path="",
            original_name=source.name,
            mime_type="video/mp4",
            file_size=source.stat().st_size,
        )
        asset_dir = settings.ORIGINALS_ROOT / str(asset.public_id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        stored_source = asset_dir / source.name
        shutil.copy2(source, stored_source)
        asset.original_path = storage_relative(stored_source)
        asset.save(update_fields=["original_path", "updated_at"])

        process_job = create_processing_job(asset=asset, created_by=user)
        audit(AuditEvent.EventType.UPLOAD_COMPLETE, f"Imported {asset.title} from local source", user=user, asset=asset, job=process_job)
        audit(AuditEvent.EventType.PROCESS_QUEUED, f"Processing queued for {asset.title}", user=user, asset=asset, job=process_job)
        process_asset_task(process_job.id)
        asset.refresh_from_db()

        self.stdout.write(self.style.SUCCESS(f"Imported asset: {asset.title} [{asset.current_status}]"))
        self.stdout.write(f"Asset id: {asset.public_id}")

        if options["skip_clip"]:
            return

        clip_job = create_clip_job(
            asset=asset,
            requested_start=options["clip_start"],
            requested_end=options["clip_end"],
            title=clip_title,
            created_by=user,
        )
        audit(AuditEvent.EventType.CLIP_QUEUED, f"Clip queued for {asset.title}", user=user, asset=asset, job=clip_job)
        create_clip_task(clip_job.id)

        clip = MediaAsset.objects.filter(parent=asset, title=clip_title).order_by("-created_at").first()
        if not clip:
            raise CommandError("Clip import did not produce a child asset.")

        self.stdout.write(self.style.SUCCESS(f"Imported clip: {clip.title} [{clip.current_status}]"))
        self.stdout.write(f"Clip id: {clip.public_id}")
