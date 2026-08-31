#!/usr/bin/env python3
"""Refresh ORCID and Google Scholar metrics without replacing valid data on failure."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ORCID_ID = "0009-0001-1102-226X"
SCHOLAR_AUTHOR_ID = "DNLvp9YAAAAJ"
DESKTOP_HERO_PATH = Path("assets/hero.svg")
MOBILE_HERO_PATH = Path("assets/hero-mobile.svg")
METRICS_PATH = Path("data/research-metrics.json")


@dataclass(frozen=True)
class ResearchMetrics:
    publications: int
    citations: int
    h_index: int
    i10_index: int

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ResearchMetrics":
        metrics = cls(
            publications=int(value["publications"]),
            citations=int(value["citations"]),
            h_index=int(value["h_index"]),
            i10_index=int(value["i10_index"]),
        )
        metrics.validate()
        return metrics

    def validate(self) -> None:
        for name, value in self.__dict__.items():
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    def to_mapping(self) -> dict[str, int]:
        return {
            "citations": self.citations,
            "h_index": self.h_index,
            "i10_index": self.i10_index,
            "publications": self.publications,
        }


class ScholarMetricsParser(HTMLParser):
    """Read the all-time value from each Google Scholar summary row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._cell_kind: str | None = None
        self._cell_data: list[str] = []
        self._row_label: str | None = None
        self._row_values: list[str] = []
        self.rows: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "td":
            return
        classes = set(dict(attrs).get("class", "").split())
        if "gsc_rsb_sth" in classes:
            self._cell_kind = "label"
            self._cell_data = []
        elif "gsc_rsb_std" in classes:
            self._cell_kind = "value"
            self._cell_data = []

    def handle_data(self, data: str) -> None:
        if self._cell_kind is not None:
            self._cell_data.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._cell_kind is not None:
            value = " ".join("".join(self._cell_data).split())
            if self._cell_kind == "label":
                self._row_label = value
            elif self._row_label is not None:
                self._row_values.append(value)
            self._cell_kind = None
            self._cell_data = []
        elif tag == "tr" and self._row_label is not None:
            self.rows[self._row_label] = self._row_values.copy()
            self._row_label = None
            self._row_values = []


def request_bytes(url: str, *, description: str, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not read {description}: {exc}") from exc


def request_json(url: str, *, description: str, accept: str = "application/json") -> Any:
    payload = request_bytes(url, description=description, accept=accept)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{description} returned invalid JSON") from exc


def parse_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else None
    return None


def fetch_orcid_publications() -> int:
    payload = request_json(
        f"https://pub.orcid.org/v3.0/{ORCID_ID}/works",
        description="the ORCID public works record",
        accept="application/vnd.orcid+json, application/json",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("group"), list):
        raise RuntimeError("ORCID returned an unexpected works response")

    publications = len(payload["group"])
    if publications <= 0:
        raise RuntimeError("ORCID returned an implausible publication count")
    return publications


def all_time_value(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("all", "all_time", "total"):
            parsed = parse_nonnegative_int(value.get(key))
            if parsed is not None:
                return parsed
        return None
    return parse_nonnegative_int(value)


def fetch_scholar_via_serpapi(api_key: str) -> tuple[int, int, int]:
    query = urllib.parse.urlencode(
        {
            "engine": "google_scholar_author",
            "author_id": SCHOLAR_AUTHOR_ID,
            "hl": "en",
            "api_key": api_key,
        }
    )
    payload = request_json(
        f"https://serpapi.com/search.json?{query}",
        description="the SerpAPI Google Scholar author response",
    )
    table = payload.get("cited_by", {}).get("table") if isinstance(payload, dict) else None
    if not isinstance(table, list):
        raise RuntimeError("SerpAPI returned no Google Scholar metrics table")

    values: dict[str, int] = {}
    for row in table:
        if not isinstance(row, dict):
            continue
        for source_key in ("citations", "h_index", "i10_index"):
            parsed = all_time_value(row.get(source_key))
            if parsed is not None:
                values[source_key] = parsed

    required = ("citations", "h_index", "i10_index")
    if any(key not in values for key in required):
        raise RuntimeError("SerpAPI returned an incomplete Google Scholar metrics table")
    return values["citations"], values["h_index"], values["i10_index"]


def fetch_scholar_direct() -> tuple[int, int, int]:
    query = urllib.parse.urlencode(
        {
            "user": SCHOLAR_AUTHOR_ID,
            "hl": "en",
            "oi": "ao",
        }
    )
    payload = request_bytes(
        f"https://scholar.google.com/citations?{query}",
        description="the public Google Scholar profile",
        accept="text/html,application/xhtml+xml",
    )
    parser = ScholarMetricsParser()
    parser.feed(payload.decode("utf-8", errors="replace"))

    normalized = {key.strip().casefold(): values for key, values in parser.rows.items()}
    result: dict[str, int] = {}
    for label, key in (
        ("citations", "citations"),
        ("h-index", "h_index"),
        ("i10-index", "i10_index"),
    ):
        values = normalized.get(label, [])
        parsed = parse_nonnegative_int(values[0]) if values else None
        if parsed is not None:
            result[key] = parsed

    required = ("citations", "h_index", "i10_index")
    if any(key not in result for key in required):
        raise RuntimeError("Google Scholar returned no complete metrics table")
    return result["citations"], result["h_index"], result["i10_index"]


def fetch_scholar_metrics() -> tuple[int, int, int]:
    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    errors: list[str] = []

    if api_key:
        try:
            return fetch_scholar_via_serpapi(api_key)
        except RuntimeError as exc:
            errors.append(str(exc))

    try:
        return fetch_scholar_direct()
    except RuntimeError as exc:
        errors.append(str(exc))

    raise RuntimeError("; ".join(errors))


def replace_svg_metric(svg: str, element_id: str, value: int) -> str:
    pattern = re.compile(
        rf'(<text\b[^>]*\bid="{re.escape(element_id)}"[^>]*>).*?(</text>)',
        flags=re.DOTALL,
    )
    updated, count = pattern.subn(rf"\g<1>{value}\g<2>", svg, count=1)
    if count != 1:
        raise RuntimeError(f"SVG metric element {element_id!r} is missing or duplicated")
    return updated


def update_svg(svg: str, metrics: ResearchMetrics) -> str:
    replacements = {
        "metric-publications": metrics.publications,
        "metric-citations": metrics.citations,
        "metric-h-index": metrics.h_index,
        "metric-i10-index": metrics.i10_index,
    }
    for element_id, value in replacements.items():
        svg = replace_svg_metric(svg, element_id, value)
    return svg


def load_metrics() -> ResearchMetrics:
    try:
        payload = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("metrics JSON must contain an object")
        return ResearchMetrics.from_mapping(payload)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not load {METRICS_PATH}: {exc}") from exc


def write_if_changed(path: Path, content: str) -> bool:
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if content == previous:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    metrics = load_metrics()

    try:
        publications = fetch_orcid_publications()
        metrics = replace(metrics, publications=publications)
        print(f"ORCID: {publications} publications.")
    except RuntimeError as exc:
        print(f"ORCID update skipped: {exc}", file=sys.stderr)

    try:
        citations, h_index, i10_index = fetch_scholar_metrics()
        metrics = replace(
            metrics,
            citations=citations,
            h_index=h_index,
            i10_index=i10_index,
        )
        print(
            f"Google Scholar: {citations} citations, h-index {h_index}, "
            f"i10-index {i10_index}."
        )
    except RuntimeError as exc:
        print(f"Google Scholar update skipped: {exc}", file=sys.stderr)

    metrics.validate()
    changed_paths: list[Path] = []

    metrics_json = json.dumps(metrics.to_mapping(), indent=2, sort_keys=True) + "\n"
    if write_if_changed(METRICS_PATH, metrics_json):
        changed_paths.append(METRICS_PATH)

    for path in (DESKTOP_HERO_PATH, MOBILE_HERO_PATH):
        svg = path.read_text(encoding="utf-8")
        updated_svg = update_svg(svg, metrics)
        if write_if_changed(path, updated_svg):
            changed_paths.append(path)

    if not changed_paths:
        print("Research metrics are already current.")
        return 0

    print("Updated: " + ", ".join(str(path) for path in changed_paths))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
