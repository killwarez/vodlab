from __future__ import annotations

from celery import shared_task

from .models import ProcessingJob
from .services import create_clip_asset, mark_job_completed, mark_job_failed, mark_job_running, process_asset


@shared_task
def process_asset_task(job_id: int) -> None:
    job = ProcessingJob.objects.select_related("asset", "created_by").get(pk=job_id)
    mark_job_running(job)
    try:
        if not job.asset:
            raise RuntimeError("Job has no asset attached")
        process_asset(job.asset, job=job)
    except Exception as exc:  # pragma: no cover - exercised through integration flow
        mark_job_failed(job, str(exc))
        raise
    else:
        mark_job_completed(job, message=f"Asset ready: {job.asset.title}")


@shared_task
def create_clip_task(job_id: int) -> None:
    job = ProcessingJob.objects.select_related("asset", "created_by").get(pk=job_id)
    mark_job_running(job)
    try:
        if not job.asset:
            raise RuntimeError("Job has no source asset attached")
        clip = create_clip_asset(
            job.asset,
            requested_start=float(job.payload["requested_start"]),
            requested_end=float(job.payload["requested_end"]),
            title=job.payload.get("title") or None,
            created_by=job.created_by,
            job=job,
        )
    except Exception as exc:  # pragma: no cover - exercised through integration flow
        mark_job_failed(job, str(exc))
        raise
    else:
        mark_job_completed(job, message=f"Created clip {clip.title}")
