"""A thin GitHub REST client scoped to one repo + installation token.

carlos_review.py (the Actions script) gets away with module-level globals
(REPO, PR, SHA, HEADERS) because one process handles exactly one PR. The App
serves every installation from one long-running process, so the equivalent
state has to be an object, not a module global — this class is that object.
"""

import requests

GH = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, repo_full_name: str, pr_number: int, sha: str = ""):
        self.repo = repo_full_name
        self.pr = pr_number
        self.sha = sha
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def gh(self, method: str, path: str, **kw):
        r = requests.request(method, f"{GH}{path}", headers=self._headers, timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.text else {}

    def get_pr(self) -> dict:
        pr = self.gh("GET", f"/repos/{self.repo}/pulls/{self.pr}")
        if not self.sha:
            self.sha = pr["head"]["sha"]
        return pr

    def get_diff(self) -> str:
        r = requests.get(
            f"{GH}/repos/{self.repo}/pulls/{self.pr}",
            headers={**self._headers, "Accept": "application/vnd.github.v3.diff"},
            timeout=60,
        )
        r.raise_for_status()
        return r.text

    def get_changed_files(self) -> list[str]:
        files, page = [], 1
        while True:
            batch = self.gh("GET", f"/repos/{self.repo}/pulls/{self.pr}/files",
                             params={"per_page": 100, "page": page})
            files += [f["filename"] for f in batch]
            if len(batch) < 100:
                return files
            page += 1

    def count_approvals(self) -> int:
        author = self.get_pr()["user"]["login"]
        reviews, page = [], 1
        while True:
            batch = self.gh("GET", f"/repos/{self.repo}/pulls/{self.pr}/reviews",
                             params={"per_page": 100, "page": page})
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

    def find_comment(self, marker: str) -> dict | None:
        page = 1
        while True:
            batch = self.gh("GET", f"/repos/{self.repo}/issues/{self.pr}/comments",
                             params={"per_page": 100, "page": page})
            for c in batch:
                if marker in c["body"]:
                    return c
            if len(batch) < 100:
                return None
            page += 1

    def upsert_comment(self, marker: str, body: str) -> None:
        existing = self.find_comment(marker)
        if existing:
            self.gh("PATCH", f"/repos/{self.repo}/issues/comments/{existing['id']}", json={"body": body})
        else:
            self.gh("POST", f"/repos/{self.repo}/issues/{self.pr}/comments", json={"body": body})

    def set_labels(self, prefix: str, new_label: str) -> None:
        labels = [l["name"] for l in self.gh("GET", f"/repos/{self.repo}/issues/{self.pr}/labels")]
        keep = [l for l in labels if not l.startswith(prefix)]
        self.gh("PUT", f"/repos/{self.repo}/issues/{self.pr}/labels", json={"labels": keep + [new_label]})

    def set_status(self, context: str, state: str, description: str) -> None:
        self.gh("POST", f"/repos/{self.repo}/statuses/{self.sha}",
                json={"state": state, "context": context, "description": description[:140]})

    def user_can_write(self, login: str) -> bool:
        try:
            perm = self.gh("GET", f"/repos/{self.repo}/collaborators/{login}/permission")["permission"]
        except requests.RequestException:
            return False
        return perm in ("write", "maintain", "admin")

    def react(self, comment_id, content: str) -> None:
        if not comment_id:
            return
        try:
            self.gh("POST", f"/repos/{self.repo}/issues/comments/{comment_id}/reactions",
                    json={"content": content})
        except requests.RequestException:
            pass  # a missing emoji must not abort a review or merge

    def reply(self, text: str) -> None:
        self.gh("POST", f"/repos/{self.repo}/issues/{self.pr}/comments", json={"body": text})

    def merge(self, title: str) -> None:
        self.gh("PUT", f"/repos/{self.repo}/pulls/{self.pr}/merge",
                json={"merge_method": "squash", "sha": self.sha, "commit_title": title})

    def failing_checks(self, status_context: str) -> list[str]:
        """Names of any non-Carlos status/check that isn't green on self.sha."""
        combined = self.gh("GET", f"/repos/{self.repo}/commits/{self.sha}/status")
        failing = [st["context"] for st in combined["statuses"]
                   if st["context"] != status_context and st["state"] != "success"]
        runs = self.gh("GET", f"/repos/{self.repo}/commits/{self.sha}/check-runs").get("check_runs", [])
        failing += [r["name"] for r in runs
                    if r["conclusion"] not in ("success", "neutral", "skipped") and r["name"] != "carlos"]
        return sorted(set(failing))

    def get_file_contents(self, path: str, ref: str = "main", repo: str | None = None) -> str | None:
        """Returns raw text, or None if the file/ref doesn't exist or isn't accessible
        with this installation's token. `repo` defaults to self.repo but can name a
        different repo (e.g. a central org/.github whitebook) — this only works if
        that repo is covered by the same installation's token."""
        r = requests.get(
            f"{GH}/repos/{repo or self.repo}/contents/{path}",
            headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            params={"ref": ref},
            timeout=30,
        )
        return r.text if r.ok else None
