import sys
import os
from pathlib import Path


# Ensure the backend package root is importable in all pytest environments.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Provide required settings defaults so app.config can initialize in tests.
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("META_APP_ID", "test-meta-app-id")
os.environ.setdefault("META_APP_SECRET", "test-meta-app-secret")
os.environ.setdefault("META_WEBHOOK_VERIFY_TOKEN", "test-meta-verify-token")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
