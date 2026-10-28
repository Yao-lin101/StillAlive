"""隔离测试配置：无需本地 PostgreSQL、Redis 或外部 AI 服务。"""

from .base import *
from tempfile import TemporaryDirectory

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
_test_media = TemporaryDirectory(prefix='stillalive-tests-')
MEDIA_ROOT = _test_media.name
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
ALLOWED_HOSTS = ['testserver', 'localhost']
IMPORTANT_EVENT_INFRA_CHECK_ON_STARTUP = False
VECTOR_BACKEND = 'none'
ANTHROPIC_API_KEY = ''
ANTHROPIC_MODEL = 'test-model'
ANTHROPIC_BASE_URL = ''
