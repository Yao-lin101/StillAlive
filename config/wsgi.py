"""
WSGI config for smtx project.
"""

import os
from pathlib import Path
from django.core.wsgi import get_wsgi_application

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / '.env'
    load_dotenv(env_path)
except ImportError:
    pass

# Default to production settings if not specified
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')

application = get_wsgi_application()
