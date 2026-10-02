"""
CDK Stack for the DevOps Agent Auto-Remediation pipeline.

Creates:
  1. devops-agent-trigger              — Parser Lambda (EventBridge → parse summary → invoke durable)
  2. devops-agent-remediation-durable  — Durable Orchestrator Lambda (Bedrock Converse + human approval)
  3. devops-agent-lambda-tool          — Tool Lambda (update/get Lambda config)
  4. EventBridge rule                  — source=aws.aidevops, detail-type=Investigation Completed
"""

import json
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
)
from cdk_nag import NagSuppressions
from constructs import Construct


class DevOpsRemediationStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ---------------------------------------------------------------
        # 3. Tool Lambda — devops-agent-lambda-tool
        # ---------------------------------------------------------------

        tool_lambda = lambda_.Function(
            self, "ToolLambda",
            function_name="devops-agent-lambda-tool",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="update_lambda_config.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/tools"),
            timeout=Duration.seconds(15),
            memory_size=128,
            description="Tool Lambda: update/get Lambda function configuration",
        )

        tool_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:UpdateFunctionConfiguration",
                    "lambda:GetFunctionConfiguration",
                ],
                resources=["arn:aws:lambda:*:*:function:*"],
            )
        )

        # ---------------------------------------------------------------
        # 2. Durable Orchestrator Lambda — devops-agent-remediation-durable
        # ---------------------------------------------------------------

        # Tool registry: tool name = service_operation (underscore-separated).
        # The tool name IS the identifier everywhere: Bedrock, ALLOWED_ACTIONS, READ_ONLY_TOOLS.
        # To add a new tool: add an entry here, create its Tool Lambda, and grant invoke.
        tool_registry = {
            "lambda_update_function_configuration": {
                "lambda_function_name": tool_lambda.function_name,
                "description": (
                    "Update an AWS Lambda function's configuration including "
                    "timeout, memory size, environment variables, and runtime."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "FunctionName": {"type": "string", "description": "The name or ARN of the Lambda function"},
                        "Timeout": {"type": "integer", "description": "Function timeout in seconds (1-900)"},
                        "MemorySize": {"type": "integer", "description": "Function memory in MB (128-10240)"},
                        "Environment": {
                            "type": "object",
                            "description": "Environment variable configuration",
                            "properties": {
                                "Variables": {"type": "object", "additionalProperties": {"type": "string"}},
                            },
                        },
                        "Runtime": {"type": "string", "description": "Lambda runtime identifier"},
                    },
                    "required": ["FunctionName"],
                },
            },
            "lambda_get_function_configuration": {
                "lambda_function_name": tool_lambda.function_name,
                "description": "Get the current configuration of an AWS Lambda function.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "FunctionName": {"type": "string", "description": "The name or ARN of the Lambda function"},
                    },
                    "required": ["FunctionName"],
                },
            },
        }

        allowed_actions = json.dumps([
            "lambda_update_function_configuration",
            "lambda_get_function_configuration",
        ])

        read_only_tools = json.dumps([
            "lambda_get_function_configuration",
        ])

        durable_lambda = lambda_.Function(
            self, "DurableOrchestratorLambda",
            function_name="devops-agent-remediation-durable",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="remediation_durable_lambda.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/orchestrator"),
            timeout=Duration.seconds(120),
            memory_size=256,
            durable_config=lambda_.DurableConfig(
                execution_timeout=Duration.hours(24),
                retention_period=Duration.days(7),
            ),
            environment={
                "TOOL_REGISTRY": json.dumps(tool_registry),
                "ALLOWED_ACTIONS": allowed_actions,
                "READ_ONLY_TOOLS": read_only_tools,
                "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
            },
            description="Durable Orchestrator: Bedrock Converse API tool use loop with human approval",
        )

        # Durable execution checkpoint permissions
        durable_lambda.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicDurableExecutionRolePolicy"
            )
        )

        # Bedrock invoke permission
        durable_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )

        # Permission to invoke the tool Lambda
        tool_lambda.grant_invoke(durable_lambda)

        # Publish a version and create alias (durable functions require qualified ARN)
        durable_version = durable_lambda.current_version
        durable_alias = lambda_.Alias(
            self, "DurableProdAlias",
            alias_name="prod",
            version=durable_version,
        )

        # ---------------------------------------------------------------
        # 1. Parser Lambda — devops-agent-trigger
        # ---------------------------------------------------------------

        trigger_lambda = lambda_.Function(
            self, "TriggerLambda",
            function_name="devops-agent-trigger",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/trigger"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "REMEDIATION_FUNCTION_NAME": durable_alias.function_arn,
            },
            description="Parser Lambda: parse EventBridge DevOps Agent events and trigger remediation",
        )

        trigger_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["aidevops:ListJournalRecords"],
                resources=["*"],
            )
        )

        # Permission to invoke the durable Lambda (via alias)
        durable_alias.grant_invoke(trigger_lambda)

        # ---------------------------------------------------------------
        # EventBridge Rule
        # ---------------------------------------------------------------

        rule = events.Rule(
            self, "DevOpsAgentInvestigationRule",
            rule_name="devops-agent-investigation-completed",
            event_pattern=events.EventPattern(
                source=["aws.aidevops"],
                detail_type=["Investigation Completed"],
            ),
            description="Triggers parser Lambda when DevOps Agent completes an investigation",
        )

        rule.add_target(targets.LambdaFunction(trigger_lambda))

        # ---------------------------------------------------------------
        # cdk-nag suppressions (AwsSolutions rule pack)
        #
        # Each suppression documents why an accepted finding is safe. They are
        # scoped with `applies_to` so only the specific policy/resource is
        # suppressed, never a blanket mute of the rule.
        # ---------------------------------------------------------------

        # IAM4 — Lambda execution roles rely on the AWS-managed basic execution
        # policies. These grant only CloudWatch Logs access (and durable-execution
        # checkpointing for the orchestrator) and are the standard, low-risk
        # baseline for Lambda logging.
        NagSuppressions.add_resource_suppressions(
            tool_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWS-managed AWSLambdaBasicExecutionRole provides only CloudWatch Logs write access, the standard Lambda logging baseline.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            durable_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWS-managed AWSLambdaBasicDurableExecutionRolePolicy is required by Lambda Durable Functions for execution checkpointing and log access.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy"
                    ],
                }
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            trigger_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "AWS-managed AWSLambdaBasicExecutionRole provides only CloudWatch Logs write access, the standard Lambda logging baseline.",
                    "appliesTo": [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                }
            ],
            apply_to_children=True,
        )

        # IAM5 — Wildcards that are inherent to the actions/resources involved.
        NagSuppressions.add_resource_suppressions(
            tool_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "The Tool Lambda remediates arbitrary Lambda functions flagged by DevOps Agent investigations; the target function is not known at deploy time, so get/update configuration is scoped to Lambda functions in the account/region.",
                    "appliesTo": ["Resource::arn:aws:lambda:*:*:function:*"],
                }
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            durable_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "bedrock:InvokeModel targets a cross-region inference profile whose underlying foundation-model ARNs span multiple regions and cannot be enumerated as a fixed resource list.",
                    "appliesTo": ["Resource::*"],
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "CDK grant_invoke adds a trailing ':*' to cover the Tool Lambda's aliases/versions; invocation is still scoped to the single Tool Lambda function.",
                    "appliesTo": ["Resource::<ToolLambda828E5CE0.Arn>:*"],
                },
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            trigger_lambda.role,
            [
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "aidevops:ListJournalRecords does not support resource-level permissions and must be granted on '*'.",
                    "appliesTo": ["Resource::*"],
                }
            ],
            apply_to_children=True,
        )

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------

        CfnOutput(self, "TriggerLambdaArn", value=trigger_lambda.function_arn)
        CfnOutput(self, "DurableAliasArn", value=durable_alias.function_arn)
        CfnOutput(self, "ToolLambdaArn", value=tool_lambda.function_arn)
