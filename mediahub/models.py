from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils.text import slugify

User = get_user_model()


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UploadSession(TimeStampedModel):
    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        UPLOADING = "uploading", "Uploading"
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    file_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField()
    uploaded_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED)
    temp_path = models.CharField(max_length=500)
    resume_key = models.CharField(max_length=255, db_index=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.file_name} ({self.status})"


class MediaAsset(TimeStampedModel):
    class AssetType(models.TextChoices):
        ORIGINAL = "original", "Original"
        CLIP = "clip", "Clip"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING = "processing", "Preparing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    asset_type = models.CharField(max_length=20, choices=AssetType.choices, default=AssetType.ORIGINAL)
    current_status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    uploaded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    source_upload = models.ForeignKey(UploadSession, null=True, blank=True, on_delete=models.SET_NULL)
    parent = models.ForeignKey("self", null=True, blank=True, related_name="children", on_delete=models.SET_NULL)
    original_path = models.CharField(max_length=500)
    original_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(default=0)
    checksum = models.CharField(max_length=128, blank=True)
    container_format = models.CharField(max_length=128, blank=True)
    video_codec = models.CharField(max_length=128, blank=True)
    audio_codec = models.CharField(max_length=128, blank=True)
    bitrate = models.BigIntegerField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    frame_rate = models.FloatField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    has_audio = models.BooleanField(default=False)
    clip_requested_start = models.FloatField(null=True, blank=True)
    clip_requested_end = models.FloatField(null=True, blank=True)
    clip_actual_start = models.FloatField(null=True, blank=True)
    clip_actual_end = models.FloatField(null=True, blank=True)
    extra_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["current_status", "asset_type"]),
            models.Index(fields=["video_codec"]),
            models.Index(fields=["width", "height"]),
        ]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:255]
        super().save(*args, **kwargs)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "-"


class DerivedOutput(TimeStampedModel):
    class OutputKind(models.TextChoices):
        PROXY = "proxy", "Proxy"
        DASH = "dash", "Manifest"
        POSTER = "poster", "Poster"
        TIMELINE = "timeline", "Timeline sheet"

    asset = models.ForeignKey(MediaAsset, related_name="outputs", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=OutputKind.choices)
    relative_path = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["kind", "created_at"]

    def __str__(self) -> str:
        return f"{self.asset.title} - {self.kind}"


class ProcessingJob(TimeStampedModel):
    class JobType(models.TextChoices):
        PROCESS_ASSET = "process_asset", "Prepare asset"
        CREATE_CLIP = "create_clip", "Create clip"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    asset = models.ForeignKey(MediaAsset, null=True, blank=True, related_name="jobs", on_delete=models.CASCADE)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    job_type = models.CharField(max_length=32, choices=JobType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    progress_stage = models.CharField(max_length=64, blank=True)
    progress_message = models.CharField(max_length=255, blank=True)
    progress_payload = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.job_type} #{self.pk} [{self.status}]"


class AuditEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        LOGIN_SUCCESS = "login_success", "Login success"
        LOGIN_FAILED = "login_failed", "Login failed"
        UPLOAD_INIT = "upload_init", "Upload initiated"
        UPLOAD_COMPLETE = "upload_complete", "Upload completed"
        PROCESS_QUEUED = "process_queued", "Asset queued"
        PROCESS_COMPLETED = "process_completed", "Asset ready"
        PROCESS_FAILED = "process_failed", "Asset failed"
        CLIP_QUEUED = "clip_queued", "Clip queued"
        CLIP_COMPLETED = "clip_completed", "Clip completed"
        CLIP_FAILED = "clip_failed", "Clip failed"
        JOB_RETRIED = "job_retried", "Job retried"

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    asset = models.ForeignKey(MediaAsset, null=True, blank=True, on_delete=models.SET_NULL)
    job = models.ForeignKey(ProcessingJob, null=True, blank=True, on_delete=models.SET_NULL)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    message = models.CharField(max_length=255)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.message
