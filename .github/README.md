# AI PR Review Gate

Claude reviews every pull request, posts a report (summary, issues, edge-cases table,
test readiness, score), and enforces a score-based approval policy.

| Score   | Policy                         |
|---------|--------------------------------|
| ≥ 95 %  | auto-merge, no approver needed |
| 50–94 % | 1 human approval               |
| < 50 %  | 2 human approvals              |

Guardrails that override auto-merge: any **blocker** issue (caps score at 49),
integration tests advised but missing (caps at 79), changed-line coverage < 60 % (−15),
changes under protected paths, or low model confidence (both force ≥ 1 approver).

## Setup

1. Copy `.github/workflows/pr-review.yml` and `.github/scripts/pr_review.py` into your repo.
2. Add the secret **`ANTHROPIC_API_KEY`** (Settings → Secrets → Actions).
3. Enable **Allow auto-merge** (Settings → General → Pull Requests).
4. Branch protection on `main`:
   - Require status checks to pass → add **`PR Review Gate`** (appears after the first run)
   - Required approvals: **0** (the gate enforces the dynamic count)
   - Optionally require your CI check too — auto-merge waits for all required checks.
5. Edit `PROTECTED_PATHS` and `MODEL` in the workflow to taste.

## How the gate works

- On `pull_request` events the script calls Claude, computes the score, posts the report
  and publishes a commit status `PR Review Gate` = success/failure depending on whether
  the current approval count meets the requirement.
- On `pull_request_review` events it skips the model and just recomputes the status from
  the score stored in the report comment, so approvals flip the gate immediately.
- At ≥ 95 % with no guardrail hits, `gh pr merge --auto --squash` is enabled; GitHub merges
  once all required checks are green.

## Comment commands

Any collaborator with write access can comment on the PR:

| Comment          | Effect                                                                 |
|------------------|------------------------------------------------------------------------|
| `carlos review`  | Re-runs the full AI review on the current head and refreshes the gate  |
| `carlos merge`   | Merges (squash) **only if** approvals ≥ required for the score and all other checks are green; otherwise replies with what is missing |

The bot reacts 👀 when it picks up a command, 🚀 on a successful merge, 😕 when it can't comply.
Rename the trigger word via `BOT_NAME` in the workflow (also update the `startsWith` filter).

## Notes

- Diffs over ~180k characters are truncated and confidence is forced to `low`
  (no auto-merge). Split very large PRs.
- The score is a model judgement. Keep CI as a required check; do not rely on the
  gate alone for correctness.