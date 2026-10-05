# jira-comment

Keeps **one living CI/CD comment** per Jira issue × repository × app × environment, instead of
a new comment on every pipeline run.

A green run after a green run **edits** the existing comment in place, without notifying
watchers. A new comment is posted only when something changed:

| Earlier comment for this repo/app/environment | This run                     | Result                                     |
| --------------------------------------------- | ---------------------------- | ------------------------------------------ |
| none                                          | anything                     | **new** comment (first deploy)             |
| any                                           | `mode: milestone`            | **new** comment, every time                |
| any                                           | `status: failure`            | **new** comment naming the failed steps    |
| failure                                       | `status: success`            | **new** "♻️ recovered" comment             |
| success / recovered / removed                 | `status: success`            | earlier comment **edited**                 |
| any                                           | `status: removed` (teardown) | earlier comment **edited** to "🧹 removed" |
| any                                           | `status: cancelled`          | nothing (a superseded run is not news)     |

A green comment is one line, plus an optional `details` line:

```
🔌 API · orders-api — DEV ✅ deployed · https://orders.dev.example.com · run #123 · 2026-10-05 14:02 UTC
infra ✅ · migrations ✅
```

`run #123` is a link to the run. Skipped steps are never listed. A failure adds one line per
failed step (`• Unit tests: ❌ failed`). The full step breakdown belongs in the job summary
(see [`job-summary`](../job-summary)).

The action **never fails the job**. A Jira or network error becomes a `::warning::` and the
step exits 0. It needs only `python3` and `bash`, which GitHub-hosted runners already have.

## Usage

```yaml
- name: Jira
  if: always()
  continue-on-error: true
  uses: jnet-platform-factory/.github/actions/jira-comment@v0.1.0
  with:
    issue-keys: ${{ needs.configure.outputs.issue_key }}
    jira-url: ${{ vars.JIRA_URL }}
    jira-email: ${{ vars.JIRA_API_TOKEN_EMAIL }}
    jira-token: ${{ secrets.JIRA_API_TOKEN }}
    environment: DEV
    status: ${{ needs.deploy.result == 'success' && 'success' || 'failure' }}
    url: ${{ needs.deploy.outputs.url }}
    failed-steps: |
      Deploy: ${{ needs.deploy.result }}
```

Production deploys and release tags are history people want to see, so post them as milestones:

```yaml
with:
  issue-keys: ${{ needs.release.outputs.issue_keys }} # every ticket in the release
  environment: PRODUCTION
  mode: milestone
```

## Inputs

| input           | default                 |                                                                                                  |
| --------------- | ----------------------- | ------------------------------------------------------------------------------------------------ |
| `issue-keys`    | —                       | Required. Keys separated by spaces, commas or newlines. Case-insensitive. Empty skips the step.  |
| `jira-url`      | —                       | Required. `https://<site>.atlassian.net`.                                                        |
| `jira-email`    | —                       | Required. Account the API token belongs to.                                                      |
| `jira-token`    | —                       | Required. Jira API token. An empty email or token skips the step with a warning.                 |
| `environment`   |                         | `DEV`, `PREVIEW`, `PR CI`, `PRE-PRODUCTION`, `PRODUCTION`, `RELEASE`, … Part of the identity.    |
| `status`        | `success`               | `success`, `failure`, `removed` or `cancelled`.                                                  |
| `mode`          | `living`                | `living` or `milestone`.                                                                         |
| `label`         | repo (app)              | Headline prefix, e.g. `🔌 API · orders-api`.                                                     |
| `app`           |                         | App within the repo. Part of the identity, so each app keeps its own comment.                    |
| `repo`          | current repository      | Repository name used in the identity.                                                            |
| `url`           |                         | Deployed URL shown on the headline.                                                              |
| `summary`       | deployed/failed/removed | Word after the status icon (`passed`, `ready`, …).                                               |
| `failed-steps`  |                         | `Step: result` lines; shown only on failure.                                                     |
| `details`       |                         | One extra line. Markdown links allowed.                                                          |
| `body`          |                         | Freeform text replacing the templated headline. The label is prefixed and the run link appended. |
| `transition-to` |                         | Target status name to move each issue to. Skipped with a warning when not available.             |
| `property-key`  | `jnet-cicd`             | Jira comment property that tags the living comment.                                              |
| `key-pattern`   | `[A-Z][A-Z0-9]+-[0-9]+` | Regex a key must match after upper-casing, e.g. `CRGN-[0-9]+`.                                   |
| `run-url`       | this run                | Link behind `run #N`.                                                                            |

Outputs: `action` (`posted`, `updated`, `skipped` or `warned`, for the first key), `comment-id`
and `results` (`KEY:action` for every key).

## How the living comment is found

Each comment the action writes carries a Jira **comment property** (`jnet-cicd` by default):

```json
{
  "v": 1,
  "repo": "orders-api",
  "app": "",
  "environment": "DEV",
  "status": "success",
  "run_id": "…",
  "run_number": "123",
  "sha": "…",
  "updated": "2026-10-05T14:02:00Z"
}
```

On each run the action lists the issue's newest 100 comments with their properties. When
Jira leaves the properties off that list, it reads them through `POST /rest/api/3/comment/list`
for the comments this account wrote. It then picks the newest comment whose `repo`, `app` and
`environment` match. Edits use `notifyUsers=false`. When the comment was deleted in the
meantime, the action posts a new one.

The token's account needs _Add comments_ and _Edit own comments_ on the project, plus
_Transition issues_ when you use `transition-to`. The action only ever edits comments that
account wrote.

## Rules for callers

- Use `continue-on-error: true` on the step as well, so a broken runner image can't decide the
  outcome either.
- Pass `status: cancelled` (or skip the step) for runs a newer push superseded. They are not news.
- Pin a tag. Every tag of this repository is immutable by convention, and a missing tag fails
  the whole job at "Set up job".

## Tests

```sh
python3 -m unittest discover -s actions/jira-comment/tests -v
```

`tests/fake_jira.py` is an in-memory Jira used by the unit tests and the self-test workflow.
