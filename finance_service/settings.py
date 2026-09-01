"""
Django settings for finance_service project.

Story 1.1 scaffold: no models, no views yet (see Stories 1.2+). This settings
module intentionally mirrors units-backend's DATABASES shape (same env-driven
DB_* vars, same shared Postgres instance) per AD-2, and stays otherwise minimal
for an API-only service per AD-1/AD-12.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Scaffold-only secret; Finance never shares/derives units-backend's SECRET_KEY
# or JWT_SECRET_KEY (AD-17) — reporting-auth JWT handling arrives in Story 3.1.
SECRET_KEY = 'django-insecure-finance-service-scaffold-story-1-1'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'ledger',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'finance_service.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'finance_service.wsgi.application'

# Same shared Postgres instance/database as units-backend, same .env-driven
# connection pattern (AD-2, AD-3) — replicated verbatim from
# property_management/settings.py:80-89 per the spec's Code Map.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME'),
        'USER': os.environ.get('DB_USER'),
        'PASSWORD': os.environ.get('DB_PASSWORD'),
        'HOST': os.environ.get('DB_HOST'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'static')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Shared secret checked on every request under the /internal/ namespace
# (AD-7, Story 2.1b). Sourced from the root .env, already added by Story 2.1a
# for units-backend's side of the channel; Finance only reads it here.
FINANCE_INTERNAL_TOKEN = os.getenv("FINANCE_INTERNAL_TOKEN")
