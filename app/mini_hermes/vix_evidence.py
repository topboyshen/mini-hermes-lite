"""Small, allowlisted VIX observations; never archive HTTP bodies or account data."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
from typing import Any, Mapping
from zoneinfo import ZoneInfo

SCHEMA = "mini-hermes.vix-evidence.v1"
RAW_FIELDS = (
    "current_price", "price_change", "price_change_percent", "prev_day_close",
    "high", "low", "last_trade_time",
)
QUOTE_FIELDS = ("price", "change", "percent", "high", "low", "as_of")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _aware(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evidence timestamp must have a timezone")
    return parsed


def quote_fields(quote: Any | None) -> dict[str, str | None] | None:
    if quote is None:
        return None
    return {key: None if getattr(quote, key) is None else str(getattr(quote, key)) for key in QUOTE_FIELDS}


def observation_json(
    payload: object, quote: Any | None, *, collected_at: datetime,
    completed_at: datetime, parser: Mapping[str, str | None], error_type: str | None = None,
) -> str:
    """Capture observed clocks, not report time; preserve missing vs explicit null."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        data = payload if isinstance(payload, Mapping) else {}
    raw: dict[str, object] = {}
    omitted: list[str] = []
    for key in RAW_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if (value is None or type(value) in (int, float, str)) and not (
            isinstance(value, str) and len(value) > 128
            or isinstance(value, float) and not math.isfinite(value)
            or isinstance(value, int) and value.bit_length() > 128
        ):
            raw[key] = value
        else:
            omitted.append(key)
    source_time = raw.get("last_trade_time")
    source_at = None
    if isinstance(source_time, str) and source_time.strip():
        try:
            parsed = datetime.fromisoformat(source_time.strip())
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
            source_at = parsed.isoformat()
        except ValueError:
            pass
    # Top-level vendor timestamp and last trade time have different semantics.
    # Retain just this known timestamp, never arbitrary response metadata.
    vendor_time = payload.get("timestamp") if isinstance(payload, Mapping) else None
    if not isinstance(vendor_time, str) or len(vendor_time) > 128:
        vendor_time = None
    result = {
        "status": "fetch_failed" if error_type else "parsed" if quote is not None else "parse_failed",
        "source": "Cboe", "raw_fields": raw, "omitted_fields": omitted,
        "vendor_timestamp_raw": vendor_time,
        "source_as_of": source_at, "source_timezone": "America/New_York",
        "collected_at": collected_at.isoformat(), "completed_at": completed_at.isoformat(),
        "parser": dict(parser), "parsed": quote_fields(quote), "error_type": error_type,
    }
    _aware(result["collected_at"])
    _aware(result["completed_at"])
    return _json(result)


def build_report_evidence(
    *, report_date: str, full: str, brief: str, generated_at: datetime | None,
    expected_trade_date: str | None, observation: str | None,
    rendered_quote: Any | None,
) -> dict[str, object]:
    """Link exact formal text to the collection and to the post-validation VIX."""
    return {
        "schema": SCHEMA, "report_date": report_date,
        "report_sha256": sha256(full.encode("utf-8")).hexdigest(),
        "brief_sha256": sha256(brief.encode("utf-8")).hexdigest(),
        # Default render-time 08:30 is NOT evidence of a real generation clock.
        "generated_at": generated_at.isoformat() if generated_at is not None else None,
        "expected_us_trade_date": expected_trade_date,
        "observation": json.loads(observation) if observation is not None else None,
        "rendered_vix": quote_fields(rendered_quote),
    }


def encode_report_evidence(
    evidence: Mapping[str, object], report_date: str, report_text: str, trigger: str,
) -> str:
    """Validate the boundary before a single database write; reject extra fields."""
    if set(evidence) != {
        "schema", "report_date", "report_sha256", "brief_sha256", "generated_at",
        "expected_us_trade_date", "observation", "rendered_vix",
    } or evidence["schema"] != SCHEMA:
        raise ValueError("invalid VIX evidence schema")
    if evidence["report_date"] != report_date or evidence["report_sha256"] != sha256(report_text.encode("utf-8")).hexdigest():
        raise ValueError("VIX evidence report binding mismatch")
    if trigger not in {"scheduled", "manual", "unspecified"}:
        raise ValueError("invalid evidence trigger")
    for key in ("report_sha256", "brief_sha256"):
        value = evidence[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid evidence hash")
    _aware(evidence["generated_at"])
    if evidence["expected_us_trade_date"] is not None:
        from datetime import date
        date.fromisoformat(evidence["expected_us_trade_date"])
    observation = evidence["observation"]
    if observation is not None:
        if not isinstance(observation, dict) or set(observation) != {
            "status", "source", "raw_fields", "omitted_fields", "vendor_timestamp_raw",
            "source_as_of", "source_timezone", "collected_at", "completed_at", "parser",
            "parsed", "error_type",
        }:
            raise ValueError("invalid VIX observation")
        if observation["source"] != "Cboe" or observation["source_timezone"] != "America/New_York" or observation["status"] not in {"parsed", "parse_failed", "fetch_failed"}:
            raise ValueError("invalid VIX source/status")
        raw = observation["raw_fields"]
        if not isinstance(raw, dict) or not set(raw) <= set(RAW_FIELDS):
            raise ValueError("unexpected raw VIX fields")
        for value in raw.values():
            if value is not None and (type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 128):
                raise ValueError("invalid raw VIX scalar")
        omitted = observation["omitted_fields"]
        if not isinstance(omitted, list) or any(key not in RAW_FIELDS for key in omitted):
            raise ValueError("invalid omitted VIX fields")
        parser = observation["parser"]
        if not isinstance(parser, dict) or set(parser) != {"version", "source_sha256"} or parser["version"] != "cboe-vix-prev-close-v1":
            raise ValueError("invalid VIX parser identity")
        fingerprint = parser["source_sha256"]
        if fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)):
            raise ValueError("invalid VIX parser fingerprint")
        start, end = _aware(observation["collected_at"]), _aware(observation["completed_at"])
        if start is None or end is None or end < start:
            raise ValueError("invalid VIX collection interval")
        _aware(observation["source_as_of"])
        for key in ("vendor_timestamp_raw", "error_type"):
            value = observation[key]
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError("invalid VIX observation label")
        _validate_quote(observation["parsed"])
        if (observation["parsed"] is not None) != (observation["status"] == "parsed"):
            raise ValueError("VIX parse status mismatch")
    _validate_quote(evidence["rendered_vix"])
    encoded = _json({**evidence, "trigger": trigger})
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("VIX evidence exceeds limit")
    return encoded


def _validate_quote(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != set(QUOTE_FIELDS) or any(
        item is not None and (not isinstance(item, str) or len(item) > 128)
        for item in value.values()
    ):
        raise ValueError("invalid VIX parsed quote")
