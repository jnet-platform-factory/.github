# job-summary

Appends a short, consistent summary to a job's summary page, so you can see what a run did
without opening the logs. It shows:

- a result headline (✅ / ❌ / 🚫 / ⏭️);
- a key/value table for stage, URL, version and similar;
- a per-job results table;
- unit test counts with the failing test names;
- E2E (Playwright) counts with **each failing test's error message and location**, plus
  any error that broke the run before tests finished;
- Terraform/Terragrunt plan and apply counts, and **every resource change** (address and
  action, riskiest first).

The action **never fails the job it runs in**. A file it can't read becomes a warning row,
and an unexpected error becomes a `::warning::` annotation. It needs only `python3` and
`bash`, which GitHub-hosted Ubuntu and macOS runners already have.

## Usage

Run the action as the last step, with `if: always()` so failed runs get a summary too:

```yaml
- name: Summary
  if: always()
  continue-on-error: true
  uses: jnet-platform-factory/.github/actions/job-summary@v1
  with:
    title: Deploy — ${{ inputs.stage }}
    status: ${{ job.status }}
    rows: |
      Stage: ${{ inputs.stage }}
      URL: ${{ steps.deploy.outputs.url }}
      Commit: ${{ github.sha }}
    junit: reports/junit-*.xml
```

At the end of a pipeline, a final job can summarize all the jobs before it:

```yaml
summary:
  needs: [build, deploy, e2e]
  if: always()
  runs-on: ubuntu-latest
  steps:
    - uses: jnet-platform-factory/.github/actions/job-summary@v1
      continue-on-error: true
      with:
        title: Release ${{ github.ref_name }}
        needs: ${{ toJSON(needs) }}
```

## Inputs

| input             | default |                                                                                                                                                                            |
| ----------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `title`           | —       | Required. Heading text. In a matrix job, include the leg (app, service), because each leg renders its own section.                                                         |
| `status`          | `auto`  | `success`, `failure`, `cancelled` or `skipped`. `auto` derives it from `needs` and from failed tests. Inside a job, pass `${{ job.status }}`.                              |
| `rows`            |         | `Key: value` lines. URLs render as links, Markdown links and values starting with ✅/❌/⏭️/🚫/⚠️ pass through, and everything else renders as code. Empty values show `—`. |
| `needs`           |         | `${{ toJSON(needs) }}`. Shows each job's result.                                                                                                                           |
| `junit`           |         | Globs of JUnit XML files (pytest `--junitxml`, jest-junit, vitest `--reporter=junit`, …), separated by newlines or commas.                                                 |
| `playwright-json` |         | Glob of a Playwright JSON report (`--reporter=json`, `PLAYWRIGHT_JSON_OUTPUT_NAME`).                                                                                       |
| `tf-plan`         |         | Globs of Terraform/Terragrunt logs. Every `Plan:` and `Apply complete!` line is summed, and any destroy is flagged.                                                        |
| `markdown`        |         | Extra Markdown appended as-is.                                                                                                                                             |
| `max-failures` | `10` | How many failing tests (with their error) to show. |
| `max-error-lines` | `20` | Lines of each error message before truncating. |
| `max-plan-resources` | `50` | Resource changes listed from `tf-plan` before truncating. |

Outputs: `overall`, `passed`, `failed`, `skipped`.

## Rules for callers

- **Never pass secrets, tokens, credentials, account IDs or ARNs.** Anyone who can read the
  repository can read its job summaries.
- Always use `if: always()` plus `continue-on-error: true`. Use `if: always()` on the step so
  failed runs still get a summary, and `continue-on-error` so the summary can never decide the
  outcome.
- Link out (URL, release, artifact) rather than pasting logs.

## Redaction

AWS account IDs, ARNs and bearer/token values are redacted from error messages and resource
addresses before they are rendered. The plan lists only resource addresses and actions,
never the attribute diff, because diffs carry ARNs, IDs and configuration values. This is
defence in depth, not a licence to pass sensitive values.

## Versioning

Callers pin `@v1`, a moving major tag. Changes land on `main` through a pull request, the
self-test workflow renders every input on a real runner, and `v1` is then moved on purpose:

```sh
git tag -f v1 <commit> && git push -f origin v1
```

Tags can't be protected on this organization's plan, so anyone with write access can move
`v1`. If you need an immutable reference, pin the full commit SHA instead.

## Development

```sh
python3 -m unittest discover -s actions/job-summary/tests -v
JS_TITLE=Local JS_JUNIT='actions/job-summary/tests/fixtures/*.xml' python3 actions/job-summary/summary.py
```
