"""
Ensure env vars exist before `config` is imported (strict validation on import).
Uses SQLite for tests. PostgreSQL is used in production.
"""
import os
import atexit
import tempfile

_tf = tempfile.NamedTemporaryFile(prefix="billedup_pytest_", suffix=".db", delete=False)
_tf.close()
_db_url = "sqlite:///" + _tf.name.replace("\\", "/")


def _cleanup():
    try:
        os.unlink(_tf.name)
    except OSError:
        pass

atexit.register(_cleanup)

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-api03-test-placeholder-not-a-real-key")
os.environ.setdefault("PLATFORM_NAME", "BilledUp")
os.environ.setdefault("PLATFORM_SUPPORT_PHONE", "+91 99999 99999")
os.environ["DATABASE_URL"] = _db_url
