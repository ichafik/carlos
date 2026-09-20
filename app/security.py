"""Webhook signature verification.

GitHub signs every webhook payload with HMAC-SHA256 using the secret set on
the app (X-Hub-Signature-256 header: "sha256=<hex digest>"). Without this
check, anyone who finds the webhook URL could post fake pull_request events
and trigger reviews, merges, or comments as Carlos (WB-SEC-05: authorization
must be enforced server-side, at the boundary where input enters the system).
"""

import hashlib
import hmac


def verify_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Constant-time comparison of the computed vs. received signature.

    Returns False (never raises) for a missing/malformed header so callers
    can uniformly respond 401 without a try/except at every call site.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)
