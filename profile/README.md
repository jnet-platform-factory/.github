# Hector

Head of Engineering & Product Development at [JunctionNet AI](https://junctionnet.ai). Amsterdam.

I build event-driven platforms on AWS: Lambda, EventBridge, SAM, Terraform.

## Three layers of a platform

Every multi-account AWS platform I build has the same three layers. Each layer has one job
and an open-source starting point in [**jnet-platform-factory**](https://github.com/jnet-platform-factory).

| Layer            | The job                                                                         | Start here                                                                                                                                                                                                        |
| ---------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1 · Account**  | Make a fresh account deployable, with no long-lived keys                        | [aws-account-bootstrap](https://github.com/jnet-platform-factory/aws-account-bootstrap)                                                                                                                           |
| **2 · Platform** | Give every tenant the same foundation: Terragrunt, events, monitoring, config   | [aws-event-driven-platform-blueprint](https://github.com/jnet-platform-factory/aws-event-driven-platform-blueprint) · [Terraform modules](https://github.com/orgs/jnet-platform-factory/repositories?q=terraform) |
| **3 · Service**  | Keep each Lambda small, with logging, errors and telemetry in shared middleware | [python-lambda-common](https://github.com/jnet-platform-factory/python-lambda-common)                                                                                                                             |

**Seeing what's running:**
[every EventBridge event in OpenSearch](https://github.com/jnet-platform-factory/aws-eventbridge-firehose-opensearch-forwarder),
and [a daily health email for each account](https://github.com/jnet-platform-factory/aws-daily-monitoring-report).
