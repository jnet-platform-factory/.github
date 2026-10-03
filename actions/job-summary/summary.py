#!/usr/bin/env python3
"""Render a GitHub Actions job summary from a run's results.

Reads its inputs from JS_* environment variables (set by action.yml), appends
Markdown to $GITHUB_STEP_SUMMARY and writes outputs to $GITHUB_OUTPUT. Without
those variables it prints to stdout, which is how the tests and a local run
use it.

It never fails the job: anything it cannot read becomes a warning row, and an
unexpected error becomes a ::warning:: annotation with exit code 0.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

STATUS_HEADLINE = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "🚫",
    "skipped": "⏭️",
}

RESULT_LABEL = {
    "success": "✅ ok",
    "failure": "❌ failed",
    "cancelled": "🚫 cancelled",
    "skipped": "⏭️ not run",
}

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PLAN = re.compile(r"Plan: (\d+) to add, (\d+) to change, (\d+) to destroy")
APPLIED = re.compile(r"Resources: (\d+) added, (\d+) changed, (\d+) destroyed")
NO_CHANGES = re.compile(r"No changes\. ")
RESOURCE = re.compile(
    r"#\s+(\S+)\s+(will be created|will be destroyed|will be updated in-place"
    r"|must be replaced|is tainted, so must be replaced)"
)
COMPONENT = re.compile(r"\[([^\]]+)\]\s+(?:terraform|tofu):")
ACTION = {
    "will be destroyed": "destroy",
    "must be replaced": "replace",
    "is tainted, so must be replaced": "replace",
    "will be updated in-place": "update",
    "will be created": "create",
}
ACTION_LABEL = {"destroy": "🗑️ destroy", "replace": "♻️ replace", "update": "🔄 update", "create": "➕ create"}
ACTION_ORDER = ["destroy", "replace", "update", "create"]

# Summaries are readable by anyone with repo access, so error messages and
# resource addresses never carry account ids, ARNs or bearer tokens.
REDACT = [
    (re.compile(r"arn:aws[a-z-]*:[^\s\"'`\]\)>,]*"), "arn:…"),
    (re.compile(r"(?<![\d.])\d{12}(?![\d.])"), "‹account›"),
    (re.compile(r"(?i)\b(bearer|token|authorization)([\s:=\"']+)[A-Za-z0-9._~+/=-]{8,}"), r"\1\2‹redacted›"),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACT:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class Failure:
    name: str
    message: str
    where: str = ""
    status: str = ""


@dataclass
class Tests:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    flaky: int = 0
    files: int = 0
    failures: list[str] = field(default_factory=list)
    # Failed tests with their error message, and errors that broke the run
    # itself (a spec that does not compile, a failing global setup).
    errors: list[Failure] = field(default_factory=list)
    run_errors: list[Failure] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def broken(self) -> bool:
        return bool(self.failed or self.run_errors)


@dataclass
class Plan:
    plans: int = 0
    no_changes: int = 0
    add: int = 0
    change: int = 0
    destroy: int = 0
    applies: int = 0
    added: int = 0
    changed: int = 0
    destroyed: int = 0
    resources: list[tuple[str, str, str]] = field(default_factory=list)  # (action, address, component)


# --------------------------------------------------------------------------- inputs


def expand(patterns: str) -> list[str]:
    """Expand a newline- or comma-separated list of globs into existing files."""
    found: list[str] = []
    for raw in re.split(r"[\n,]", patterns or ""):
        pattern = raw.strip()
        if not pattern:
            continue
        for path in sorted(glob.glob(pattern, recursive=True)):
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def parse_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            rows.append((line.strip(), ""))
            continue
        rows.append((key.strip(), value.strip()))
    return rows


def parse_needs(text: str) -> dict[str, str]:
    if not (text or "").strip():
        return {}
    data = json.loads(text)
    return {name: (job or {}).get("result", "") for name, job in data.items()}


def parse_junit(paths: list[str], warnings: list[str], max_failures: int) -> Tests:
    tests = Tests()
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            warnings.append(f"could not read JUnit file `{path}` ({exc.__class__.__name__})")
            continue
        tests.files += 1
        for case in root.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                tests.failed += 1
                if len(tests.failures) < max_failures:
                    name = case.get("name", "?")
                    owner = case.get("classname") or case.get("file") or ""
                    tests.failures.append(f"{owner}::{name}" if owner else name)
            elif case.find("skipped") is not None:
                tests.skipped += 1
            else:
                tests.passed += 1
    return tests


def _walk_specs(suite: dict, prefix: str = ""):
    title = suite.get("title", "")
    path = f"{prefix} › {title}" if prefix and title else (title or prefix)
    for spec in suite.get("specs", []):
        yield path, spec
    for child in suite.get("suites", []):
        yield from _walk_specs(child, path)


def parse_playwright(paths: list[str], warnings: list[str], max_failures: int) -> Tests:
    tests = Tests()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError) as exc:
            warnings.append(f"could not read Playwright report `{path}` ({exc.__class__.__name__})")
            continue
        tests.files += 1
        stats = report.get("stats", {})
        tests.passed += int(stats.get("expected", 0))
        tests.failed += int(stats.get("unexpected", 0))
        tests.flaky += int(stats.get("flaky", 0))
        tests.skipped += int(stats.get("skipped", 0))
        roots = [os.environ.get("GITHUB_WORKSPACE", ""), (report.get("config") or {}).get("rootDir", "")]
        for error in report.get("errors", []):
            if len(tests.run_errors) < max_failures:
                tests.run_errors.append(
                    Failure("Run error", error.get("message", ""), _location(error.get("location"), roots))
                )
        for suite in report.get("suites", []):
            for where, spec in _walk_specs(suite):
                if spec.get("ok", True) or len(tests.failures) >= max_failures:
                    continue
                name = f"{where} › {spec.get('title', '?')}".strip(" ›")
                tests.failures.append(name)
                tests.errors.append(_spec_failure(name, spec, roots))
    return tests


def _location(location: dict | None, roots: list[str]) -> str:
    if not location or not location.get("file"):
        return ""
    path = location["file"]
    for root in roots:
        if root and path.startswith(root.rstrip("/") + "/"):
            path = path[len(root.rstrip("/")) + 1 :]
            break
    return f"{path}:{location['line']}" if location.get("line") else path


def _spec_failure(name: str, spec: dict, roots: list[str]) -> Failure:
    """The error of the last failing attempt (after retries) of a failed spec."""
    for test in spec.get("tests", []):
        for result in reversed(test.get("results", [])):
            if result.get("status") in ("passed", "skipped"):
                continue
            error = result.get("error") or next(iter(result.get("errors") or []), {})
            where = _location(error.get("location") or result.get("errorLocation"), roots)
            return Failure(name, error.get("message", ""), where or _spec_location(spec, roots),
                           result.get("status", ""))
    return Failure(name, "", _spec_location(spec, roots), "failed")


def _spec_location(spec: dict, roots: list[str]) -> str:
    """Where the test is declared; timeouts carry no error location of their own."""
    return _location({"file": spec.get("file"), "line": spec.get("line")}, roots)


def parse_plan(paths: list[str], warnings: list[str]) -> Plan:
    plan = Plan()
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            warnings.append(f"could not read plan log `{path}` ({exc.__class__.__name__})")
            continue
        for line in lines:
            line = ANSI.sub("", line)
            if m := RESOURCE.search(line):
                component = c.group(1) if (c := COMPONENT.search(line)) else ""
                entry = (ACTION[m.group(2)], redact(m.group(1)), component)
                if entry not in plan.resources:  # an apply log repeats its plan
                    plan.resources.append(entry)
            elif m := PLAN.search(line):
                plan.plans += 1
                plan.add += int(m.group(1))
                plan.change += int(m.group(2))
                plan.destroy += int(m.group(3))
            elif m := APPLIED.search(line):
                plan.applies += 1
                plan.added += int(m.group(1))
                plan.changed += int(m.group(2))
                plan.destroyed += int(m.group(3))
            elif NO_CHANGES.search(line):
                plan.no_changes += 1
    return plan


# --------------------------------------------------------------------------- render


def cell(value: str) -> str:
    """Format a table value: URLs and Markdown links stay clickable, the rest is code."""
    value = value.replace("|", "\\|").replace("\n", " ")
    if not value:
        return "—"
    if re.match(r"^https?://\S+$", value):
        return f"[{re.sub(r'^https?://', '', value)}]({value})"
    if "](" in value or value.startswith(("✅", "❌", "⏭️", "🚫", "⚠️")):
        return value
    return "`" + value.replace("`", "'") + "`"


def overall_status(explicit: str, needs: dict[str, str], tests: list[Tests]) -> str:
    explicit = (explicit or "auto").strip().lower()
    if explicit and explicit != "auto":
        return explicit
    results = list(needs.values())
    if any(r == "failure" for r in results) or any(t.broken for t in tests):
        return "failure"
    if any(r == "cancelled" for r in results):
        return "cancelled"
    if results and all(r == "skipped" for r in results):
        return "skipped"
    return "success"


def test_line(label: str, tests: Tests) -> str:
    if tests.run_errors and not tests.total:
        return f"❌ **{label}** — the run failed before any test finished"
    icon = "❌" if tests.broken else "✅"
    parts = [f"{tests.passed} passed", f"{tests.failed} failed", f"{tests.skipped} skipped"]
    if tests.flaky:
        parts.append(f"{tests.flaky} flaky")
    return f"{icon} **{label}** — " + " · ".join(parts)


def code_block(text: str, max_lines: int) -> list[str]:
    text = redact(ANSI.sub("", text)).strip("\n")
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip() or (lines and lines[-1].strip()):  # collapse blank runs
            lines.append(line.rstrip())
    hidden = len(lines) - max_lines
    if hidden > 0:
        lines = lines[:max_lines] + [f"… {hidden} more line(s) — see the full report"]
    fence = "```"
    while fence in text:
        fence += "`"
    return [fence + "text", *lines, fence]


def failure_block(failure: Failure, max_lines: int) -> list[str]:
    head = f"**❌ {failure.name}**"
    if failure.where:
        head += f" — `{failure.where}`"
    if failure.status == "timedOut":
        head += " · timed out"
    if not failure.message.strip():
        return [head, "", "_No error message in the report._", ""]
    return [head, "", *code_block(failure.message, max_lines), ""]


def plan_table(plan: Plan, max_rows: int) -> list[str]:
    if not plan.resources:
        return []
    rows = sorted(plan.resources, key=lambda r: ACTION_ORDER.index(r[0]))
    with_component = any(r[2] for r in rows)
    out = ["| Change | Resource |" + (" Component |" if with_component else ""),
           "|---|---|" + ("---|" if with_component else "")]
    for action, address, component in rows[:max_rows]:
        out.append(f"| {ACTION_LABEL[action]} | `{address}` |" + (f" `{component}` |" if with_component else ""))
    if len(rows) > max_rows:
        out.append(f"| | …and {len(rows) - max_rows} more |" + (" |" if with_component else ""))
    return out + [""]


def render(env: dict[str, str]) -> tuple[str, dict[str, str]]:
    warnings: list[str] = []
    max_failures = int(env.get("JS_MAX_FAILURES") or 10)
    max_error_lines = int(env.get("JS_MAX_ERROR_LINES") or 20)
    max_plan_rows = int(env.get("JS_MAX_PLAN_RESOURCES") or 50)

    needs: dict[str, str] = {}
    try:
        needs = parse_needs(env.get("JS_NEEDS", ""))
    except ValueError:
        warnings.append("could not read `needs` (expected `${{ toJSON(needs) }}`)")

    unit = None
    if (env.get("JS_JUNIT") or "").strip():
        files = expand(env["JS_JUNIT"])
        if files:
            unit = parse_junit(files, warnings, max_failures)
        else:
            warnings.append(f"no JUnit file matched `{env['JS_JUNIT'].strip()}`")

    e2e = None
    if (env.get("JS_PLAYWRIGHT_JSON") or "").strip():
        files = expand(env["JS_PLAYWRIGHT_JSON"])
        if files:
            e2e = parse_playwright(files, warnings, max_failures)
        else:
            warnings.append(f"no Playwright report matched `{env['JS_PLAYWRIGHT_JSON'].strip()}`")

    plan = None
    if (env.get("JS_TF_PLAN") or "").strip():
        files = expand(env["JS_TF_PLAN"])
        if files:
            plan = parse_plan(files, warnings)
        else:
            warnings.append(f"no plan log matched `{env['JS_TF_PLAN'].strip()}`")

    test_sets = [t for t in (unit, e2e) if t is not None]
    status = overall_status(env.get("JS_STATUS", ""), needs, test_sets)
    headline = STATUS_HEADLINE.get(status, "ℹ️")
    title = (env.get("JS_TITLE") or "Job summary").strip()

    out: list[str] = [f"### {headline} {title}", ""]

    rows = parse_rows(env.get("JS_ROWS", ""))
    if rows:
        out += ["| | |", "|---|---|"]
        out += [f"| **{k}** | {cell(v)} |" for k, v in rows]
        out.append("")

    if needs:
        out += ["| Job | Result |", "|---|---|"]
        out += [f"| {name} | {RESULT_LABEL.get(r, r or '—')} |" for name, r in needs.items()]
        out.append("")

    for label, tests in (("Unit tests", unit), ("E2E tests", e2e)):
        if tests is None:
            continue
        out.append(test_line(label, tests))
        out.append("")
        for failure in tests.run_errors:
            out += failure_block(failure, max_error_lines)
        if tests.errors:
            for failure in tests.errors:
                out += failure_block(failure, max_error_lines)
        elif tests.failures:
            out += [f"- `{f}`" for f in tests.failures]
        hidden = tests.failed - len(tests.failures)
        if hidden > 0:
            out.append(f"- …and {hidden} more failing test(s)")
        if tests.failures or tests.run_errors:
            out.append("")

    if plan is not None:
        if plan.plans or plan.no_changes:
            warn = " ⚠️ **destroys resources**" if plan.destroy else ""
            out.append(
                f"**Plan** — {plan.add} to add · {plan.change} to change · "
                f"{plan.destroy} to destroy ({plan.plans + plan.no_changes} plan(s), "
                f"{plan.no_changes} without changes){warn}"
            )
        if plan.applies:
            out.append(
                f"**Applied** — {plan.added} added · {plan.changed} changed · "
                f"{plan.destroyed} destroyed ({plan.applies} apply(s))"
            )
        if not (plan.plans or plan.no_changes or plan.applies):
            out.append("**Plan** — no plan or apply result found in the log")
        out.append("")
        out += plan_table(plan, max_plan_rows)

    extra = (env.get("JS_MARKDOWN") or "").strip()
    if extra:
        out += [extra, ""]

    for w in warnings:
        out.append(f"> ⚠️ {w}")
    if warnings:
        out.append("")

    totals = Tests()
    for t in test_sets:
        totals.passed += t.passed
        totals.failed += t.failed
        totals.skipped += t.skipped
    outputs = {
        "overall": status,
        "passed": str(totals.passed),
        "failed": str(totals.failed),
        "skipped": str(totals.skipped),
    }
    return "\n".join(out) + "\n", outputs


def main() -> int:
    try:
        markdown, outputs = render(dict(os.environ))
    except Exception as exc:  # never fail the caller's job
        print(f"::warning title=job-summary::could not render the summary: {exc!r}")
        return 0

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    else:
        sys.stdout.write(markdown)

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
