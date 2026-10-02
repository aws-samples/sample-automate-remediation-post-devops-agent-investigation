# DevOps Agent Remediation — Project Rules

## Project Context
This is a CDK project that deploys an automated remediation pipeline for AWS DevOps Agent.
The stack creates 3 Lambda functions, 1 EventBridge rule, and associated IAM roles.

## AWS Conventions
- Region: use the region from the user's AWS CLI configuration unless specified.
- All Lambda functions use Python 3.14 runtime.
- The CDK stack name is `DevOpsRemediationStack`.

## Deployment
- Always use CDK for deploying and destroying the stack (`cdk deploy`, `cdk destroy`).
- Run `cdk bootstrap` before the first deployment in a new environment.
- Use a Python virtual environment (`.venv/`) for CDK dependencies.

## Demo Incident Setup
- The test function name is `devops-agent-timeout`.
- The test IAM role name is `devops-agent-timeout-role`.
- The test function must have `--timeout 3` and code that sleeps for 10 seconds.
- Use Python 3.14 runtime for the test function.
- After deployment, invoke the function once to generate timeout errors in CloudWatch.

## Safety
- The remediation pipeline uses a curated allowlist of tools. Do not bypass the allowlist.
- Mutating actions require human approval via Lambda Durable Functions callbacks.
- Read-only tools (e.g., `lambda_get_function_configuration`) run autonomously without approval.
- When cleaning up, always destroy the CDK stack first, then delete the test function and its IAM role.

## Code Style
- Python Lambda handlers follow the pattern: `def lambda_handler(event, context)`.
- Use structured logging with `logging.getLogger(__name__)`.
- Tool names use `service_operation` format (e.g., `lambda_update_function_configuration`).
- Environment variables drive configuration — no hardcoded values in Lambda code.
