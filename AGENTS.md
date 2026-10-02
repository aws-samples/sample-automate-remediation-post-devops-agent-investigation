# Agent Instructions

This repository contains an automated remediation pipeline for AWS DevOps Agent. It uses AWS Lambda Durable Functions, Amazon Bedrock, and Amazon EventBridge to transform DevOps Agent investigation summaries into pre-validated fixes with human-in-the-loop approval.

## Architecture

- **devops-agent-trigger** — Lambda function triggered by EventBridge when DevOps Agent completes an investigation. Fetches the investigation summary and invokes the durable orchestrator.
- **devops-agent-remediation-durable** — Durable Lambda that runs an agentic loop: sends investigation context to Amazon Bedrock, executes approved tools, and suspends for human approval on mutating actions.
- **devops-agent-lambda-tool** — Tool Lambda that executes scoped AWS API calls (get/update Lambda configuration). Bedrock can only invoke tools from a curated allowlist.
- **EventBridge rule** — Captures `Investigation Completed` events from `aws.aidevops` source.

## Project Structure

```
├── app.py                              # CDK app entry point
├── cdk.json                            # CDK configuration
├── requirements.txt                    # Python dependencies (aws-cdk-lib, constructs)
├── devops_remediation/
│   └── devops_remediation_stack.py     # CDK stack definition (all resources)
└── lambdas/
    ├── trigger/
    │   └── lambda_function.py          # EventBridge trigger Lambda
    ├── orchestrator/
    │   └── remediation_durable_lambda.py  # Durable orchestrator Lambda
    └── tools/
        └── update_lambda_config.py     # Tool Lambda for Lambda config operations
```

## Deployment

### Prerequisites
- Python 3.14
- AWS CDK CLI (`npm install -g aws-cdk`)
- AWS credentials configured
- An active AWS DevOps Agent space

### Steps

1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Bootstrap CDK (first time only):
   ```bash
   cdk bootstrap
   ```

4. Deploy:
   ```bash
   cdk deploy
   ```

## Demo: Simulating an Incident

To test the end-to-end flow, create a Lambda function that always times out:

1. Create a file `lambda_function.py` with a function that sleeps for 10 seconds.
2. Create an IAM execution role `devops-agent-timeout-role` with `AWSLambdaBasicExecutionRole`.
3. Deploy the function `devops-agent-timeout` with `--timeout 3` and Python 3.14 runtime.
4. Invoke the function to generate timeout errors.
5. Open AWS DevOps Agent and investigate: "What is happening with the devops-agent-timeout function?"

The investigation completion triggers the remediation pipeline automatically.

## Cleanup

1. `cdk destroy` to remove the remediation stack.
2. Delete the test function: `aws lambda delete-function --function-name devops-agent-timeout`
3. Delete the IAM role and CloudWatch log group for the test function:
   ```bash
   aws iam detach-role-policy \
     --role-name devops-agent-timeout-role \
     --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

   aws iam delete-role --role-name devops-agent-timeout-role

   aws logs delete-log-group --log-group-name /aws/lambda/devops-agent-timeout
   ```

## Adding New Remediation Tools

The orchestrator is generic. To add a new tool:

1. Create a new Tool Lambda under `lambdas/tools/`.
2. Add the tool to the `tool_registry` dict in `devops_remediation_stack.py`.
3. Add the tool name to `ALLOWED_ACTIONS` and optionally `READ_ONLY_TOOLS`.
4. Grant the durable Lambda permission to invoke the new Tool Lambda.
5. Deploy with `cdk deploy`.

No changes to the orchestrator code are needed.
