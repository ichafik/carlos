"""GitHub App authentication: sign a JWT with the app's private key, trade it
for a short-lived installation access token, and cache that token until it's
close to expiring.

Two-step flow (see https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app):
  1. JWT signed with the app's RSA private key, claiming to be this app
     (`iss`), valid for a few minutes.
  2. POST that JWT to /app/installations/{id}/access_tokens to get a
     repo-scoped token valid for ~1 hour — this is what actually calls the
     GitHub REST API on the installation's behalf.
"""

import calendar
import time

import jwt  # PyJWT
import requests

from .config import get_settings

GH = "https://api.github.com"
# Installation tokens are valid for 60 minutes; refresh a bit early so a
# long-running request never gets a token that expires mid-call.
_TOKEN_REFRESH_MARGIN_SECONDS = 120

# installation_id -> (token, expires_at_epoch_seconds)
_token_cache: dict[int, tuple[str, float]] = {}


def _build_app_jwt() -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now - 60,   # backdate 60s to tolerate clock drift with GitHub's servers
        "exp": now + 600,  # GitHub caps this at 10 minutes
        "iss": settings.app_id,
    }
    return jwt.encode(payload, settings.private_key, algorithm="RS256")


def get_installation_token(installation_id: int, force_refresh: bool = False) -> str:
    """Return a valid installation access token, using the cache when possible.

    Raises requests.HTTPError if GitHub rejects the request (e.g. the app's
    private key doesn't match its registered app_id, or the installation
    was removed).
    """
    cached = _token_cache.get(installation_id)
    if not force_refresh and cached:
        token, expires_at = cached
        if time.time() < expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return token

    app_jwt = _build_app_jwt()
    resp = requests.post(
        f"{GH}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["token"]
    # expires_at looks like "2024-01-01T12:00:00Z" (UTC). time.mktime() would
    # wrongly interpret the parsed struct as local time; calendar.timegm()
    # treats it as UTC, matching time.time()'s epoch.
    expires_at = calendar.timegm(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _token_cache[installation_id] = (token, expires_at)
    return token
