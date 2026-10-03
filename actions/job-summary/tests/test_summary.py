"""Tests for summary.py. Run: python3 -m unittest discover -s actions/job-summary/tests"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))

import summary  # noqa: E402


def fx(name: str) -> str:
    return os.path.join(FIXTURES, name)


def render(**env):
    return summary.render({f"JS_{k.upper()}": v for k, v in env.items()})


class Headline(unittest.TestCase):
    def test_explicit_status_wins(self):
        md, out = render(title="Deploy — dev", status="failure")
        self.assertTrue(md.startswith("### ❌ Deploy — dev"))
        self.assertEqual(out["overall"], "failure")

    def test_auto_without_signals_is_success(self):
        md, out = render(title="Lint")
        self.assertTrue(md.startswith("### ✅ Lint"))
        self.assertEqual(out["overall"], "success")

    def test_needs_failure_drives_auto(self):
        with open(fx("needs.json")) as fh:
            md, out = render(title="Pipeline", needs=fh.read())
        self.assertEqual(out["overall"], "failure")
        self.assertIn("| deploy | ✅ ok |", md)
        self.assertIn("| migrations | ⏭️ not run |", md)
        self.assertIn("| e2e | ❌ failed |", md)

    def test_all_skipped_needs(self):
        _, out = render(title="x", needs='{"a": {"result": "skipped"}}')
        self.assertEqual(out["overall"], "skipped")

    def test_cancelled_needs(self):
        _, out = render(title="x", needs='{"a": {"result": "success"}, "b": {"result": "cancelled"}}')
        self.assertEqual(out["overall"], "cancelled")

    def test_bad_needs_is_a_warning_not_a_crash(self):
        md, out = render(title="x", needs="not json")
        self.assertIn("could not read `needs`", md)
        self.assertEqual(out["overall"], "success")


class Rows(unittest.TestCase):
    def test_url_becomes_link_and_value_becomes_code(self):
        md, _ = render(title="x", rows="URL: https://app.example.test/path\nStage: dev\nEmpty:")
        self.assertIn("| **URL** | [app.example.test/path](https://app.example.test/path) |", md)
        self.assertIn("| **Stage** | `dev` |", md)
        self.assertIn("| **Empty** | — |", md)

    def test_pipes_and_backticks_are_escaped(self):
        md, _ = render(title="x", rows="Odd: a|b`c")
        self.assertIn("| **Odd** | `a\\|b'c` |", md)

    def test_markdown_links_and_icons_pass_through(self):
        md, _ = render(title="x", rows="Release: [v1.2.0](https://example.test/r)\nGate: ✅ passed")
        self.assertIn("| **Release** | [v1.2.0](https://example.test/r) |", md)
        self.assertIn("| **Gate** | ✅ passed |", md)


class JUnit(unittest.TestCase):
    def test_counts_and_failures_across_formats(self):
        md, out = render(title="CI", junit=f"{fx('pytest-fail.xml')}\n{fx('jest-pass.xml')}, {fx('vitest-error.xml')}")
        self.assertEqual((out["passed"], out["failed"], out["skipped"]), ("4", "2", "1"))
        self.assertEqual(out["overall"], "failure")
        self.assertIn("❌ **Unit tests** — 4 passed · 2 failed · 1 skipped", md)
        self.assertIn("- `tests.test_handler::test_broken`", md)
        self.assertIn("- `src/lib/format.test.ts::formats dates`", md)

    def test_glob(self):
        _, out = render(title="CI", junit=os.path.join(FIXTURES, "jest-*.xml"))
        self.assertEqual(out["passed"], "2")

    def test_failure_list_is_capped(self):
        md, _ = render(title="CI", junit=fx("pytest-fail.xml") + "," + fx("vitest-error.xml"), max_failures="1")
        self.assertIn("…and 1 more", md)

    def test_missing_and_broken_files_warn(self):
        md, out = render(title="CI", junit=f"{fx('nope-*.xml')}")
        self.assertIn("no JUnit file matched", md)
        md, out = render(title="CI", junit=fx("broken.xml"))
        self.assertIn("could not read JUnit file", md)
        self.assertEqual(out["overall"], "success")


class Playwright(unittest.TestCase):
    def test_stats_and_failed_specs(self):
        md, out = render(title="E2E", playwright_json=fx("playwright.json"))
        self.assertEqual((out["passed"], out["failed"], out["skipped"]), ("7", "2", "3"))
        self.assertIn("❌ **E2E tests** — 7 passed · 2 failed · 3 skipped · 1 flaky", md)
        self.assertIn("**❌ login.spec.ts › rejects bad password**", md)
        self.assertIn("**❌ login.spec.ts › sso › redirects to provider**", md)
        self.assertIn("_No error message in the report._", md)

    def test_error_message_location_and_timeout_from_a_real_report(self):
        md, out = render(title="E2E", playwright_json=fx("playwright-errors.json"))
        self.assertEqual((out["passed"], out["failed"], out["skipped"]), ("1", "2", "1"))
        self.assertIn("**❌ sample.spec.js › orders api › lists orders** — `sample.spec.js:6`", md)
        self.assertIn("Error: GET /orders should succeed", md)
        self.assertIn("Expected: 200\nReceived: 500", md)   # ANSI colours stripped
        self.assertNotIn("\x1b[", md)
        self.assertIn("**❌ sample.spec.js › orders api › times out** — `sample.spec.js:8` · timed out", md)
        self.assertIn("Test timeout of 2000ms exceeded.", md)

    def test_run_error_is_a_failure_even_with_zero_stats(self):
        md, out = render(title="E2E", playwright_json=fx("playwright-run-error.json"))
        self.assertEqual(out["overall"], "failure")
        self.assertIn("❌ **E2E tests** — the run failed before any test finished", md)
        self.assertIn("**❌ Run error** — `broken.spec.js:1`", md)
        self.assertIn("SyntaxError:", md)
        self.assertNotIn("/home/runner/work/app/app/e2e/broken.spec.js:", md.split("```")[0])

    def test_long_messages_are_truncated(self):
        md, _ = render(title="E2E", playwright_json=fx("playwright-errors.json"), max_error_lines="2")
        self.assertIn("more line(s) — see the full report", md)


class TerraformPlan(unittest.TestCase):
    def test_sums_plans_strips_ansi_and_flags_destroy(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-plan.log"))
        self.assertIn("**Plan** — 2 to add · 1 to change · 1 to destroy (3 plan(s), 1 without changes)", md)
        self.assertIn("destroys resources", md)

    def test_apply(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-apply.log"))
        self.assertIn("**Applied** — 2 added · 1 changed · 0 destroyed (1 apply(s))", md)

    def test_resource_table_sorted_by_risk_with_component(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-plan.log"))
        table = md[md.index("| Change | Resource | Component |"):]
        self.assertLess(table.index("🗑️ destroy"), table.index("♻️ replace"))
        self.assertLess(table.index("♻️ replace"), table.index("🔄 update"))
        self.assertLess(table.index("🔄 update"), table.index("➕ create"))
        self.assertIn("| ➕ create | `module.api.aws_lambda_function.handler` | `components/api` |", md)
        self.assertIn("| 🗑️ destroy | `aws_sqs_queue.legacy` | `components/queue` |", md)

    def test_apply_log_does_not_double_count_resources(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-apply.log"))
        self.assertEqual(md.count("`aws_s3_bucket.assets`"), 1)

    def test_resource_table_is_capped(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-plan.log"), max_plan_resources="1")
        self.assertIn("…and 3 more", md)

    def test_log_without_plan(self):
        md, _ = render(title="Infra", tf_plan=fx("needs.json"))
        self.assertIn("no plan or apply result found", md)


class Redaction(unittest.TestCase):
    def test_account_ids_arns_and_tokens_never_render(self):
        text = ("arn:aws:iam::123456789012:role/deploy failed for 123456789012; "
                "Authorization: Bearer abcdefghijklmnop12345 ; build 1.123456789012.3")
        out = summary.redact(text)
        self.assertNotIn("123456789012:role", out)
        self.assertIn("arn:…", out)
        self.assertIn("for ‹account›", out)
        self.assertNotIn("abcdefghijklmnop12345", out)

    def test_plan_addresses_are_redacted(self):
        md, _ = render(title="Infra", tf_plan=fx("terragrunt-plan.log"))
        self.assertNotIn("210987654321", md)


class Script(unittest.TestCase):
    def test_writes_summary_and_outputs_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            step_summary = os.path.join(tmp, "summary.md")
            output = os.path.join(tmp, "output")
            env = dict(os.environ, GITHUB_STEP_SUMMARY=step_summary, GITHUB_OUTPUT=output,
                       JS_TITLE="Run", JS_JUNIT=fx("pytest-fail.xml"))
            proc = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "summary.py")], env=env)
            self.assertEqual(proc.returncode, 0)
            with open(step_summary) as fh:
                self.assertIn("### ❌ Run", fh.read())
            with open(output) as fh:
                self.assertIn("failed=1", fh.read())

    def test_unexpected_error_exits_zero(self):
        env = dict(os.environ, JS_TITLE="Run", JS_MAX_FAILURES="not-a-number")
        proc = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "summary.py")],
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("::warning", proc.stdout)


if __name__ == "__main__":
    unittest.main()
