# Carlos — GitHub App

The hosted, installable version of Carlos: one FastAPI server, installed by
any org via the GitHub Apps Marketplace, reviewing PRs with whichever LLM
provider that installation registers a key for.

This is a different deployment from `.github/scripts/carlos_review.py` (the
GitHub Actions version, copy-pasted into each consumer repo's own
workflow). Both share the same scoring/rendering logic
(`.github/scripts/review_core.py`) and the same provider abstraction
(`.github/scripts/llm_providers.py`) — see the module docstrings in those
two files for why.

## Architecture

```
app/
├── main.py            FastAPI app: /webhook, /healthz, key registration endpoints
├── config.py           Env-driven settings, fail-fast on missing required vars
├── github_auth.py       JWT signing + installation access token exchange/cache
├── github_client.py     Thin GitHub REST wrapper scoped to one repo + token
├── key_store.py         Per-installation (provider, encrypted API key) storage
├── security.py          Webhook HMAC signature verification
├── review_engine.py      Ties it together: webhook payload -> review_core -> GitHub
└── requirements.txt
```

## 1. Register the GitHub App

On github.com: Settings → Developer settings → GitHub Apps → New GitHub App.

- **Homepage URL**: wherever you're hosting this (see step 3).
- **Webhook URL**: `https://<your-host>/webhook`
- **Webhook secret**: generate one (`openssl rand -hex 32`), save it — this becomes `GITHUB_WEBHOOK_SECRET`.
- **Permissions** (repository): Pull requests — Read & write, Contents — Read & write (needed to merge), Commit statuses — Read & write, Issues — Read & write (for comments/labels/reactions), Checks — Read-only.
- **Subscribe to events**: Pull request, Pull request review, Issue comment.
- **Where can this GitHub App be installed?**: Any account (required for Marketplace).

After creating it, note the **App ID**, and generate + download a **private key** (PEM file) — GitHub only shows it once.

## 2. Required environment variables

| Variable | Purpose |
|---|---|
| `GITHUB_APP_ID` | From the app's settings page |
| `GITHUB_APP_PRIVATE_KEY` | Contents of the downloaded `.pem`. If your platform doesn't support multiline env vars, replace newlines with `\n`; `config.py` un-escapes them. |
| `GITHUB_WEBHOOK_SECRET` | The webhook secret you set in step 1 |
| `KEY_ENCRYPTION_SECRET` | Fernet key encrypting stored BYO keys. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ADMIN_API_TOKEN` | Bearer token protecting the `/installations/{id}/key` endpoints (see the caveat below) |
| `CARLOS_DB_PATH` | Optional, default `carlos.db` — SQLite file for the key store |
| `BOT_NAME`, `STATUS_CONTEXT`, `MAX_DIFF_CHARS`, `PROTECTED_PATHS` | Optional, same meaning as the Actions version — see `.github/README.md` |

## 3. Run it

```
pip install -r app/requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Deploy that anywhere with a stable HTTPS URL (Render, Fly.io, a small VM
behind a reverse proxy) — GitHub needs to reach `/webhook` over HTTPS.

## 4. Register a BYO key for an installation

There's no settings UI yet. Until there is, an admin registers a key by
calling the API directly once an org has installed the app:

```
curl -X POST https://<your-host>/installations/<installation_id>/key \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude", "api_key": "sk-..."}'
```

`installation_id` shows up in the webhook payload's `installation.id` field,
or in the URL when you view the installation in GitHub's UI. This is
explicitly a stopgap: a real deployment should let an org's own admins
register their key through a page that authenticates them via GitHub OAuth,
not a shared bearer token that can manage every installation. That settings
UI is the next piece of work, not part of this scaffold.

## 5. Test end-to-end before publishing

Install the app on one of your own repos, open a throwaway PR, and confirm
you see: a `pending` → `success`/`failure` status named "Carlos Review
Gate", a review comment with the same rubric/whitebook report as the Actions
version, and `carlos review` / `carlos merge` comments working.

## What's intentionally not here yet

- **Settings UI.** Key registration is the raw admin API above.
- **Multi-instance safety.** The key store is SQLite; fine for one process,
  not for horizontally scaled replicas (swap `key_store.py`'s internals for
  Postgres if you need that — the `set_key`/`get_key`/`delete_key` call
  sites don't change).
- **Marketplace billing.** Free-tier install works as-is; a paid plan needs
  GitHub Marketplace billing wired up separately (see the main conversation
  history / your own notes on the Marketplace submission steps).
