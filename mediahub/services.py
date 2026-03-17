from __future__ import annotations

import bisect
import hashlib
import json
import logging
import mimetypes
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import FileResponse, Http404
from django.utils import timezone
from django.utils.text import slugify

from .models import AuditEvent, DerivedOutput, MediaAsset, ProcessingJob, UploadSession

logger = logging.getLogger(__name__)
User = get_user_model()

ProgressCallback = Callable[[float, dict | None], None]


def client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def audit(event_type: str, message: str, *, user=None, asset=None, job=None, remote_addr=None, metadata=None) -> None:
    AuditEvent.objects.create(
        user=user,
        asset=asset,
        job=job,
        event_type=event_type,
        message=message,
        remote_addr=remote_addr,
        metadata=metadata or {},
    )


def login_cache_key(username: str, remote_addr: str | None) -> str:
    remote = remote_addr or "unknown"
    return f"login-attempts:{remote}:{username.lower()}"


def login_is_blocked(username: str, remote_addr: str | None) -> bool:
    return int(cache.get(login_cache_key(username, remote_addr), 0) or 0) >= settings.LOGIN_RATE_LIMIT_ATTEMPTS


def record_login_failure(username: str, remote_addr: str | None) -> None:
    key = login_cache_key(username, remote_addr)
    attempts = int(cache.get(key, 0) or 0) + 1
    cache.set(key, attempts, timeout=settings.LOGIN_RATE_LIMIT_WINDOW)


def clear_login_failures(username: str, remote_addr: str | None) -> None:
    cache.delete(login_cache_key(username, remote_addr))


def make_resume_key(file_name: str, file_size: int, last_modified: str | None) -> str:
    return f"{file_name}:{file_size}:{last_modified or 'na'}"


def safe_file_stem(file_name: str) -> str:
    stem = Path(file_name).stem or "asset"
    return slugify(stem) or "asset"


def storage_relative(path: Path) -> str:
    return str(path.relative_to(settings.STORAGE_ROOT)).replace("\\", "/")


def storage_absolute(relative_path: str) -> Path:
    candidate = (settings.STORAGE_ROOT / relative_path).resolve()
    if settings.STORAGE_ROOT.resolve() not in candidate.parents and candidate != settings.STORAGE_ROOT.resolve():
        raise ValueError("Unsafe storage path")
    return candidate


def upload_temp_path(session: UploadSession) -> Path:
    return storage_absolute(session.temp_path)


def create_upload_session(*, user, file_name: str, content_type: str, file_size: int, resume_key: str) -> UploadSession:
    now = timezone.now()
    existing = (
        UploadSession.objects.filter(
            created_by=user,
            resume_key=resume_key,
            status__in=[UploadSession.Status.INITIATED, UploadSession.Status.UPLOADING],
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing

    session_uuid = uuid.uuid4()
    temp_dir = settings.TEMP_ROOT / str(session_uuid)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / "upload.bin"
    temp_path.touch(exist_ok=True)
    return UploadSession.objects.create(
        created_by=user,
        file_name=file_name,
        content_type=content_type,
        file_size=file_size,
        temp_path=storage_relative(temp_path),
        resume_key=resume_key,
        expires_at=now + timedelta(seconds=settings.UPLOAD_SESSION_EXPIRY_SECONDS),
    )


def append_upload_chunk(session: UploadSession, offset: int, payload: bytes) -> int:
    temp_path = upload_temp_path(session)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    if session.uploaded_bytes != offset:
        raise ValueError("Offset mismatch")
    if session.uploaded_bytes + len(payload) > session.file_size:
        raise ValueError("Chunk exceeds declared file size")
    with temp_path.open("ab") as handle:
        handle.write(payload)
    session.uploaded_bytes += len(payload)
    session.status = UploadSession.Status.UPLOADING
    session.save(update_fields=["uploaded_bytes", "status", "updated_at"])
    return session.uploaded_bytes


def checksum_for_file(file_path: Path, *, progress_callback: ProgressCallback | None = None) -> str:
    digest = hashlib.sha256()
    total_bytes = file_path.stat().st_size
    processed_bytes = 0
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            processed_bytes += len(chunk)
            if progress_callback and total_bytes > 0:
                progress_callback(
                    processed_bytes / total_bytes,
                    {
                        "processed_bytes": processed_bytes,
                        "total_bytes": total_bytes,
                    },
                )
    if progress_callback:
        progress_callback(1.0, {"processed_bytes": total_bytes, "total_bytes": total_bytes})
    return digest.hexdigest()


def guess_mime(path: Path) -> str:
    if path.suffix == ".mpd":
        return "application/dash+xml"
    if path.suffix == ".m4s":
        return "video/iso.segment"
    if path.suffix == ".jpg":
        return "image/jpeg"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def run_command(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    logger.info("Running command: %s", " ".join(command))
    completed = subprocess.run(command, capture_output=True, text=True, check=False, cwd=str(cwd) if cwd else None)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Command failed")
    return completed


def clamp_progress_percent(value: float | int | None) -> int:
    if value in (None, ""):
        return 0
    return max(0, min(100, int(round(float(value)))))


def scale_progress(progress_window: tuple[float, float], local_percent: float | int) -> int:
    start, end = progress_window
    bounded_local = max(0.0, min(100.0, float(local_percent)))
    return clamp_progress_percent(start + ((end - start) * (bounded_local / 100.0)))


def ffmpeg_time_to_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        hours, minutes, seconds = value.split(":", 2)
        return (int(hours) * 3600) + (int(minutes) * 60) + float(seconds)
    except (TypeError, ValueError):
        return None


def set_job_progress(
    job: ProcessingJob | None,
    percent: float | int,
    stage: str,
    message: str = "",
    *,
    payload: dict | None = None,
    force: bool = False,
) -> None:
    if not job:
        return

    next_percent = clamp_progress_percent(percent)
    next_stage = (stage or "")[:64]
    next_message = (message or "")[:255]
    should_save = force or (
        job.progress_percent != next_percent
        or job.progress_stage != next_stage
        or job.progress_message != next_message
    )
    if not should_save:
        return

    job.progress_percent = next_percent
    job.progress_stage = next_stage
    job.progress_message = next_message
    if payload is not None:
        job.progress_payload = payload
    job.save(update_fields=["progress_percent", "progress_stage", "progress_message", "progress_payload", "updated_at"])


def run_ffmpeg_with_progress(
    command: list[str],
    *,
    duration_seconds: float | None,
    progress_callback: ProgressCallback | None = None,
    cwd: Path | None = None,
) -> None:
    ffmpeg_command = list(command)
    if "-loglevel" not in ffmpeg_command:
        ffmpeg_command[1:1] = ["-loglevel", "error"]
    if "-progress" not in ffmpeg_command:
        ffmpeg_command[1:1] = ["-progress", "pipe:1", "-nostats"]

    logger.info("Running ffmpeg command: %s", " ".join(ffmpeg_command))
    process = subprocess.Popen(
        ffmpeg_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        bufsize=1,
    )

    stderr_output = ""
    progress_state: dict[str, str] = {}
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            progress_state[key] = value
            if key == "out_time" and progress_callback:
                current_seconds = ffmpeg_time_to_seconds(value)
                if current_seconds is None:
                    continue
                if duration_seconds and duration_seconds > 0:
                    ratio = max(0.0, min(current_seconds / duration_seconds, 1.0))
                else:
                    ratio = 0.0
                payload = {
                    "current_seconds": round(current_seconds, 3),
                    "duration_seconds": round(duration_seconds or 0.0, 3),
                }
                if progress_state.get("speed"):
                    payload["speed"] = progress_state["speed"]
                progress_callback(ratio, payload)
            elif key == "progress" and value == "end" and progress_callback:
                payload = {
                    "current_seconds": round(duration_seconds or 0.0, 3),
                    "duration_seconds": round(duration_seconds or 0.0, 3),
                }
                if progress_state.get("speed"):
                    payload["speed"] = progress_state["speed"]
                progress_callback(1.0, payload)

        assert process.stderr is not None
        stderr_output = process.stderr.read()
        return_code = process.wait()
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    if return_code != 0:
        raise RuntimeError(stderr_output.strip() or "Command failed")


def ffprobe_json(file_path: Path) -> dict:
    completed = run_command(
        [
            settings.FFPROBE_BIN,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
    )
    return json.loads(completed.stdout)


def parse_frame_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        left, right = value.split("/", 1)
        if right and float(right) != 0:
            return round(float(left) / float(right), 3)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_media_metadata(file_path: Path) -> dict:
    probe = ffprobe_json(file_path)
    format_info = probe.get("format", {})
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    return {
        "container_format": (format_info.get("format_name") or "").split(",")[0],
        "duration_seconds": float(format_info["duration"]) if format_info.get("duration") else None,
        "bitrate": int(format_info["bit_rate"]) if format_info.get("bit_rate") else None,
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "has_audio": bool(audio),
        "mime_type": mimetypes.guess_type(file_path.name)[0] or "video/mp4",
        "extra_metadata": {"probe": probe},
    }


def clear_outputs(asset: MediaAsset) -> None:
    for output in asset.outputs.all():
        try:
            target = storage_absolute(output.relative_path)
        except ValueError:
            continue
        if target.exists():
            if target.is_file():
                target.unlink(missing_ok=True)
        output.delete()
    for root in (settings.DERIVED_ROOT / str(asset.public_id), settings.DASH_ROOT / str(asset.public_id), settings.THUMBS_ROOT / str(asset.public_id)):
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)


def remove_upload_session_artifacts(session: UploadSession | None) -> None:
    if not session:
        return
    try:
        temp_path = upload_temp_path(session)
    except ValueError:
        temp_path = None
    if temp_path and temp_path.parent.exists():
        shutil.rmtree(temp_path.parent, ignore_errors=True)
    session.delete()


def delete_asset_storage(asset: MediaAsset) -> None:
    clear_outputs(asset)
    original_dir = settings.ORIGINALS_ROOT / str(asset.public_id)
    if original_dir.exists():
        shutil.rmtree(original_dir, ignore_errors=True)


def delete_media_asset(asset: MediaAsset) -> int:
    source_upload = asset.source_upload
    delete_asset_storage(asset)
    asset.delete()
    remove_upload_session_artifacts(source_upload)
    return 1


def asset_tree(root: MediaAsset) -> list[MediaAsset]:
    collected: list[MediaAsset] = []
    pending_ids = [root.id]
    seen_ids: set[int] = set()

    while pending_ids:
        asset_id = pending_ids.pop()
        if asset_id in seen_ids:
            continue
        seen_ids.add(asset_id)
        asset = MediaAsset.objects.select_related("source_upload").prefetch_related("outputs").get(pk=asset_id)
        collected.append(asset)
        child_ids = list(MediaAsset.objects.filter(parent_id=asset_id).values_list("id", flat=True))
        pending_ids.extend(child_ids)

    return collected


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def asset_storage_bytes(asset: MediaAsset) -> int:
    return sum(
        directory_size(root / str(asset.public_id))
        for root in (settings.ORIGINALS_ROOT, settings.DERIVED_ROOT, settings.DASH_ROOT, settings.THUMBS_ROOT)
    )


def asset_tree_storage_bytes(root: MediaAsset) -> int:
    return sum(asset_storage_bytes(asset) for asset in asset_tree(root))


def delete_media_asset_tree(root: MediaAsset) -> int:
    assets = asset_tree(root)
    for asset in reversed(assets):
        source_upload = asset.source_upload
        delete_asset_storage(asset)
        asset.delete()
        remove_upload_session_artifacts(source_upload)
    return len(assets)


def create_output(asset: MediaAsset, kind: str, path: Path, *, width=None, height=None, duration=None, metadata=None) -> DerivedOutput:
    return DerivedOutput.objects.create(
        asset=asset,
        kind=kind,
        relative_path=storage_relative(path),
        mime_type=guess_mime(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        width=width,
        height=height,
        duration_seconds=duration,
        metadata=metadata or {},
    )


def process_asset(
    asset: MediaAsset,
    *,
    job: ProcessingJob | None = None,
    progress_window: tuple[float, float] = (0, 100),
    stage_prefix: str = "",
) -> None:
    def stage_name(name: str) -> str:
        return f"{stage_prefix}{name}" if stage_prefix else name

    def report(local_percent: float | int, stage: str, message: str = "", payload: dict | None = None) -> None:
        set_job_progress(job, scale_progress(progress_window, local_percent), stage_name(stage), message, payload=payload)

    def report_range(
        local_start: float,
        local_end: float,
        ratio: float,
        stage: str,
        message: str = "",
        payload: dict | None = None,
    ) -> None:
        bounded_ratio = max(0.0, min(1.0, ratio))
        local_percent = local_start + ((local_end - local_start) * bounded_ratio)
        report(local_percent, stage, message, payload)

    source_path = storage_absolute(asset.original_path)
    if not source_path.exists():
        raise FileNotFoundError("Original asset file is missing")

    clear_outputs(asset)
    report(5, "Inspecting file", "Reading technical metadata")
    metadata = extract_media_metadata(source_path)
    asset.current_status = MediaAsset.Status.PROCESSING
    asset.file_size = source_path.stat().st_size
    report(10, "Checking file", "Calculating checksum")
    asset.checksum = checksum_for_file(
        source_path,
        progress_callback=lambda ratio, payload: report_range(10, 20, ratio, "Checking file", "Calculating checksum", payload),
    )
    asset.container_format = metadata["container_format"]
    asset.video_codec = metadata["video_codec"]
    asset.audio_codec = metadata["audio_codec"]
    asset.bitrate = metadata["bitrate"]
    asset.width = metadata["width"]
    asset.height = metadata["height"]
    asset.frame_rate = metadata["frame_rate"]
    asset.duration_seconds = metadata["duration_seconds"]
    asset.has_audio = metadata["has_audio"]
    asset.mime_type = metadata["mime_type"]
    asset.extra_metadata = metadata["extra_metadata"]
    asset.save()
    report(22, "Preparing asset", "Setting up derived outputs")

    derived_dir = settings.DERIVED_ROOT / str(asset.public_id)
    dash_dir = settings.DASH_ROOT / str(asset.public_id)
    thumbs_dir = settings.THUMBS_ROOT / str(asset.public_id)
    derived_dir.mkdir(parents=True, exist_ok=True)
    dash_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    proxy_path = derived_dir / "proxy.mp4"
    poster_path = thumbs_dir / "poster.jpg"
    timeline_path = thumbs_dir / "timeline.jpg"
    manifest_path = dash_dir / "stream.mpd"
    duration_seconds = float(metadata["duration_seconds"] or 0.0)
    effective_duration = max(duration_seconds, 0.1)
    poster_seek = min(1.0, effective_duration / 2)
    timeline_fps = min(max(12 / effective_duration, 0.1), 4.0)

    scale_filter = "scale='min(1280,iw)':-2"
    proxy_command = [
        settings.FFMPEG_BIN,
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-sc_threshold",
        "0",
        "-vf",
        scale_filter,
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(proxy_path),
    ]
    report(25, "Generating proxy", "Creating preview video")
    run_ffmpeg_with_progress(
        proxy_command,
        duration_seconds=effective_duration,
        progress_callback=lambda ratio, payload: report_range(25, 65, ratio, "Generating proxy", "Creating preview video", payload),
    )

    report(68, "Creating poster", "Capturing poster frame")
    run_command(
        [
            settings.FFMPEG_BIN,
            "-y",
            "-ss",
            f"{poster_seek:.3f}",
            "-i",
            str(proxy_path),
            "-frames:v",
            "1",
            str(poster_path),
        ]
    )

    report(74, "Creating timeline", "Building timeline sheet")
    run_command(
        [
            settings.FFMPEG_BIN,
            "-y",
            "-i",
            str(proxy_path),
            "-vf",
            f"fps={timeline_fps:.3f},scale=240:-1,tile=4x3",
            "-frames:v",
            "1",
            str(timeline_path),
        ]
    )

    report(80, "Packaging playback", "Preparing manifest and segments")
    run_ffmpeg_with_progress(
        [
            settings.FFMPEG_BIN,
            "-y",
            "-i",
            str(proxy_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "dash",
            "-seg_duration",
            "4",
            "-use_template",
            "1",
            "-use_timeline",
            "1",
            "stream.mpd",
        ],
        cwd=dash_dir,
        duration_seconds=effective_duration,
        progress_callback=lambda ratio, payload: report_range(80, 95, ratio, "Packaging playback", "Preparing manifest and segments", payload),
    )

    report(96, "Registering outputs", "Saving derived file metadata")
    proxy_meta = extract_media_metadata(proxy_path)
    create_output(
        asset,
        DerivedOutput.OutputKind.PROXY,
        proxy_path,
        width=proxy_meta["width"],
        height=proxy_meta["height"],
        duration=proxy_meta["duration_seconds"],
    )
    create_output(asset, DerivedOutput.OutputKind.POSTER, poster_path)
    create_output(asset, DerivedOutput.OutputKind.TIMELINE, timeline_path)
    create_output(
        asset,
        DerivedOutput.OutputKind.DASH,
        manifest_path,
        width=proxy_meta["width"],
        height=proxy_meta["height"],
        duration=proxy_meta["duration_seconds"],
    )

    asset.current_status = MediaAsset.Status.READY
    asset.save(update_fields=["current_status", "updated_at"])
    report(99, "Finishing", "Asset is ready")


def keyframes_for_file(file_path: Path) -> list[float]:
    completed = run_command(
        [
            settings.FFPROBE_BIN,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "csv=p=0",
            str(file_path),
        ]
    )
    points = []
    for raw in completed.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        pts_time, _, flags = raw.partition(",")
        if "K" not in flags:
            continue
        try:
            points.append(float(pts_time))
        except ValueError:
            continue
    return sorted(set(points))


def snap_clip_boundaries(keyframes: list[float], requested_start: float, requested_end: float, duration: float | None) -> tuple[float, float]:
    if not keyframes:
        raise RuntimeError("No keyframes found for stream copy clipping")
    start_index = bisect.bisect_right(keyframes, requested_start) - 1
    actual_start = keyframes[max(start_index, 0)]
    end_index = bisect.bisect_left(keyframes, requested_end)
    if end_index >= len(keyframes):
        actual_end = duration or requested_end
    else:
        actual_end = keyframes[end_index]
    if duration is not None:
        actual_end = min(actual_end, duration)
    if actual_end <= actual_start:
        raise RuntimeError("Unable to snap clip boundaries safely")
    return round(actual_start, 3), round(actual_end, 3)


def create_clip_asset(
    parent: MediaAsset,
    *,
    requested_start: float,
    requested_end: float,
    title: str | None,
    created_by=None,
    job: ProcessingJob | None = None,
    progress_window: tuple[float, float] = (0, 100),
) -> MediaAsset:
    def report(local_percent: float | int, stage: str, message: str = "", payload: dict | None = None) -> None:
        set_job_progress(job, scale_progress(progress_window, local_percent), stage, message, payload=payload)

    def report_range(
        local_start: float,
        local_end: float,
        ratio: float,
        stage: str,
        message: str = "",
        payload: dict | None = None,
    ) -> None:
        bounded_ratio = max(0.0, min(1.0, ratio))
        report(local_start + ((local_end - local_start) * bounded_ratio), stage, message, payload)

    source_path = storage_absolute(parent.original_path)
    report(5, "Scanning source", "Reading source keyframes")
    keyframes = keyframes_for_file(source_path)
    report(15, "Choosing cut points", "Snapping requested range to safe keyframes")
    actual_start, actual_end = snap_clip_boundaries(keyframes, requested_start, requested_end, parent.duration_seconds)
    clip_title = title or f"{parent.title} clip {requested_start:.1f}-{requested_end:.1f}s"
    clip = MediaAsset.objects.create(
        title=clip_title,
        asset_type=MediaAsset.AssetType.CLIP,
        parent=parent,
        uploaded_by=created_by,
        original_path="",
        original_name=f"{safe_file_stem(parent.original_name)}-clip.mp4",
        current_status=MediaAsset.Status.PROCESSING,
        clip_requested_start=requested_start,
        clip_requested_end=requested_end,
        clip_actual_start=actual_start,
        clip_actual_end=actual_end,
    )
    clip_dir = settings.ORIGINALS_ROOT / str(clip.public_id)
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / clip.original_name
    clip_duration = max(round(actual_end - actual_start, 3), 0.1)
    report(20, "Creating clip", "Copying selected range")
    run_ffmpeg_with_progress(
        [
            settings.FFMPEG_BIN,
            "-y",
            "-ss",
            str(actual_start),
            "-t",
            str(clip_duration),
            "-i",
            str(source_path),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "1",
            str(clip_path),
        ],
        duration_seconds=clip_duration,
        progress_callback=lambda ratio, payload: report_range(20, 50, ratio, "Creating clip", "Copying selected range", payload),
    )
    clip.original_path = storage_relative(clip_path)
    clip.save(update_fields=["original_path", "updated_at"])
    report(55, "Preparing clip", "Generating playback outputs for the new clip")
    process_asset(clip, job=job, progress_window=(55, 99), stage_prefix="Preparing clip: ")
    return clip


def create_processing_job(*, asset: MediaAsset, created_by=None) -> ProcessingJob:
    return ProcessingJob.objects.create(
        asset=asset,
        created_by=created_by,
        job_type=ProcessingJob.JobType.PROCESS_ASSET,
        progress_stage="Queued",
        progress_message="Waiting for worker",
    )


def create_clip_job(*, asset: MediaAsset, requested_start: float, requested_end: float, title: str | None, created_by=None) -> ProcessingJob:
    return ProcessingJob.objects.create(
        asset=asset,
        created_by=created_by,
        job_type=ProcessingJob.JobType.CREATE_CLIP,
        progress_stage="Queued",
        progress_message="Waiting for worker",
        payload={"requested_start": requested_start, "requested_end": requested_end, "title": title or ""},
    )


def mark_job_running(job: ProcessingJob) -> None:
    job.status = ProcessingJob.Status.RUNNING
    job.started_at = timezone.now()
    job.attempts += 1
    job.error_message = ""
    job.progress_percent = 0
    job.progress_stage = "Starting"
    job.progress_message = "Worker picked up the job"
    job.progress_payload = {}
    job.save(
        update_fields=[
            "status",
            "started_at",
            "attempts",
            "error_message",
            "progress_percent",
            "progress_stage",
            "progress_message",
            "progress_payload",
            "updated_at",
        ]
    )


def mark_job_completed(job: ProcessingJob, *, message: str) -> None:
    job.status = ProcessingJob.Status.COMPLETED
    job.finished_at = timezone.now()
    job.progress_percent = 100
    job.progress_stage = "Completed"
    job.progress_message = message[:255]
    job.save(update_fields=["status", "finished_at", "progress_percent", "progress_stage", "progress_message", "updated_at"])
    event = AuditEvent.EventType.PROCESS_COMPLETED if job.job_type == ProcessingJob.JobType.PROCESS_ASSET else AuditEvent.EventType.CLIP_COMPLETED
    audit(event, message, user=job.created_by, asset=job.asset, job=job)


def mark_job_failed(job: ProcessingJob, error_message: str) -> None:
    job.status = ProcessingJob.Status.FAILED
    job.finished_at = timezone.now()
    job.error_message = error_message[:4000]
    job.progress_stage = "Failed"
    job.progress_message = error_message[:255]
    job.save(update_fields=["status", "finished_at", "error_message", "progress_stage", "progress_message", "updated_at"])
    if job.asset and job.job_type == ProcessingJob.JobType.PROCESS_ASSET:
        job.asset.current_status = MediaAsset.Status.FAILED
        job.asset.save(update_fields=["current_status", "updated_at"])
    event = AuditEvent.EventType.PROCESS_FAILED if job.job_type == ProcessingJob.JobType.PROCESS_ASSET else AuditEvent.EventType.CLIP_FAILED
    audit(event, error_message[:255], user=job.created_by, asset=job.asset, job=job)


def serve_storage_file(relative_path: str) -> FileResponse:
    absolute = storage_absolute(relative_path)
    if not absolute.exists() or absolute.is_dir():
        raise Http404("File not found")
    response = FileResponse(absolute.open("rb"), content_type=guess_mime(absolute))
    response["Content-Length"] = absolute.stat().st_size
    response["X-Content-Type-Options"] = "nosniff"
    return response
