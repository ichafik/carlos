# Carlos Code Reviewer

Carlos reviews every pull request with the Gemini API, posts a structured report, and
enforces a score-based approval policy through a required status check, so nothing reaches
`main` without meeting the bar.

```
.github/
├── workflows/pr-review.yml      # triggers, permissions, env config
└── scripts/carlos_review.py     # review, scoring, gate, comment commands
README.md
```

## Merge policy

| Score   | Policy                               |
|---------|--------------------------------------|
| ≥ 95 %  | auto-merge, no human approval needed |
| 50–94 % | 1 human approval                     |
| < 50 %  | 2 human approvals                    |

The PR author's own approval and bot reviews are not counted.

### Score rubric (0–100)

| Area                    | Max |
|-------------------------|-----|
| Correctness & logic     | 40  |
| Test coverage & quality | 25  |
| Security                | 20  |
| Style / maintainability | 15  |

### Guardrails applied after the rubric

| Condition                                            | Effect                           |
|------------------------------------------------------|----------------------------------|
| Any **blocker** issue                                | score capped at 49 (2 approvals) |
| Any **potential bug introduced**                     | score capped at 79               |
| Integration tests advised but missing                | score capped at 79               |
| Estimated changed-line coverage < 60 %               | −15                              |
| Touches a protected path (`PROTECTED_PATHS`)         | no auto-merge, ≥ 1 approval      |
| Low model confidence or truncated diff (>180k chars) | no auto-merge, ≥ 1 approval      |

## What the report contains

Carlos posts one comment per PR and updates it on every push:

1. **Verdict** — score, approvals required vs. current, and any adjustments applied
2. **Summary** — what changes, why, which modules are touched
3. **Potential issues** — severity-tagged (blocker / major / minor / nit) with file:line and a suggestion
4. **Potential bugs introduced** — regressions this PR risks in existing behaviour, each with a concrete trigger and fix
5. **Edge cases report** — null/empty inputs, boundaries, concurrency, failure paths, auth; marked covered / not covered / n/a
6. **Test readiness** — unit tests updated, coverage estimate, integration tests advised/present
7. **Suggested test scenarios** — given/when/then table with test names and priority, whenever tests are missing or weak
8. **Score breakdown** — points per rubric area and confidence

He also adds a `carlos:<score>` label and publishes the **`Carlos Review Gate`** commit status.

## Comment commands

Available to any collaborator with write access:

| Comment         | Effect |
|-----------------|--------|
| `carlos review` | Re-runs the full review on the current head and refreshes the gate |
| `carlos merge`  | Squash-merges **only if** approvals ≥ required for the score and all other checks are green; otherwise replies with what is missing |

Reactions: 👀 command received · 🚀 merged · 😕 could not comply (a reply explains why).

## Setup

### 1. Repository visibility / plan
Branch protection and auto-merge need a **public repo** or **GitHub Pro / Team**. On a private
repo with a free plan the status check still posts, but nothing enforces it.

### 2. Files
Commit both files to `main` — `issue_comment` and `pull_request_review` workflows only run from
the default branch. The workflow **must** live in `.github/workflows/`, not `.github/`.

### 3. Secret
Settings → Secrets and variables → Actions → New repository secret
`GEMINI_API_KEY` = key from Google AI Studio.

### 4. Actions permissions
Settings → Actions → General → Workflow permissions:
*Read and write permissions* ✔ and *Allow GitHub Actions to create and approve pull requests* ✔.

### 5. Pull request settings
Settings → General → Pull Requests: *Allow auto-merge* ✔, *Allow squash merging* ✔.

### 6. First run
Open a throwaway PR. The `Carlos Review Gate` status only appears in the branch-protection
picker after it has been reported once.

### 7. Branch protection on `main`
- ✔ Require a pull request before merging
  - ☐ *Require approvals* — leave **unticked** (Carlos enforces 0/1/2 dynamically)
- ✔ Require status checks to pass → add **`Carlos Review Gate`** (+ your CI check if any)
- Leave the bypass list empty, otherwise admins can merge around the gate

If GitHub offers to *convert to ruleset*, either choice works; a ruleset needs the same three
settings with required approvals set to 0.

## Configuration (workflow `env`)

| Variable          | Default                                                 | Purpose |
|-------------------|---------------------------------------------------------|---------|
| `MODEL`           | `gemini-3.8-flash`                                      | Gemini model id |
| `BOT_NAME`        | `carlos`                                                | Trigger word for comment commands (also update the `startsWith` filter in the job `if`) |
| `STATUS_CONTEXT`  | `Carlos Review Gate`                                    | Name of the commit status; must match the required check in branch protection |
| `PROTECTED_PATHS` | `auth/,payments/,migrations/,infra/,.github/workflows/` | Comma-separated prefixes that always need a human approver |

## How it works

| Event                 | Behaviour |
|-----------------------|-----------|
| `pull_request`        | Fetches the diff, asks Gemini for a structured JSON review, applies rubric + guardrails, upserts the report comment, sets the label, publishes the gate status. At ≥ 95 % with no guardrail hits, enables GitHub auto-merge; GitHub merges once all required checks pass. |
| `pull_request_review` | No model call. Reads the stored score from the report comment, recounts approvals, republishes the gate status so approvals flip it immediately. |
| `issue_comment`       | Parses `carlos <command>`, checks the commenter's permission, then re-reviews or merges. The merge pins the head SHA, so a push between comment and merge is rejected. |

Model robustness: 32k output-token budget, truncation detection with one retry, tolerant JSON
parsing (`json-repair`) with one retry, raw head/tail logged on failure. Any unhandled error
sets the status to `error` so the PR is never left on a stale `pending`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Actions tab shows the "Get started" marketplace page | No workflow found: file is not under `.github/workflows/` on `main` |
| PR shows *Checks: 0* | PR opened before the workflow landed on `main`; close/reopen or push a commit |
| `Carlos Review Gate` not in the branch-protection picker | It hasn't run yet; complete step 6 first |
| `JSONDecodeError: Unterminated string` | Model output truncated; handled by the retry logic — check `[carlos] attempt …` lines in the log |
| `carlos merge` says approvals missing on a solo repo | Author approvals don't count; use a second account/collaborator |
| Comment commands do nothing | Workflow not on the default branch, or comment doesn't start with `carlos ` |
| Auto-merge never fires | *Allow auto-merge* off, or *Require approvals* ticked in branch protection |

## Notes

- The score is a model judgement. Keep real CI as a required check; do not rely on Carlos alone.
- Very large PRs are truncated at ~180k characters of diff and forced to low confidence. Split them.
