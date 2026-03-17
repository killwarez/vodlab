from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_local_env()


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


APP_NAME = env("APP_NAME", "Media Hub") or "Media Hub"

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-secret-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["*"])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

STORAGE_ROOT = BASE_DIR / "storage"
ORIGINALS_ROOT = STORAGE_ROOT / "originals"
DERIVED_ROOT = STORAGE_ROOT / "derived"
DASH_ROOT = STORAGE_ROOT / "dash"
THUMBS_ROOT = STORAGE_ROOT / "thumbs"
TEMP_ROOT = STORAGE_ROOT / "temp"
BROKER_IN = STORAGE_ROOT / "broker_in"
BROKER_OUT = STORAGE_ROOT / "broker_out"
BROKER_PROCESSED = STORAGE_ROOT / "broker_processed"
BROKER_QUEUE = STORAGE_ROOT / "broker_queue"
CACHE_ROOT = STORAGE_ROOT / "cache"
LOGS_ROOT = STORAGE_ROOT / "logs"

for path in (
    STORAGE_ROOT,
    ORIGINALS_ROOT,
    DERIVED_ROOT,
    DASH_ROOT,
    THUMBS_ROOT,
    TEMP_ROOT,
    BROKER_IN,
    BROKER_OUT,
    BROKER_PROCESSED,
    BROKER_QUEUE,
    CACHE_ROOT,
    LOGS_ROOT,
):
    path.mkdir(parents=True, exist_ok=True)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mediahub.apps.MediahubConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vodlab.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.csrf",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "mediahub.context_processors.app_shell",
            ],
        },
    },
]

WSGI_APPLICATION = "vodlab.wsgi.application"
ASGI_APPLICATION = "vodlab.asgi.application"

POSTGRES_NAME = env("POSTGRES_DB")
POSTGRES_USER = env("POSTGRES_USER")
POSTGRES_PASSWORD = env("POSTGRES_PASSWORD")
POSTGRES_HOST = env("POSTGRES_HOST")
POSTGRES_PORT = env("POSTGRES_PORT", "5432")
USE_POSTGRES = all([POSTGRES_NAME, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST])

if USE_POSTGRES:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": POSTGRES_NAME,
            "USER": POSTGRES_USER,
            "PASSWORD": POSTGRES_PASSWORD,
            "HOST": POSTGRES_HOST,
            "PORT": POSTGRES_PORT,
            "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60") or 60),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("APP_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "library"
LOGOUT_REDIRECT_URL = "login"

FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024
MEDIA_CHUNK_SIZE = int(env("MEDIA_CHUNK_SIZE", str(4 * 1024 * 1024)) or 4 * 1024 * 1024)
MAX_UPLOAD_SIZE = int(env("MEDIA_MAX_UPLOAD_SIZE", str(10 * 1024 * 1024 * 1024)) or 10 * 1024 * 1024 * 1024)
LOGIN_RATE_LIMIT_ATTEMPTS = int(env("LOGIN_RATE_LIMIT_ATTEMPTS", "5") or 5)
LOGIN_RATE_LIMIT_WINDOW = int(env("LOGIN_RATE_LIMIT_WINDOW", "900") or 900)
UPLOAD_SESSION_EXPIRY_SECONDS = int(env("UPLOAD_SESSION_EXPIRY_SECONDS", "86400") or 86400)
MEDIA_ALLOWED_EXTENSIONS = env_list("MEDIA_ALLOWED_EXTENSIONS", [".mp4", ".mov", ".mkv", ".m4v", ".webm"])
DEMO_USERNAME = env("DEMO_USERNAME", "")
DEMO_PASSWORD = env("DEMO_PASSWORD", "")
FFMPEG_BIN = env("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = env("FFPROBE_BIN", "ffprobe")

SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": str(CACHE_ROOT),
        "TIMEOUT": 60 * 60,
        "OPTIONS": {"MAX_ENTRIES": 10_000},
    }
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "filesystem://")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "cache+memory://")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    # Use one shared queue folder so the web process and worker see the same messages on Windows.
    "data_folder_in": str(BROKER_QUEUE),
    "data_folder_out": str(BROKER_QUEUE),
    "data_folder_processed": str(BROKER_PROCESSED),
}
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "auth_file": {
            "class": "logging.FileHandler",
            "filename": str(LOGS_ROOT / "auth.log"),
            "formatter": "standard",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "auth.activity": {
            "handlers": ["auth_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
