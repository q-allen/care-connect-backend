from datetime import timedelta
from pathlib import Path
import dj_database_url
import os
import warnings

from dotenv import load_dotenv
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY")
DEBUG = os.environ.get("DEBUG", "True") == "True"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# ── Installed Apps ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "daphne",
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "channels",
    *([ "django_celery_beat"] if __import__("importlib.util", fromlist=["find_spec"]).find_spec("django_celery_beat") else []),
    # local
    "users",
    "doctors",
    "appointments",
    "records",
    "chat",
    "pharmacy",
    "notifications",
    "cloudinary_storage",
    "cloudinary",
]

FRONTEND_URL      = os.environ.get("FRONTEND_URL", "http://localhost:3000")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
CORS_ALLOW_ALL_ORIGINS = DEBUG  # True only in dev; env-controlled in prod
CORS_ALLOW_CREDENTIALS = True
CORS_URLS_REGEX = r"^/(api|media)/.*$"  # also cover /media/ for PDF downloads

# ── Middleware ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
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

# ── JWT ───────────────────────────────────────────────────────────────────────
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ── DRF ───────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "users.authentication.CookieJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/minute",
        "user": "300/minute",
        "otp": "5/minute",
    },
}

ROOT_URLCONF = "backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "backend.wsgi.application"
ASGI_APPLICATION  = "backend.asgi.application"

# ── Channel Layers ────────────────────────────────────────────────────────────
_REDIS_URL = os.environ.get("REDIS_URL")
_USE_REDIS  = os.environ.get("USE_REDIS", "True" if _REDIS_URL else "False") == "True"

if _USE_REDIS and _REDIS_URL:
    try:
        import redis as _redis_ch
        _redis_ch.StrictRedis.from_url(_REDIS_URL).ping()
        CHANNEL_LAYERS = {
            "default": {
                "BACKEND": "channels_redis.core.RedisChannelLayer",
                "CONFIG":  {"hosts": [_REDIS_URL]},
            }
        }
    except Exception:
        warnings.warn(
            "Redis unreachable — CHANNEL_LAYERS falling back to InMemoryChannelLayer. "
            "Real-time messages will NOT reach the other party across connections. "
            "Start Redis (redis-server) to fix this.",
            stacklevel=1,
        )
        CHANNEL_LAYERS = {
            "default": {
                "BACKEND": "channels.layers.InMemoryChannelLayer",
            }
        }
else:
    if not DEBUG:
        raise RuntimeError(
            "REDIS_URL must be set in production. "
            "InMemoryChannelLayer does not work across multiple processes."
        )
    warnings.warn(
        "CHANNEL_LAYERS is using InMemoryChannelLayer. "
        "WebSocket broadcasts will NOT work across multiple processes. "
        "Set REDIS_URL in your .env to use RedisChannelLayer.",
        stacklevel=1,
    )
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME"),
        "USER": os.environ.get("DB_USER"),
        "PASSWORD": os.environ.get("DB_PASSWORD"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}

DATABASES["default"] = dj_database_url.parse(os.environ.get("DATABASE_URL"), conn_max_age=600, ssl_require=not DEBUG) if os.environ.get("DATABASE_URL") else DATABASES["default"]

# ── Cache (Redis optional, with local-memory fallback) ────────────────────────
# Reuses the same _REDIS_URL / _USE_REDIS resolved above for Channel Layers.

if _USE_REDIS and _REDIS_URL:
    try:
        import redis as _redis_lib
        _redis_lib.StrictRedis.from_url(_REDIS_URL).ping()
        CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": _REDIS_URL,
            }
        }
    except Exception:
        # Redis unavailable — fall back to in-process memory cache (dev only)
        warnings.warn(
            "Redis unavailable, falling back to LocMemCache. OTP rate-limiting will not persist across processes.",
            stacklevel=1,
        )
        CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

APPEND_SLASH = False

# ── Password Validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Internationalisation ──────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True

# ── Static / Media ────────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── Cloudinary ────────────────────────────────────────────────────────────────
CLOUDINARY_STORAGE = {
    "CLOUD_NAME":    os.environ.get("CLOUDINARY_CLOUD_NAME"),
    "API_KEY":       os.environ.get("CLOUDINARY_API_KEY"),
    "API_SECRET":    os.environ.get("CLOUDINARY_API_SECRET"),
    "RESOURCE_TYPE": "auto",  # allows images, PDFs, and raw files to each go to their upload_to folder
}
STORAGES = {
    "default": {
        "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Email (Brevo SMTP) ────────────────────────────────────────────────────────
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp-relay.brevo.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or EMAIL_HOST_USER

if not DEBUG:
    if not EMAIL_HOST_USER:
        raise RuntimeError("EMAIL_HOST_USER must be set in production.")
    if not EMAIL_HOST_PASSWORD:
        raise RuntimeError("EMAIL_HOST_PASSWORD (Brevo SMTP key) must be set in production.")

AUTH_USER_MODEL = "users.User"

# ── Celery ───────────────────────────────────────────────────────────────────
_CELERY_REDIS = os.environ.get("REDIS_URL")
if not _CELERY_REDIS and not DEBUG:
    raise RuntimeError("REDIS_URL must be set in production for Celery broker/backend.")
CELERY_BROKER_URL     = os.environ.get("CELERY_BROKER_URL") or _CELERY_REDIS
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND") or _CELERY_REDIS
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "appointment-reminders-every-30min": {
        "task": "appointments.tasks.send_appointment_reminders",
        "schedule": 1800,  # every 30 minutes
    },
    "appointment-preconsult-reminders-every-5min": {
        "task": "notifications.tasks.process_preconsult_reminders",
        "schedule": 300,  # every 5 minutes
    },
    "appointment-no-show-auto-every-5min": {
        "task": "notifications.tasks.auto_mark_no_shows",
        "schedule": 300,  # every 5 minutes
    },
}

# ── PayMongo (LIVE MODE) ─────────────────────────────────────────────────────
# CRITICAL: Using live keys — real money will be charged!
# Live keys start with sk_live_ and pk_live_ (not sk_test_ / pk_test_)
PAYMONGO_SECRET_KEY    = os.environ.get("PAYMONGO_SECRET_KEY")
PAYMONGO_PUBLIC_KEY    = os.environ.get("PAYMONGO_PUBLIC_KEY")
PAYMONGO_WEBHOOK_SECRET = os.environ.get("PAYMONGO_WEBHOOK_SECRET")

# Validate that live keys are configured in production
# Temporarily commented out to allow migrations
# if not DEBUG:
#     if not PAYMONGO_SECRET_KEY or not PAYMONGO_PUBLIC_KEY:
#         raise RuntimeError(
#             "PAYMONGO_SECRET_KEY and PAYMONGO_PUBLIC_KEY must be set in production. "
#             "Use live keys (sk_live_... and pk_live_...) from your PayMongo dashboard."
#         )
#     if PAYMONGO_SECRET_KEY.startswith("sk_test_") or PAYMONGO_PUBLIC_KEY.startswith("pk_test_"):
#         raise RuntimeError(
#             "Production environment detected but test PayMongo keys are configured. "
#             "Replace with live keys (sk_live_... and pk_live_...) to process real payments."
#         )

# ── Jitsi ───────────────────────────────────────────────────────────────────────────
JITSI_HOST   = os.environ.get("JITSI_HOST")
JITSI_DOMAIN = os.environ.get("JITSI_DOMAIN")  # used by serializer for video_room_url

# ── File uploads ──────────────────────────────────────────────────────────────
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024   # 10 MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ── Jazzmin Admin ─────────────────────────────────────────────────────────────
JAZZMIN_SETTINGS = {
    # ── Branding ──────────────────────────────────────────────────────────────
    "site_title":    "CareConnect Admin",
    "site_header":   "CareConnect",
    "site_brand":    "CareConnect",
    "site_logo":     "icon.svg",
    "site_icon":     "icon.svg",
    "site_logo_classes": "img-circle elevation-3",
    "welcome_sign":  "Welcome back to CareConnect Admin",
    "copyright":     "CareConnect © 2026",

    # ── Search ────────────────────────────────────────────────────────────────
    "search_model": ["users.User", "appointments.Appointment", "doctors.DoctorProfile"],

    # ── Top menu ──────────────────────────────────────────────────────────────
    "topmenu_links": [
        {"name": "Dashboard",    "url": "admin:index",                              "permissions": ["auth.view_user"]},
        {"name": "Users",        "url": "admin:users_user_changelist",               "permissions": ["auth.view_user"]},
        {"name": "Doctors",      "url": "admin:doctors_doctorprofile_changelist",    "permissions": ["auth.view_user"]},
        {"name": "Appointments", "url": "admin:appointments_appointment_changelist","permissions": ["auth.view_user"]},
        # {"name": "Invite Doctor", "url": "/admin/doctors/doctorinvite/add/",      "permissions": ["auth.view_user"]},
    ],

    # ── User menu (top-right avatar dropdown) ─────────────────────────────────
    "usermenu_links": [
        {"name": "CareConnect App", "url": "http://localhost:3000", "new_window": True},
    ],

    # ── Sidebar ───────────────────────────────────────────────────────────────
    "show_sidebar":         True,
    "navigation_expanded":  True,
    "hide_apps":            ["auth", "token_blacklist"],
    "hide_models":          ["doctors.doctorinvite"],
    "order_with_respect_to": [
        "users",
        "doctors",
        "appointments",
        "records",
        "pharmacy",
        "chat",
        "notifications",
    ],

    # ── Custom sidebar links ──────────────────────────────────────────────────
    "custom_links": {
        "doctors": [{
            "name":        "Invite Doctor",
            "url":         "/admin/doctors/doctorinvite/add/",
            "icon":        "fas fa-paper-plane",
            "permissions": ["auth.view_user"],
        }],
    },

    # ── Icons ─────────────────────────────────────────────────────────────────
    "icons": {
        # Apps
        "users":                        "fas fa-users",
        "doctors":                      "fas fa-stethoscope",
        "appointments":                 "fas fa-calendar-alt",
        "records":                      "fas fa-file-medical",
        "pharmacy":                     "fas fa-pills",
        "chat":                         "fas fa-comments",
        "notifications":                "fas fa-bell",
        # Models
        "users.user":                   "fas fa-user-circle",
        "doctors.doctorprofile":        "fas fa-user-md",
        "doctors.patienthmo":           "fas fa-id-card",
        "doctors.doctorhospital":       "fas fa-hospital",
        "doctors.doctorservice":        "fas fa-hand-holding-medical",
        "doctors.doctorhmo":            "fas fa-shield-alt",
        "appointments.appointment":     "fas fa-calendar-check",
        "appointments.review":          "fas fa-star",
        "records.prescription":         "fas fa-prescription",
        "records.labresult":            "fas fa-flask",
        "records.medicalcertificate":   "fas fa-certificate",
        "records.certificaterequest":   "fas fa-file-signature",
        "pharmacy.medicine":            "fas fa-capsules",
        "pharmacy.order":               "fas fa-shopping-bag",
        "chat.conversation":            "fas fa-comments",
        "chat.message":                 "fas fa-comment-dots",
        "notifications.notification":   "fas fa-bell",
    },
    "default_icon_parents":  "fas fa-folder",
    "default_icon_children": "fas fa-circle",

    # ── UI options ────────────────────────────────────────────────────────────
    "related_modal_active":  True,
    "custom_css":            "admin/css/custom.css",
    "custom_js":             "admin/js/admin_fixes.js",
    "use_google_fonts_cdn":  True,
    "show_ui_builder":       False,
    "changeform_format":     "horizontal_tabs",
    "changeform_format_overrides": {
        "auth.user":    "collapsible",
        "users.user":   "collapsible",
    },
    "language_chooser": False,
}

JAZZMIN_UI_TWEAKS = {
    "navbar_small_text":       False,
    "footer_small_text":       False,
    "body_small_text":         False,
    "brand_small_text":        False,
    "brand_colour":            "navbar-dark",
    "accent":                  "accent-teal",
    "navbar":                  "navbar-dark",
    "no_navbar_border":        True,
    "navbar_fixed":            True,
    "layout_boxed":            False,
    "footer_fixed":            False,
    "sidebar_fixed":           True,
    "sidebar":                 "sidebar-dark-teal",
    "sidebar_nav_small_text":  False,
    "sidebar_disable_expand":  False,
    "sidebar_nav_child_indent": True,
    "sidebar_nav_compact_style": False,
    "sidebar_nav_legacy_style": False,
    "sidebar_nav_flat_style":  False,
    "theme":                   "default",
    "dark_mode_theme":         None,
    "button_classes": {
        "primary":   "btn-primary",
        "secondary": "btn-secondary",
        "info":      "btn-info",
        "warning":   "btn-warning",
        "danger":    "btn-danger",
        "success":   "btn-success",
    },
}
