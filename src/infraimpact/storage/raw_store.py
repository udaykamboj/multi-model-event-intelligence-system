"""Immutable raw payload store (brief section 9).

Local filesystem now, S3-compatible in production. Layout is identical:
``/source/year/month/day/hour/observation_id``. Keeping parsers out of the
ledger is what allows a parser to be improved later and every historical
observation reprocessed.
"""

from __future__ import annotations

import gzip
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..domain.ids import content_hash


class RawStore(ABC):
    @abstractmethod
    def put(self, source_id: str, observation_id: str, payload: Any, when: Any) -> str: ...

    @abstractmethod
    def get(self, uri: str) -> Any: ...


class FileRawStore(RawStore):
    def __init__(self, root: str | Path = "./data/raw") -> None:
        self.root = Path(root)

    def _path(self, source_id: str, observation_id: str, when: Any) -> Path:
        dt = getattr(when, "timetuple", None)
        parts = (
            source_id.replace("/", "_"),
            str(when.year),
            f"{when.month:02d}",
            f"{when.day:02d}",
            f"{when.hour:02d}",
            observation_id,
        )
        _ = dt
        return self.root.joinpath(*parts)

    def put(self, source_id: str, observation_id: str, payload: Any, when: Any) -> str:
        path = self._path(source_id, observation_id, when)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, (bytes, bytearray)):
            blob = bytes(payload)
            path.write_bytes(blob)
        else:
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return f"file://{path.as_posix()}"

    def get(self, uri: str) -> Any:
        raw = uri.split("file://", 1)[-1]
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(raw)
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


class S3RawStore(RawStore):
    """Requires the optional ``s3`` extra. Same key layout as FileRawStore."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("install infraimpact[s3] to use S3RawStore") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3")

    def _key(self, source_id: str, observation_id: str, when: Any) -> str:
        base = (
            f"{self.prefix}/" if self.prefix else ""
        ) + f"{source_id}/{when.year}/{when.month:02d}/{when.day:02d}/{when.hour:02d}/{observation_id}"
        return base

    def put(self, source_id: str, observation_id: str, payload: Any, when: Any) -> str:
        key = self._key(source_id, observation_id, when)
        body = (
            payload
            if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload, default=str).encode("utf-8")
        )
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentEncoding="gzip" if isinstance(body, (bytes, bytearray)) else None,
        )
        return f"s3://{self.bucket}/{key}"

    def get(self, uri: str) -> Any:
        raw = uri.split("s3://", 1)[-1]
        bucket, _, key = raw.partition("/")
        obj = self._client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def build_raw_store(root: str | Path = "./data/raw") -> RawStore:
    return FileRawStore(root)


def hash_payload(payload: Any) -> str:
    return content_hash(payload)