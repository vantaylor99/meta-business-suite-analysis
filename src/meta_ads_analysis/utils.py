"""Small utility helpers."""

from __future__ import annotations

import csv
import json
import re
from ast import literal_eval
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def standardize_header(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = cleaned.replace("%", " percent ")
    cleaned = cleaned.replace("$", " usd ")
    cleaned = cleaned.replace("&", " and ")
    cleaned = re.sub(r"[\[\]\(\)]", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    return cleaned.strip("_")


def unique_headers(headers: list[str]) -> list[str]:
    counts: defaultdict[str, int] = defaultdict(int)
    unique: list[str] = []
    for header in headers:
        base = header.strip() or "unnamed_column"
        counts[base] += 1
        unique.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return unique


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            headers = next(reader)
        except StopIteration:
            return []
        headers = unique_headers(headers)
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            padded = raw_row + [""] * (len(headers) - len(raw_row))
            rows.append(dict(zip(headers, padded[: len(headers)], strict=False)))
        return rows


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=json_default)
        handle.write("\n")


def json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    formats = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw or raw in {"--", "-", "N/A", "n/a"}:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    raw = raw.replace("$", "").replace(",", "").replace("%", "").strip()
    raw = raw.replace("—", "").replace("–", "")
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return -parsed if negative else parsed


def parse_int(value: str | None) -> int | None:
    parsed = parse_number(value)
    if parsed is None:
        return None
    return int(round(parsed))


def safe_divide(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous


def parse_metric_blob(value: str | None, key_field: str = "action_type") -> dict[str, float]:
    if value is None:
        return {}
    raw = value.strip()
    if not raw:
        return {}
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = literal_eval(raw)
        except (SyntaxError, ValueError):
            parsed = None
    if isinstance(parsed, list):
        metrics: dict[str, float] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            key = item.get(key_field) or item.get("metric") or item.get("name")
            metric_value = parse_number(str(item.get("value", "")).strip())
            if key and metric_value is not None:
                metrics[str(key).strip().lower()] = metric_value
        return metrics
    if isinstance(parsed, dict):
        metrics = {}
        for key, item in parsed.items():
            metric_value = parse_number(str(item))
            if metric_value is not None:
                metrics[str(key).strip().lower()] = metric_value
        return metrics

    metrics = {}
    chunks = re.split(r"[;|]", raw)
    for chunk in chunks:
        if ":" not in chunk:
            continue
        key, metric_value = chunk.split(":", 1)
        parsed_value = parse_number(metric_value)
        if parsed_value is not None:
            metrics[key.strip().lower()] = parsed_value
    return metrics
