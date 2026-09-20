"""
Carlos Code Reviewer — PR reviewer and merge gatekeeper (GitHub Actions entry point)

Reviews every pull request with a BYO-key LLM (see llm_providers.py), scores
it, and enforces a score-based approval policy before anything reaches main.
The provider-agnostic scoring/rendering logic lives in review_core.py, shared
with the GitHub App (app/review_engine.py) — this file is just the env-var +
GitHub-REST-API glue specific to running inside a GitHub Actions workflow.

Modes (derived from the GitHub event)
  pull_request        -> review : ask the model, score, post report, publish gate status
  pull_request_review -> gate   : recompute gate status from stored score + current approvals
  issue_comment        -> command:
        "carlos review"  re-run Carlos's full review on the current head
        "carlos merge"   merge now if the gate conditions are fulfilled

Merge policy
  score >= 95  -> auto-merge, 0 approvals (unless blockers / protected paths / low confidence)
  50..94       -> 1 approval required
  < 50         -> 2 approvals required
"""

import os
import re

import requests
from llm_providers import DEFAULT_MODELS, canonical_provider_name, get_provider
from review_core import MARKER, apply_policy, build_system_prompt, normalize_review, parse_review, render
from review_core import SYSTEM_PROMPT

# ---------- config ----------
GH = "https://api.github.com"
REPO = os.environ["REPO"]
PR = os.environ["PR_NUMBER"]
EVENT = os.environ.get("EVENT_NAME", "pull_request")
COMMENT_BODY = os.environ.get("COMMENT_BODY", "").strip()
COMMENT_AUTHOR = os.environ.get("COMMENT_AUTHOR", "")
COMMENT_ID = os.environ.get("COMMENT_ID", "")
BOT_NAME = os.environ.get("BOT_NAME", "carlos").lower()
SHA = os.environ.get("HEAD_SHA") or ""   # resolved from the PR when empty (comment events)
# PROVIDER picks which BYO API key/SDK is used: gemini (default), openai, or claude.
# Each repo sets its own PROVIDER + matching secret; see llm_providers.py.
PROVIDER = os.environ.get("PROVIDER", "gemini").strip().lower()
# .get(..., "") rather than [...]: an invalid PROVIDER must surface as the
# clear RuntimeError from get_provider() when it's actually dispatched, not
# as a KeyError here at import time.
MODEL = os.environ.get("MODEL") or DEFAULT_MODELS.get(canonical_provider_name(PROVIDER), "")
PROTECTED = [p.strip() for p in os.environ.get("PROTECTED_PATHS", "").split(",") if p.strip()]
STATUS_CONTEXT = os.environ.get("STATUS_CONTEXT", "Carlos Review Gate")
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "500000"))
WHITEBOOK_PATH = os.environ.get("WHITEBOOK_PATH", ".github/WHITEBOOK.md")   # local override
WHITEBOOK_REPO = os.environ.get("WHITEBOOK_REPO", "")   # e.g. "my-org/.github" → central source of truth
WHITEBOOK_REF = os.environ.get("WHITEBOOK_REF", "main")
MAX_WHITEBOOK_CHARS = 40_000
SEED = int(os.environ.get("SEED", "42"))                # fixed seed → same diff, same review (best-effort)
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))  # 0 = most deterministic

HEADERS = {
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ---------- github helpers ----------
def gh(method, path, **kw):
    r = requests.request(method, f"{GH}{path}", headers=HEADERS, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def get_pr():
    global SHA
    pr = gh("GET", f"/repos/{REPO}/pulls/{PR}")
    if not SHA:
        SHA = pr["head"]["sha"]
    return pr


def user_can_write(login):
    try:
        perm = gh("GET", f"/repos/{REPO}/collaborators/{login}/permission")["permission"]
    except requests.RequestException as e:
        # Fail closed: an unknown permission level is treated as "no access".
        print(f"[carlos] permission lookup failed for {login}: {e}")
        return False
    return perm in ("write", "maintain", "admin")


def react(content):
    """Acknowledge a command comment with an emoji reaction (eyes / rocket / confused)."""
    if not COMMENT_ID:
        return
    try:
        gh("POST", f"/repos/{REPO}/issues/comments/{COMMENT_ID}/reactions", json={"content": content})
    except requests.RequestException as e:
        # Non-fatal: a missing emoji must not abort a review or merge, but we still want a trace.
        print(f"[carlos] failed to react '{content}' on comment {COMMENT_ID}: {e}")


def reply(text):
    gh("POST", f"/repos/{REPO}/issues/{PR}/comments", json={"body": text})


def get_diff():
    r = requests.get(
        f"{GH}/repos/{REPO}/pulls/{PR}",
        headers={**HEADERS, "Accept": "application/vnd.github.v3.diff"},
        timeout=60,
    )
    r.raise_for_status()
    return r.text


def get_changed_files():
    files, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO}/pulls/{PR}/files", params={"per_page": 100, "page": page})
        files += [f["filename"] for f in batch]
        if len(batch) < 100:
            return files
        page += 1


def count_approvals():
    """Latest review per user; count APPROVED, excluding the PR author and bots."""
    author = get_pr()["user"]["login"]
    reviews, page = [], 1
    while True:
        batch = gh("GET", f"/repos/{REPO}/pulls/{PR}/reviews", params={"per_page": 100, "page": page})
        reviews += batch
        if len(batch) < 100:
            break
        page += 1
    latest = {}
    for rv in reviews:
        user = rv["user"]["login"]
        if user == author or rv["user"].get("type") == "Bot":
            continue
        if rv["state"] in ("APPROVED", "CHANGES_REQUESTED"):
            latest[user] = rv["state"]
    return sum(1 for s in latest.values() if s == "APPROVED")


def find_report_comment():
    page = 1
    while True:
        batch = gh("GET", f"/repos/{REPO}/issues/{PR}/comments", params={"per_page": 100, "page": page})
        for c in batch:
            if MARKER in c["body"]:
                return c
        if len(batch) < 100:
            return None
        page += 1


def upsert_comment(body):
    existing = find_report_comment()
    if existing:
        gh("PATCH", f"/repos/{REPO}/issues/comments/{existing['id']}", json={"body": body})
    else:
        gh("POST", f"/repos/{REPO}/issues/{PR}/comments", json={"body": body})


def set_labels(score_label):
    labels = [l["name"] for l in gh("GET", f"/repos/{REPO}/issues/{PR}/labels")]
    keep = [l for l in labels if not l.startswith("carlos:")]
    gh("PUT", f"/repos/{REPO}/issues/{PR}/labels", json={"labels": keep + [score_label]})


def set_status(state, description):
    gh("POST", f"/repos/{REPO}/statuses/{SHA}",
       json={"state": state, "context": STATUS_CONTEXT, "description": description[:140]})


def set_output(key, value):
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
        f.write(f"{key}={value}\n")


# ---------- whitebook ----------
def load_whitebook():
    """Central org whitebook (WHITEBOOK_REPO) first, then a local file, else none."""
    text, source = "", ""
    if WHITEBOOK_REPO:
        try:
            r = requests.get(f"{GH}/repos/{WHITEBOOK_REPO}/contents/{WHITEBOOK_PATH}",
                             headers={**HEADERS, "Accept": "application/vnd.github.raw+json"},
                             params={"ref": WHITEBOOK_REF}, timeout=30)
            if r.ok:
                text, source = r.text, f"{WHITEBOOK_REPO}@{WHITEBOOK_REF}:{WHITEBOOK_PATH}"
            else:
                print(f"[carlos] central whitebook not found ({r.status_code}), trying local")
        except requests.RequestException as e:
            print(f"[carlos] central whitebook fetch failed: {e}")
    if not text and os.path.exists(WHITEBOOK_PATH):
        with open(WHITEBOOK_PATH, encoding="utf-8") as f:
            text, source = f.read(), WHITEBOOK_PATH
    if len(text) > MAX_WHITEBOOK_CHARS:
        text = text[:MAX_WHITEBOOK_CHARS] + "\n[whitebook truncated]"
    print(f"[carlos] whitebook: {source or 'none'} ({len(text)} chars)")
    return text, source


# ---------- model ----------
def ask_model(prompt, system_prompt=SYSTEM_PROMPT, attempts=2):
    provider = get_provider(PROVIDER)
    last_err = None
    for attempt in range(1, attempts + 1):
        text, truncated = provider.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            seed=SEED + attempt - 1,   # retry with a different seed so a bad sample isn't repeated
            temperature=TEMPERATURE,
            max_output_tokens=32768,
        )
        print(f"[carlos] attempt {attempt}: provider={PROVIDER} model={MODEL} "
              f"truncated={truncated}, chars={len(text)}")
        if truncated:
            last_err = f"response truncated after {len(text)} chars"
            prompt += "\n\nYour previous answer was cut off. Be more concise: shorter strings, at most 6 items per list."
            continue
        try:
            return normalize_review(parse_review(text))
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            print(f"[carlos] JSON parse failed: {last_err}\n--- head ---\n{text[:400]}\n--- tail ---\n{text[-400:]}")
            prompt += "\n\nYour previous answer was not valid JSON. Return ONLY one valid JSON object."
    raise RuntimeError(f"Carlos could not get a valid review from {PROVIDER}/{MODEL}: {last_err}")


# ---------- gate ----------
def publish_gate(score, required, approvals, auto_merge):
    if approvals >= required:
        set_status("success", f"Carlos: {score}% · {approvals}/{required} approvals ✓")
    else:
        set_status("failure", f"Carlos: {score}% · needs {required} approval(s), has {approvals}")
    set_output("score", score)
    set_output("required_approvals", required)
    set_output("auto_merge", "true" if auto_merge else "false")


# ---------- main ----------
def run_review():
    set_status("pending", "Carlos is reviewing…")
    pr = get_pr()
    diff = get_diff()
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncated]"
    changed = get_changed_files()

    # Keep the prompt byte-identical for an identical diff so the seed can do its job:
    # sorted file list, no timestamps / run ids / PR numbers.
    prompt = (
        f"PR title: {pr['title']}\n\nPR description:\n{pr.get('body') or '(none)'}\n\n"
        f"Changed files:\n" + "\n".join(sorted(changed)) + f"\n\nDiff:\n{diff}"
    )
    whitebook, wb_source = load_whitebook()
    review = ask_model(prompt, build_system_prompt(whitebook))
    review["_whitebook_source"] = wb_source
    if truncated:
        review["confidence"] = "low"
        review["score_justification"] += " (Diff was truncated; confidence lowered.)"

    policy = apply_policy(review, changed, PROTECTED)
    approvals = count_approvals()
    upsert_comment(render(review, policy, approvals, BOT_NAME, PROVIDER))
    set_labels(f"carlos:{policy['score']}")
    publish_gate(policy["score"], policy["required"], approvals, policy["auto_merge"])
    print(f"score={policy['score']} required={policy['required']} auto_merge={policy['auto_merge']}")


def run_gate():
    stored = read_stored()
    if not stored:
        print("No stored review yet; nothing to gate.")
        set_status("pending", "Waiting for Carlos to review")
        return
    approvals = count_approvals()
    publish_gate(stored["score"], stored["required"], approvals, stored["auto"])
    print(f"gate recomputed: {stored} approvals={approvals}")


def read_stored():
    c = find_report_comment()
    if not c:
        return None
    m = re.search(r"<!-- score:(\d+) required:(\d+) auto:(True|False) -->", c["body"])
    return {"score": int(m.group(1)), "required": int(m.group(2)), "auto": m.group(3) == "True"}


def run_merge():
    pr = get_pr()
    stored = read_stored()
    if not stored:
        react("confused")
        reply(f"@{COMMENT_AUTHOR} Carlos has not reviewed this PR yet. Comment `{BOT_NAME} review` first.")
        return

    approvals = count_approvals()
    publish_gate(stored["score"], stored["required"], approvals, stored["auto"])

    if approvals < stored["required"]:
        react("confused")
        reply(f"@{COMMENT_AUTHOR} not merging: score **{stored['score']}%** requires "
              f"**{stored['required']}** approval(s), currently **{approvals}**.")
        return

    # Check the head commit's other status checks (CI etc.) are green.
    combined = gh("GET", f"/repos/{REPO}/commits/{SHA}/status")
    failing = [st["context"] for st in combined["statuses"]
               if st["context"] != STATUS_CONTEXT and st["state"] != "success"]
    runs = gh("GET", f"/repos/{REPO}/commits/{SHA}/check-runs").get("check_runs", [])
    failing += [r["name"] for r in runs
                if r["conclusion"] not in ("success", "neutral", "skipped") and r["name"] != "carlos"]
    if failing:
        react("confused")
        reply(f"@{COMMENT_AUTHOR} not merging: these checks are not green yet: "
              + ", ".join(f"`{f}`" for f in sorted(set(failing))))
        return

    try:
        gh("PUT", f"/repos/{REPO}/pulls/{PR}/merge",
           json={"merge_method": "squash", "sha": SHA,
                 "commit_title": f"{pr['title']} (#{PR})"})
        react("rocket")
        reply(f"✅ Merged by request of @{COMMENT_AUTHOR} — score {stored['score']}%, "
              f"{approvals}/{stored['required']} approvals, all checks green.")
    except requests.HTTPError as e:
        react("confused")
        msg = e.response.json().get("message", str(e)) if e.response is not None else str(e)
        reply(f"@{COMMENT_AUTHOR} GitHub refused the merge: {msg}")


def run_command():
    words = COMMENT_BODY.lower().split()
    if len(words) < 2 or words[0] != BOT_NAME:
        return
    cmd = words[1]
    if not user_can_write(COMMENT_AUTHOR):
        react("confused")
        reply(f"@{COMMENT_AUTHOR} only collaborators with write access can use `{BOT_NAME}` commands.")
        return
    if cmd == "review":
        react("eyes")
        get_pr()
        run_review()
    elif cmd == "merge":
        react("eyes")
        run_merge()
    else:
        react("confused")
        reply(f"@{COMMENT_AUTHOR} unknown command `{cmd}`. Available: `{BOT_NAME} review`, `{BOT_NAME} merge`.")


if __name__ == "__main__":
    try:
        if EVENT == "issue_comment":
            run_command()
        elif EVENT == "pull_request_review":
            get_pr()
            run_gate()
        else:
            run_review()
    except Exception as e:  # never leave the status hanging in "pending"
        if SHA:
            set_status("error", f"Carlos stumbled: {type(e).__name__}")
        raise
