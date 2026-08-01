"""
Drop-in replacement for reading SCORO_API_KEY from os.environ.

Paste this near the top of handler.py (below your existing imports),
then replace every `os.environ["SCORO_API_KEY"]` / `os.getenv("SCORO_API_KEY")`
with a call to `get_scoro_api_key()`.

Behaviour:
  - Fetches from Secrets Manager on first use, then caches for the life of
    the execution environment (so warm invocations cost nothing extra).
  - Picks the right secret automatically from the function name, or you can
    override with a SCORO_SECRET_NAME env var.
  - Falls back to the existing SCORO_API_KEY env var if the fetch fails, so
    deploying this is zero-risk. Remove the env var only once you've confirmed
    it's reading from Secrets Manager.
"""

import json
import os
import boto3
from botocore.config import Config

_SECRET_CACHE: dict[str, str] = {}

# Keep timeouts tight: a hung secrets call shouldn't eat the Lambda timeout.
_SM_CONFIG = Config(
    connect_timeout=2,
    read_timeout=2,
    retries={"max_attempts": 3, "mode": "standard"},
)
_sm_client = None


def _client():
    global _sm_client
    if _sm_client is None:
        _sm_client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "eu-north-1"),
            config=_SM_CONFIG,
        )
    return _sm_client


def _secret_name() -> str:
    explicit = os.environ.get("SCORO_SECRET_NAME")
    if explicit:
        return explicit
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
    stage = "prod" if "-prod-" in fn else "dev"
    return f"zembr/{stage}/scoro-api-key"


def get_scoro_api_key() -> str:
    """Return the Scoro API key, cached across warm invocations."""
    name = _secret_name()

    if name in _SECRET_CACHE:
        return _SECRET_CACHE[name]

    try:
        raw = _client().get_secret_value(SecretId=name)["SecretString"]
        # Secret is stored as {"SCORO_API_KEY": "..."}, but tolerate a bare string.
        try:
            key = json.loads(raw)["SCORO_API_KEY"]
        except (json.JSONDecodeError, TypeError, KeyError):
            key = raw
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the run
        key = os.environ.get("SCORO_API_KEY")
        if not key:
            raise RuntimeError(
                f"Could not read secret {name!r} and no SCORO_API_KEY env var set"
            ) from exc
        print(f"WARNING: Secrets Manager fetch failed ({exc}); using env var fallback")

    _SECRET_CACHE[name] = key
    return key