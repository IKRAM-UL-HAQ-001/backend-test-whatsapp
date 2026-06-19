import os
from datetime import timedelta
from pathlib import Path

from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured
import firebase_admin
from firebase_admin import credentials
from kombu import Queue


BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())
AUTH_USER_MODEL = "users.User"

CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="https://localhost", cast=Csv())
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = [origin for origin in config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv()) if origin]

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

DB_ENGINE = config("DB_ENGINE", default="sqlite")

if DB_ENGINE == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": config("DB_NAME"),
            "USER": config("DB_USER"),
            "PASSWORD": config("DB_PASSWORD"),
            "HOST": config("DB_HOST"),
            "PORT": config("DB_PORT", default="3306"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Redis-backed realtime and background jobs. Local development defaults to a
# local Redis server so runserver/Celery do not need repeated shell exports.
# Production should still set these explicitly in the process environment.
REDIS_URL = config("REDIS_URL", default="redis://127.0.0.1:6379/0")
# Optional overrides; in production these must remain Redis URLs.
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_QUEUES = (
    Queue("default"),
    Queue("push_notifications"),
    Queue("call_notifications"),
)
CELERY_TASK_ROUTES = {
    "chat.tasks.send_message_notification": {
        "queue": "default",
        "priority": 5,
    },
    "chat.tasks.send_push_notification_task": {
        "queue": "default",
        "priority": 5,
    },
    "calls.tasks.send_incoming_call_notification": {
        "queue": "default",
        "priority": 9,
    },
    "calls.tasks.send_missed_call_notification": {
        "queue": "default",
        "priority": 5,
    },
}


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
TIME_ZONE = config("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Optional S3 support
USE_S3 = config("USE_S3", default=False, cast=bool)

if USE_S3:
    AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default="ap-south-1")
    AWS_S3_CUSTOM_DOMAIN = config("AWS_S3_CUSTOM_DOMAIN", default="")
    AWS_LOCATION = "media"

    # django-storages S3 configuration. No AWS keys here - uses EC2 IAM Role.
    aws_default_acl = config("AWS_DEFAULT_ACL", default=None)
    if aws_default_acl in ("None", "", None):
        aws_default_acl = None

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "location": AWS_LOCATION,
                # Generate presigned URLs against the bucket's regional
                # endpoint. The global S3 endpoint redirects this bucket to
                # ap-south-1, and that host change invalidates SigV4 URLs.
                "region_name": AWS_S3_REGION_NAME,
                "endpoint_url": f"https://s3.{AWS_S3_REGION_NAME}.amazonaws.com",
                "addressing_style": "virtual",
                "file_overwrite": config("AWS_S3_FILE_OVERWRITE", default=False, cast=bool),
                "default_acl": aws_default_acl,
                "querystring_auth": config("AWS_QUERYSTRING_AUTH", default=True, cast=bool),
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

    if AWS_S3_CUSTOM_DOMAIN:
        MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/{AWS_LOCATION}/"
    else:
        MEDIA_URL = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com/{AWS_LOCATION}/"
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    MEDIA_URL = "/media/"
    MEDIA_ROOT = BASE_DIR / "media"

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

ENABLE_DEV_OTP = config("ENABLE_DEV_OTP", default=False, cast=bool)
DEV_OTP_CODE = config("DEV_OTP_CODE", default="000000")
OTP_EXPIRY_SECONDS = config("OTP_EXPIRY_SECONDS", default=600, cast=int)
OTP_RATE_LIMIT_MAX = config("OTP_RATE_LIMIT_MAX", default=5, cast=int)
OTP_RATE_LIMIT_WINDOW_SECONDS = config("OTP_RATE_LIMIT_WINDOW_SECONDS", default=600, cast=int)
LINK_TOKEN_RATE_LIMIT_MAX = config("LINK_TOKEN_RATE_LIMIT_MAX", default=3, cast=int)
LINK_TOKEN_RATE_LIMIT_WINDOW_SECONDS = config("LINK_TOKEN_RATE_LIMIT_WINDOW_SECONDS", default=60, cast=int)
LINK_STATUS_POLL_SECONDS = config("LINK_STATUS_POLL_SECONDS", default=1, cast=int)
WEBSOCKET_TICKET_TTL_SECONDS = config("WEBSOCKET_TICKET_TTL_SECONDS", default=30, cast=int)
PRESENCE_TTL_SECONDS = config("PRESENCE_TTL_SECONDS", default=45, cast=int)
CALL_RING_TIMEOUT_SECONDS = config("CALL_RING_TIMEOUT_SECONDS", default=60, cast=int)
ACTIVE_CALL_STALE_TIMEOUT_SECONDS = config("ACTIVE_CALL_STALE_TIMEOUT_SECONDS", default=180, cast=int)
CONTACT_DEFAULT_COUNTRY_CODE = config("CONTACT_DEFAULT_COUNTRY_CODE", default="+92")

SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True, cast=bool)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=True, cast=bool)
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=False, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=False, cast=bool)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = config("USE_X_FORWARDED_HOST", default=not DEBUG, cast=bool)

CELERY_BEAT_SCHEDULE = {
    "cleanup-expired-statuses-hourly": {
        "task": "status.tasks.cleanup_expired_statuses",
        "schedule": 3600.0,
    },
    "cleanup-stale-active-calls-every-minute": {
        "task": "calls.tasks.cleanup_stale_active_calls_task",
        "schedule": 60.0,
    },
}

FIREBASE_PROJECT_ID = config("FIREBASE_PROJECT_ID", default="")
FIREBASE_CLIENT_EMAIL = config("FIREBASE_CLIENT_EMAIL", default="")
FIREBASE_PRIVATE_KEY = config("FIREBASE_PRIVATE_KEY", default="").replace("\\n", "\n")
FIREBASE_CREDENTIALS_PATH = config("FIREBASE_CREDENTIALS_PATH", default="service-account.json")

# AWS/Chime calling settings
CHIME_ENABLED = config("CHIME_ENABLED", default=True, cast=bool)
AWS_REGION = config("AWS_REGION", default="ap-south-1")
CHIME_MEDIA_REGION = config("CHIME_MEDIA_REGION", default=AWS_REGION)


if not firebase_admin._apps and os.path.exists(FIREBASE_CREDENTIALS_PATH):
    cred = credentials.Certificate(FIREBASE_CREDENTIALS_PATH)
    firebase_admin.initialize_app(cred)

TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID", default="")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN", default="")
TWILIO_PHONE_NUMBER = config("TWILIO_PHONE_NUMBER", default="")
TWILIO_MESSAGING_SERVICE_SID = config("TWILIO_MESSAGING_SERVICE_SID", default="")
