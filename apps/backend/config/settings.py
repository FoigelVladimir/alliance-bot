import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("APP_ENV") == "development"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY before starting Django.")

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL.startswith(("postgresql://", "postgres://")):
    raise ImproperlyConfigured("DATABASE_URL must be a PostgreSQL URL.")
try:
    database_options = conninfo_to_dict(DATABASE_URL)
except ProgrammingError as exc:
    raise ImproperlyConfigured("Invalid DATABASE_URL.") from exc

database_fields = {"dbname": "NAME", "user": "USER", "password": "PASSWORD", "host": "HOST", "port": "PORT"}
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        **{field: database_options.pop(key, "") for key, field in database_fields.items()},
        "OPTIONS": database_options,
    },
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig",
    "vk_channel.apps.VKChannelConfig",
    "support.apps.SupportConfig",
    "jobs.apps.JobsConfig",
    "storage.apps.StorageConfig",
    "workspace.apps.WorkspaceConfig",
    "knowledge.apps.KnowledgeConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["config.static_assets.assets", "workspace.queries.navigation"]},
}]
LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Moscow"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
if os.environ.get("DJANGO_TRUST_HTTPS_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR.parent.parent / "data" / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}
CAS_ROOT = Path(os.environ.get("CAS_ROOT", str(BASE_DIR.parent.parent / "data" / "cas")))
CAS_ROOT = CAS_ROOT.resolve()
if any(CAS_ROOT.is_relative_to(path.resolve()) or path.resolve().is_relative_to(CAS_ROOT)
       for path in [STATIC_ROOT, *STATICFILES_DIRS]):
    raise ImproperlyConfigured("CAS_ROOT must be separate from public static files.")
BACKGROUND_TASK_HANDLERS = {
    "support.route_inbound": "vk_channel.services.route_inbound_task",
    "vk.handle_student_click": "vk_channel.student_actions.handle_click_task",
}

VK_ID_CLIENT_ID = os.environ.get("VK_ID_CLIENT_ID", "")
VK_ID_REDIRECT_URI = os.environ.get("VK_ID_REDIRECT_URI", "")


def redact_login_query(record):
    message = record.getMessage()
    for route in ("/auth/vk/callback/", "/auth/vk/recovery/"):
        prefix, sep, suffix = message.partition(f"{route}?")
        if sep:
            record.msg = f"{prefix}{route}" + (" " + suffix.partition(" ")[2] if " " in suffix else "")
            record.args = ()
            break
    return True


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_login_query": {"()": "django.utils.log.CallbackFilter", "callback": redact_login_query}},
    "handlers": {"local_server": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django.server": {"handlers": ["local_server"], "filters": ["redact_login_query"], "propagate": False},
        "django.request": {"filters": ["redact_login_query"]},
    },
}

# Set before starting application processes against a restored database.
STAFF_QUARANTINE_INCIDENT = os.environ.get("STAFF_QUARANTINE_INCIDENT", "").strip()
if len(STAFF_QUARANTINE_INCIDENT) > 128:
    raise ImproperlyConfigured("STAFF_QUARANTINE_INCIDENT must be at most 128 characters.")

CSRF_FAILURE_VIEW = "support.views.csrf_failure"

VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "")
VK_ACCESS_TOKEN = os.environ.get("VK_ACCESS_TOKEN", "")
VK_SEND_ENABLED = os.environ.get("VK_SEND_ENABLED") == "1"
VK_HISTORY_CORRELATION_VERIFIED = os.environ.get("VK_HISTORY_CORRELATION_VERIFIED") == "1"
VK_LONG_POLL_ENABLED = os.environ.get("VK_LONG_POLL_ENABLED") == "1"

VK_ATTACHMENT_FETCH_ENABLED = os.environ.get("VK_ATTACHMENT_FETCH_ENABLED") == "1"
VK_ATTACHMENT_HOSTS = frozenset(
    host.strip().lower() for host in os.environ.get("VK_ATTACHMENT_HOSTS", "").split(",") if host.strip()
)

VK_GAP_HISTORY_VERIFIED = os.environ.get("VK_GAP_HISTORY_VERIFIED") == "1"
VK_CLICK_ANSWERS_VERIFIED = os.environ.get("VK_CLICK_ANSWERS_VERIFIED") == "1"
