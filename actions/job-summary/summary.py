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


@dataclass
class Tests:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    flaky: int = 0
    files: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


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
        for suite in report.get("suites", []):
            for where, spec in _walk_specs(suite):
                if spec.get("ok", True) or len(tests.failures) >= max_failures:
                    continue
                tests.failures.append(f"{where} › {spec.get('title', '?')}".strip(" ›"))
    return tests


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
            if m := PLAN.search(line):
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
    if any(r == "failure" for r in results) or any(t.failed for t in tests):
        return "failure"
    if any(r == "cancelled" for r in results):
        return "cancelled"
    if results and all(r == "skipped" for r in results):
        return "skipped"
    return "success"


def test_line(label: str, tests: Tests) -> str:
    icon = "❌" if tests.failed else "✅"
    parts = [f"{tests.passed} passed", f"{tests.failed} failed", f"{tests.skipped} skipped"]
    if tests.flaky:
        parts.append(f"{tests.flaky} flaky")
    return f"{icon} **{label}** — " + " · ".join(parts)


def render(env: dict[str, str]) -> tuple[str, dict[str, str]]:
    warnings: list[str] = []
    max_failures = int(env.get("JS_MAX_FAILURES") or 10)

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
        if tests.failures:
            out.append("")
            out += [f"- `{f}`" for f in tests.failures]
            hidden = tests.failed - len(tests.failures)
            if hidden > 0:
                out.append(f"- …and {hidden} more")
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
