"""Independent official offer, price/rate and paid-coupon evidence, without activation."""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.request

from . import treasury_current


PRICE_URL = ("https://www.tesourotransparente.gov.br/ckan/dataset/"
             "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
             "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv")
COUPON_URL = ("https://www.tesourotransparente.gov.br/ckan/dataset/"
              "f30db6e4-6123-416c-b094-be8dfc823601/resource/"
              "b04812f4-c453-4298-a12e-0ad190998243/download/cupomjurostesourodireto.csv")
PRICE_FIELDS = {"Tipo Titulo", "Data Vencimento", "Data Base", "Taxa Compra Manha", "Taxa Venda Manha",
                "PU Compra Manha", "PU Venda Manha", "PU Base Manha"}
COUPON_FIELDS = {"Tipo Titulo", "Vencimento do Titulo", "Data Resgate", "PU", "Quantidade", "Valor"}
COUPON_KINDS = {"Tesouro Prefixado com Juros Semestrais", "Tesouro IPCA+ com Juros Semestrais",
                "Tesouro IGPM+ com Juros Semestrais"}
UNVALIDATED_KINDS = {"Tesouro Renda+", "Tesouro Educa+", "Tesouro Reserva"}
MAX_BYTES = 128 * 1024 * 1024
MAX_ROWS = 2_000_000
HEADER_NAMES = {"date", "last-modified", "etag", "content-type", "content-length", "cache-control", "retry-after"}


def _utcnow():
    return datetime.now(timezone.utc)


def _cutoff(value, now):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00")) if "T" in value else date.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value > now:
            raise ValueError("as_of must include a timezone and cannot be in the future")
        return value.astimezone(treasury_current.BRT).date(), value
    if not isinstance(value, date) or value > now.astimezone(treasury_current.BRT).date():
        raise ValueError("as_of must be an observed date, not a future date")
    return value, min(datetime.combine(value, time.max, treasury_current.BRT), now)


def _kind(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_instrument_kind")
    return treasury_current._kind(value.strip())


def _day(value):
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except (ValueError, TypeError) as error:
        raise ValueError("invalid_observation_date") from error


def _decimal(value, *, positive=False, price=False):
    value = value.strip()
    if value in {"", "-", "--"}:
        return None
    if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", value):
        raise ValueError("invalid_brazilian_numeric_field")
    try:
        result = Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("invalid_numeric_field") from error
    if not result.is_finite() or not math.isfinite(float(result)) or positive and result <= 0 or price and result < 0:
        raise ValueError("nonfinite_or_nonpositive_numeric_field")
    if price and result == 0:
        return None
    return result


def _csv(content, required, *, exact=False):
    if len(content) > MAX_BYTES:
        raise ValueError("csv_size_limit")
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=";")
    fields = reader.fieldnames or []
    if (len(fields) != len(set(fields)) or not required <= set(fields)
            or exact and set(fields) != required):
        raise ValueError("csv_schema_changed_or_duplicate_columns")
    for number, row in enumerate(reader, 1):
        if number > MAX_ROWS:
            raise ValueError("csv_row_limit")
        if None in row or any(value is None for value in row.values()):
            raise ValueError("misaligned_csv_row")
        yield number, row


def _summary(series, count, skipped, rejected, issues, all_dates):
    return {"series": series, "row_count": count, "included_row_count": sum(len(v) for v in series.values()),
            "rows_after_cutoff": skipped, "rejected_row_count": rejected, "issues": issues,
            "first_observation_date": min(all_dates).isoformat() if all_dates else None,
            "last_observation_date": max(all_dates).isoformat() if all_dates else None}


def parse_price_history(content, as_of):
    """Retain every observed date/type; prices are quotes, never total-return paths."""
    series, seen, issues, all_dates = defaultdict(list), {}, [], []
    count = skipped = rejected = 0
    numeric = {"buy_price": "PU Compra Manha", "sell_price": "PU Venda Manha", "base_price": "PU Base Manha",
               "buy_rate": "Taxa Compra Manha", "sell_rate": "Taxa Venda Manha"}
    for number, row in _csv(content, PRICE_FIELDS):
        count += 1
        try:
            day, maturity = _day(row["Data Base"]), _day(row["Data Vencimento"])
            kind = _kind(row["Tipo Titulo"])
            if day > as_of:
                skipped += 1
                continue
            if day > maturity:
                raise ValueError("price_observed_after_maturity")
            item = {"date": day.isoformat()}
            for field, original in numeric.items():
                number_value = _decimal(row[original], price=field.endswith("price"))
                item[field] = float(number_value) if number_value is not None else None
            if all(item[field] is None for field in ("buy_price", "sell_price", "base_price")):
                raise ValueError("no_observed_price")
            identity = (kind, maturity.isoformat())
            key = (*identity, item["date"])
            if key in seen:
                if seen[key] != item:
                    raise ValueError("conflicting_price_identity_and_date")
                continue
            seen[key] = item
            series[identity].append(item)
            all_dates.append(day)
        except ValueError as error:
            if str(error) == "conflicting_price_identity_and_date":
                raise
            rejected += 1
            if len(issues) < 50:
                issues.append({"code": str(error), "row": number})
    for observations in series.values():
        observations.sort(key=lambda item: item["date"])
    return _summary(dict(series), count, skipped, rejected, issues, all_dates)


def parse_paid_coupons(content, as_of):
    """Only the coupon-only resource and known coupon structures establish interest."""
    series, seen, issues, all_dates, unclassified = defaultdict(list), {}, [], [], []
    count = skipped = rejected = 0
    for number, row in _csv(content, COUPON_FIELDS, exact=True):
        count += 1
        try:
            day, maturity = _day(row["Data Resgate"]), _day(row["Vencimento do Titulo"])
            kind = _kind(row["Tipo Titulo"])
            if day > as_of:
                skipped += 1
                continue
            unit, quantity, total = [_decimal(row[field], positive=True) for field in ("PU", "Quantidade", "Valor")]
            if any(value is None for value in (unit, quantity, total)):
                raise ValueError("missing_coupon_amount_or_quantity")
            if abs(unit * quantity - total) > max(Decimal(".05"), total * Decimal(".000001")):
                raise ValueError("coupon_amount_does_not_reconcile")
            identity = (kind, maturity.isoformat())
            item = {"payment_date": day.isoformat(), "gross_coupon_per_unit": float(unit),
                    "observed_quantity": float(quantity), "observed_aggregate_amount": float(total),
                    "principal_counted_as_income": False, "component": "coupon_interest_only",
                    "after_nominal_maturity": day > maturity,
                    "source_id": "paid_coupons"}
            all_dates.append(day)
            if kind not in COUPON_KINDS:
                unclassified.append({"kind": kind, "maturity": maturity.isoformat(), "payment_date": day.isoformat(),
                                     "observed_unit_amount": float(unit), "observed_aggregate_amount": float(total),
                                     "income_per_unit": None, "status": "principal_interest_split_unvalidated"})
                continue
            key = (*identity, item["payment_date"])
            if key in seen:
                if seen[key]["gross_coupon_per_unit"] != item["gross_coupon_per_unit"]:
                    raise ValueError("conflicting_coupon_identity_and_date")
                continue
            seen[key] = item
            series[identity].append(item)
        except ValueError as error:
            if str(error) == "conflicting_coupon_identity_and_date":
                raise
            rejected += 1
            if len(issues) < 50:
                issues.append({"code": str(error), "row": number})
    for events in series.values():
        events.sort(key=lambda item: item["payment_date"])
    result = _summary(dict(series), count, skipped, rejected, issues, all_dates)
    result["unclassified_payments"] = unclassified
    return result


def _download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "RendaFutura academic official Treasury bundle",
                                                   "Accept": "application/json,text/csv,text/html"})
    opener = urllib.request.build_opener(treasury_current._PublicRedirects(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=25) as response:
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise ValueError("download_size_limit")
        if response.status != 200:
            raise ValueError("unexpected_http_status")
        return content, {"http_status": response.status,
                         "headers": {key.lower(): value for key, value in response.headers.items()
                                     if key.lower() in HEADER_NAMES}}


def _read(path, root):
    treasury_current._contained(path, root)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("missing_or_oversized_cache")
    return path.read_bytes()


def _load_cache(path, meta_path, legacy, url, root):
    if path.exists() or meta_path.exists():
        content = _read(path, root)
        metadata = json.loads(_read(meta_path, root))
        if metadata.get("url") != url or metadata.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError("cache_integrity_mismatch")
        return content, metadata
    if legacy is None or not legacy.is_file():
        return None, None
    content = _read(legacy, root)
    meta_file = legacy.with_name(legacy.name + ".meta.json")
    metadata = {}
    if meta_file.is_file():
        previous = json.loads(_read(meta_file, root))
        if previous.get("url") == url and previous.get("sha256") == hashlib.sha256(content).hexdigest():
            metadata = {key: previous[key] for key in ("retrieved_at", "http_status", "headers") if key in previous}
    return content, {"url": url, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                     "origin": "existing_local_cache", "original_file": legacy.relative_to(root).as_posix(),
                     "retrieved_at": None, "http_status": None, "headers": {}, **metadata}


def _source(source_id, url, name, legacy, parser, root, cache, refresh, now):
    path, meta_path = cache / name, cache / (name + ".source.json")
    request_path = cache / (name + ".request.json")
    issues, cached, metadata, parsed = [], None, None, None
    record = {"source_id": source_id, "url": url, "status": "unavailable", "data_status": "unavailable",
              "http_status": None, "headers": {}, "request_attempts": 0, "as_of": None}
    if request_path.is_file():
        try:
            previous_request = json.loads(_read(request_path, root))
            if previous_request.get("url") == url:
                record["last_request"] = previous_request
        except (ValueError, OSError, TypeError):
            pass
    try:
        cached, metadata = _load_cache(path, meta_path, legacy, url, root)
        if cached is not None:
            parsed = parser(cached)
    except (ValueError, OSError, TypeError) as error:
        issues.append({"source_id": source_id, "code": "invalid_cached_source", "error_type": type(error).__name__})
        cached = metadata = parsed = None
    if refresh or cached is None:
        record["request_attempts"] = 1
        request_error = None
        try:
            content, response = _download(url)
            record.update(response)
            fresh = parser(content)
            metadata = {"url": url, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                        "retrieved_at": now.isoformat(), "origin": "official_https_download", **response}
            treasury_current._write(path, content, root)
            treasury_current._write(meta_path, json.dumps(metadata).encode(), root)
            cached, parsed = content, fresh
            record["status"] = "downloaded"
        except (ValueError, OSError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                record["http_status"] = error.code
                record["headers"] = {key.lower(): value for key, value in (error.headers or {}).items()
                                     if key.lower() in HEADER_NAMES}
                code = "http_rate_limited" if error.code == 429 else "http_access_denied" if error.code == 403 else "http_error"
            elif isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
                code = "tls_verification_failed"
            else:
                code = "download_or_parse_failed"
            issues.append({"source_id": source_id, "code": code, "http_status": record["http_status"],
                           "request_attempts": 1, "error_type": type(error).__name__})
            request_error = code
            record["status"] = "cached_after_failed_refresh" if cached is not None else "unavailable"
        record["last_request"] = {"url": url, "requested_at": now.isoformat(), "http_status": record["http_status"],
                                  "headers": record["headers"], "error_code": request_error, "attempts": 1}
        treasury_current._write(request_path, json.dumps(record["last_request"]).encode(), root)
    elif cached is not None:
        record["status"] = "cached"
    if cached is not None:
        # Imported caches keep their original evidence; refresh failures cannot advance their date.
        if not path.is_file():
            treasury_current._write(path, cached, root)
            treasury_current._write(meta_path, json.dumps(metadata).encode(), root)
        record.update({"file": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(cached).hexdigest(),
                       "bytes": len(cached), "retrieved_at": metadata.get("retrieved_at"),
                       "cached_http_status": metadata.get("http_status"), "cached_headers": metadata.get("headers", {}),
                       "cache_origin": metadata.get("origin"),
                       "data_status": "partial" if parsed.get("rejected_row_count") or parsed.get("failures") else "observed"})
        for key in ("row_count", "included_row_count", "rows_after_cutoff", "rejected_row_count",
                    "first_observation_date", "last_observation_date"):
            if key in parsed:
                record[key] = parsed[key]
        record["as_of"] = parsed.get("last_observation_date")
        issues.extend({"source_id": source_id, **item} for item in parsed.get("issues", parsed.get("failures", [])))
    return parsed, record, issues


def _quote_source(parser, content):
    result = parser(content)
    dates = [item["as_of"] for item in result.get("instruments", [])]
    if not dates and (result.get("market_status") or {}).get("as_of"):
        dates = [result["market_status"]["as_of"]]
    result["first_observation_date"] = min(dates) if dates else None
    result["last_observation_date"] = max(dates) if dates else None
    result["row_count"] = result.get("observed_rows", 0)
    return result


def collect_treasury_bundle(folder, as_of, refresh=False):
    now, root = _utcnow(), Path(folder).resolve()
    cutoff, instant = _cutoff(as_of, now)
    cache = treasury_current._contained(root / "raw" / "official-treasury-bundle", root)
    cache.mkdir(parents=True, exist_ok=True)
    definitions = [
        ("current_offers", treasury_current.CURRENT_URL, "current-offers.json", root / "raw/current/official-investir.json",
         lambda body: _quote_source(lambda data: treasury_current.parse_official_json(data, now=now), body)),
        ("price_history", PRICE_URL, "price-history.csv", root / "raw/tesouro-precos.csv", lambda body: parse_price_history(body, cutoff)),
        ("paid_coupons", COUPON_URL, "paid-coupons.csv", root / "raw/income/treasury-coupons.csv", lambda body: parse_paid_coupons(body, cutoff)),
        ("market_status", treasury_current.PAGE_URL, "market-status.html", root / "raw/current/official-offers.html",
         lambda body: _quote_source(lambda data: treasury_current.parse_public_html(data, now=now), body)),
    ]
    data, sources, issues = {}, [], []
    for source_id, url, name, legacy, parser in definitions:
        parsed, source, source_issues = _source(source_id, url, name, legacy, parser, root, cache, refresh, now)
        data[source_id] = parsed
        sources.append(source)
        issues.extend(source_issues)
    prices = (data.get("price_history") or {}).get("series", {})
    coupons = (data.get("paid_coupons") or {}).get("series", {})
    market = (data.get("market_status") or {}).get("market_status", {})
    instruments = []
    for original in (data.get("current_offers") or {}).get("instruments", []):
        item = dict(original)
        identity = (item["kind"], item["maturity"])
        history, events = prices.get(identity, []), coupons.get(identity, [])
        if datetime.fromisoformat(item["as_of"]) > instant:
            issues.append({"source_id": "current_offers", "code": "quote_after_requested_as_of",
                           "kind": item["kind"], "maturity": item["maturity"]})
            item.update(buy_price=None, buyPrice=None, sell_price=None, buy_rate=None, sell_rate=None, tradable=None)
        elif item.get("catalogue_group") == "TesouroLegado" and market.get("state") in {"closed", "suspended"}:
            item["tradable"], item["market_status"] = False, market["state"]
            item["market_status_source_url"] = treasury_current.PAGE_URL
        elif item.get("catalogue_group") == "TesouroLegado" and market.get("state") == "open" and item["buy_price"] is not None:
            item["tradable"], item["market_status"] = True, "open"
        invalid_contract = item["kind"] in UNVALIDATED_KINDS
        item.update({"observed_history_count": len(history),
                     "observed_history_month_count": len({row["date"][:7] for row in history}),
                     "history_first_date": history[0]["date"] if history else None,
                     "history_last_date": history[-1]["date"] if history else None,
                     "history_status": "observed_quotes_only" if history else "unknown_no_observed_history",
                     "price_history_ref": {"kind": item["kind"], "maturity": item["maturity"], "source_id": "price_history"},
                     "paid_coupon_status": "unknown_unvalidated_principal_interest_split" if invalid_contract else
                                           "observed_partial_coverage" if events else "unknown_no_observed_coupon",
                     "paid_coupon_count": len(events), "actual_coupon_events": events,
                     "last_observed_coupon_date": events[-1]["payment_date"] if events else None,
                     "coupon_coverage_complete": None, "missing_coupon_is_zero": False,
                     "contractual_model_status": "unvalidated" if invalid_contract else "not_evaluated_by_data_bundle",
                     "principal_counted_as_income": False, "catalogue_requires_minimum_history": False})
        instruments.append(item)
    offered = {(item["kind"], item["maturity"]) for item in instruments}
    historical = [{"kind": kind, "maturity": maturity, "currently_offered": (kind, maturity) in offered,
                   "observed_history_count": len(rows), "observed_history_month_count": len({row["date"][:7] for row in rows}),
                   "observations": rows, "source_id": "price_history"}
                  for (kind, maturity), rows in sorted(prices.items())]
    paid = [{"kind": kind, "maturity": maturity, "events": events, "source_id": "paid_coupons"}
            for (kind, maturity), events in sorted(coupons.items())]
    for source in sources:
        end = source.get("last_observation_date")
        if source["source_id"] in {"price_history", "paid_coupons"} and end and end < cutoff.isoformat():
            issues.append({"source_id": source["source_id"], "code": "observations_end_before_as_of",
                           "last_observation_date": end, "requested_as_of": cutoff.isoformat(),
                           "absence_after_last_observation_is_unknown": True})
    path = cache / "bundle.json"
    report = {"schema_version": 1, "as_of": cutoff.isoformat(), "collected_at": now.isoformat(),
              "status": "catalogued_with_gaps" if instruments and issues else "catalogued" if instruments else "current_offers_unavailable",
              "instruments": instruments, "sources": sources, "issues": issues,
              "historical_instruments": historical, "paid_coupon_instruments": paid,
              "unclassified_payments": (data.get("paid_coupons") or {}).get("unclassified_payments", []),
              "catalogue_complete": bool((data.get("current_offers") or {}).get("catalogue_complete")),
              "catalogue_requires_minimum_history": False, "uses_synthetic_or_proxy_history": False,
              "historical_quotes_are_total_returns": False, "recommendation_ready": False,
              "counts": {"current_offers": len(instruments), "historical_instruments": len(historical),
                         "historical_observations": sum(len(rows) for rows in prices.values()),
                         "paid_coupon_events": sum(len(events) for events in coupons.values()),
                         "current_offers_with_history": sum(item["observed_history_count"] > 0 for item in instruments),
                         "current_offers_with_coupon_events": sum(item["paid_coupon_count"] > 0 for item in instruments)},
              "report_path": str(path)}
    treasury_current._write(path, json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(), root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    report = collect_treasury_bundle(args.folder, args.as_of, refresh=args.refresh)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "issues": report["issues"],
                      "sources": [{key: source.get(key) for key in ("source_id", "status", "http_status", "as_of", "row_count")}
                                  for source in report["sources"]], "report_path": report["report_path"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
