"""Carlos GitHub App — FastAPI webhook server.

Receives pull_request / pull_request_review / issue_comment webhooks,
verifies they actually came from GitHub, and hands them to review_engine.py
in a background task so the webhook request itself returns fast (GitHub
retries deliveries that take too long to acknowledge; the actual review can
take tens of seconds waiting on an LLM call, so it must not block the
response).
"""

import hmac
import logging
import os
import sys

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import key_store, review_engine
from .config import get_settings
from .security import verify_signature

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 ".github", "scripts"))
from llm_providers import canonical_provider_name  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("carlos.app")

app = FastAPI(title="Carlos GitHub App")

# Simple bearer-token guard for the admin key-registration endpoint (MVP —
# see app/README.md for why this isn't a full OAuth settings UI yet).
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")

# Providers key_store will accept — kept in sync with llm_providers.py's
# dispatch table (canonical names + aliases) so a typo'd provider name fails
# at registration time, not three weeks later on the first PR webhook.
_KNOWN_PROVIDERS = {"gemini", "openai", "chatgpt", "claude", "anthropic"}
_MAX_API_KEY_LENGTH = 512


def _require_admin(authorization: str | None) -> None:
    if not ADMIN_API_TOKEN or not authorization or not hmac.compare_digest(
        authorization.removeprefix("Bearer ").strip(), ADMIN_API_TOKEN
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    settings = get_settings()
    body = await request.body()

    if not verify_signature(body, x_hub_signature_256, settings.webhook_secret):
        # WB-SEC-05: reject anything that isn't provably from GitHub before
        # touching the payload at all.
        logger.warning("rejected webhook with invalid signature", extra={"delivery": x_github_delivery})
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    payload = await request.json()
    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    if installation_id is None:
        # Not an installation-scoped event (e.g. a marketplace/ping event
        # with no PR context) — nothing for the review engine to do.
        logger.info("ignored webhook with no installation_id",
                    extra={"delivery": x_github_delivery, "event": x_github_event})
        return JSONResponse({"status": "ignored", "reason": "no installation_id"})

    logger.info("received webhook", extra={"delivery": x_github_delivery, "event": x_github_event,
                                            "installation_id": installation_id})

    if x_github_event == "pull_request":
        _handle_pull_request(payload, installation_id, background_tasks, x_github_delivery)
    elif x_github_event == "pull_request_review":
        _handle_pull_request_review(payload, installation_id, background_tasks, x_github_delivery)
    elif x_github_event == "issue_comment":
        _handle_issue_comment(payload, installation_id, background_tasks, x_github_delivery)
    # Other event types (installation, ping, etc.) are accepted but ignored.

    return JSONResponse({"status": "accepted", "delivery": x_github_delivery})


def _handle_pull_request(payload, installation_id, background_tasks, delivery_id):
    action = payload.get("action")
    pr = payload.get("pull_request") or {}
    if action not in ("opened", "synchronize", "reopened", "ready_for_review") or pr.get("draft"):
        return
    repo_full_name = payload["repository"]["full_name"]
    pr_number = pr["number"]
    background_tasks.add_task(_run_and_log, delivery_id, review_engine.run_review,
                               installation_id, repo_full_name, pr_number)


def _handle_pull_request_review(payload, installation_id, background_tasks, delivery_id):
    if payload.get("action") not in ("submitted", "dismissed"):
        return
    repo_full_name = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    background_tasks.add_task(_run_and_log, delivery_id, review_engine.run_gate,
                               installation_id, repo_full_name, pr_number)


def _handle_issue_comment(payload, installation_id, background_tasks, delivery_id):
    if payload.get("action") != "created":
        return
    issue = payload.get("issue") or {}
    if "pull_request" not in issue:
        return  # a comment on a plain issue, not a PR
    comment = payload["comment"]
    repo_full_name = payload["repository"]["full_name"]
    pr_number = issue["number"]
    background_tasks.add_task(
        _run_and_log, delivery_id, review_engine.run_command,
        installation_id, repo_full_name, pr_number,
        comment["body"], comment["user"]["login"], comment["id"],
    )


def _run_and_log(delivery_id, fn, *args):
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        # Background tasks that raise are otherwise swallowed silently by
        # FastAPI/Starlette — always log so a failure is at least visible
        # in the server's logs (WB-REL-01), tagged with the delivery ID so
        # it can be correlated back to the webhook that triggered it.
        logger.exception("background task %s failed (delivery=%s)", fn.__name__, delivery_id)


# ---------- installation key registration (MVP admin endpoint) ----------
@app.post("/installations/{installation_id}/key")
async def set_installation_key(installation_id: int, request: Request,
                                authorization: str | None = Header(default=None)):
    """Registers (or replaces) the provider + API key for one installation.

    This is a stopgap: a real deployment should put this behind GitHub's own
    OAuth login (so only that installation's org admins can call it) rather
    than a single shared admin token. See app/README.md.
    """
    _require_admin(authorization)

    body = await request.json()
    provider = (body.get("provider") or "").strip().lower()
    api_key = (body.get("api_key") or "").strip()
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")
    if provider not in _KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider '{provider}'. Supported: {sorted(_KNOWN_PROVIDERS)}",
        )
    if len(api_key) > _MAX_API_KEY_LENGTH:
        raise HTTPException(status_code=400, detail=f"api_key exceeds {_MAX_API_KEY_LENGTH} characters")

    key_store.set_key(installation_id, canonical_provider_name(provider), api_key)
    logger.info("registered key for installation", extra={"installation_id": installation_id, "provider": provider})
    return {"status": "ok", "installation_id": installation_id, "provider": canonical_provider_name(provider)}


@app.delete("/installations/{installation_id}/key")
async def delete_installation_key(installation_id: int, authorization: str | None = Header(default=None)):
    _require_admin(authorization)
    key_store.delete_key(installation_id)
    logger.info("deleted key for installation", extra={"installation_id": installation_id})
    return {"status": "ok", "installation_id": installation_id}
