# Engineering Whitebook

The rules every pull request in this organization is reviewed against. Carlos loads this file
into every review; a **MUST NOT** violation is a blocker, a **MUST** violation is major, and
**SHOULD** rules are minor. Keep rules short, testable and free of style opinions that a linter
already enforces.

Rule IDs are stable. Reference them in PR comments (`WB-SEC-01`) and never reuse a retired ID.

---

## 1. Security (SEC)

- **WB-SEC-01 MUST NOT** commit secrets, tokens, passwords, private keys or connection strings. Use the secret manager / CI secrets.
- **WB-SEC-02 MUST NOT** build SQL, shell commands or file paths by string concatenation with user input. Use parameterised queries and library APIs.
- **WB-SEC-03 MUST NOT** disable TLS verification, CSRF protection or authentication checks, even "temporarily".
- **WB-SEC-04 MUST** validate and bound all external input (size, type, range) at the boundary where it enters the system.
- **WB-SEC-05 MUST** apply authorization checks server-side for every endpoint or handler that reads or mutates data.
- **WB-SEC-06 SHOULD** pin third-party dependencies to exact versions and avoid adding a dependency for something under ~50 lines of code.

## 2. Reliability & error handling (REL)

- **WB-REL-01 MUST NOT** swallow exceptions silently (`except: pass`, empty `catch {}`). Either handle, re-raise, or log with context.
- **WB-REL-02 MUST NOT** introduce unbounded retries, loops or queues; every retry has a limit and a backoff.
- **WB-REL-03 MUST** set explicit timeouts on every network, database and subprocess call.
- **WB-REL-04 MUST** make write operations idempotent or guard them with a uniqueness constraint when they can be retried.
- **WB-REL-05 SHOULD** fail fast on invalid configuration at startup rather than at first use.

## 3. Data & persistence (DAT)

- **WB-DAT-01 MUST NOT** change or drop a database column, table or index without a migration that is backward compatible with the currently deployed code.
- **WB-DAT-02 MUST NOT** log or persist personal data (emails, names, IDs, payment data) outside the systems designated for it.
- **WB-DAT-03 MUST** accompany every schema migration with a rollback path.
- **WB-DAT-04 SHOULD** avoid N+1 query patterns; batch or join instead.

## 4. APIs & contracts (API)

- **WB-API-01 MUST NOT** change the meaning, type or name of an existing public API field, event or CLI flag without a versioned path and a deprecation notice.
- **WB-API-02 MUST** return structured errors (code + message) rather than raw stack traces or free text.
- **WB-API-03 MUST** document new endpoints, events or config flags in the same PR that introduces them.
- **WB-API-04 SHOULD** keep endpoints and functions small and single-purpose; a handler that does I/O, business logic and formatting is a smell.

## 5. Testing (TST)

- **WB-TST-01 MUST NOT** merge behaviour changes without a test that would fail if the change were reverted.
- **WB-TST-02 MUST NOT** delete or skip existing tests to make a build green; fix or explicitly justify in the PR.
- **WB-TST-03 MUST** add an integration test when a PR touches a service boundary, schema, external API, queue or config.
- **WB-TST-04 SHOULD** cover the not-covered edge cases Carlos lists before requesting review.

## 6. Code health (CDE)

- **WB-CDE-01 MUST NOT** leave debugging artefacts: `print`/`console.log` debugging, commented-out code, TODOs without an issue link.
- **WB-CDE-02 MUST NOT** duplicate an existing helper or utility; reuse or extend it.
- **WB-CDE-03 MUST** keep functions under ~50 lines and files under ~500 lines unless there is a documented reason.
- **WB-CDE-04 MUST** name things by intent (`retryDelayMs`, not `x`); booleans read as predicates (`isReady`, `hasAccess`).
- **WB-CDE-05 SHOULD** prefer explicit over clever; if a reviewer needs a comment to follow it, simplify it.

## 7. Observability (OBS)

- **WB-OBS-01 MUST** log at the boundaries (request in, external call out, failure) with a correlation/request ID.
- **WB-OBS-02 MUST NOT** log at `error` level for expected conditions (validation failures, 404s).
- **WB-OBS-03 SHOULD** emit a metric for every new background job or scheduled task (runs, failures, duration).

## 8. Pull request hygiene (PRH)

- **WB-PRH-01 MUST** keep a PR to one logical change; refactors ship separately from behaviour changes.
- **WB-PRH-02 MUST** explain *why* in the PR description, not just *what*.
- **WB-PRH-03 MUST NOT** include generated files, vendored dependencies or large binaries unless the repo explicitly expects them.
- **WB-PRH-04 SHOULD** stay under ~400 changed lines; split larger work.

---

## Changing this whitebook

Propose changes via a PR to this file in the organization `.github` repository. Every rule needs:
an ID, a level (MUST NOT / MUST / SHOULD), one sentence, and a reason if it isn't obvious.
Retired rules move to a "Retired" section with their ID kept.