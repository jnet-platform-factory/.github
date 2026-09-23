# Security policy

## Reporting a vulnerability

Please report security issues privately, **not** as a public issue.

Use GitHub's private vulnerability reporting on the affected repository:
**Security → Report a vulnerability**. If that is unavailable, open an issue
saying only that you have a security report and asking for a contact — no
details — and a maintainer will follow up.

Please include, where you can:

- which repository and version or commit
- what an attacker could do, not only what is wrong
- the smallest reproduction you have

We will acknowledge a report within five working days.

## What these repositories are

Every repository in this organization is a **published mirror**. Development
happens in a private repository, and the code here is generated from a narrow
allowlist of publishable files on each release. That has three consequences
worth knowing before you report something:

- **A fix lands upstream first**, then appears here on the next release. You
  will not see a commit on this side until then.
- **Pull requests cannot be merged**, because the next release would overwrite
  them. See the note below.
- **Published versions are immutable.** A Serverless Application Repository
  version cannot be withdrawn, and a Terraform Registry tag is not re-ingested
  if it moves. A fix is always a new version, never a replaced one.

## Scope

In scope: the code in these repositories, and anything it does by default.

Out of scope: our own infrastructure, which is not what these repositories
are. If you have found something suggesting otherwise — an internal hostname,
an account identifier, a credential of any kind, anywhere in a repository here
or in its history — that is exactly the sort of report we want, and it is in
scope.

## Contributing

These repositories do not take pull requests. This is not a judgement on the
contribution: the mirror is regenerated from the private source on every
release, so a merge here is reverted by the next one.

Issues are welcome and are read. A bug report, a missing parameter, or a
question about how something is meant to work all reach the people who can
change it upstream.
