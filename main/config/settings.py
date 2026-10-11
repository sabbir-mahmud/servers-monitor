"""
Settings for the Server Monitor control plane ("main").

Environment-driven (12-factor): copy `.env.example` to `.env` and override per
environment. Nothing in this file should need editing between local, staging
and production.
"""

import sys
from pathlib import Path

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
    "apps.monitors",
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

# SQLite in WAL mode: readers never block the one writer, and the web and relay processes
# share the file safely. IMMEDIATE transactions take the write lock up front, so two writers
# queue on busy_timeout instead of failing with "database is locked" halfway through.
SQLITE_PATH = Path(config("SQLITE_PATH", default=str(BASE_DIR / "db.sqlite3")))
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA busy_timeout=20000;"
                "PRAGMA foreign_keys=ON;"
                "PRAGMA temp_store=MEMORY;"
                "PRAGMA cache_size=-20000;"
            ),
        },
        "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=600, cast=int),
        "CONN_HEALTH_CHECKS": True,
    }
}

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
# Cache & relay
# ---------------------------------------------------------------------------#

# Run the web app as ONE Gunicorn worker (with threads), so this per-process cache is the
# whole cache: the login throttle needs no Redis round trips.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# The relay: agents and this dashboard both dial out to Redis over TLS (e.g. Upstash).
# Admin credentials: they create one ACL user per server and never leave this machine.
REDIS_URL = config("REDIS_URL", default="")
if REDIS_URL.startswith("redis://") and ".upstash.io" in REDIS_URL:
    REDIS_URL = "rediss://" + REDIS_URL.removeprefix("redis://")  # Upstash is TLS-only
if not REDIS_URL and not DEBUG and "test" not in sys.argv and "pytest" not in sys.modules:
    raise ImproperlyConfigured("REDIS_URL is required: agents reach the dashboard through it")
RELAY_KEY_PREFIX = config("RELAY_KEY_PREFIX", default="sm")
# Host agents should use if it differs from REDIS_URL's (rare: a different DNS name).
RELAY_PUBLIC_HOST = config("RELAY_PUBLIC_HOST", default="")
# How long the bridge blocks waiting for agent messages per Redis command. Longer = fewer
# billed commands when idle; new servers are noticed within this many seconds.
RELAY_BLOCK_SECONDS = config("RELAY_BLOCK_SECONDS", default=60, cast=int)

# Agents install from GitHub, not from this dashboard (which isn't reachable from servers).
AGENT_REPO = config("AGENT_REPO", default="sabbir-mahmud/servers-monitor")
AGENT_REF = config("AGENT_REF", default="main")  # pin to a release tag in production
AGENT_INSTALL_URL = f"https://raw.githubusercontent.com/{AGENT_REPO}/{AGENT_REF}/agent/install.sh"

# ---------------------------------------------------------------------------#
# Monitoring behaviour
# ---------------------------------------------------------------------------#

SITE_NAME = config("SITE_NAME", default="Server Monitor")
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
# Application health checks run concurrently from the relay process.
MONITOR_CHECK_WORKERS = config("MONITOR_CHECK_WORKERS", default=8, cast=int)
MONITOR_SWEEP_SECONDS = 15  # offline detection + command expiry, run by the relay bridge

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

# HTTPS=True when the dashboard sits behind TLS (Caddy, nginx, Tailscale Serve, Cloudflare).
# Left False, it runs over plain HTTP on a local device or LAN: secure-only cookies would
# make login impossible there.
HTTPS = config("HTTPS", default=False, cast=bool)
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
if HTTPS and not DEBUG:
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
            for n in ("urllib3",)
        },
    },
    "root": {"handlers": ["console"], "level": config("LOG_LEVEL", default="INFO")},
}
