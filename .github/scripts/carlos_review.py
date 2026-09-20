"""
Carlos Code Reviewer — PR reviewer and merge gatekeeper

Reviews every pull request with the Gemini API, scores it, and enforces a
score-based approval policy before anything reaches main.

Modes (derived from the GitHub event)
  pull_request        -> review : ask Gemini, score, post report, publish gate status
  pull_request_review -> gate   : recompute gate status from stored score + current approvals
  issue_comment       -> command:
        "carlos review"  re-run Carlos's full review on the current head
        "carlos merge"   merge now if the gate conditions are fulfilled

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
from llm_providers import DEFAULT_MODELS, get_provider

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
MODEL = os.environ.get("MODEL") or DEFAULT_MODELS.get(PROVIDER, DEFAULT_MODELS["gemini"])
PROTECTED = [p.strip() for p in os.environ.get("PROTECTED_PATHS", "").split(",") if p.strip()]
MARKER = "<!-- carlos-pr-review -->"
STATUS_CONTEXT = os.environ.get("STATUS_CONTEXT", "Carlos Review Gate")
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "500000"))
WHITEBOOK_PATH = os.environ.get("WHITEBOOK_PATH", ".github/WHITEBOOK.md")   # local override
WHITEBOOK_REPO = os.environ.get("WHITEBOOK_REPO", "")   # e.g. "my-org/.github" → central source of truth
WHITEBOOK_REF = os.environ.get("WHITEBOOK_REF", "main")
MAX_WHITEBOOK_CHARS = 40_000
SEED = int(os.environ.get("SEED", "42"))                # fixed seed → same diff, same review (best-effort)
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0"))  # 0 = most deterministic
SEVERITIES = ("blocker", "major", "minor", "nit")
SEV_RANK = {sev: n for n, sev in enumerate(SEVERITIES)}

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

SYSTEM_PROMPT = f"""You are Carlos, a senior software engineer acting as the code reviewer for this repository. Perform a rigorous pull-request review.
Return ONLY a JSON object (no prose, no markdown fences) with this exact shape:

{{
  "summary": "2-4 sentences: what changes, why, which modules/services are touched",
  "issues": [
    {{"severity": "blocker|major|minor|nit", "file": "path", "line": 0, "title": "...", "detail": "...", "suggestion": "..."}}
  ],
  "edge_cases": [
    {{"case": "...", "status": "covered|not_covered|not_applicable", "note": "..."}}
  ],
  "policy_violations": [
    {{"rule_id": "WB-SEC-01", "level": "MUST NOT|MUST|SHOULD", "file": "path", "line": 0,
      "evidence": "what in the diff violates it", "fix": "..."}}
  ],
  "potential_bugs": [
    {{"file": "path", "line": 0, "title": "...", "why": "how this change can break existing behaviour",
      "trigger": "concrete input / sequence that exposes it", "fix": "..."}}
  ],
  "tests": {{
    "unit_tests_updated": true,
    "estimated_changed_line_coverage_pct": 0,
    "integration_tests_advised": false,
    "integration_tests_present": false,
    "reason": "...",
    "suggested_scenarios": [
      {{"type": "unit|integration|e2e", "name": "test_...", "given": "...", "when": "...", "then": "...", "priority": "high|medium|low"}}
    ]
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
- "policy_violations": check the diff against every rule in the ENGINEERING WHITEBOOK section
  (if present). Report only rules you can point to concrete evidence for in the diff; quote the
  rule_id exactly. Do not invent rules that are not in the whitebook. Empty list if compliant.
- "potential_bugs" is for defects this PR INTRODUCES or regressions it risks in existing
  behaviour (changed signatures, altered defaults, removed checks, off-by-one, changed
  error handling, concurrency). Give a concrete trigger for each. Empty list if none.
- "suggested_scenarios": whenever tests are missing, weak, or an edge case is not_covered,
  propose concrete test scenarios in given/when/then form with a suggested test name,
  ordered by priority. Aim for 3-8; cover every not_covered edge case and every
  potential_bug. Empty list only if the PR is fully and well tested.
- Be concrete: reference real files and lines from the diff. Do not invent issues.
- Your training knowledge has a cutoff; the code you review may be newer than it. NEVER report
  as an issue that a model identifier, library/package version, API name, SDK method, dependency,
  CLI flag, cloud service, or date "does not exist", "is deprecated", or "is invalid" based on
  your own knowledge. Treat such identifiers as data you cannot verify. Flag them ONLY if the diff
  itself contradicts them (e.g. two different values for the same setting, or a call that does not
  match the imported signature shown in the diff). Do not suggest replacing them with values you
  know from training.
- Use the maximum points listed here: {RUBRIC}
"""


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


def build_system_prompt(whitebook):
    if not whitebook:
        return SYSTEM_PROMPT
    return (SYSTEM_PROMPT + "\n\n=== ENGINEERING WHITEBOOK (organization rules; enforce these) ===\n"
            + whitebook + "\n=== END WHITEBOOK ===\n")


# ---------- model ----------
def parse_review(text):
    """Best-effort JSON extraction: strip fences, slice to outer braces, repair if needed."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        from json_repair import repair_json  # tolerant parser for slightly broken output
        return json.loads(repair_json(text))


def normalize_review(review):
    """Coerce the model output into the shape the rest of the script expects:
    lower-cased enums, fallbacks for unknown values, lists/dicts always present."""
    sev_alias = {"critical": "blocker", "high": "major", "medium": "minor", "low": "nit",
                 "warning": "minor", "info": "nit", "suggestion": "nit"}
    review.setdefault("summary", "")
    review["issues"] = review.get("issues") or []
    for i in review["issues"]:
        sev = str(i.get("severity", "minor")).strip().lower()
        i["severity"] = sev_alias.get(sev, sev) if sev_alias.get(sev, sev) in SEVERITIES else "minor"
        i.setdefault("file", ""); i.setdefault("title", ""); i.setdefault("detail", "")
    review["potential_bugs"] = review.get("potential_bugs") or []
    review["policy_violations"] = review.get("policy_violations") or []
    for v in review["policy_violations"]:
        lvl = str(v.get("level", "SHOULD")).strip().upper().replace("_", " ")
        v["level"] = lvl if lvl in ("MUST NOT", "MUST", "SHOULD") else "SHOULD"
        v.setdefault("rule_id", "?"); v.setdefault("file", ""); v.setdefault("evidence", ""); v.setdefault("fix", "")
    review["edge_cases"] = review.get("edge_cases") or []
    for e in review["edge_cases"]:
        st = str(e.get("status", "not_covered")).strip().lower().replace(" ", "_").replace("-", "_")
        e["status"] = st if st in ("covered", "not_covered", "not_applicable") else "not_covered"
        e.setdefault("case", ""); e.setdefault("note", "")
    t = review["tests"] = review.get("tests") or {}
    t["suggested_scenarios"] = t.get("suggested_scenarios") or []
    for sc in t["suggested_scenarios"]:
        sc["priority"] = str(sc.get("priority", "medium")).strip().lower()
    sc_ = review["scores"] = review.get("scores") or {}
    for k in ("correctness", "tests", "security", "style"):
        try:
            sc_[k] = max(0, int(float(sc_.get(k, 0))))
        except (TypeError, ValueError):
            sc_[k] = 0
    review["confidence"] = str(review.get("confidence", "medium")).strip().lower()
    review.setdefault("score_justification", "")
    return review


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

    viol = review.get("policy_violations") or []
    must_not = [v for v in viol if v["level"] == "MUST NOT"]
    must = [v for v in viol if v["level"] == "MUST"]
    if must_not:
        score = min(score, 49)
        caps.append(f"{len(must_not)} MUST NOT whitebook violation(s) → capped at 49")
    elif must:
        score = min(score, 79)
        caps.append(f"{len(must)} MUST whitebook violation(s) → capped at 79")

    bugs = review.get("potential_bugs") or []
    if bugs:
        score = min(score, 79)
        caps.append(f"{len(bugs)} potential bug(s) introduced → capped at 79")

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
        guard.append("Carlos is not confident about this one")

    return {"raw": raw, "score": score, "caps": caps, "required": required,
            "auto_merge": auto_merge, "guard": guard}


# ---------- report ----------
def render(review, policy, approvals):
    sev_icon = {"blocker": "🟥", "major": "🟧", "minor": "🟨", "nit": "⬜"}
    st_icon = {"covered": "✅", "not_covered": "❌", "not_applicable": "➖"}
    L = [MARKER, f"<!-- score:{policy['score']} required:{policy['required']} auto:{policy['auto_merge']} -->",
         f"## 🔍 Carlos Code Review — Score **{policy['score']}%**", ""]

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
        for i in sorted(review["issues"], key=lambda x: SEV_RANK.get(x.get("severity"), 99)):
            loc = f"`{i['file']}`" + (f":{i['line']}" if i.get("line") else "")
            L.append(f"- {sev_icon.get(i['severity'],'⬜')} **{i['severity'].upper()}** — {i['title']} ({loc})  ")
            L.append(f"  {i['detail']}")
            if i.get("suggestion"):
                L.append(f"  _Suggestion:_ {i['suggestion']}")
    else:
        L.append("None found.")
    L.append("")

    viol = review.get("policy_violations") or []
    lvl_icon = {"MUST NOT": "🟥", "MUST": "🟧", "SHOULD": "🟨"}
    L.append("### Whitebook compliance")
    if not review.get("_whitebook_source"):
        L.append("No whitebook configured.")
    elif viol:
        order = {"MUST NOT": 0, "MUST": 1, "SHOULD": 2}
        for v in sorted(viol, key=lambda x: order.get(x["level"], 9)):
            loc = f"`{v['file']}`" + (f":{v['line']}" if v.get("line") else "")
            L.append(f"- {lvl_icon.get(v['level'],'')} **{v['rule_id']}** ({v['level']}) — {v['evidence']} ({loc})  ")
            if v.get("fix"):
                L.append(f"  _Fix:_ {v['fix']}")
        L.append(f"\n<sub>Rules from `{review['_whitebook_source']}`</sub>")
    else:
        L.append(f"✅ Compliant with `{review['_whitebook_source']}`.")
    L.append("")

    bugs = review.get("potential_bugs") or []
    L.append("### Potential bugs introduced")
    if bugs:
        for b in bugs:
            loc = f"`{b.get('file','')}`" + (f":{b['line']}" if b.get("line") else "")
            L.append(f"- 🐞 **{b['title']}** ({loc})  ")
            L.append(f"  _Why:_ {b.get('why','')}  ")
            L.append(f"  _Trigger:_ {b.get('trigger','')}  ")
            if b.get("fix"):
                L.append(f"  _Fix:_ {b['fix']}")
    else:
        L.append("None spotted.")
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

    scenarios = t.get("suggested_scenarios") or []
    if scenarios:
        L.append("### Suggested test scenarios")
        L.append("| # | Priority | Type | Test | Given | When | Then |")
        L.append("|---|---|---|---|---|---|---|")
        order = {"high": 0, "medium": 1, "low": 2}
        for n, sc in enumerate(sorted(scenarios, key=lambda x: order.get(x.get("priority"), 3)), 1):
            L.append(f"| {n} | {sc.get('priority','')} | {sc.get('type','')} | `{sc.get('name','')}` "
                     f"| {sc.get('given','')} | {sc.get('when','')} | {sc.get('then','')} |")
        L.append("")

    s = review["scores"]
    L.append("### Score breakdown")
    L.append(f"| Correctness | Tests | Security | Style | Confidence |\n|---|---|---|---|---|")
    L.append(f"| {s['correctness']}/40 | {s['tests']}/25 | {s['security']}/20 | {s['style']}/15 | {review.get('confidence','?')} |")
    L.append(f"\n_{review.get('score_justification','')}_")
    L.append("\n<sub>Policy: ≥95 auto-merge · 50–94 one approver · <50 two approvers. "
             f"Commands: `{BOT_NAME} review` · `{BOT_NAME} merge`. "
             f"Carlos Code Reviewer · powered by {PROVIDER}.</sub>")
    return "\n".join(L)


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

    policy = apply_policy(review, changed)
    approvals = count_approvals()
    upsert_comment(render(review, policy, approvals))
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