"""Validation and canonical representations for the public memory model."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .security import reject_secrets, validate_text


CATEGORIES = (
    "preferences", "machine_setup", "project_decisions", "task_progress",
    "lessons_learned", "development_conventions", "technical_context", "general",
)
SCOPES = ("global", "machine", "project")
PUBLIC_FIELDS = (
    "id", "content", "category", "scope", "project", "source", "created_at",
    "updated_at", "expires_at", "superseded_by", "status",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_content(content: str) -> str:
    return " ".join(unicodedata.normalize("NFC", content).split())


def normalize_project(project: object) -> str | None:
    if project is None:
        return None
    value = validate_text(project, "project", maximum=2048)
    value = unicodedata.normalize("NFKC", value).strip()
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("project file URI must identify a local absolute directory.")
        value = unquote(parsed.path)
        if not Path(value).is_absolute():
            raise ValueError("project file URI must identify a local absolute directory.")
    reject_secrets(value)
    if Path(value).is_absolute():
        return Path(value).resolve(strict=False).as_uri()
    # Relative filesystem paths are ambiguous; use a stable name or absolute URI.
    if "/" in value or "\\" in value:
        raise ValueError("project must be a stable name or an absolute local path.")
    normalized = re.sub(r"\s+", "-", value.casefold())
    if not normalized or not re.fullmatch(r"[\w][\w.-]*", normalized, re.UNICODE):
        raise ValueError("project name contains unsupported characters.")
    reject_secrets(normalized)
    return normalized


def normalize_enum(value: object, field: str, choices: tuple[str, ...]) -> str:
    normalized = validate_text(value, field, maximum=64).casefold()
    if normalized not in choices:
        raise ValueError(f"Unsupported {field}.")
    return normalized


def normalize_expiration(value: object) -> str | None:
    if value is None:
        return None
    text = validate_text(value, "expires_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError("expires_at must be an ISO 8601 timestamp with a timezone.") from None


def normalize_memory(content: object, category: object, scope: object,
                     project: object = None, source: object = None,
                     expires_at: object = None) -> dict:
    result = {
        "content": validate_text(content, "content", maximum=16000),
        "category": normalize_enum(category, "category", CATEGORIES),
        "scope": normalize_enum(scope, "scope", SCOPES),
        "project": normalize_project(project),
        "source": None if source is None or (isinstance(source, str) and not source.strip()) else validate_text(source, "source", maximum=1000),
        "expires_at": normalize_expiration(expires_at),
    }
    if result["scope"] == "project" and result["project"] is None:
        raise ValueError("Project-scoped memories require a project identifier.")
    if result["scope"] != "project" and result["project"] is not None:
        raise ValueError("Only project-scoped memories may have a project identifier.")
    return result


def duplicate_key(record: dict) -> str:
    fields = [normalize_content(record["content"]), record["category"], record["scope"],
              record["project"], record["source"], record["expires_at"]]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_id(identifier: object) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("A valid memory ID is required.")
    return identifier


def validate_limit(limit: object, maximum: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")
    return min(limit, maximum)


def validate_filters(scope: object, project: object, category: object) -> tuple[str | None, str | None, str | None]:
    normalized_scope = None if scope is None else normalize_enum(scope, "scope", SCOPES)
    normalized_project = normalize_project(project)
    normalized_category = None if category is None else normalize_enum(category, "category", CATEGORIES)
    if normalized_scope == "project" and normalized_project is None:
        raise ValueError("Project scope requires a project identifier.")
    return normalized_scope, normalized_project, normalized_category
