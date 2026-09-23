# Platform Factory

Reusable AWS and CI building blocks, published from a private monorepo.

Each repository here is a **generated mirror** of a narrow, declared set of
files — a template, its source, a licence, a readme, and nothing else.
Development happens privately; a release regenerates the mirror, so what you
read here is exactly what the corresponding published version ships.

## What that means if you are reading one of these

- **They do not take pull requests.** The next release would overwrite the
  merge. Issues are read and are the way to reach us.
- **A version is never replaced.** Serverless Application Repository versions
  are immutable and Terraform Registry tags are not re-ingested when moved, so
  a fix is always a new version.
- **Nothing here is specific to us.** Anything account-, tenant- or
  hostname-shaped is a parameter with no default. If you find one that is not,
  please report it — see [SECURITY.md](../SECURITY.md).

## Licence

Apache-2.0 unless a repository says otherwise.
