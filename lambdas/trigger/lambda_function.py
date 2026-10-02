import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def validate_event(event):
    print(event)

    source = event.get("source")
    if source != "aws.aidevops":
        logger.warning("Unexpected event source: %s", source)
        return None

    detail_type = event.get("detail-type")
    if detail_type != "Investigation Completed":
        logger.warning("Unexpected detail-type: %s", detail_type)
        return None

    detail = event.get("detail", {})
    metadata = detail.get("metadata", {})

    required_keys = ["agent_space_id", "task_id", "execution_id"]
    missing_keys = [k for k in required_keys if k not in metadata]
    if missing_keys:
        raise ValueError(f"Missing required metadata keys: {', '.join(missing_keys)}")

    data = detail.get("data", {})
    return (metadata, data)


def fetch_investigation_summary(agent_space_id, execution_id):
    try:
        client = boto3.client("devops-agent")
        response = client.list_journal_records(
            agentSpaceId=agent_space_id,
            executionId=execution_id,
            limit=1,
            recordType="investigation_summary",
        )
        records = response.get("records", [])
        return records[0] if records else None
    except Exception:
        logger.warning(
            "Failed to fetch journal records for agent_space_id=%s, execution_id=%s",
            agent_space_id,
            execution_id,
            exc_info=True,
        )
        raise


def parse_summary_content(content_str):
    try:
        return json.loads(content_str)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Failed to parse summary content: %s", e)
        raise ValueError(f"Invalid JSON in summary content: {e}") from e


def build_response(status_code, body):
    return {"statusCode": status_code, "body": json.dumps(body)}


def invoke_remediation_lambda(investigation_summary, metadata):
    """Invoke the Orchestrator Lambda asynchronously with the investigation summary."""
    function_name = os.environ.get("REMEDIATION_FUNCTION_NAME")
    if not function_name:
        logger.warning("REMEDIATION_FUNCTION_NAME not set, skipping remediation")
        return
    try:
        client = boto3.client("lambda")
        payload = {
            "investigation_summary": investigation_summary,
            "metadata": metadata,
        }
        client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        logger.info("Remediation Lambda invoked asynchronously")
    except Exception as e:
        logger.error("Failed to invoke Remediation Lambda: %s", e, exc_info=True)


def lambda_handler(event, context):
    try:
        result = validate_event(event)
        if result is None:
            return None

        metadata, data = result

        summary_record = fetch_investigation_summary(
            metadata["agent_space_id"], metadata["execution_id"]
        )
        if summary_record is None:
            logger.warning("No investigation_summary record found")
            return build_response(200, {"message": "No investigation summary found"})

        parsed_summary = parse_summary_content(summary_record["content"])

        print("=== SYMPTOMS ===")
        for symptom in parsed_summary.get("symptoms", []):
            print(f"[{symptom['title']}]")
            print(f"  {symptom['description']}")
            print()

        print("=== ROOT CAUSE(S) ===")
        for finding in parsed_summary.get("findings", []):
            if finding.get("type") == "root_cause":
                print(f"[{finding['title']}]")
                print(f"  {finding['description']}")
                print()

        print("=== CONTRIBUTING CAUSE(S) ===")
        for finding in parsed_summary.get("findings", []):
            if finding.get("type") == "cause":
                print(f"[{finding['title']}]")
                print(f"  {finding['description']}")
                print()

        print("=== INVESTIGATION GAPS ===")
        for gap in parsed_summary.get("investigation_gaps", []):
            print(f"[{gap['title']}]")
            print(f"  {gap['description']}")
            print()

        # Trigger async remediation
        invoke_remediation_lambda(parsed_summary, metadata)

        return build_response(200, parsed_summary)

    except Exception as e:
        logger.error("Unhandled error: %s", e, exc_info=True)
        return build_response(500, {"error": str(e)})
