"""
Settings for the Server Monitor control plane ("main").

Environment-driven (12-factor): copy `.env.example` to `.env` and override per
environment. Nothing in this file should need editing between local, staging
and production.
"""

import sys
from pathlib import Path

import dj_database_url
from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------#
# Core
# ---------------------------------------------------------------------------#

SECRET_KEY = config("SECRET_KEY")  # no default: fail loudly
SECRET_KEY_FALLBACKS = config("SECRET_KEY_FALLBACKS", default="", cast=Csv())
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost,127.0.0.1,[::1]", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

AUTH_USER_MODEL = "users.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core",
    "apps.users",
    "apps.audit",
    "apps.fleet",
    "apps.operations",
    "apps.alerts",
    "apps.agent_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise must sit directly below SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "apps.core.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "config.urls"
ADMIN_URL = config("ADMIN_URL", default="admin/")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.site",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------#
# Database
# ---------------------------------------------------------------------------#

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        # Reuse connections: a new TCP+TLS+auth handshake per request costs 5-20 ms, and
        # every agent heartbeat is a request.
        conn_max_age=config("DB_CONN_MAX_AGE", default=600, cast=int),
        conn_health_checks=True,
    )
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    # A runaway query dies in Postgres instead of holding a worker forever. Kept below
    # Gunicorn's 30 s timeout. Migrations and Celery run with DB_STATEMENT_TIMEOUT_MS=0.
    _stmt_ms = config("DB_STATEMENT_TIMEOUT_MS", default=20000, cast=int)
    DATABASES["default"].setdefault("OPTIONS", {}).update(
        {
            "options": f"-c statement_timeout={_stmt_ms} -c lock_timeout=5000"
            " -c idle_in_transaction_session_timeout=60000",
            "connect_timeout": 5,
        }
    )

# The test runner cannot DROP the test DB while a persistent connection is attached.
if "test" in sys.argv or "pytest" in sys.modules:
    DATABASES["default"]["CONN_MAX_AGE"] = 0

# ---------------------------------------------------------------------------#
# Authentication
# ---------------------------------------------------------------------------#

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "fleet:dashboard"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_AGE = config("SESSION_COOKIE_AGE", default=60 * 60 * 12, cast=int)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = CSRF_COOKIE_SAMESITE = "Lax"

LOGIN_FAILURE_LIMIT = config("LOGIN_FAILURE_LIMIT", default=6, cast=int)
LOGIN_FAILURE_TIMEOUT = config("LOGIN_FAILURE_TIMEOUT", default=900, cast=int)
# Only honour X-Forwarded-For when a trusted proxy overwrites it.
TRUST_PROXY_HEADERS = config("TRUST_PROXY_HEADERS", default=False, cast=bool)

# ---------------------------------------------------------------------------#
# I18N, static
# ---------------------------------------------------------------------------#

LANGUAGE_CODE = "en-us"
TIME_ZONE = config("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}

# ---------------------------------------------------------------------------#
# Cache & Celery
# ---------------------------------------------------------------------------#

REDIS_URL = config("REDIS_URL", default="")
# The login throttle needs a cache shared by every Gunicorn worker; locmem is per-process.
CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": f"{REDIS_URL}/1"}
        if REDIS_URL
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    )
}

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=f"{REDIS_URL}/0" if REDIS_URL else "")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# A heartbeat must not wait seconds on kombu retries when the broker is down.
CELERY_BROKER_CONNECTION_TIMEOUT = 2
CELERY_TASK_PUBLISH_RETRY_POLICY = {
    "max_retries": 2,
    "interval_start": 0,
    "interval_step": 0.1,
    "interval_max": 0.3,
}
CELERY_TASK_SOFT_TIME_LIMIT = 45
CELERY_TASK_TIME_LIMIT = 60
CELERY_TASK_SERIALIZER = CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_IGNORE_RESULT = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ALWAYS_EAGER = config(
    "CELERY_TASK_ALWAYS_EAGER", default=not bool(CELERY_BROKER_URL), cast=bool
)
CELERY_TASK_EAGER_PROPAGATES = False  # "webhook down" must not 500 a heartbeat
if not DEBUG and CELERY_TASK_ALWAYS_EAGER:
    # Without a broker, alert webhooks would run inside agent requests and the offline
    # sweep would never run at all. Fail at boot rather than silently degrade.
    raise ImproperlyConfigured("CELERY_BROKER_URL (or REDIS_URL) is required when DEBUG=False")
CELERY_BEAT_SCHEDULE = {
    # Offline detection + command expiry. 15 s keeps detection within one interval of the
    # default 30 s report cadence without hammering the DB.
    "monitor-sweep": {"task": "alerts.sweep", "schedule": 15.0},
    "prune-metrics": {"task": "fleet.prune_metrics", "schedule": 3600.0},
}

# ---------------------------------------------------------------------------#
# Monitoring behaviour
# ---------------------------------------------------------------------------#

SITE_NAME = config("SITE_NAME", default="Server Monitor")
# Public base URL the agents use; shown in the enrolment snippet.
MONITOR_PUBLIC_URL = config("MONITOR_PUBLIC_URL", default="http://127.0.0.1:8000")
# A server is "late" after this many missed intervals and "offline" after OFFLINE many.
MONITOR_LATE_AFTER_INTERVALS = config("MONITOR_LATE_AFTER_INTERVALS", default=2, cast=int)
MONITOR_OFFLINE_AFTER_INTERVALS = config("MONITOR_OFFLINE_AFTER_INTERVALS", default=6, cast=int)
# Network jitter allowance added on top of the interval maths.
MONITOR_GRACE_SECONDS = config("MONITOR_GRACE_SECONDS", default=10, cast=int)
MONITOR_METRIC_RETENTION_DAYS = config("MONITOR_METRIC_RETENTION_DAYS", default=14, cast=int)
# Pending commands no agent picked up within this window expire instead of firing late —
# a deploy that runs an hour after someone clicked it is worse than one that never ran.
MONITOR_COMMAND_DELIVERY_SECONDS = config("MONITOR_COMMAND_DELIVERY_SECONDS", default=300, cast=int)
MONITOR_COMMAND_OUTPUT_LIMIT = config("MONITOR_COMMAND_OUTPUT_LIMIT", default=256 * 1024, cast=int)
MONITOR_COMMANDS_PER_POLL = 10

ALERT_CPU_PERCENT = config("ALERT_CPU_PERCENT", default=90, cast=float)
ALERT_MEMORY_PERCENT = config("ALERT_MEMORY_PERCENT", default=90, cast=float)
ALERT_DISK_PERCENT = config("ALERT_DISK_PERCENT", default=90, cast=float)
# Resolve only once the value drops this far below the threshold, so a value hovering at
# the line doesn't open and close an alert every heartbeat.
ALERT_HYSTERESIS_PERCENT = 5.0
# CPU alerts on the average of the last N samples: one compile spike is not an incident.
ALERT_CPU_SAMPLES = 3
# Slack/Discord/Teams-compatible incoming webhook. Empty disables notifications.
ALERT_WEBHOOK_URL = config("ALERT_WEBHOOK_URL", default="")

# Where the agent files served at /agent/files/ live.
AGENT_DIST_DIR = Path(config("AGENT_DIST_DIR", default=str(BASE_DIR.parent / "agent")))

# ---------------------------------------------------------------------------#
# Security
# ---------------------------------------------------------------------------#

# All JS/CSS is vendored under static/, so scripts need nothing beyond 'self'.
# Inline style attributes are allowed for meter widths; inline scripts are not.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
    "base-uri": [CSP.SELF],
}

if not DEBUG:
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
    # Only behind a proxy that OVERWRITES X-Forwarded-Proto.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # Preload is a months-long, browser-shipped commitment for EVERY subdomain: opt in
    # deliberately via env. Silenced so `check --deploy` stays a hard gate for the rest.
    SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)
    if not SECURE_HSTS_PRELOAD:
        SILENCED_SYSTEM_CHECKS = ["security.W021"]
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Command output chunks are capped at 64 KiB by the agent; 2.5 MiB leaves ample headroom.
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440

# ---------------------------------------------------------------------------#
# Logging
# ---------------------------------------------------------------------------#

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
        **{
            n: {"handlers": ["console"], "level": "WARNING", "propagate": False}
            for n in ("urllib3", "celery", "kombu", "amqp")
        },
    },
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
}
