"""GitHub-App-side driver for Carlos's review/gate/command logic.

Mirrors carlos_review.py's run_review/run_gate/run_merge/run_command, but:
  - state is a GitHubClient instance per request, not module globals (one
    process here serves every installation, not one PR per process)
  - the installation access token comes from github_auth.py, not a
    GITHUB_TOKEN env var
  - the provider + API key come from key_store.py (per installation),
    not repo secrets
The actual scoring/parsing/rendering logic is imported from review_core.py
so it can't drift from what the Actions script does.
"""

import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 ".github", "scripts"))
from llm_providers import DEFAULT_MODELS, canonical_provider_name, get_provider  # noqa: E402
from review_core import MARKER, apply_policy, build_system_prompt, normalize_review, parse_review, render  # noqa: E402

from . import key_store
from .config import get_settings
from .github_auth import get_installation_token
from .github_client import GitHubClient

logger = logging.getLogger("carlos.app")

MAX_WHITEBOOK_CHARS = 40_000
SEED = 42
TEMPERATURE = 0.0


class NotConfigured(Exception):
    """Raised when an installation hasn't set a provider/key yet."""


def _client(installation_id: int, repo_full_name: str, pr_number: int, sha: str = "") -> GitHubClient:
    token = get_installation_token(installation_id)
    return GitHubClient(token, repo_full_name, pr_number, sha)


def _resolve_provider(installation_id: int):
    """Returns (provider_name, model, provider_instance). Raises NotConfigured
    if the installation hasn't registered a key yet."""
    stored = key_store.get_key(installation_id)
    if stored is None:
        raise NotConfigured(
            f"Installation {installation_id} has no provider/key configured yet."
        )
    canonical = canonical_provider_name(stored.provider)
    model = os.environ.get("MODEL") or DEFAULT_MODELS.get(canonical, "")
    provider = get_provider(stored.provider, api_key=stored.api_key)
    return stored.provider, model, provider


def _load_whitebook(client: GitHubClient) -> tuple[str, str]:
    settings = get_settings()
    whitebook_repo = os.environ.get("WHITEBOOK_REPO", "")
    whitebook_ref = os.environ.get("WHITEBOOK_REF", "main")
    whitebook_path = os.environ.get("WHITEBOOK_PATH", ".github/WHITEBOOK.md")
    text, source = "", ""
    if whitebook_repo:
        text = client.get_file_contents(whitebook_path, ref=whitebook_ref, repo=whitebook_repo) or ""
        if text:
            source = f"{whitebook_repo}@{whitebook_ref}:{whitebook_path}"
    if not text:
        text = client.get_file_contents(whitebook_path, ref="HEAD") or ""
        if text:
            source = whitebook_path
    if len(text) > MAX_WHITEBOOK_CHARS:
        text = text[:MAX_WHITEBOOK_CHARS] + "\n[whitebook truncated]"
    return text, source


def _ask_model(provider, provider_name, model, prompt, system_prompt, attempts=2):
    last_err = None
    for attempt in range(1, attempts + 1):
        text, truncated = provider.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            seed=SEED + attempt - 1,
            temperature=TEMPERATURE,
            max_output_tokens=32768,
        )
        logger.info("attempt %d: provider=%s model=%s truncated=%s chars=%d",
                    attempt, provider_name, model, truncated, len(text))
        if truncated:
            last_err = f"response truncated after {len(text)} chars"
            prompt += "\n\nYour previous answer was cut off. Be more concise: shorter strings, at most 6 items per list."
            continue
        try:
            return normalize_review(parse_review(text))
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            prompt += "\n\nYour previous answer was not valid JSON. Return ONLY one valid JSON object."
    raise RuntimeError(f"Carlos could not get a valid review from {provider_name}/{model}: {last_err}")


def run_review(installation_id: int, repo_full_name: str, pr_number: int) -> dict:
    """Full review pass: fetch diff, ask the model, score, post the comment,
    publish the gate status. Returns the policy dict for the caller to log."""
    settings = get_settings()
    client = _client(installation_id, repo_full_name, pr_number)
    # get_pr() must run before any set_status() call: GitHubClient starts
    # with sha="" until get_pr() resolves it, and POSTing a commit status to
    # /repos/{repo}/statuses/ with an empty SHA is rejected by GitHub's API
    # (this was shipped broken once already — see the regression test
    # test_run_review_sets_status_only_after_resolving_sha).
    pr = client.get_pr()
    client.set_status(settings.status_context, "pending", "Carlos is reviewing…")

    try:
        provider_name, model, provider = _resolve_provider(installation_id)
    except NotConfigured as e:
        client.set_status(settings.status_context, "error", "Carlos has no API key configured")
        client.reply(
            f"⚠️ Carlos isn't configured for this installation yet: {e}\n\n"
            f"An org admin needs to register a provider + API key (see app/README.md)."
        )
        raise

    diff = client.get_diff()
    truncated = len(diff) > settings.max_diff_chars
    if truncated:
        diff = diff[:settings.max_diff_chars] + "\n\n[diff truncated]"
    changed = client.get_changed_files()

    prompt = (
        f"PR title: {pr['title']}\n\nPR description:\n{pr.get('body') or '(none)'}\n\n"
        f"Changed files:\n" + "\n".join(sorted(changed)) + f"\n\nDiff:\n{diff}"
    )
    whitebook, wb_source = _load_whitebook(client)
    review = _ask_model(provider, provider_name, model, prompt, build_system_prompt(whitebook))
    review["_whitebook_source"] = wb_source
    if truncated:
        review["confidence"] = "low"
        review["score_justification"] = review.get("score_justification", "") + " (Diff was truncated; confidence lowered.)"

    policy = apply_policy(review, changed, settings.protected_paths)
    approvals = client.count_approvals()
    client.upsert_comment(MARKER, render(review, policy, approvals, settings.bot_name, provider_name))
    client.set_labels("carlos:", f"carlos:{policy['score']}")
    _publish_gate(client, settings, policy["score"], policy["required"], approvals, policy["auto_merge"])
    return policy


def _publish_gate(client: GitHubClient, settings, score, required, approvals, auto_merge):
    if approvals >= required:
        client.set_status(settings.status_context, "success", f"Carlos: {score}% · {approvals}/{required} approvals ✓")
    else:
        client.set_status(settings.status_context, "failure",
                           f"Carlos: {score}% · needs {required} approval(s), has {approvals}")


def _read_stored(client: GitHubClient) -> dict | None:
    c = client.find_comment(MARKER)
    if not c:
        return None
    m = re.search(r"<!-- score:(\d+) required:(\d+) auto:(True|False) -->", c["body"])
    if not m:
        return None
    return {"score": int(m.group(1)), "required": int(m.group(2)), "auto": m.group(3) == "True"}


def run_gate(installation_id: int, repo_full_name: str, pr_number: int) -> None:
    """Recompute gate status from the stored score + current approvals
    (triggered by pull_request_review events; no model call)."""
    settings = get_settings()
    client = _client(installation_id, repo_full_name, pr_number)
    client.get_pr()  # resolves client.sha
    stored = _read_stored(client)
    if not stored:
        client.set_status(settings.status_context, "pending", "Waiting for Carlos to review")
        return
    approvals = client.count_approvals()
    _publish_gate(client, settings, stored["score"], stored["required"], approvals, stored["auto"])


def run_merge(installation_id: int, repo_full_name: str, pr_number: int, requested_by: str) -> None:
    settings = get_settings()
    client = _client(installation_id, repo_full_name, pr_number)
    pr = client.get_pr()
    stored = _read_stored(client)
    if not stored:
        client.reply(f"@{requested_by} Carlos has not reviewed this PR yet. Comment `{settings.bot_name} review` first.")
        return

    approvals = client.count_approvals()
    _publish_gate(client, settings, stored["score"], stored["required"], approvals, stored["auto"])

    if approvals < stored["required"]:
        client.reply(f"@{requested_by} not merging: score **{stored['score']}%** requires "
                     f"**{stored['required']}** approval(s), currently **{approvals}**.")
        return

    failing = client.failing_checks(settings.status_context)
    if failing:
        client.reply(f"@{requested_by} not merging: these checks are not green yet: "
                     + ", ".join(f"`{f}`" for f in failing))
        return

    client.merge(f"{pr['title']} (#{pr_number})")
    client.reply(f"✅ Merged by request of @{requested_by} — score {stored['score']}%, "
                 f"{approvals}/{stored['required']} approvals, all checks green.")


def run_command(installation_id: int, repo_full_name: str, pr_number: int,
                 comment_body: str, comment_author: str, comment_id) -> None:
    settings = get_settings()
    words = comment_body.strip().lower().split()
    if len(words) < 2 or words[0] != settings.bot_name:
        return

    client = _client(installation_id, repo_full_name, pr_number)
    if not client.user_can_write(comment_author):
        client.react(comment_id, "confused")
        client.reply(f"@{comment_author} only collaborators with write access can use `{settings.bot_name}` commands.")
        return

    cmd = words[1]
    if cmd == "review":
        client.react(comment_id, "eyes")
        run_review(installation_id, repo_full_name, pr_number)
    elif cmd == "merge":
        client.react(comment_id, "eyes")
        run_merge(installation_id, repo_full_name, pr_number, comment_author)
    else:
        client.react(comment_id, "confused")
        client.reply(f"@{comment_author} unknown command `{cmd}`. "
                     f"Available: `{settings.bot_name} review`, `{settings.bot_name} merge`.")
