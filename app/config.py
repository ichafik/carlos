"""Environment-driven settings for the Carlos GitHub App server.

Fails fast (WB-REL-05): every required setting is read once at import time,
so a missing GITHUB_APP_ID or bad private key surfaces immediately on
startup instead of on the first webhook the server happens to receive.
"""

import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. See app/README.md for the full list of "
            f"required environment variables."
        )
    return value


class Settings:
    def __init__(self):
        # GitHub App identity, from the app's settings page on github.com.
        self.app_id: str = _required("GITHUB_APP_ID")
        # PEM-encoded private key downloaded once when the app was created.
        # Stored as a single env var with literal "\n" for newlines (typical
        # for platforms that don't support multiline secrets), normalized here.
        self.private_key: str = _required("GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n")
        # Verifies that inbound webhook payloads actually came from GitHub.
        self.webhook_secret: str = _required("GITHUB_WEBHOOK_SECRET")
        # Fernet key (32 url-safe base64 bytes) encrypting BYO keys at rest
        # in key_store.py. Generate with:
        #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        self.key_encryption_key: str = _required("KEY_ENCRYPTION_SECRET")
        # Where the (installation_id -> provider, encrypted key) table lives.
        self.database_path: str = os.environ.get("CARLOS_DB_PATH", "carlos.db")
        self.bot_name: str = os.environ.get("BOT_NAME", "carlos").lower()
        self.status_context: str = os.environ.get("STATUS_CONTEXT", "Carlos Review Gate")
        self.max_diff_chars: int = int(os.environ.get("MAX_DIFF_CHARS", "500000"))
        self.protected_paths: list[str] = [
            p.strip() for p in os.environ.get(
                "PROTECTED_PATHS", "auth/,payments/,migrations/,infra/,.github/workflows/"
            ).split(",") if p.strip()
        ]


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton so importing this module doesn't require env vars to
    already be set (useful for tests that only need a few of them)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
