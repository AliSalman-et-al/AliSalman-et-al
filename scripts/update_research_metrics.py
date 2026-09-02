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
SCHOLAR_BADGE_BASE_URL = "https://google-scholar-badge.vercel.app"
SCHOLAR_BADGE_ENDPOINTS = (
    ("citations", "citations"),
    ("h_index", "h-index"),
    ("i10_index", "i10-index"),
)
SERPAPI_BASE_URL = "https://serpapi.com/search"
SERPAPI_JSON_RESTRICTOR = (
    "search_metadata.status,"
    "search_parameters.{engine,author_id,hl},"
    "error,"
    "cited_by.table"
)
SERPAPI_KEY_ENV_VARS = ("SERPAPI_KEY", "SERPAPI_API_KEY")
SERPAPI_METRIC_KEYS = {
    "citations": "citations",
    "h_index": "h_index",
    "hindex": "h_index",
    "indice_h": "h_index",
    "i10_index": "i10_index",
    "i10index": "i10_index",
    "indice_i10": "i10_index",
}
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


def http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except OSError:
        body = b""

    if body:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            text = body.decode("utf-8", errors="replace").strip()
            if text:
                return text[:200]
        else:
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])

    return str(exc.reason)


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
    except urllib.error.HTTPError as exc:
        detail = http_error_detail(exc)
        raise RuntimeError(
            f"Could not read {description}: HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not read {description}: {exc}") from exc


def request_json(url: str, *, description: str, accept: str = "application/json") -> Any:
    payload = request_bytes(url, description=description, accept=accept)
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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


def parse_badge_metric(value: Any) -> int | None:
    """Parse a successful Shields endpoint message without accepting error codes."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        if normalized.isdecimal():
            return int(normalized)
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


def fetch_scholar_via_badge() -> tuple[int, int, int]:
    """Read all-time Scholar metrics from the cached badge endpoints."""

    query = urllib.parse.urlencode({"user": SCHOLAR_AUTHOR_ID})
    values: dict[str, int] = {}

    for metric_name, endpoint in SCHOLAR_BADGE_ENDPOINTS:
        payload = request_json(
            f"{SCHOLAR_BADGE_BASE_URL}/{endpoint}?{query}",
            description=f"the Google Scholar badge {endpoint} endpoint",
        )
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            raise RuntimeError(
                f"Google Scholar badge {endpoint} returned an unexpected response"
            )

        value = parse_badge_metric(payload.get("message"))
        if value is None:
            message = str(payload.get("message") or "unknown error")
            raise RuntimeError(
                f"Google Scholar badge {endpoint} returned {message!r}"
            )
        values[metric_name] = value

    return values["citations"], values["h_index"], values["i10_index"]


def all_time_value(value: Any) -> int | None:
    if isinstance(value, dict):
        return parse_nonnegative_int(value.get("all"))
    return None


def normalize_serpapi_metric_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def parse_serpapi_metrics(payload: Any) -> tuple[int, int, int]:
    """Validate a Google Scholar Author response and read all-time metrics."""

    if not isinstance(payload, dict):
        raise RuntimeError("SerpApi returned an unexpected response")

    error = payload.get("error")
    if error:
        raise RuntimeError(f"SerpApi returned an error: {error}")

    metadata = payload.get("search_metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("SerpApi returned no search metadata")

    status = metadata.get("status")
    if status != "Success":
        raise RuntimeError(f"SerpApi search status was {status!r}")

    parameters = payload.get("search_parameters")
    if not isinstance(parameters, dict):
        raise RuntimeError("SerpApi returned no search parameters")
    if parameters.get("engine") != "google_scholar_author":
        raise RuntimeError("SerpApi returned results from the wrong engine")
    if parameters.get("author_id") != SCHOLAR_AUTHOR_ID:
        raise RuntimeError("SerpApi returned results for the wrong author")

    table = payload.get("cited_by", {}).get("table")
    if not isinstance(table, list):
        raise RuntimeError("SerpApi returned no Google Scholar metrics table")

    values: dict[str, int] = {}
    for row in table:
        if not isinstance(row, dict):
            continue
        for raw_key, raw_value in row.items():
            normalized_key = normalize_serpapi_metric_key(str(raw_key))
            metric_name = SERPAPI_METRIC_KEYS.get(normalized_key)
            if metric_name is None:
                continue
            parsed = all_time_value(raw_value)
            if parsed is not None:
                values[metric_name] = parsed

    required = ("citations", "h_index", "i10_index")
    missing = [metric for metric in required if metric not in values]
    if missing:
        raise RuntimeError(
            "SerpApi returned an incomplete Google Scholar metrics table: "
            + ", ".join(missing)
        )

    return values["citations"], values["h_index"], values["i10_index"]


def fetch_scholar_via_serpapi(api_key: str) -> tuple[int, int, int]:
    # Keep the documented synchronous and cache-enabled defaults. SerpApi caches
    # identical searches for one hour and does not count cached results as searches.
    query = urllib.parse.urlencode(
        {
            "engine": "google_scholar_author",
            "author_id": SCHOLAR_AUTHOR_ID,
            "hl": "en",
            "output": "json",
            "json_restrictor": SERPAPI_JSON_RESTRICTOR,
            "api_key": api_key,
        }
    )
    payload = request_json(
        f"{SERPAPI_BASE_URL}?{query}",
        description="the SerpApi Google Scholar Author response",
    )
    return parse_serpapi_metrics(payload)


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


def get_serpapi_key() -> str | None:
    for name in SERPAPI_KEY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def fetch_scholar_metrics() -> tuple[int, int, int]:
    errors: list[str] = []

    try:
        metrics = fetch_scholar_via_badge()
        print("Google Scholar source: google-scholar-badge.")
        return metrics
    except RuntimeError as exc:
        errors.append(str(exc))

    api_key = get_serpapi_key()
    if api_key:
        try:
            metrics = fetch_scholar_via_serpapi(api_key)
            print("Google Scholar source: SerpApi.")
            return metrics
        except RuntimeError as exc:
            errors.append(str(exc))

    try:
        metrics = fetch_scholar_direct()
        print("Google Scholar source: direct profile.")
        return metrics
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
