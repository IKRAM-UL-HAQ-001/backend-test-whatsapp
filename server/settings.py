import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
import firebase_admin
from firebase_admin import credentials


BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    raw = env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "unsafe-dev-secret-key-change-me")
DEBUG = env_bool("DEBUG", False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*")
AUTH_USER_MODEL = "users.User"

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "https://localhost")
CORS_ALLOW_CREDENTIALS = True

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "channels",
    "storages",
    "users",
    "chat",
    "status",
    "calls",
]

MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "server.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "server.wsgi.application"
ASGI_APPLICATION = "server.asgi.application"

DATABASE_URL = env("DATABASE_URL", "")
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=int(env("DB_CONN_MAX_AGE", "60")),
            conn_health_checks=True,
        )
    }
    DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = False
    DATABASES["default"]["OPTIONS"] = {
        **DATABASES["default"].get("OPTIONS", {}),
        "application_name": "m2m-backend",
        "pool": {
            "min_size": int(env("DB_POOL_MIN_SIZE", "5")),
            "max_size": int(env("DB_POOL_MAX_SIZE", "20")),
        },
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Production realtime and background jobs require Redis. In local development,
# set DEBUG=True to allow in-memory Channels/cache and memory Celery defaults.
REDIS_URL = env("REDIS_URL", "")
# Optional overrides; in production these must remain Redis URLs.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL or "memory://")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", REDIS_URL or "cache+memory://")

if not DEBUG and not REDIS_URL:
    raise ImproperlyConfigured(
        "REDIS_URL is required when DEBUG=False. "
        "Production cannot use in-memory Channels/cache or memory Celery."
    )

if not DEBUG and not (
    CELERY_BROKER_URL.startswith(("redis://", "rediss://"))
    and CELERY_RESULT_BACKEND.startswith(("redis://", "rediss://"))
):
    raise ImproperlyConfigured(
        "Production Celery requires Redis-backed CELERY_BROKER_URL and "
        "CELERY_RESULT_BACKEND. Set REDIS_URL, or explicitly set both Celery "
        "URLs to Redis endpoints."
    )

if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_URL],
                "capacity": 1000,
                "expiry": 60,
            },
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": 300,
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "m2m-local-cache",
            "TIMEOUT": 300,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = env("MEDIA_URL", "/media/")
MEDIA_ROOT = BASE_DIR / "media"

AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", "")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", "")
AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", "")
AWS_QUERYSTRING_AUTH = False
AWS_DEFAULT_ACL = None

USE_S3_STORAGE = env_bool("USE_S3_STORAGE", False)
if USE_S3_STORAGE and AWS_STORAGE_BUCKET_NAME:
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    MEDIA_URL = env("MEDIA_URL", f"https://{AWS_S3_CUSTOM_DOMAIN or AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com/")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 20,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

ENABLE_DEV_OTP = env_bool("ENABLE_DEV_OTP", False)
DEV_OTP_CODE = env("DEV_OTP_CODE", "000000")
OTP_EXPIRY_SECONDS = int(env("OTP_EXPIRY_SECONDS", "600"))
OTP_RATE_LIMIT_MAX = int(env("OTP_RATE_LIMIT_MAX", "5"))
OTP_RATE_LIMIT_WINDOW_SECONDS = int(env("OTP_RATE_LIMIT_WINDOW_SECONDS", "600"))
LINK_TOKEN_RATE_LIMIT_MAX = int(env("LINK_TOKEN_RATE_LIMIT_MAX", "3"))
LINK_TOKEN_RATE_LIMIT_WINDOW_SECONDS = int(env("LINK_TOKEN_RATE_LIMIT_WINDOW_SECONDS", "60"))
LINK_STATUS_POLL_SECONDS = int(env("LINK_STATUS_POLL_SECONDS", "1"))
WEBSOCKET_TICKET_TTL_SECONDS = int(env("WEBSOCKET_TICKET_TTL_SECONDS", "30"))
PRESENCE_TTL_SECONDS = int(env("PRESENCE_TTL_SECONDS", "45"))
CONTACT_DEFAULT_COUNTRY_CODE = env("CONTACT_DEFAULT_COUNTRY_CODE", "+92")

SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", True)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", not DEBUG)

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

CELERY_BEAT_SCHEDULE = {
    "cleanup-expired-statuses-hourly": {
        "task": "status.tasks.cleanup_expired_statuses",
        "schedule": 3600.0,
    },
}

FIREBASE_PROJECT_ID = env("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = env("FIREBASE_CLIENT_EMAIL", "")
FIREBASE_PRIVATE_KEY = env("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
FIREBASE_CREDENTIALS_PATH = env("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")

# LiveKit token generation. Production should set these from the self-hosted
# LiveKit server config; never expose LIVEKIT_API_SECRET to clients.
LIVEKIT_URL="wss://livekit.qubrixe.com"
LIVEKIT_API_KEY="d8fd03b13da97650db3d0640"
LIVEKIT_API_SECRET="1a7c83220bd57f8bb8d43d28ea486ff9f99a8df267be3589249234de2ba9a780"
LIVEKIT_TOKEN_TTL_MINUTES = "15"

if not firebase_admin._apps and os.path.exists(FIREBASE_CREDENTIALS_PATH):
    cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred)

TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = env("TWILIO_PHONE_NUMBER", "")
TWILIO_MESSAGING_SERVICE_SID = env("TWILIO_MESSAGING_SERVICE_SID", "")
