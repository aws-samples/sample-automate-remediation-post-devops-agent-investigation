import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SUPPORTED_TOOLS = {
    "lambda_update_function_configuration",
    "lambda_get_function_configuration",
}


def handle_update_function_configuration(params):
    """Call boto3 update_function_configuration with the provided parameters."""
    """It is an high-impact function (can change env vars/runtime on any function)"""
    client = boto3.client("lambda")
    response = client.update_function_configuration(**params)
    return {"status": "success", "response": response}


def handle_get_function_configuration(params):
    """Call boto3 get_function_configuration with the provided parameters."""
    client = boto3.client("lambda")
    response = client.get_function_configuration(**params)
    return {"status": "success", "response": response}


TOOL_HANDLERS = {
    "lambda_update_function_configuration": handle_update_function_configuration,
    "lambda_get_function_configuration": handle_get_function_configuration,
}


def lambda_handler(event, context):
    """Route invocation to the correct boto3 handler based on tool_name."""
    try:
        tool_name = event.get("tool_name")
        parameters = event.get("parameters", {})

        logger.info("Tool Lambda invoked with tool_name=%s", tool_name)

        if tool_name not in TOOL_HANDLERS:
            logger.error("Unknown tool_name: %s", tool_name)
            return {
                "status": "error",
                "error_type": "UnknownTool",
                "error_message": f"Unknown tool: {tool_name}",
            }

        handler = TOOL_HANDLERS[tool_name]
        return handler(parameters)

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        logger.error(
            "boto3 ClientError for tool_name=%s: %s - %s",
            event.get("tool_name"),
            error_code,
            error_message,
        )
        return {
            "status": "error",
            "error_type": error_code,
            "error_message": error_message,
        }
    except Exception as e:
        logger.error("Unhandled error in Tool Lambda: %s", e, exc_info=True)
        return {
            "status": "error",
            "error_type": type(e).__name__,
            "error_message": str(e),
        }
