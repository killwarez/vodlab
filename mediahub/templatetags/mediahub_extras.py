from __future__ import annotations

from django import template

register = template.Library()


@register.filter
def filesize_mb(value):
    if value in (None, ""):
        return "-"
    return f"{float(value) / (1024 * 1024):.1f} MB"


@register.filter
def filesize_human(value):
    if value in (None, ""):
        return "-"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    decimals = 0 if unit == "B" else 1
    return f"{size:.{decimals}f} {unit}"


@register.filter
def bitrate_mbps(value):
    if value in (None, ""):
        return "-"
    return f"{float(value) / 1_000_000:.2f} Mbps"


@register.filter
def seconds_hms(value):
    if value in (None, ""):
        return "-"
    total_seconds = int(float(value))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@register.filter
def status_color(value):
    return {
        "ready": "green",
        "processing": "azure",
        "uploaded": "yellow",
        "queued": "yellow",
        "running": "azure",
        "completed": "green",
        "failed": "red",
    }.get(str(value), "secondary")
