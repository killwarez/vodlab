from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import ClipCreateForm, SharedLoginForm
from .models import AuditEvent, MediaAsset, ProcessingJob, UploadSession
from .services import (
    asset_storage_bytes,
    asset_tree_storage_bytes,
    audit,
    client_ip,
    create_clip_job,
    create_processing_job,
    create_upload_session,
    delete_media_asset,
    delete_media_asset_tree,
    login_is_blocked,
    make_resume_key,
    record_login_failure,
    clear_login_failures,
    safe_file_stem,
    serve_storage_file,
    storage_relative,
    upload_temp_path,
    append_upload_chunk,
)

auth_logger = logging.getLogger("auth.activity")


def pick_focus_job(jobs: list[ProcessingJob]) -> ProcessingJob | None:
    for job in jobs:
        if job.status in {ProcessingJob.Status.RUNNING, ProcessingJob.Status.QUEUED}:
            return job
    return jobs[0] if jobs else None


def build_library_groups(assets: list[MediaAsset]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    group_map: dict[int, dict[str, object]] = {}
    matched_ids = {asset.id for asset in assets}

    for asset in assets:
        root_asset = asset.parent if asset.parent_id else asset
        if root_asset is None:
            continue

        group = group_map.get(root_asset.id)
        if group is None:
            group = {
                "asset": root_asset,
                "children": [],
                "context_only": root_asset.id not in matched_ids,
            }
            group_map[root_asset.id] = group
            groups.append(group)

        if asset.parent_id:
            group["children"].append(asset)
        else:
            group["asset"] = asset
            group["context_only"] = False

    for group in groups:
        asset = group["asset"]
        group["asset_storage_bytes"] = asset_storage_bytes(asset)
        group["family_storage_bytes"] = asset_tree_storage_bytes(asset)
        group["has_family_storage"] = group["family_storage_bytes"] > group["asset_storage_bytes"]
        for child in group["children"]:
            child.storage_bytes = asset_storage_bytes(child)

    return groups


def safe_redirect_target(request: HttpRequest, fallback: str = "library") -> HttpResponse:
    candidate = (request.POST.get("next") or "").strip()
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(candidate)
    return redirect(fallback)


def dispatch_job(job: ProcessingJob) -> None:
    from .tasks import create_clip_task, process_asset_task

    if job.job_type == ProcessingJob.JobType.PROCESS_ASSET:
        process_asset_task.delay(job.id)
    elif job.job_type == ProcessingJob.JobType.CREATE_CLIP:
        create_clip_task.delay(job.id)


def home(request: HttpRequest) -> HttpResponse:
    return redirect("library" if request.user.is_authenticated else "login")


def is_allowed_upload(file_name: str) -> bool:
    suffix = Path(file_name).suffix.lower()
    return suffix in {item.lower() for item in settings.MEDIA_ALLOWED_EXTENSIONS}


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("library")

    remote_addr = client_ip(request)
    form = SharedLoginForm(request, data=request.POST or None)
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        if login_is_blocked(username, remote_addr):
            form.add_error(None, "Too many failed attempts. Please wait and try again.")
            auth_logger.warning("login_blocked username=%s ip=%s", username or "-", remote_addr or "-")
        elif form.is_valid():
            login(request, form.get_user())
            clear_login_failures(username, remote_addr)
            audit(AuditEvent.EventType.LOGIN_SUCCESS, "User login", user=form.get_user(), remote_addr=remote_addr)
            auth_logger.info(
                "login_success username=%s user_id=%s ip=%s",
                form.get_user().get_username(),
                form.get_user().pk,
                remote_addr or "-",
            )
            return redirect("library")
        else:
            if username:
                record_login_failure(username, remote_addr)
            audit(AuditEvent.EventType.LOGIN_FAILED, "Failed login attempt", remote_addr=remote_addr, metadata={"username": username})
            auth_logger.warning("login_failed username=%s ip=%s", username or "-", remote_addr or "-")

    return render(request, "auth/login.html", {"form": form})


@login_required
@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    auth_logger.info("logout username=%s user_id=%s ip=%s", request.user.get_username(), request.user.pk, client_ip(request) or "-")
    logout(request)
    return redirect("login")


@login_required
@require_GET
def library_view(request: HttpRequest) -> HttpResponse:
    assets = MediaAsset.objects.select_related("parent", "uploaded_by").all()

    query = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    codec = (request.GET.get("codec") or "").strip()
    resolution = (request.GET.get("resolution") or "").strip()
    asset_type = (request.GET.get("asset_type") or "").strip()

    if query:
        assets = assets.filter(Q(title__icontains=query) | Q(original_name__icontains=query))
    if status:
        assets = assets.filter(current_status=status)
    if codec:
        assets = assets.filter(video_codec__icontains=codec)
    if resolution:
        try:
            width, height = resolution.lower().split("x", 1)
            assets = assets.filter(width=int(width), height=int(height))
        except ValueError:
            pass
    if asset_type:
        assets = assets.filter(asset_type=asset_type)

    visible_assets = list(assets[:100])
    visible_original_count = sum(1 for asset in visible_assets if asset.asset_type == MediaAsset.AssetType.ORIGINAL)
    visible_clip_count = sum(1 for asset in visible_assets if asset.asset_type == MediaAsset.AssetType.CLIP)
    context = {
        "asset_groups": build_library_groups(visible_assets),
        "visible_asset_count": len(visible_assets),
        "visible_original_count": visible_original_count,
        "visible_clip_count": visible_clip_count,
        "filters": {
            "q": query,
            "status": status,
            "codec": codec,
            "resolution": resolution,
            "asset_type": asset_type,
        },
    }
    return render(request, "mediahub/library.html", context)


@login_required
@require_GET
def upload_page(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "mediahub/upload.html",
        {
            "chunk_size": settings.MEDIA_CHUNK_SIZE,
            "max_upload_size": settings.MAX_UPLOAD_SIZE,
            "allowed_extensions": settings.MEDIA_ALLOWED_EXTENSIONS,
            "supports_multiple_uploads": True,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def asset_detail(request: HttpRequest, asset_id) -> HttpResponse:
    asset = get_object_or_404(
        MediaAsset.objects.prefetch_related("outputs", "children", "jobs", "children__outputs"),
        public_id=asset_id,
    )
    jobs = list(asset.jobs.all())
    focus_job = pick_focus_job(jobs)
    clip_form = ClipCreateForm(
        initial={
            "requested_start": 0,
            "requested_end": min(asset.duration_seconds or 10, 10),
        }
    )
    outputs = {output.kind: output for output in asset.outputs.all()}
    recent_audit = AuditEvent.objects.filter(asset=asset).select_related("user", "job")[:20]
    asset_storage_total = asset_storage_bytes(asset)
    family_storage_total = asset_tree_storage_bytes(asset)
    return render(
        request,
        "mediahub/asset_detail.html",
        {
            "asset": asset,
            "outputs": outputs,
            "children": asset.children.all(),
            "jobs": jobs[:20],
            "focus_job": focus_job,
            "clip_form": clip_form,
            "audit_events": recent_audit,
            "asset_storage_total": asset_storage_total,
            "family_storage_total": family_storage_total,
        },
    )


@login_required
@require_GET
def jobs_view(request: HttpRequest) -> HttpResponse:
    jobs = ProcessingJob.objects.select_related("asset", "created_by")[:100]
    return render(request, "mediahub/jobs.html", {"jobs": jobs})


@login_required
@require_GET
def jobs_partial(request: HttpRequest) -> HttpResponse:
    jobs = ProcessingJob.objects.select_related("asset", "created_by")[:100]
    return render(request, "mediahub/partials/jobs_table.html", {"jobs": jobs})


@login_required
@require_GET
def asset_progress_partial(request: HttpRequest, asset_id) -> HttpResponse:
    asset = get_object_or_404(MediaAsset.objects.prefetch_related("jobs"), public_id=asset_id)
    jobs = list(asset.jobs.all())
    return render(
        request,
        "mediahub/partials/asset_progress.html",
        {
            "asset": asset,
            "focus_job": pick_focus_job(jobs),
        },
    )


@login_required
@require_POST
def retry_job(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(ProcessingJob, pk=job_id)
    job.status = ProcessingJob.Status.QUEUED
    job.error_message = ""
    job.finished_at = None
    job.started_at = None
    job.progress_percent = 0
    job.progress_stage = "Queued"
    job.progress_message = "Waiting for worker"
    job.progress_payload = {}
    job.save(
        update_fields=[
            "status",
            "error_message",
            "finished_at",
            "started_at",
            "progress_percent",
            "progress_stage",
            "progress_message",
            "progress_payload",
            "updated_at",
        ]
    )
    dispatch_job(job)
    audit(AuditEvent.EventType.JOB_RETRIED, f"Retried job #{job.id}", user=request.user, asset=job.asset, job=job, remote_addr=client_ip(request))
    messages.success(request, f"Job #{job.id} was queued again.")
    return redirect("jobs")


@login_required
@require_POST
def upload_init(request: HttpRequest) -> JsonResponse:
    payload = json.loads(request.body or "{}")
    file_name = (payload.get("file_name") or "").strip()
    file_size = int(payload.get("file_size") or 0)
    content_type = (payload.get("content_type") or "").strip()
    last_modified = str(payload.get("last_modified") or "")
    if not file_name or file_size <= 0:
        return JsonResponse({"error": "File name and size are required."}, status=400)
    if file_size > settings.MAX_UPLOAD_SIZE:
        return JsonResponse({"error": "File is larger than the upload limit."}, status=400)
    if not is_allowed_upload(file_name):
        return JsonResponse(
            {
                "error": "Unsupported file type.",
                "allowed_extensions": settings.MEDIA_ALLOWED_EXTENSIONS,
            },
            status=400,
        )

    session = create_upload_session(
        user=request.user,
        file_name=file_name,
        content_type=content_type,
        file_size=file_size,
        resume_key=make_resume_key(file_name, file_size, last_modified),
    )
    audit(AuditEvent.EventType.UPLOAD_INIT, f"Upload initialized for {file_name}", user=request.user, remote_addr=client_ip(request), metadata={"session_id": str(session.public_id)})
    return JsonResponse(
        {
            "session_id": str(session.public_id),
            "uploaded_bytes": session.uploaded_bytes,
            "chunk_size": settings.MEDIA_CHUNK_SIZE,
            "expires_at": session.expires_at.isoformat(),
        }
    )


@login_required
@require_GET
def upload_status(request: HttpRequest, session_id) -> JsonResponse:
    session = get_object_or_404(UploadSession, public_id=session_id, created_by=request.user)
    return JsonResponse(
        {
            "session_id": str(session.public_id),
            "uploaded_bytes": session.uploaded_bytes,
            "file_size": session.file_size,
            "status": session.status,
        }
    )


@login_required
@require_http_methods(["PUT", "POST"])
def upload_chunk(request: HttpRequest, session_id) -> JsonResponse:
    session = get_object_or_404(UploadSession, public_id=session_id, created_by=request.user)
    try:
        offset = int(request.headers.get("X-Chunk-Offset", "0"))
    except ValueError:
        return JsonResponse({"error": "Invalid chunk offset."}, status=400)
    chunk = request.body
    if not chunk:
        return JsonResponse({"error": "Empty chunk payload."}, status=400)
    try:
        uploaded_bytes = append_upload_chunk(session, offset, chunk)
    except ValueError:
        session.refresh_from_db(fields=["uploaded_bytes"])
        return JsonResponse({"error": "Offset mismatch.", "uploaded_bytes": session.uploaded_bytes}, status=409)
    return JsonResponse({"uploaded_bytes": uploaded_bytes, "complete": uploaded_bytes >= session.file_size})


@login_required
@require_POST
def upload_complete(request: HttpRequest, session_id) -> JsonResponse:
    session = get_object_or_404(UploadSession, public_id=session_id, created_by=request.user)
    if session.uploaded_bytes != session.file_size:
        return JsonResponse({"error": "Upload is incomplete."}, status=400)

    existing = MediaAsset.objects.filter(source_upload=session).first()
    if existing:
        return JsonResponse({"asset_id": str(existing.public_id), "detail_url": f"/assets/{existing.public_id}/"})

    title = (json.loads(request.body or "{}").get("title") or "").strip()
    if not title:
        title = Path(session.file_name).stem

    asset = MediaAsset.objects.create(
        title=title,
        asset_type=MediaAsset.AssetType.ORIGINAL,
        current_status=MediaAsset.Status.UPLOADED,
        uploaded_by=request.user,
        source_upload=session,
        original_path="",
        original_name=session.file_name,
        mime_type=session.content_type,
        file_size=session.file_size,
    )

    original_dir = settings.ORIGINALS_ROOT / str(asset.public_id)
    original_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{safe_file_stem(session.file_name)}{Path(session.file_name).suffix or '.bin'}"
    final_path = original_dir / file_name
    shutil.move(str(upload_temp_path(session)), str(final_path))
    asset.original_path = storage_relative(final_path)
    asset.original_name = file_name
    asset.current_status = MediaAsset.Status.PROCESSING
    asset.save(update_fields=["original_path", "original_name", "current_status", "updated_at"])

    session.status = UploadSession.Status.COMPLETED
    session.save(update_fields=["status", "updated_at"])

    job = create_processing_job(asset=asset, created_by=request.user)
    dispatch_job(job)
    audit(AuditEvent.EventType.UPLOAD_COMPLETE, f"Upload completed for {asset.title}", user=request.user, asset=asset, job=job, remote_addr=client_ip(request))
    audit(AuditEvent.EventType.PROCESS_QUEUED, f"Asset queued for {asset.title}", user=request.user, asset=asset, job=job, remote_addr=client_ip(request))
    return JsonResponse({"asset_id": str(asset.public_id), "detail_url": f"/assets/{asset.public_id}/"})


@login_required
@require_POST
def create_clip(request: HttpRequest, asset_id) -> HttpResponse:
    asset = get_object_or_404(MediaAsset, public_id=asset_id)
    form = ClipCreateForm(request.POST)
    if not form.is_valid():
        for error in form.errors.values():
            messages.error(request, " ".join(error))
        return redirect("asset-detail", asset_id=asset.public_id)

    job = create_clip_job(
        asset=asset,
        requested_start=form.cleaned_data["requested_start"],
        requested_end=form.cleaned_data["requested_end"],
        title=form.cleaned_data.get("title"),
        created_by=request.user,
    )
    dispatch_job(job)
    audit(AuditEvent.EventType.CLIP_QUEUED, f"Clip queued for {asset.title}", user=request.user, asset=asset, job=job, remote_addr=client_ip(request))
    messages.success(request, "Clip job queued. Refresh the asset page or check the jobs view in a moment.")
    return redirect("asset-detail", asset_id=asset.public_id)


@login_required
@require_POST
def delete_asset(request: HttpRequest, asset_id) -> HttpResponse:
    asset = get_object_or_404(MediaAsset.objects.select_related("source_upload"), public_id=asset_id)
    asset_title = asset.title
    parent_title = asset.parent.title if asset.parent_id and asset.parent else None
    delete_media_asset(asset)
    if parent_title:
        messages.success(request, f'Removed "{asset_title}" from "{parent_title}".')
    else:
        messages.success(request, f'Removed "{asset_title}".')
    return safe_redirect_target(request)


@login_required
@require_POST
def delete_asset_tree(request: HttpRequest, asset_id) -> HttpResponse:
    asset = get_object_or_404(MediaAsset.objects.select_related("source_upload"), public_id=asset_id)
    asset_title = asset.title
    deleted_count = delete_media_asset_tree(asset)
    if deleted_count > 1:
        messages.success(request, f'Removed "{asset_title}" and {deleted_count - 1} child asset(s).')
    else:
        messages.success(request, f'Removed "{asset_title}".')
    return safe_redirect_target(request)


@login_required
@require_GET
def protected_media(request: HttpRequest, relative_path: str) -> HttpResponse:
    return serve_storage_file(relative_path)
