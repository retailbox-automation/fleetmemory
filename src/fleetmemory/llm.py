"""LLM access via AWS Bedrock (converse API)."""

import json
import os
import re

import boto3

DEFAULT_MODEL = os.environ.get(
    "FLEETMEM_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

_client = None


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    return _client


def complete(prompt: str, *, system: str | None = None, model: str | None = None,
             max_tokens: int = 1024, temperature: float = 0.0) -> str:
    kwargs = {
        "modelId": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    resp = client().converse(**kwargs)
    return resp["output"]["message"]["content"][0]["text"]


def complete_json(prompt: str, **kw):
    """complete() that returns parsed JSON (tolerates ```json fences)."""
    text = complete(prompt, **kw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    return json.loads(text)
