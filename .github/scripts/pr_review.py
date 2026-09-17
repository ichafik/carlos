"""
AI PR Review Gate

MODE=review : fetch the diff, ask Claude for a structured review, compute the score,
              post/update the report comment, publish the gate status, decide auto-merge.
MODE=gate   : re-read the stored score from the report comment and re-publish the gate
              status based on the current number of approvals (runs on review events).

Merge policy
  score >= 95  -> auto-merge, 0 approvals (unless blockers / protected paths / low confidence)
  50..94       -> 1 approval required
  < 50         -> 2 approvals required
"""

import json
import os
import re
import sys

import requests
from anthropic import Anthropic

# ---------- config ----------
GH = "https://api.github.com"
REPO = os.environ["REPO"]
PR = os.environ["PR_NUMBER"]
SHA = os.environ["HEAD_SHA"]
MODE = os.environ.get("MODE", "review")
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
PROTECTED = [p.strip() for p in os.environ.get("PROTECTED_PATHS", "").split(",") if p.strip()]
MARKER = "<!-- ai-pr-review-gate -->"
STATUS_CONTEXT = "PR Review Gate"
MAX_DIFF_CHARS = 180_000

HEADERS = {
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

RUBRIC = """
Score rubric (0-100):
  correctness & logic ............ 40
  test coverage & quality ........ 25
  security ....................... 20
  style / maintainability ........ 15
"""

SYSTEM_PROMPT = f"""You are a senior software engineer performing a rigorous pull-request review.
Return ONLY a JSON object (no prose, no markdown fences) with this exact shape:

{{
  "summary": "2-4 sentences: what changes, why, which modules/services are touched",
  "issues": [
    {{"severity": "blocker|major|minor|nit", "file": "path", "line": 0, "title": "...", "detail": "...", "suggestion": "..."}}
  ],
  "edge_cases": [
    {{"case": "...", "status": "covered|not_covered|not_applicable", "note": "..."}}
  ],
  "tests": {{
    "unit_tests_updated": true,
    "estimated_changed_line_coverage_pct": 0,
    "integration_tests_advised": false,
    "integration_tests_present": false,
    "reason": "..."
  }},
  "scores": {{"correctness": 0, "tests": 0, "security": 0, "style": 0}},
  "confidence": "high|medium|low",
  "score_justification": "1-3 sentences"
}}

Rules:
- Edge cases must consider: null/empty inputs, boundary values, concurrency/races,
  failure paths (timeouts, partial writes, retries), auth/permission boundaries.
- Set integration_tests_advised=true when the PR touches cross-service boundaries,
  DB schema, external APIs, queues, or configuration.
- Be concrete: reference real files and lines from the diff. Do not invent issues.
- Use the maximum points listed here: {RUBRIC}
"""


# ---------- github helpers ----------
def gh(method, path, **kw):
    r = requests.request(method, f"{GH}{path}", headers=HEADERS, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def get_pr():
    return gh("GET", f"/repos/{REPO}/pulls/{PR}")


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
    keep = [l for l in labels if not l.startswith("ai-review:")]
    gh("PUT", f"/repos/{REPO}/issues/{PR}/labels", json={"labels": keep + [score_label]})


def set_status(state, description):
    gh("POST", f"/repos/{REPO}/statuses/{SHA}",
       json={"state": state, "context": STATUS_CONTEXT, "description": description[:140]})


def set_output(key, value):
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
        f.write(f"{key}={value}\n")


# ---------- policy ----------
def apply_policy(review, changed_files):
    s = review["scores"]
    raw = min(100, s["correctness"] + s["tests"] + s["security"] + s["style"])
    score = raw
    caps = []

    blockers = [i for i in review["issues"] if i["severity"] == "blocker"]
    if blockers:
        score = min(score, 49)
        caps.append(f"{len(blockers)} blocker issue(s) → capped at 49")

    t = review["tests"]
    if t.get("integration_tests_advised") and not t.get("integration_tests_present"):
        score = min(score, 79)
        caps.append("integration tests advised but missing → capped at 79")

    cov = t.get("estimated_changed_line_coverage_pct")
    if isinstance(cov, (int, float)) and cov < 60:
        score = max(0, score - 15)
        caps.append(f"changed-line coverage ~{cov}% (<60) → −15")

    protected_hits = [f for f in changed_files if any(f.startswith(p) or f"/{p}" in f for p in PROTECTED)]

    if score >= 95:
        required = 0
    elif score >= 50:
        required = 1
    else:
        required = 2

    auto_merge = required == 0
    guard = []
    if auto_merge and protected_hits:
        auto_merge, required = False, 1
        guard.append("touches protected paths: " + ", ".join(protected_hits[:5]))
    if auto_merge and review.get("confidence") == "low":
        auto_merge, required = False, 1
        guard.append("model confidence is low")

    return {"raw": raw, "score": score, "caps": caps, "required": required,
            "auto_merge": auto_merge, "guard": guard}


# ---------- report ----------
def render(review, policy, approvals):
    sev_icon = {"blocker": "🟥", "major": "🟧", "minor": "🟨", "nit": "⬜"}
    st_icon = {"covered": "✅", "not_covered": "❌", "not_applicable": "➖"}
    L = [MARKER, f"<!-- score:{policy['score']} required:{policy['required']} auto:{policy['auto_merge']} -->",
         f"## 🤖 AI PR Review — Score **{policy['score']}%**", ""]

    if policy["required"] == 0 and policy["auto_merge"]:
        L.append("✅ **Eligible for auto-merge** (≥95%, no blockers, no protected paths).")
    else:
        L.append(f"🔒 **Requires {policy['required']} human approval(s)** — currently {approvals}.")
    if policy["caps"]:
        L.append("\n**Score adjustments:**")
        L += [f"- {c}" for c in policy["caps"]]
        L.append(f"- raw rubric score was {policy['raw']}")
    if policy["guard"]:
        L.append("\n**Auto-merge withheld:**")
        L += [f"- {g}" for g in policy["guard"]]

    L += ["", "### Summary", review["summary"], ""]

    L.append("### Potential issues")
    if review["issues"]:
        for i in sorted(review["issues"], key=lambda x: ["blocker", "major", "minor", "nit"].index(x["severity"])):
            loc = f"`{i['file']}`" + (f":{i['line']}" if i.get("line") else "")
            L.append(f"- {sev_icon.get(i['severity'],'')} **{i['severity'].upper()}** — {i['title']} ({loc})  ")
            L.append(f"  {i['detail']}")
            if i.get("suggestion"):
                L.append(f"  _Suggestion:_ {i['suggestion']}")
    else:
        L.append("None found.")
    L.append("")

    L.append("### Edge cases report")
    L.append("| Case | Status | Note |")
    L.append("|---|---|---|")
    for e in review["edge_cases"]:
        L.append(f"| {e['case']} | {st_icon.get(e['status'],'')} {e['status'].replace('_',' ')} | {e.get('note','')} |")
    L.append("")

    t = review["tests"]
    L.append("### Test readiness")
    L.append(f"- Unit tests updated: {'yes' if t.get('unit_tests_updated') else 'no'}")
    L.append(f"- Estimated changed-line coverage: ~{t.get('estimated_changed_line_coverage_pct','?')}%")
    if t.get("integration_tests_advised"):
        L.append("- ⚠️ **Integration tests advised** — " +
                 ("present ✅" if t.get("integration_tests_present") else "**missing** ❌"))
    L.append(f"- {t.get('reason','')}")
    L.append("")

    s = review["scores"]
    L.append("### Score breakdown")
    L.append(f"| Correctness | Tests | Security | Style | Confidence |\n|---|---|---|---|---|")
    L.append(f"| {s['correctness']}/40 | {s['tests']}/25 | {s['security']}/20 | {s['style']}/15 | {review.get('confidence','?')} |")
    L.append(f"\n_{review.get('score_justification','')}_")
    L.append("\n<sub>Policy: ≥95 auto-merge · 50–94 one approver · <50 two approvers. Model: "
             f"{MODEL}. Re-runs on every push.</sub>")
    return "\n".join(L)


# ---------- gate ----------
def publish_gate(score, required, approvals, auto_merge):
    if approvals >= required:
        set_status("success", f"Score {score}% · {approvals}/{required} approvals ✓")
    else:
        set_status("failure", f"Score {score}% · needs {required} approval(s), has {approvals}")
    set_output("score", score)
    set_output("required_approvals", required)
    set_output("auto_merge", "true" if auto_merge else "false")


# ---------- main ----------
def run_review():
    set_status("pending", "AI review in progress…")
    pr = get_pr()
    diff = get_diff()
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncated]"
    changed = get_changed_files()

    client = Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content":
            f"PR title: {pr['title']}\n\nPR description:\n{pr.get('body') or '(none)'}\n\n"
            f"Changed files:\n" + "\n".join(changed) + f"\n\nDiff:\n{diff}"}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    review = json.loads(text)
    if truncated:
        review["confidence"] = "low"
        review["score_justification"] += " (Diff was truncated; confidence lowered.)"

    policy = apply_policy(review, changed)
    approvals = count_approvals()
    upsert_comment(render(review, policy, approvals))
    set_labels(f"ai-review:{policy['score']}")
    publish_gate(policy["score"], policy["required"], approvals, policy["auto_merge"])
    print(f"score={policy['score']} required={policy['required']} auto_merge={policy['auto_merge']}")


def run_gate():
    c = find_report_comment()
    if not c:
        print("No stored review yet; nothing to gate.")
        set_status("pending", "Waiting for AI review")
        return
    m = re.search(r"<!-- score:(\d+) required:(\d+) auto:(True|False) -->", c["body"])
    score, required, auto = int(m.group(1)), int(m.group(2)), m.group(3) == "True"
    approvals = count_approvals()
    publish_gate(score, required, approvals, auto)
    print(f"gate recomputed: score={score} required={required} approvals={approvals}")


if __name__ == "__main__":
    try:
        run_review() if MODE == "review" else run_gate()
    except Exception as e:  # never leave the status hanging in "pending"
        set_status("error", f"Review failed: {type(e).__name__}")
        raise