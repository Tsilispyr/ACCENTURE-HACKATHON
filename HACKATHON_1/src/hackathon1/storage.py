"""MinIO-backed storage for generated incident reports -- the one piece of
application code in the whole repo that actually touches MinIO (per
notes/07-HACKATHON1-REQUIREMENTS.md, every other file only relies on
Langfuse's own internal S3 usage)."""

from __future__ import annotations

import io
import logging
import os

from minio import Minio

logger = logging.getLogger(__name__)

_BUCKET = os.getenv("MINIO_BUCKET", "hackathon1-reports")
_client: Minio | None = None


def _get_client() -> Minio | None:
    global _client
    if _client is not None:
        return _client
    endpoint = os.getenv("MINIO_ENDPOINT")
    access_key = os.getenv("MINIO_ACCESS_KEY")
    secret_key = os.getenv("MINIO_SECRET_KEY")
    if not (endpoint and access_key and secret_key):
        return None
    _client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
    return _client


def upload_report(ticket_id: str, text: str) -> str | None:
    """Upload a resolution report to MinIO, creating the bucket if needed.

    Best-effort: a MinIO outage or missing config must not fail ticket
    resolution, so every failure is caught and logged at WARNING (per
    notes/02-METHODOLOGY.md's logging table) instead of raised.

    Catches Exception broadly, not just minio.error.S3Error -- confirmed by
    actually running this against a not-yet-started MinIO locally, which
    raises urllib3.exceptions.MaxRetryError (a connection-level failure, not
    an S3Error) at the bucket_exists() call, before MinIO's own API ever gets
    a chance to return an S3-flavoured error. A narrower except here would
    have defeated this whole function's "must not fail ticket resolution"
    purpose on exactly the case it exists for -- MinIO being unreachable.
    """
    client = _get_client()
    if client is None:
        logger.warning("MinIO not configured -- skipping report upload for %s", ticket_id)
        return None

    object_key = f"{ticket_id}.txt"
    try:
        if not client.bucket_exists(_BUCKET):
            client.make_bucket(_BUCKET)
        data = text.encode("utf-8")
        client.put_object(_BUCKET, object_key, io.BytesIO(data), length=len(data), content_type="text/plain")
        return object_key
    except Exception as exc:
        logger.warning("MinIO upload failed for %s: %s", ticket_id, exc)
        return None
