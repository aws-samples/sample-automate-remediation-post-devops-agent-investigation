"""
Generic Durable Orchestrator Lambda for auto-remediation.

Uses AWS Lambda Durable Functions with human approval callbacks.
All tool definitions and routing are driven by environment variables,
making this orchestrator completely generic — add new tools by updating
config, no code changes needed.

Environment variables:
  TOOL_REGISTRY     — JSON object mapping tool names to their config:
                      {
                        "service_operation_name": {
                          "lambda_function_name": "my-tool-lambda",
                          "description": "What this tool does",
                          "input_schema": { ... JSON Schema ... }
                        }
                      }
                      Tool names use service_operation format (e.g., lambda_get_function_configuration)
  ALLOWED_ACTIONS   — JSON list of tool names that pass the allowlist
  READ_ONLY_TOOLS   — JSON list of tool names that skip human approval
  BEDROCK_MODEL_ID  — Bedrock model ID (default: eu.anthropic.claude-sonnet-4-20250514-v1:0)
  SYSTEM_PROMPT     — Optional custom system prompt for Bedrock
"""

import json
import logging
import os

import boto3
from aws_durable_execution_sdk_python import (
    DurableContext,
    StepContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import CallbackConfig, Duration

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-20250514-v1:0")

DEFAULT_SYSTEM_PROMPT = (
    "You are an automated remediation system. Analyze the investigation summary "
    "provided and use the available tools to fix the identified issues. "
    "First check the current state before making changes. "
    "After making changes, confirm what was done."
)
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


def load_tool_registry():
    """Load tool registry from TOOL_REGISTRY env var."""
    raw = os.environ.get("TOOL_REGISTRY", "{}")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse TOOL_REGISTRY: %s", raw)
        return {}


def load_allowed_actions():
    """Load allowed service:operation pairs from ALLOWED_ACTIONS env var."""
    raw = os.environ.get("ALLOWED_ACTIONS", "[]")
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse ALLOWED_ACTIONS: %s", raw)
        return set()


def load_read_only_tools():
    """Load read-only tool names from READ_ONLY_TOOLS env var."""
    raw = os.environ.get("READ_ONLY_TOOLS", "[]")
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse READ_ONLY_TOOLS: %s", raw)
        return set()


def build_tool_config(registry):
    """Build Bedrock toolConfig from the tool registry."""
    tools = []
    for tool_name, config in registry.items():
        tools.append({
            "toolSpec": {
                "name": tool_name,
                "description": config.get("description", f"Execute {tool_name}"),
                "inputSchema": {"json": config.get("input_schema", {"type": "object"})},
            }
        })
    return {"tools": tools}


def validate_tool_request(tool_name, registry, allowed_actions):
    """Check if a tool is in the registry and is allowed."""
    if tool_name not in registry:
        return False
    return tool_name in allowed_actions


def invoke_tool_lambda(function_name, payload):
    """Invoke a Tool Lambda synchronously and return the parsed response."""
    client = boto3.client("lambda")
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    return json.loads(response["Payload"].read())


@durable_step
def call_bedrock(step_context: StepContext, messages: list, tool_config: dict) -> dict:
    """Call Bedrock Converse API — checkpointed as a durable step."""
    step_context.logger.info("Calling Bedrock Converse API")
    client = boto3.client("bedrock-runtime")
    response = client.converse(
        modelId=MODEL_ID,
        messages=messages,
        system=[{"text": SYSTEM_PROMPT}],
        toolConfig=tool_config,
    )
    return response


@durable_step
def execute_tool(step_context: StepContext, tool_name: str, tool_input: dict, registry: dict) -> dict:
    """Execute a tool via its Tool Lambda — checkpointed as a durable step."""
    entry = registry.get(tool_name)
    if entry is None:
        return {"status": "error", "error_type": "UnknownTool", "error_message": f"Unknown tool: {tool_name}"}

    function_name = entry.get("lambda_function_name")
    if not function_name:
        return {"status": "error", "error_type": "NoLambda", "error_message": f"No Lambda configured for tool: {tool_name}"}

    step_context.logger.info("Invoking Tool Lambda: %s for tool: %s", function_name, tool_name)
    return invoke_tool_lambda(function_name, {"tool_name": tool_name, "parameters": tool_input})


@durable_execution
def lambda_handler(event: dict, context: DurableContext) -> dict:
    """Generic durable orchestrator with human approval for mutating actions."""
    execution_id = "unknown"
    try:
        investigation_summary = event.get("investigation_summary")
        metadata = event.get("metadata", {})
        execution_id = metadata.get("execution_id", "unknown")

        if not investigation_summary or not isinstance(investigation_summary, dict):
            context.logger.error("Missing or invalid investigation_summary")
            return {"statusCode": 400, "body": json.dumps({"error": "Missing investigation_summary"})}

        if "symptoms" not in investigation_summary or "findings" not in investigation_summary:
            context.logger.warning("Summary missing required fields, skipping remediation")
            return {"statusCode": 200, "body": json.dumps({"status": "skipped", "execution_id": execution_id})}

        # Load all config from environment
        registry = load_tool_registry()
        allowed_actions = load_allowed_actions()
        read_only_tools = load_read_only_tools()
        tool_config = build_tool_config(registry)

        if not registry:
            context.logger.error("TOOL_REGISTRY is empty, no tools available")
            return {"statusCode": 500, "body": json.dumps({"error": "No tools configured"})}

        actions = []
        messages = [
            {"role": "user", "content": [{"text": json.dumps(investigation_summary)}]}
        ]

        max_iterations = 10
        for iteration in range(1, max_iterations + 1):
            context.logger.info("Iteration %d, execution_id=%s", iteration, execution_id)

            # Step: call Bedrock (checkpointed)
            response = context.step(
                call_bedrock(messages, tool_config),
                name=f"bedrock-call-{iteration}",
            )

            assistant_message = response["output"]["message"]
            messages.append(assistant_message)

            # If no tool use, we're done
            if response["stopReason"] != "tool_use":
                summary_text = ""
                for block in assistant_message.get("content", []):
                    if "text" in block:
                        summary_text += block["text"] + "\n"
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "execution_id": execution_id,
                        "actions": actions,
                        "summary": summary_text.strip(),
                        "iterations": iteration,
                        "status": "completed",
                    }),
                }

            # Process tool use blocks
            tool_results = []
            for block in assistant_message.get("content", []):
                if "toolUse" not in block:
                    continue

                tool_use = block["toolUse"]
                tool_use_id = tool_use["toolUseId"]
                tool_name = tool_use["name"]
                tool_input = tool_use["input"]

                context.logger.info(
                    "Tool request: %s with params: %s, execution_id=%s",
                    tool_name, json.dumps(tool_input), execution_id,
                )

                # Check allowlist
                if not validate_tool_request(tool_name, registry, allowed_actions):
                    context.logger.warning("Rejected tool: %s", tool_name)
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": {"error": "Action not permitted"}}],
                            "status": "error",
                        }
                    })
                    actions.append({"tool_name": tool_name, "parameters": tool_input, "status": "rejected"})
                    continue

                # For mutating tools: require human approval via callback
                if tool_name not in read_only_tools:
                    context.logger.info(
                        "Requesting human approval for: %s %s",
                        tool_name, json.dumps(tool_input),
                    )

                    callback = context.create_callback(
                        name=f"approve-{tool_name}-{tool_use_id}",
                        config=CallbackConfig(timeout=Duration.from_hours(24)),
                    )

                    context.logger.info(
                        "APPROVAL REQUIRED — callback_id=%s, tool=%s, params=%s. "
                        "Approve: aws lambda send-durable-execution-callback-success "
                        "--callback-id %s --cli-binary-format raw-in-base64-out "
                        '--result \'{"approved": true}\'',
                        callback.callback_id, tool_name, json.dumps(tool_input),
                        callback.callback_id,
                    )

                    # Function suspends here until human responds
                    approval_result = callback.result()

                    # Check if approved
                    approved = False
                    if isinstance(approval_result, dict):
                        approved = approval_result.get("approved", False)
                    elif isinstance(approval_result, str):
                        try:
                            approved = json.loads(approval_result).get("approved", False)
                        except (json.JSONDecodeError, AttributeError):
                            approved = False

                    if not approved:
                        context.logger.info("Human declined tool: %s", tool_name)
                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"json": {"error": "Action declined by human operator"}}],
                                "status": "error",
                            }
                        })
                        actions.append({"tool_name": tool_name, "parameters": tool_input, "status": "declined"})
                        continue

                    context.logger.info("Human approved tool: %s", tool_name)

                # Execute the tool (checkpointed step)
                tool_response = context.step(
                    execute_tool(tool_name, tool_input, registry),
                    name=f"execute-{tool_name}-{tool_use_id}",
                )

                if tool_response.get("status") == "success":
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": tool_response}],
                            "status": "success",
                        }
                    })
                    actions.append({"tool_name": tool_name, "parameters": tool_input, "status": "success"})
                else:
                    error_msg = tool_response.get("error_message", "Tool execution failed")
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"json": tool_response}],
                            "status": "error",
                        }
                    })
                    actions.append({"tool_name": tool_name, "parameters": tool_input, "status": "failure", "error": error_msg})

            messages.append({"role": "user", "content": tool_results})

        # Max iterations reached
        return {
            "statusCode": 200,
            "body": json.dumps({
                "execution_id": execution_id,
                "actions": actions,
                "summary": "",
                "iterations": max_iterations,
                "status": "max_iterations_reached",
            }),
        }

    except Exception as e:
        context.logger.error("Unhandled error: %s, execution_id=%s", str(e), execution_id)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
