"""Tests for jira_comment.py. Run: python3 -m unittest discover -s actions/jira-comment/tests"""

import contextlib
import datetime as dt
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_jira  # noqa: E402
import jira_comment as jc  # noqa: E402

NOW = dt.datetime(2026, 10, 5, 14, 2, tzinfo=dt.timezone.utc)
RUN = "https://github.com/acme/documents-manager-api/actions/runs/42"


def base_env(**over):
    env = {
        "GITHUB_REPOSITORY": "acme/documents-manager-api",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_NUMBER": "123",
        "GITHUB_SHA": "abc123",
        "JC_ISSUE_KEYS": "jnet-1",
        "JC_JIRA_EMAIL": "ci@example.com",
        "JC_JIRA_TOKEN": "t0ken",
        "JC_ENVIRONMENT": "DEV",
        "JC_STATUS": "success",
        "JC_LABEL": "🔌 API · documents-manager-api",
        "JC_URL": "https://docs.dev.example.com",
    }
    env.update({k if k.startswith(("JC_", "GITHUB_")) else f"JC_{k.upper()}": v for k, v in over.items()})
    return env


def text_of(adf: dict) -> str:
    out = []
    for node in adf["content"][0]["content"]:
        out.append("\n" if node["type"] == "hardBreak" else node["text"])
    return "".join(out)


class Keys(unittest.TestCase):
    def test_split_upper_dedupe(self):
        self.assertEqual(jc.parse_keys("jnet-1, CRGN-2 jnet-1\nJNET-3"), (["JNET-1", "CRGN-2", "JNET-3"], []))

    def test_invalid_keys_reported(self):
        self.assertEqual(jc.parse_keys("JNET-1 main feature/x"), (["JNET-1"], ["main", "feature/x"]))

    def test_custom_pattern(self):
        self.assertEqual(jc.parse_keys("CRGN-1 JNET-2", r"CRGN-[0-9]+"), (["CRGN-1"], ["JNET-2"]))


class Decide(unittest.TestCase):
    def test_table(self):
        cases = [
            # prior,     status,      mode,        expected
            (None, "success", "living", ("post", "first")),
            (None, "failure", "living", ("post", "first")),
            (None, "removed", "living", ("post", "first")),
            ("success", "success", "living", ("put", "update")),
            ("success", "failure", "living", ("post", "failure")),
            ("failure", "failure", "living", ("post", "failure")),
            ("failure", "success", "living", ("post", "recovered")),
            ("success", "removed", "living", ("put", "removed")),
            ("failure", "removed", "living", ("put", "removed")),
            ("removed", "success", "living", ("put", "update")),
            ("success", "success", "milestone", ("post", "milestone")),
            (None, "failure", "milestone", ("post", "milestone")),
            ("success", "cancelled", "living", ("skip", "cancelled")),
            (None, "cancelled", "milestone", ("skip", "cancelled")),
        ]
        for prior, status, mode, expected in cases:
            with self.subTest(prior=prior, status=status, mode=mode):
                self.assertEqual(jc.decide(prior, status, mode), expected)


class Render(unittest.TestCase):
    def cfg(self, **over):
        return jc.load_config(base_env(**over))

    def test_success_is_one_line(self):
        lines = jc.render_lines(self.cfg(), "update", NOW)
        self.assertEqual(
            lines,
            [
                "🔌 API · documents-manager-api — DEV ✅ deployed · https://docs.dev.example.com"
                f" · [run #123]({RUN}) · 2026-10-05 14:02 UTC"
            ],
        )

    def test_success_with_details_is_two_lines_and_ignores_failed_steps(self):
        lines = jc.render_lines(
            self.cfg(details="infra ✅ · migrations ✅", failed_steps="E2E: skipped"), "update", NOW
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "infra ✅ · migrations ✅")
        self.assertNotIn("not run", "\n".join(lines))

    def test_failure_names_failed_steps(self):
        lines = jc.render_lines(
            self.cfg(status="failure", failed_steps="Deploy: failure\nE2E: cancelled\nVerify"), "failure", NOW
        )
        self.assertIn("DEV ❌ failed", lines[0])
        self.assertEqual(lines[1:], ["• Deploy: ❌ failed", "• E2E: 🚫 cancelled", "• Verify: ❌ failed"])

    def test_recovered(self):
        self.assertIn("DEV ♻️ recovered", jc.render_lines(self.cfg(), "recovered", NOW)[0])

    def test_removed_and_custom_summary(self):
        self.assertIn("DEV 🧹 removed", jc.render_lines(self.cfg(status="removed"), "removed", NOW)[0])
        self.assertIn("PR CI ✅ passed", jc.render_lines(self.cfg(environment="PR CI", summary="passed"), "x", NOW)[0])

    def test_default_label_uses_repo_and_app(self):
        lines = jc.render_lines(self.cfg(label="", app="udm"), "update", NOW)
        self.assertTrue(lines[0].startswith("documents-manager-api (udm) — DEV"))

    def test_body_keeps_text_adds_label_and_run(self):
        lines = jc.render_lines(self.cfg(body="🏷️ Included in release tag v1.2.3.\\nSecond line"), "milestone", NOW)
        self.assertEqual(lines[0], "🔌 API · documents-manager-api")
        self.assertEqual(lines[1], "🏷️ Included in release tag v1.2.3.")
        self.assertEqual(lines[2], f"Second line · [run #123]({RUN})")

    def test_body_with_label_and_run_url_is_untouched(self):
        body = f"🔌 API · documents-manager-api\nDeployed (Pipeline: {RUN})"
        self.assertEqual(jc.render_lines(self.cfg(body=body), "milestone", NOW), body.splitlines())


class Adf(unittest.TestCase):
    def test_links_and_breaks(self):
        adf = jc.to_adf(["see [run #1](https://x.test/r/1) at https://app.test/a.", "two"])
        nodes = adf["content"][0]["content"]
        self.assertEqual(nodes[0], {"type": "text", "text": "see "})
        self.assertEqual(nodes[1]["text"], "run #1")
        self.assertEqual(nodes[1]["marks"][0]["attrs"]["href"], "https://x.test/r/1")
        self.assertEqual(nodes[3]["text"], "https://app.test/a")
        self.assertEqual(nodes[4], {"type": "text", "text": "."})
        self.assertEqual(nodes[5], {"type": "hardBreak"})
        self.assertEqual(text_of(adf), "see run #1 at https://app.test/a.\ntwo")


class EndToEnd(unittest.TestCase):
    expand_supported = True

    def setUp(self):
        self.state = fake_jira.State(expand_supported=self.expand_supported)
        self.server = fake_jira.serve(self.state)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_jc(self, **over):
        env = base_env(JC_JIRA_URL=self.url, **over)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = jc.run(env, now=NOW)
        out["log"] = buf.getvalue()
        return out

    def comments(self, key="JNET-1"):
        return self.state.comments.get(key, [])

    def test_three_green_runs_leave_one_comment(self):
        self.assertEqual(self.run_jc()["action"], "posted")
        self.assertEqual(self.run_jc(GITHUB_RUN_NUMBER="124")["action"], "updated")
        out = self.run_jc(GITHUB_RUN_NUMBER="125")
        self.assertEqual(out["action"], "updated")
        self.assertEqual(len(self.comments()), 1)
        c = self.comments()[0]
        self.assertIn("run #125", text_of(c["body"]))
        self.assertEqual(c["notifyUsers"], "false")
        self.assertEqual(c["properties"][0]["value"]["run_number"], "125")

    def test_failure_then_recovery_then_quiet(self):
        self.run_jc()
        self.assertEqual(self.run_jc(JC_STATUS="failure", JC_FAILED_STEPS="Unit tests: failure")["action"], "posted")
        self.assertEqual(len(self.comments()), 2)
        self.assertIn("• Unit tests: ❌ failed", text_of(self.comments()[1]["body"]))
        self.assertEqual(self.run_jc()["action"], "posted")
        self.assertIn("♻️ recovered", text_of(self.comments()[2]["body"]))
        self.assertEqual(self.run_jc()["action"], "updated")
        self.assertEqual(len(self.comments()), 3)

    def test_environments_repos_and_apps_are_separate(self):
        self.run_jc()
        self.run_jc(JC_ENVIRONMENT="PREVIEW")
        self.run_jc(GITHUB_REPOSITORY="acme/other-api")
        self.run_jc(JC_APP="udm")
        self.assertEqual(len(self.comments()), 4)
        self.assertEqual(self.run_jc(JC_APP="udm")["action"], "updated")
        self.assertEqual(len(self.comments()), 4)

    def test_milestones_always_post(self):
        self.run_jc(JC_MODE="milestone", JC_ENVIRONMENT="PRODUCTION")
        self.run_jc(JC_MODE="milestone", JC_ENVIRONMENT="PRODUCTION")
        self.assertEqual(len(self.comments()), 2)

    def test_removed_edits_the_living_comment(self):
        self.run_jc()
        self.assertEqual(self.run_jc(JC_STATUS="removed")["action"], "updated")
        self.assertIn("🧹 removed", text_of(self.comments()[0]["body"]))

    def test_cancelled_does_nothing(self):
        self.assertEqual(self.run_jc(JC_STATUS="cancelled")["action"], "skipped")
        self.assertEqual(self.comments(), [])

    def test_deleted_comment_falls_back_to_post(self):
        self.run_jc()
        cid = self.comments()[0]["id"]
        self.state.put_404 = True  # a human deleted the comment after it was listed
        out = self.run_jc()
        self.assertEqual(out["action"], "posted")
        self.assertIn("is gone", out["log"])
        self.assertNotEqual(out["comment-id"], cid)

    def test_human_comments_are_ignored(self):
        self.state.add("JNET-1", jc.to_adf(["a human wrote this"]), author=fake_jira.OTHER)
        self.run_jc()
        self.run_jc()
        self.assertEqual(len(self.comments()), 2)

    def test_multiple_keys(self):
        out = self.run_jc(JC_ISSUE_KEYS="JNET-1 jnet-2")
        self.assertEqual(out["results"], "JNET-1:posted JNET-2:posted")
        self.assertEqual(len(self.comments("JNET-2")), 1)

    def test_server_errors_warn_and_never_raise(self):
        self.state.fail_all = True
        out = self.run_jc()
        self.assertEqual(out["action"], "warned")
        self.assertIn("::warning", out["log"])

    def test_missing_token_skips(self):
        out = self.run_jc(JC_JIRA_TOKEN="")
        self.assertEqual(out["action"], "skipped")
        self.assertEqual(self.state.log, [])

    def test_no_keys_skips(self):
        self.assertEqual(self.run_jc(JC_ISSUE_KEYS="  ")["action"], "skipped")

    def test_transition(self):
        out = self.run_jc(JC_TRANSITION_TO="testing")
        self.assertEqual(self.state.statuses["JNET-1"], "Testing")
        self.assertIn("moved to", out["log"])
        out = self.run_jc(JC_TRANSITION_TO="Nowhere")
        self.assertIn("no transition to 'Nowhere'", out["log"])

    def test_token_never_logged(self):
        self.state.fail_all = True
        self.assertNotIn("t0ken", self.run_jc()["log"])


class EndToEndWithoutExpand(EndToEnd):
    """Jira ignores expand=properties on the issue comment list: use /comment/list."""

    expand_supported = False

    def test_uses_comment_list(self):
        self.run_jc()
        self.run_jc()
        self.assertTrue(any(line.startswith("POST /rest/api/3/comment/list") for line in self.state.log))
        self.assertEqual(len(self.comments()), 1)


class Main(unittest.TestCase):
    def test_main_writes_outputs_and_exits_zero(self):
        import tempfile

        with tempfile.NamedTemporaryFile("r", suffix=".out", delete=False) as fh:
            path = fh.name
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update({"GITHUB_OUTPUT": path, "JC_ISSUE_KEYS": ""})
            self.assertEqual(jc.main(), 0)
        finally:
            os.environ.clear()
            os.environ.update(old)
        with open(path) as fh:
            self.assertIn("action=skipped", fh.read())
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
