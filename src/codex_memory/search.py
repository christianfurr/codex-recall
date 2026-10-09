"""Safe keyword query preparation; semantic retrieval can be added separately."""

from __future__ import annotations

import re
import unicodedata


def fts_queries(query: object) -> tuple[str, str] | None:
    """Return safe AND/OR FTS expressions with phrases and explicit prefixes."""
    if not isinstance(query, str):
        raise ValueError("query must be a string.")
    if len(query) > 1024:
        raise ValueError("query exceeds the supported length.")
    query = unicodedata.normalize("NFKC", query).casefold().strip()
    terms: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'"([^"\n]+)"|(\w+\*?)', query, re.UNICODE):
        phrase, word = match.groups()
        if phrase is not None:
            tokens = re.findall(r"\w+", phrase, re.UNICODE)
            if not tokens:
                continue
            term = '"' + " ".join(tokens) + '"'
        else:
            prefix = word.endswith("*")
            token = word[:-1] if prefix else word
            term = '"' + token + '"' + ("*" if prefix else "")
        if term not in seen:
            terms.append(term)
            seen.add(term)
        if len(terms) == 32:
            break
    if not terms:
        return None
    return " AND ".join(terms), " OR ".join(terms)


def filter_sql(scope: str | None, project: str | None, category: str | None,
               include_superseded: bool, now: str, *, alias: str = "m") -> tuple[str, list]:
    """Build only fixed SQL fragments; all supplied values stay parameters."""
    if alias not in ("m", "memories"):
        raise ValueError("Unsupported SQL alias.")
    conditions = [f"({alias}.expires_at IS NULL OR {alias}.expires_at > ?)"]
    parameters: list = [now]
    if not include_superseded:
        conditions.append(f"{alias}.status = 'active'")
    if scope is not None:
        conditions.append(f"{alias}.scope = ?")
        parameters.append(scope)
        if scope == "project":
            conditions.append(f"{alias}.project = ?")
            parameters.append(project)
    elif project is not None:
        conditions.append(f"({alias}.scope IN ('global', 'machine') OR ({alias}.scope = 'project' AND {alias}.project = ?))")
        parameters.append(project)
    else:
        conditions.append(f"{alias}.scope IN ('global', 'machine')")
    if category is not None:
        conditions.append(f"{alias}.category = ?")
        parameters.append(category)
    return " AND ".join(conditions), parameters
