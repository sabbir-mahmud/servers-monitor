"""Push webhooks from GitHub, GitLab, Gitea/Forgejo and Bitbucket.

Every provider proves the request with the project's secret: an HMAC-SHA256 of the body
(GitHub, Gitea, Forgejo, Bitbucket) or the secret itself in a header (GitLab). Nothing in
the body is trusted until that check passes, and only the branch, commit id, message and
author are taken from it. Each delivery id is accepted once.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass

ZERO_SHA = "0" * 40


class WebhookError(Exception):
    """Refused: the status code to answer with, and why."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Push:
    provider: str
    branch: str
    sha: str
    message: str = ""
    author: str = ""
    delivery_id: str = ""


@dataclass
class Ignored:
    reason: str


def _hmac_ok(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256=").strip().lower())


def _provider(headers) -> str:
    # Gitea and Forgejo also send X-GitHub-Event, so they're recognised first.
    if headers.get("X-Gitea-Event") or headers.get("X-Forgejo-Event"):
        return "gitea"
    if headers.get("X-GitHub-Event"):
        return "github"
    if headers.get("X-Gitlab-Event"):
        return "gitlab"
    if headers.get("X-Event-Key"):
        return "bitbucket"
    raise WebhookError(400, "Not a GitHub, GitLab, Gitea, Forgejo or Bitbucket webhook.")


def _verify(provider: str, headers, body: bytes, secret: str) -> None:
    if provider == "gitlab":
        token = headers.get("X-Gitlab-Token", "")
        if not token or not hmac.compare_digest(token, secret):
            raise WebhookError(403, "Wrong or missing secret token.")
        return
    signature = (
        headers.get("X-Forgejo-Signature") or headers.get("X-Gitea-Signature")
        if provider == "gitea"
        else headers.get("X-Hub-Signature-256") or headers.get("X-Hub-Signature", "")
    )
    if not signature or not _hmac_ok(secret, body, signature):
        raise WebhookError(403, "Wrong or missing signature. Is the secret right?")


def _branch(ref) -> str:
    ref = str(ref or "")
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""


def _first_line(text) -> str:
    return str(text or "").strip().splitlines()[0][:300] if str(text or "").strip() else ""


def parse(headers, body: bytes, secret: str) -> Push | Ignored:
    provider = _provider(headers)
    _verify(provider, headers, body, secret)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise WebhookError(
            400, "The body isn't JSON (choose content type application/json)."
        ) from None
    if not isinstance(data, dict):
        raise WebhookError(400, "The body isn't a JSON object.")

    if provider in ("github", "gitea"):
        event = headers.get("X-GitHub-Event") if provider == "github" else (
            headers.get("X-Gitea-Event") or headers.get("X-Forgejo-Event"))  # fmt: skip
        delivery = headers.get("X-GitHub-Delivery") or headers.get("X-Gitea-Delivery") or ""
        if event == "ping":
            return Ignored("ping: the webhook is set up")
        if event != "push":
            return Ignored(f"{event} events don't deploy")
        head = data.get("head_commit") or {}
        sha = str(data.get("after") or "")
        if data.get("deleted") or sha == ZERO_SHA:
            return Ignored("a branch was deleted")
        return Push(provider, _branch(data.get("ref")), sha, _first_line(head.get("message")),
                    str((head.get("author") or {}).get("name") or ""), delivery)  # fmt: skip

    if provider == "gitlab":
        if headers.get("X-Gitlab-Event") != "Push Hook":
            return Ignored(f"{headers.get('X-Gitlab-Event')} events don't deploy")
        sha = str(data.get("checkout_sha") or data.get("after") or "")
        if not sha or sha == ZERO_SHA:
            return Ignored("a branch was deleted")
        commit = next((c for c in data.get("commits") or [] if c.get("id") == sha), {})
        return Push(provider, _branch(data.get("ref")), sha, _first_line(commit.get("message")),
                    str(data.get("user_name") or ""),
                    headers.get("X-Gitlab-Event-UUID", ""))  # fmt: skip

    # Bitbucket Cloud
    if headers.get("X-Event-Key") == "diagnostics:ping":
        return Ignored("ping: the webhook is set up")
    if headers.get("X-Event-Key") != "repo:push":
        return Ignored(f"{headers.get('X-Event-Key')} events don't deploy")
    changes = ((data.get("push") or {}).get("changes")) or []
    for change in reversed(changes):
        new = change.get("new") or {}
        if new.get("type") == "branch":
            target = new.get("target") or {}
            author = target.get("author") or {}
            name = (author.get("user") or {}).get("display_name") or author.get("raw") or ""
            return Push("bitbucket", str(new.get("name") or ""), str(target.get("hash") or ""),
                        _first_line(target.get("message")), str(name),
                        headers.get("X-Request-UUID", ""))  # fmt: skip
    return Ignored("no branch was pushed")
