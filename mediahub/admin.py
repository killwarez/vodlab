from django.contrib import admin

from .models import AuditEvent, DerivedOutput, MediaAsset, ProcessingJob, UploadSession


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("title", "asset_type", "current_status", "resolution", "video_codec", "created_at")
    list_filter = ("asset_type", "current_status", "video_codec")
    search_fields = ("title", "original_name", "checksum")
    readonly_fields = ("public_id", "created_at", "updated_at")


@admin.register(DerivedOutput)
class DerivedOutputAdmin(admin.ModelAdmin):
    list_display = ("asset", "kind", "relative_path", "size_bytes")
    list_filter = ("kind",)


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "job_type", "status", "asset", "attempts", "created_at")
    list_filter = ("job_type", "status")
    search_fields = ("error_message",)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "message", "user", "asset", "created_at")
    list_filter = ("event_type",)
    search_fields = ("message", "metadata")


@admin.register(UploadSession)
class UploadSessionAdmin(admin.ModelAdmin):
    list_display = ("file_name", "status", "uploaded_bytes", "file_size", "created_at")
    list_filter = ("status",)
