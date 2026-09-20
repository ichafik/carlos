"""Provider- and transport-agnostic review logic shared by both Carlos
deployments: the GitHub Actions script (carlos_review.py) and the GitHub
App (app/review_engine.py).

Nothing in this module reads an env var, calls the GitHub API, or knows
which LLM SDK produced the text it's given — it only turns a prompt into a
system prompt, turns a model's raw text into a normalized review dict, turns
a review + policy into a rubric-based merge decision, and renders both into
the markdown comment Carlos posts. Keeping this pure and duplicated nowhere
means a rubric change or a rendering fix only has to happen once (WB-CDE-02).
"""

import json
import re

MARKER = "<!-- carlos-pr-review -->"
SEVERITIES = ("blocker", "major", "minor", "nit")
SEV_RANK = {sev: n for n, sev in enumerate(SEVERITIES)}

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


def build_system_prompt(whitebook: str, base: str = SYSTEM_PROMPT) -> str:
    if not whitebook:
        return base
    return (base + "\n\n=== ENGINEERING WHITEBOOK (organization rules; enforce these) ===\n"
            + whitebook + "\n=== END WHITEBOOK ===\n")


def parse_review(text: str) -> dict:
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


def normalize_review(review: dict) -> dict:
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


def apply_policy(review: dict, changed_files: list, protected_paths: list) -> dict:
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

    protected_hits = [f for f in changed_files
                       if any(f.startswith(p) or f"/{p}" in f for p in protected_paths)]

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


def render(review: dict, policy: dict, approvals: int, bot_name: str, provider_name: str) -> str:
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
    L.append("| Correctness | Tests | Security | Style | Confidence |\n|---|---|---|---|---|")
    L.append(f"| {s['correctness']}/40 | {s['tests']}/25 | {s['security']}/20 | {s['style']}/15 | {review.get('confidence','?')} |")
    L.append(f"\n_{review.get('score_justification','')}_")
    L.append("\n<sub>Policy: ≥95 auto-merge · 50–94 one approver · <50 two approvers. "
             f"Commands: `{bot_name} review` · `{bot_name} merge`. "
             f"Carlos Code Reviewer · powered by {provider_name}.</sub>")
    return "\n".join(L)
