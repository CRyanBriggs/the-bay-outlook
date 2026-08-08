from __future__ import annotations

import re
from typing import Any


SUPPRESSION_MARKERS = {"*", "**", "-", "--", "N", "NA", "N/A", "null", "None", ""}


def parse_number(value: Any) -> tuple[float | None, str]:
    if value is None:
        return None, "missing"
    text = str(value).strip()
    if text in SUPPRESSION_MARKERS:
        return None, "suppressed" if "*" in text else "missing"
    text = text.replace(",", "").replace("$", "").replace("%", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text), "final"
    except ValueError:
        return None, "missing"


def normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def first_present(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    normalized = {normalized_header(key): value for key, value in row.items() if key is not None}
    for alias in aliases:
        key = normalized_header(alias)
        if key in normalized:
            return normalized[key]
    return None
