#!/usr/bin/env python3
"""Keep one living CI/CD comment per Jira issue × repo × app × environment.

Reads its inputs from JC_* environment variables (set by action.yml). For each
issue key it finds the newest comment this pipeline left for the same repo,
app and environment (tagged with a Jira comment property), then either edits
that comment in place or posts a new one:

  * edit   — a green run after a green run, and a teardown ("removed");
  * post   — the first run, every failure, the first green run after a
             failure ("recovered"), and every milestone (mode=milestone).

It never fails the job: a Jira or network problem becomes a ::warning::
annotation and the script exits 0.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_PROPERTY_KEY = "jnet-cicd"
DEFAULT_KEY_PATTERN = r"[A-Z][A-Z0-9]+-[0-9]+"
TIMEOUT = 20

STATUS_ICON = {"success": "✅", "failure": "❌", "removed": "🧹"}
DEFAULT_SUMMARY = {"success": "deployed", "failure": "failed", "removed": "removed"}
STEP_RESULT = {"failure": "❌ failed", "cancelled": "🚫 cancelled", "timed_out": "⏱️ timed out"}

LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)|(https?://[^\s<>]+)")
TRAILING_PUNCT = ".,;:!?)"


class JiraError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str = ""):
        super().__init__(f"{method} {path} → HTTP {status} {detail}".strip())
        self.status = status


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass
class Config:
    issue_keys: list[str]
    jira_url: str
    jira_email: str
    jira_token: str
    environment: str
    status: str
    mode: str
    repo: str
    app: str
    label: str
    url: str
    summary: str
    failed_steps: list[str]
    details: str
    body: str
    property_key: str
    transition_to: str
    run_url: str
    run_number: str
    run_id: str
    sha: str
    invalid_keys: list[str] = field(default_factory=list)


def _env(env: dict, name: str, default: str = "") -> str:
    return (env.get(f"JC_{name}") or default).strip()


def parse_keys(raw: str, pattern: str = DEFAULT_KEY_PATTERN) -> tuple[list[str], list[str]]:
    """Split, upper-case and de-duplicate issue keys. Returns (valid, invalid)."""
    rx = re.compile(rf"^(?:{pattern})$")
    valid: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[\s,]+", raw or ""):
        if not token:
            continue
        key = token.upper()
        if not rx.match(key):
            invalid.append(token)
        elif key not in valid:
            valid.append(key)
    return valid, invalid


def load_config(env: dict) -> Config:
    repo_full = env.get("GITHUB_REPOSITORY", "")
    repo = _env(env, "REPO") or repo_full.split("/")[-1]
    run_id = env.get("GITHUB_RUN_ID", "")
    default_run_url = ""
    if repo_full and run_id:
        server = env.get("GITHUB_SERVER_URL", "https://github.com")
        default_run_url = f"{server}/{repo_full}/actions/runs/{run_id}"
    keys, invalid = parse_keys(_env(env, "ISSUE_KEYS"), _env(env, "KEY_PATTERN") or DEFAULT_KEY_PATTERN)
    status = _env(env, "STATUS", "success").lower()
    # The body may carry literal "\n" from older callers that escaped newlines.
    body = (env.get("JC_BODY") or "").replace("\\n", "\n").strip("\n")
    return Config(
        issue_keys=keys,
        invalid_keys=invalid,
        jira_url=_env(env, "JIRA_URL").rstrip("/"),
        jira_email=_env(env, "JIRA_EMAIL"),
        jira_token=_env(env, "JIRA_TOKEN"),
        environment=_env(env, "ENVIRONMENT"),
        status=status,
        mode=_env(env, "MODE", "living").lower(),
        repo=repo,
        app=_env(env, "APP"),
        label=_env(env, "LABEL"),
        url=_env(env, "URL"),
        summary=_env(env, "SUMMARY"),
        failed_steps=[s.strip() for s in (env.get("JC_FAILED_STEPS") or "").splitlines() if s.strip()],
        details=" ".join((env.get("JC_DETAILS") or "").split()),
        body=body,
        property_key=_env(env, "PROPERTY_KEY") or DEFAULT_PROPERTY_KEY,
        transition_to=_env(env, "TRANSITION_TO"),
        run_url=_env(env, "RUN_URL") or default_run_url,
        run_number=_env(env, "RUN_NUMBER") or env.get("GITHUB_RUN_NUMBER", ""),
        run_id=run_id,
        sha=env.get("GITHUB_SHA", ""),
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def decide(prior_status: str | None, status: str, mode: str) -> tuple[str, str]:
    """Return (operation, kind). operation is post | put | skip.

    kind names the comment: first, milestone, failure, recovered, update,
    removed, or cancelled (skip).
    """
    if status == "cancelled":
        return "skip", "cancelled"
    if mode == "milestone":
        return "post", "milestone"
    if prior_status is None:
        return "post", "first"
    if status == "failure":
        return "post", "failure"
    if status == "removed":
        return "put", "removed"
    if prior_status == "failure":
        return "post", "recovered"
    return "put", "update"


def identity(cfg: Config) -> dict:
    return {"repo": cfg.repo, "app": cfg.app, "environment": cfg.environment.upper()}


def matches(value: object, ident: dict) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("repo") == ident["repo"]
        and (value.get("app") or "") == ident["app"]
        and str(value.get("environment") or "").upper() == ident["environment"]
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def default_label(cfg: Config) -> str:
    return cfg.repo + (f" ({cfg.app})" if cfg.app else "")


def run_link(cfg: Config) -> str:
    if not cfg.run_url:
        return ""
    text = f"run #{cfg.run_number}" if cfg.run_number else "run"
    return f"[{text}]({cfg.run_url})"


def render_lines(cfg: Config, kind: str, now: dt.datetime | None = None) -> list[str]:
    """Render the comment as plain lines; links stay in [text](url) form."""
    label = cfg.label or default_label(cfg)
    if cfg.body:
        lines = cfg.body.splitlines()
        if not lines[0].startswith(label):
            lines.insert(0, label)
        link = run_link(cfg)
        if link and cfg.run_url not in cfg.body:
            lines[-1] = f"{lines[-1]} · {link}"
        return lines

    now = now or dt.datetime.now(dt.timezone.utc)
    if kind == "recovered":
        state = "♻️ recovered"
    else:
        icon = STATUS_ICON.get(cfg.status, "ℹ️")
        state = f"{icon} {cfg.summary or DEFAULT_SUMMARY.get(cfg.status, cfg.status)}"
    head = f"{label} — {cfg.environment} {state}" if cfg.environment else f"{label} — {state}"
    parts = [head]
    if cfg.url:
        parts.append(cfg.url)
    link = run_link(cfg)
    if link:
        parts.append(link)
    parts.append(now.strftime("%Y-%m-%d %H:%M UTC"))
    lines = [" · ".join(parts)]

    if cfg.status == "failure":
        for step in cfg.failed_steps:
            name, _, result = step.partition(":")
            result = result.strip().lower() or "failure"
            lines.append(f"• {name.strip()}: {STEP_RESULT.get(result, result)}")
    if cfg.details:
        lines.append(cfg.details)
    return lines


def _inline(text: str) -> list[dict]:
    nodes: list[dict] = []
    pos = 0
    for m in LINK.finditer(text):
        label, href, bare = m.group(1), m.group(2), m.group(3)
        end = m.end()
        if bare:
            stripped = bare.rstrip(TRAILING_PUNCT)
            end -= len(bare) - len(stripped)
            label = href = stripped
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos : m.start()]})
        nodes.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": href}}]})
        pos = end
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return nodes


def to_adf(lines: list[str]) -> dict:
    """One paragraph, lines joined by hardBreaks, links as link marks."""
    content: list[dict] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str):
        self.base = base_url
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._me: str | None = None

    def request(self, method: str, path: str, body: object = None, query: dict | None = None) -> object:
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("errorMessages", [""])[0]
            except Exception:  # noqa: BLE001 — the status code is what matters
                pass
            raise JiraError(method, path, exc.code, detail) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise JiraError(method, path, 0, str(exc)) from None
        return json.loads(raw) if raw.strip() else None

    def myself(self) -> str:
        if self._me is None:
            self._me = (self.request("GET", "/rest/api/3/myself") or {}).get("accountId", "")
        return self._me

    def find_prior(self, key: str, prop_key: str, ident: dict) -> tuple[str, dict] | None:
        """Newest comment tagged with prop_key for this identity, as (id, value)."""
        page = self.request(
            "GET",
            f"/rest/api/3/issue/{key}/comment",
            query={"orderBy": "-created", "maxResults": 100, "expand": "properties"},
        )
        comments = (page or {}).get("comments", [])
        if comments and not any("properties" in c for c in comments):
            # `properties` is only a documented expand on /comment/list, so look
            # up this account's own comments there.
            me = self.myself()
            ids = [int(c["id"]) for c in comments if (c.get("author") or {}).get("accountId") == me]
            if not ids:
                return None
            listed = self.request(
                "POST", "/rest/api/3/comment/list", body={"ids": ids}, query={"expand": "properties"}
            )
            comments = (listed or {}).get("values", [])
        comments = sorted(comments, key=lambda c: c.get("created", ""), reverse=True)
        for c in comments:
            for prop in c.get("properties") or []:
                if prop.get("key") == prop_key and matches(prop.get("value"), ident):
                    return str(c["id"]), prop["value"]
        return None

    def set_property(self, comment_id: str, prop_key: str, value: dict) -> None:
        self.request("PUT", f"/rest/api/3/comment/{comment_id}/properties/{prop_key}", body=value)

    def post(self, key: str, adf: dict) -> str:
        created = self.request("POST", f"/rest/api/3/issue/{key}/comment", body={"body": adf})
        return str((created or {}).get("id", ""))

    def put(self, key: str, comment_id: str, adf: dict) -> None:
        self.request(
            "PUT",
            f"/rest/api/3/issue/{key}/comment/{comment_id}",
            body={"body": adf},
            query={"notifyUsers": "false"},
        )

    def transition(self, key: str, target: str) -> str:
        data = self.request("GET", f"/rest/api/3/issue/{key}/transitions") or {}
        want = target.lower()
        for t in data.get("transitions", []):
            if str((t.get("to") or {}).get("name", "")).lower() == want:
                self.request("POST", f"/rest/api/3/issue/{key}/transitions", body={"transition": {"id": t["id"]}})
                return "moved"
        return "unavailable"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def warn(msg: str) -> None:
    print(f"::warning title=jira-comment::{msg}")


def property_value(cfg: Config, now: dt.datetime) -> dict:
    stored = "success" if cfg.status not in ("failure", "removed") else cfg.status
    return {
        "v": 1,
        **identity(cfg),
        "status": stored,
        "run_id": cfg.run_id,
        "run_number": cfg.run_number,
        "sha": cfg.sha,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def process_key(client: JiraClient, cfg: Config, key: str, now: dt.datetime) -> tuple[str, str]:
    """Comment on one issue. Returns (action, comment_id)."""
    ident = identity(cfg)
    prior = None
    if cfg.mode != "milestone":
        try:
            prior = client.find_prior(key, cfg.property_key, ident)
        except JiraError as exc:
            warn(f"{key}: could not look up the earlier comment ({exc}); posting a new one.")

    op, kind = decide(prior[1].get("status") if prior else None, cfg.status, cfg.mode)
    if op == "skip":
        print(f"{key}: run was {cfg.status}; leaving the ticket alone.")
        return "skipped", ""

    adf = to_adf(render_lines(cfg, kind, now))
    value = property_value(cfg, now)
    if op == "put" and prior:
        try:
            client.put(key, prior[0], adf)
            client.set_property(prior[0], cfg.property_key, value)
            print(f"{key}: updated comment {prior[0]} ({kind}).")
            return "updated", prior[0]
        except JiraError as exc:
            if exc.status != 404:
                raise
            print(f"{key}: comment {prior[0]} is gone; posting a new one.")
    comment_id = client.post(key, adf)
    if comment_id:
        client.set_property(comment_id, cfg.property_key, value)
    print(f"{key}: posted comment {comment_id} ({kind}).")
    return "posted", comment_id


def run(env: dict, client: JiraClient | None = None, now: dt.datetime | None = None) -> dict:
    cfg = load_config(env)
    now = now or dt.datetime.now(dt.timezone.utc)
    for bad in cfg.invalid_keys:
        warn(f"'{bad}' is not a Jira issue key; skipping it.")
    if not cfg.issue_keys:
        print("No Jira issue keys — nothing to comment on.")
        return {"action": "skipped", "comment-id": "", "results": ""}
    if not (cfg.jira_url and cfg.jira_email and cfg.jira_token):
        warn("jira-url, jira-email or jira-token is empty — skipping the Jira update.")
        return {"action": "skipped", "comment-id": "", "results": ""}

    client = client or JiraClient(cfg.jira_url, cfg.jira_email, cfg.jira_token)
    results = []
    for key in cfg.issue_keys:
        action, comment_id = "warned", ""
        try:
            action, comment_id = process_key(client, cfg, key, now)
        except JiraError as exc:
            warn(f"{key}: Jira comment failed ({exc}) — continuing.")
        except Exception as exc:  # noqa: BLE001 — never fail the build
            warn(f"{key}: unexpected error ({type(exc).__name__}: {exc}) — continuing.")
        if cfg.transition_to:
            try:
                if client.transition(key, cfg.transition_to) == "moved":
                    print(f"{key}: moved to '{cfg.transition_to}'.")
                else:
                    warn(f"{key}: no transition to '{cfg.transition_to}' from its current status.")
            except JiraError as exc:
                warn(f"{key}: transition to '{cfg.transition_to}' failed ({exc}) — continuing.")
        results.append((key, action, comment_id))

    first = results[0]
    return {
        "action": first[1],
        "comment-id": first[2],
        "results": " ".join(f"{k}:{a}" for k, a, _ in results),
    }


def main() -> int:
    try:
        outputs = run(dict(os.environ))
    except Exception as exc:  # noqa: BLE001 — never fail the build
        warn(f"jira-comment failed ({type(exc).__name__}: {exc}); the job is unaffected.")
        outputs = {"action": "warned", "comment-id": "", "results": ""}
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in outputs.items():
                fh.write(f"{k}={v}\n")
    else:
        print(json.dumps(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
