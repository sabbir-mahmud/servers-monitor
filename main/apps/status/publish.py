"""Push rendered status pages to GitHub. Called by the relay's scheduler.

Uses the contents API with a fine-grained token that can only write that one repository.
GitHub Pages serves it; Cloudflare Pages can also deploy the same repository.
"""

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.utils import timezone

from .models import PublishKind, StatusPage
from .render import content_hash, render

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
CHECK_EVERY = timedelta(minutes=2)
REFRESH_EVERY = timedelta(hours=6)  # republish now and then so "Updated …" stays honest
RETRY_FAILED_EVERY = timedelta(minutes=30)
TIMEOUT = 15


class PublishError(Exception):
    pass


def _github(method: str, path: str, token: str, body: dict | None = None) -> dict | None:
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ServerMonitor/status-pages",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        try:
            detail = json.loads(exc.read(2000) or b"{}").get("message", "")
        except ValueError, OSError:
            detail = ""
        hint = {401: "token rejected", 403: "token can't write this repository",
                404: "repository or branch not found"}.get(exc.code, "")  # fmt: skip
        raise PublishError(f"GitHub HTTP {exc.code}: {hint or detail}"[:300]) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PublishError(f"could not reach GitHub: {getattr(exc, 'reason', exc)}"[:300]) from exc


def github_put(cfg: dict, name: str, content: str, message: str) -> None:
    repo, branch = cfg["repo"], cfg.get("branch") or "main"
    path = "/".join(p for p in (cfg.get("path", "").strip("/"), name) if p)
    url = f"/repos/{repo}/contents/{urllib.parse.quote(path)}"
    existing = _github("GET", f"{url}?ref={urllib.parse.quote(branch)}", cfg["token"])
    encoded = base64.b64encode(content.encode()).decode()
    if existing and existing.get("content", "").replace("\n", "") == encoded:
        return  # unchanged: no empty commit
    body = {"message": message, "content": encoded, "branch": branch}
    if existing and existing.get("sha"):
        body["sha"] = existing["sha"]
    _github("PUT", url, cfg["token"], body)


def publish(page: StatusPage, now=None) -> bool:
    """Render and push one page. Records the outcome on the page; returns success."""
    now = now or timezone.now()
    snap, html, data = render(page, now)
    digest = content_hash(snap)
    updates = {"needs_publish": False, "last_attempt_at": now}
    try:
        if page.publish_kind != PublishKind.GITHUB:
            raise PublishError("publishing is off for this page")
        cfg = page.config
        if not cfg.get("repo") or not cfg.get("token"):
            raise PublishError("set the repository and token first")
        message = f"Status: {snap['status']['label']}"
        github_put(cfg, "index.html", html, message)
        github_put(cfg, "status.json", data, message)
        if cfg.get("domain"):
            github_put(cfg, "CNAME", cfg["domain"] + "\n", "Status page domain")
        updates.update(last_published_at=now, last_hash=digest, last_error="")
        ok = True
    except PublishError as exc:
        updates["last_error"] = str(exc)[:300]
        logger.warning("Publishing status page %s failed: %s", page.name, exc)
        ok = False
    StatusPage.objects.filter(pk=page.pk).update(**updates)
    return ok


def publish_due(now=None) -> int:
    """Pages whose public content changed (or that were asked to publish). Returns count."""
    now = now or timezone.now()
    done = 0
    for page in StatusPage.objects.filter(publish_kind=PublishKind.GITHUB):
        failing = (
            page.last_error
            and page.last_attempt_at
            and now - page.last_attempt_at < RETRY_FAILED_EVERY
        )
        if failing and not page.needs_publish:
            continue  # a bad token or repo: don't hammer GitHub every two minutes
        snap, _, _ = render(page, now)
        stale = page.last_published_at is None or now - page.last_published_at >= REFRESH_EVERY
        if page.needs_publish or stale or content_hash(snap) != page.last_hash:
            done += publish(page, now)
    return done


def requested() -> bool:
    """Someone posted an incident or pressed Publish: don't wait for the 2-minute round."""
    return StatusPage.objects.filter(publish_kind=PublishKind.GITHUB, needs_publish=True).exists()
