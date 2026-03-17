from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("library/", views.library_view, name="library"),
    path("upload/", views.upload_page, name="upload"),
    path("assets/<uuid:asset_id>/", views.asset_detail, name="asset-detail"),
    path("assets/<uuid:asset_id>/progress/", views.asset_progress_partial, name="asset-progress-partial"),
    path("assets/<uuid:asset_id>/delete/", views.delete_asset, name="asset-delete"),
    path("assets/<uuid:asset_id>/delete-tree/", views.delete_asset_tree, name="asset-delete-tree"),
    path("jobs/", views.jobs_view, name="jobs"),
    path("jobs/<int:job_id>/retry/", views.retry_job, name="job-retry"),
    path("api/uploads/init/", views.upload_init, name="upload-init"),
    path("api/uploads/<uuid:session_id>/status/", views.upload_status, name="upload-status"),
    path("api/uploads/<uuid:session_id>/chunk/", views.upload_chunk, name="upload-chunk"),
    path("api/uploads/<uuid:session_id>/complete/", views.upload_complete, name="upload-complete"),
    path("api/assets/<uuid:asset_id>/clip/", views.create_clip, name="asset-clip"),
    path("partials/jobs/", views.jobs_partial, name="jobs-partial"),
    path("protected-media/<path:relative_path>", views.protected_media, name="protected-media"),
]
