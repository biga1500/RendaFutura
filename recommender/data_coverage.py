"""Read-only coverage diagnostics. No HTTP requests, collection, or model fitting.

``coverage_summary(dataset, catalog_path=None)`` accepts an already loaded Dataset.
Readiness means inclusion in that real Dataset, not eligibility or financial
validation. An explicit catalogue also locates its adjacent snapshot diagnostics;
otherwise RF_DATA_DIR (including a direct snapshot path) is respected.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .credentials import brapi_token


ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ("PETR4", "VALE3", "ITUB4", "MGLU3")
SHARE_ACTIONS = {"DESDOBRAMENTO", "GRUPAMENTO", "BONIFICACAO"}
# collect_income_data.treasury_prices accepts precisely these structures.
INCOME_TREASURY_KINDS = {
    "Tesouro Selic", "Tesouro IPCA+", "Tesouro Prefixado",
    "Tesouro Prefixado com Juros Semestrais", "Tesouro IPCA+ com Juros Semestrais",
}
REASONS = {
    "outside_collector_universe": "Symbol is outside the configured, audited stock collector universe.",
    "share_scale_normalization_required": "Reported share actions require explicit normalization of prices, cash per share, and financial features.",
    "less_than_61_consecutive_prices": "The snapshot reports fewer than 61 consecutive monthly prices for 60 monthly returns.",
    "missing_observed_coupon": "The snapshot reports an expected coupon absent from observed payments; zero is not imputed.",
    "unsupported_treasury_structure": "The current income collector does not implement this Treasury structure.",
    "not_evaluated_or_diagnostics_unavailable": "No matching snapshot exclusion establishes the reason for this catalogue gap.",
    "fractional_alias_not_distinct_asset": "Fractional trading code duplicates the same share; not another company or independent asset.",
    "unit_or_other_share_structure_not_reconciled": "Unit or other share structure needs explicit composition and share-scale reconciliation.",
    "B3_active_issuer_mapping_unavailable": "The exact active issuer identity could not be established in the B3 catalogue.",
    "public_source_http_error": "A public data source returned an HTTP error; no missing series was fabricated.",
    "payment_history_too_short": "The reported dated-payment history is too short for the common sample and checks.",
    "recent_share_action_requires_reconciliation": "A recent share transformation requires reconciliation across prices, cash and earnings per share.",
    "cash_amount_or_share_scale_disagrees_with_B3": "The secondary payment amounts do not reconcile with B3 cash rights on a common share scale.",
    "mature_B3_cash_event_missing_from_payment_table": "A mature B3 cash event is absent from the secondary dated-payment history.",
    "B3_cash_event_missing_from_payment_table": "A B3 cash event, including recent rights, is absent from the dated installment history.",
    "insufficient_independent_cash_matches": "Too few mature payment amounts could be independently checked against B3.",
    "current_CVM_accounts_or_share_scale_unavailable": "Required dated CVM financial accounts or their share basis are not available.",
    "fewer_than_24_valid_training_observations": "Fewer than 24 dated training observations survive the financial/share-scale checks.",
    "stock_source_validation_failed": "A stock source/schema/identity check failed; details remain in the immutable collection report.",
}
OFFICIAL_DOCS = {
    "sandbox": "https://brapi.dev/docs/authentication",
    "freeAccess": "https://brapi.dev/faq/api-e-gratis-mesmo",
    "prices": "https://brapi.dev/docs/acoes/historico",
    "distributions": "https://brapi.dev/docs/acoes/dividendos",
}


def _object(path, diagnostics, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        diagnostics.append({"source": label, "code": "missing"})
        return {}
    except (OSError, UnicodeError, ValueError):
        diagnostics.append({"source": label, "code": "unreadable_or_invalid_json"})
        return {}
    if not isinstance(value, dict):
        diagnostics.append({"source": label, "code": "expected_object"})
        return {}
    return value


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _day(value):
    parsed = _instant(value)
    return parsed.date().isoformat() if parsed else None


def _latest(values):
    parsed = [stamp for value in values if (stamp := _instant(value)) is not None]
    return max(parsed).isoformat() if parsed else None


def _modified(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _digest(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _field(asset, name):
    return asset.get(name) if isinstance(asset, dict) else getattr(asset, name, None)


def _symbol(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,29}", value) else None


def _bond_key(kind, maturity):
    day = _day(maturity)
    if isinstance(kind, str) and kind.startswith("Tesouro ") and day:
        return kind, day
    return None


def _catalogue(data, diagnostics):
    stocks, treasury = {}, {}
    for section, result in (("stocks", stocks), ("treasury", treasury)):
        rows = data.get(section)
        if not isinstance(rows, list):
            diagnostics.append({"source": "catalogue." + section, "code": "missing_or_invalid_list"})
            continue
        for row in rows:
            key = (_symbol(row.get("ticker")) if section == "stocks" else
                   _bond_key(row.get("name"), row.get("maturity"))) if isinstance(row, dict) else None
            if key is None or key in result:
                diagnostics.append({"source": "catalogue." + section, "code": "invalid_or_duplicate_instrument"})
                continue
            result[key] = row
    return stocks, treasury


def _snapshot_report(folder, metadata, tickers, diagnostics):
    """Never substitute the stale root report for an immutable snapshot report."""
    pointer_path = folder / "current.json"
    if pointer_path.exists():
        pointer = _object(pointer_path, diagnostics, "snapshotPointer")
        relative = pointer.get("snapshot")
        if (pointer.get("schema_version") != 1 or pointer.get("status") not in ("ready", "partial")
                or not isinstance(relative, str)):
            diagnostics.append({"source": "snapshotPointer", "code": "invalid_or_blocked_pointer"})
            return {}, None
        if not re.fullmatch(r"snapshots[/\\][A-Za-z0-9][A-Za-z0-9._-]*", relative):
            diagnostics.append({"source": "snapshotPointer", "code": "unsafe_snapshot_path"})
            return {}, None
        path = Path(relative)
        try:
            resolved = (folder / path).resolve()
            safe = len(path.parts) == 2 and resolved.is_relative_to((folder / "snapshots").resolve())
        except (OSError, ValueError):
            safe = False
        if not safe:
            diagnostics.append({"source": "snapshotPointer", "code": "unsafe_snapshot_path"})
            return {}, None
        folder = resolved
    stored = _object(folder / "assets.json", diagnostics, "snapshotMetadata")
    if stored != metadata or not metadata:
        diagnostics.append({"source": "snapshotMetadata", "code": "does_not_match_loaded_dataset"})
        return {}, None
    report = _object(folder / "collection-report.json", diagnostics, "snapshotReport")
    reported = report.get("assets")
    if (not isinstance(reported, list) or any(not _symbol(t) for t in reported)
            or set(reported) != tickers or len(reported) != len(tickers)
            or _day(report.get("reference_date")) != _day(metadata.get("reference_date"))):
        diagnostics.append({"source": "snapshotReport", "code": "does_not_match_loaded_dataset"})
        return {}, None
    return report, folder.name if folder.parent.name == "snapshots" else None


def _sources(rows):
    """Only allowlisted provenance fields; never return query strings or credentials."""
    result = {}
    for row in _rows(rows):
        file = row.get("file")
        if (not isinstance(file, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", file)
                or file.startswith("/") or ".." in file.split("/")):
            continue
        try:
            host = urlsplit(str(row.get("url", ""))).hostname
        except ValueError:
            host = None
        if not host or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            host = None
        digest = row.get("sha256")
        digest = digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
        stamp = _latest([row.get("retrieved_at")])
        result[(file, digest)] = {"file": file, "host": host, "sha256": digest, "retrievedAt": stamp}
    return sorted(result.values(), key=lambda row: (row["file"], row["sha256"] or ""))


def _access_probe(diagnostics):
    path = ROOT / ".sites-runtime/brapi-access.json"
    data = _object(path, diagnostics, "accessProbe")
    known = {"basic_quote": "BBAS3", "short_history": "BBAS3", "long_history": "BBAS3",
             "dividends": "BBAS3", "sandbox_raw": "PETR4"}
    checks = []
    seen = set()
    for row in _rows(data.get("checks")):
        name, status = row.get("name"), row.get("status")
        if not isinstance(name, str) or name not in known or name in seen:
            diagnostics.append({"source": "accessProbe", "code": "invalid_or_duplicate_check"})
            continue
        seen.add(name)
        valid = type(status) is int and 100 <= status <= 599
        if not valid:
            diagnostics.append({"source": "accessProbe", "code": "invalid_http_status"})
        checks.append({"name": name, "symbol": known[name], "httpStatus": status if valid else None,
                       "available": 200 <= status < 300 if valid else None})
    complete = seen == set(known) and all(check["httpStatus"] is not None for check in checks)
    if data and not complete:
        diagnostics.append({"source": "accessProbe", "code": "incomplete_checks"})
    return {
        "status": "available" if complete else "incomplete" if data else "unavailable",
        "credentialConfiguredAtProbe": data.get("credentialConfigured") if type(data.get("credentialConfigured")) is bool else None,
        "checkedAt": _latest([data.get("checkedAt"), data.get("collectedAt")]),
        "fileModifiedAt": _modified(path),
        "checks": checks,
        "scope": "Saved observations for these symbols and requests only; not a live probe or account-plan identification. A 400 alone does not prove a plan restriction.",
    }


def _sandbox(folder, tickers, cutoff, collector_symbols, diagnostics):
    results = []
    for ticker in SANDBOX:
        path = folder / "coverage" / ("sandbox-" + ticker + ".json")
        if not path.is_file():
            path = folder / "raw" / ("brapi-" + ticker + ".json")
        data = _object(path, diagnostics, "sandbox." + ticker)
        rows = _rows(data.get("results"))
        row = rows[0] if len(rows) == 1 and rows[0].get("symbol") == ticker else {}
        if data and not row:
            diagnostics.append({"source": "sandbox." + ticker, "code": "invalid_symbol_or_result"})
        dividend_data = row.get("dividendsData")
        dividend_data = dividend_data if isinstance(dividend_data, dict) else {}
        actions = []
        for event in _rows(dividend_data.get("stockDividends")):
            day = _day(event.get("lastDatePrior"))
            label = event.get("label")
            if (not isinstance(label, str) or label not in SHARE_ACTIONS
                    or not day or not cutoff or not "2010-01-01" <= day <= cutoff):
                continue
            factor = event.get("factor")
            factor = factor if type(factor) in (int, float) and math.isfinite(factor) else None
            actions.append({"label": label, "recordDate": day, "factor": factor})
        blockers = []
        if ticker not in tickers:
            if actions:
                blockers.append("share_scale_normalization_required")
            if ticker not in collector_symbols:
                blockers.append("outside_collector_universe")
            blockers.append("paid_cash_and_financial_features_not_validated_for_snapshot")
        results.append({
            "ticker": ticker, "modelReady": ticker in tickers,
            "configuredInCollector": ticker in collector_symbols,
            "evidenceStatus": "cached_provider_payload" if row else "unavailable",
            "payloadSha256": _digest(path) if row else None,
            "requestedAt": _latest([data.get("requestedAt")]),
            "fileModifiedAt": _modified(path),
            "priceHistoryRows": len(row["historicalDataPrice"]) if isinstance(row.get("historicalDataPrice"), list) else None,
            "cashEvents": len(dividend_data["cashDividends"]) if isinstance(dividend_data.get("cashDividends"), list) else None,
            "shareEventsInCollectorWindow": sorted(actions, key=lambda event: event["recordDate"]),
            "blockers": blockers,
            "evidenceCaveat": "Provider-reported events, not an independently reconciled factor chain. Counts alone do not establish complete history or paid cash.",
        })
    return results


def _counts(catalogue, ready, matched):
    if ready is None or catalogue is None:
        matched = None
    return {"catalogue": catalogue, "modelReady": ready, "catalogueReady": matched,
            "catalogueGap": catalogue - matched if catalogue is not None and matched is not None else None,
            "readyOutsideCatalogue": ready - matched if ready is not None and matched is not None else None}


def coverage_summary(dataset, catalog_path=None):
    """Return JSON-safe coverage, observed access, dates, and factual data gaps.

    Missing diagnostics yield null/unknown fields and diagnostic codes. This does
    not validate, enlarge, or invalidate the supplied dataset, nor infer a plan
    from a configured credential. No credentials or raw error messages are emitted.
    """
    from .collect_market_data import STOCKS

    diagnostics = []
    configured = Path(os.environ.get("RF_DATA_DIR", ROOT / "recommender/data/market"))
    base = configured.parent.parent if configured.parent.name == "snapshots" else configured
    catalog_path = Path(catalog_path) if catalog_path is not None else base / "catalog.json"
    base = catalog_path.parent
    catalogue = _object(catalog_path, diagnostics, "catalogue")
    stocks, treasury = _catalogue(catalogue, diagnostics)
    stock_count = len(stocks) if isinstance(catalogue.get("stocks"), list) else None
    treasury_count = len(treasury) if isinstance(catalogue.get("treasury"), list) else None
    mode = getattr(dataset, "mode", None)
    assets = getattr(dataset, "assets", []) if mode == "real" else []
    assets = {_symbol(_field(a, "ticker")): a for a in assets if _symbol(_field(a, "ticker"))}
    ready = len(assets) if dataset is not None else None
    if mode != "real":
        diagnostics.append({"source": "dataset", "code": "not_a_loaded_real_dataset"})
    metadata = getattr(dataset, "metadata", {}) if mode == "real" else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    preparation = metadata.get("stock_universe_preparation")
    if isinstance(preparation, dict) and isinstance(preparation.get("stocks"), list):
        catalogue = {"stocks": [{"ticker": row["ticker"], "subType": "unit" if row["ticker"].endswith("11") else "stock"}
                                for row in preparation["stocks"]],
                     "treasury": [{"name": row["kind"], "maturity": row["maturity"]}
                                  for row in metadata.get("current_treasury_offers", [])],
                     "collectedAt": preparation["prepared_at"], "treasuryReference": preparation["as_of"]}
        stocks, treasury = _catalogue(catalogue, diagnostics)
        stock_count, treasury_count = len(stocks), len(treasury)
    report_folder = configured if configured.parent.name == "snapshots" and configured.parent.parent == base else base
    report, snapshot_id = _snapshot_report(report_folder, metadata, set(assets), diagnostics) if mode == "real" else ({}, None)
    dates = getattr(dataset, "dates", []) if mode == "real" else []
    reference = max(filter(None, [_day(d) for d in dates]), default=None)
    reference = reference or _day(metadata.get("reference_date"))
    collector_symbols = set(metadata.get("collector_stocks", STOCKS))
    sandbox = _sandbox(base, set(assets), reference, collector_symbols, diagnostics)
    sandbox_by_ticker = {row["ticker"]: row for row in sandbox}
    try:
        credential = bool(brapi_token())
    except (OSError, ValueError, subprocess.SubprocessError):
        credential = False
        diagnostics.append({"source": "credential", "code": "unavailable_or_unreadable"})
    probe = _access_probe(diagnostics)

    bond_mapping = {row.get("ticker"): _bond_key(row.get("kind"), row.get("maturity"))
                    for row in _rows(metadata.get("bonds")) if _symbol(row.get("ticker"))}
    matched_stocks, matched_bonds = set(), set()
    ready_by_class, matched_by_class = Counter(), Counter()
    for ticker, asset in assets.items():
        kind = _field(asset, "asset_class")
        kind = kind if kind in ("acoes", "renda_fixa", "liquidez") else "outros"
        ready_by_class[kind] += 1
        if kind == "acoes" and ticker in stocks:
            matched_stocks.add(ticker)
            matched_by_class[kind] += 1
        elif kind != "acoes":
            key = bond_mapping.get(ticker)
            expected_class = "liquidez" if key and key[0] == "Tesouro Selic" else "renda_fixa"
            if key in treasury and key not in matched_bonds and kind == expected_class:
                matched_bonds.add(key)
                matched_by_class[kind] += 1

    exclusions = {}
    for row in _rows(report.get("excluded_bonds")):
        key = _bond_key(row.get("kind"), row.get("maturity"))
        if key:
            exclusions[key] = row
    gaps = defaultdict(list)
    treasury_gaps = []
    stock_exclusions = {row.get("ticker"): row for row in _rows(report.get("excluded_stocks"))}
    for ticker in sorted(stocks.keys() - matched_stocks):
        evidence = sandbox_by_ticker.get(ticker, {})
        recorded = stock_exclusions.get(ticker, {}).get("reason")
        if isinstance(recorded, str):
            reason = recorded if recorded in REASONS else "stock_source_validation_failed"
        else:
            reason = ("share_scale_normalization_required" if evidence.get("shareEventsInCollectorWindow") else
                      "outside_collector_universe" if ticker not in collector_symbols else
                      "not_evaluated_or_diagnostics_unavailable")
        gaps[("acoes", reason)].append(ticker)
    payment_target = metadata.get("forecast_target")
    is_income = isinstance(payment_target, dict) and payment_target.get("id") == "cash_payments_12m_v2"
    for key in sorted(treasury.keys() - matched_bonds):
        kind, maturity = key
        exclusion = exclusions.get(key, {})
        reason = exclusion.get("reason")
        if not isinstance(reason, str) or reason not in REASONS:
            reason = ("unsupported_treasury_structure" if is_income and kind not in INCOME_TREASURY_KINDS else
                      "not_evaluated_or_diagnostics_unavailable")
        asset_class = "liquidez" if kind == "Tesouro Selic" else "renda_fixa"
        gaps[(asset_class, reason)].append(kind + " " + maturity)
        treasury_gaps.append({"name": kind, "maturity": maturity, "reason": reason,
                              "missingCouponMonths": sorted({day for value in exclusion.get("months", [])
                                                            if (day := _day(value))})
                              if isinstance(exclusion.get("months"), list) else []})
    catalogue_by_class = {"acoes": stock_count,
                          "liquidez": sum(key[0] == "Tesouro Selic" for key in treasury) if treasury_count is not None else None,
                          "renda_fixa": sum(key[0] != "Tesouro Selic" for key in treasury) if treasury_count is not None else None,
                          "outros": 0}
    by_class = {kind: _counts(count, ready_by_class[kind] if ready is not None else None, matched_by_class[kind])
                for kind, count in catalogue_by_class.items()}
    total = stock_count + treasury_count if stock_count is not None and treasury_count is not None else None
    counts = _counts(total, ready, len(matched_stocks) + len(matched_bonds))
    source_rows = _rows(metadata.get("sources")) + _rows(report.get("source_manifest"))
    snapshot_sources = _sources(source_rows)
    catalogue_sources = _sources(catalogue.get("sources"))
    subtypes = Counter(row.get("subType") if row.get("subType") in ("stock", "unit") else "other" for row in stocks.values())
    return {
        "schemaVersion": 1,
        "status": "unavailable" if total is None or ready is None or mode != "real" else
                  "complete" if counts["catalogueGap"] == 0 and counts["readyOutsideCatalogue"] == 0 else "partial",
        "datasetMode": mode if mode in ("real", "demo") else None,
        "counts": counts, "byAssetClass": by_class,
        "catalogue": {"stocks": stock_count, "treasury": treasury_count,
                      "fractionalAliases": sum(ticker.endswith("F") for ticker in stocks),
                      "nonFractionalStockCodes": sum(not ticker.endswith("F") for ticker in stocks),
                      "stockSubtypes": dict(sorted(subtypes.items())),
                      "collectedAt": _latest([catalogue.get("collectedAt")]),
                      "treasuryReference": _day(catalogue.get("treasuryReference")),
                      "policy": "Catalogue entries are symbols/instruments, including fractional symbols and units; not unique issuers, guaranteed tradability, or model readiness."},
        "readiness": {"tickers": sorted(assets), "snapshotId": snapshot_id,
                      "financiallyValidated": metadata.get("financially_validated") is True,
                      "policy": "Included in the supplied, loaded real dataset. This does not mean a financially validated forecast or suitability for every investor."},
        "credentialConfigured": credential,
        "accessProbe": probe, "sandbox": sandbox,
        "dates": {"referenceDate": reference,
                  "firstReturnDate": min(filter(None, [_day(d) for d in dates]), default=None),
                  "latestAssetReference": max(filter(None, [_day(_field(a, "as_of")) for a in assets.values()]), default=None),
                  "snapshotCollectedAt": _latest([metadata.get("collected_at")]),
                  "latestSnapshotSourceRetrievedAt": _latest([row["retrievedAt"] for row in snapshot_sources]),
                  "latestCatalogueSourceRetrievedAt": _latest([row["retrievedAt"] for row in catalogue_sources]),
                  "latestSourceRetrievedAt": _latest([row["retrievedAt"] for row in snapshot_sources + catalogue_sources])},
        "sources": {"snapshot": snapshot_sources, "catalogue": catalogue_sources},
        "gaps": [{"assetClass": kind, "reason": reason, "count": len(items),
                  "description": REASONS[reason], "examples": items[:5]}
                 for (kind, reason), items in sorted(gaps.items())],
        "treasuryGaps": treasury_gaps,
        "externalNeeds": [
            {"code": "equity_history_and_share_basis", "requirement": "For additional symbols: authorized long price histories with at least 61 aligned monthly prices, corporate-action factors and verified cash per share on the same share basis. Reconcile splits, reverse splits, bonuses, conversions and units before combining prices, cash and EPS; never add cash again to adjusted total-return prices."},
            {"code": "equity_payments_and_disclosures", "requirement": "Observed payment dates and amounts, record/ex dates, approval/publication dates and completeness evidence; issuer/class-to-CVM mapping and dated DFP/ITR filings with justified sector-specific financial features. The current collector filters financial statements to its configured issuers; free sandbox access alone cannot add ITUB4 or MGLU3."},
            {"code": "treasury_history_and_coupons", "requirement": "For the listed exclusions: 61 actual consecutive monthly prices per title and observed coupon-only payments for the missing months. Recent titles may require more elapsed history. Do not invent prices, coupons, or substitute principal redemptions for income."},
            {"code": "treasury_structure_support", "requirement": "Official cash-flow and amortization evidence plus collector support for Educa+, Renda+ and IGPM structures; separate principal from income and validate the payment schedule before inclusion."},
            {"code": "capacity_and_validation", "requirement": "A broad universe is screened before a bounded optimization shortlist; deferred eligible instruments are reported separately. More coverage does not establish out-of-sample forecast quality or historical point-in-time provenance."},
        ] if counts["catalogueGap"] is None or counts["catalogueGap"] > 0 else [],
        "officialDocumentation": OFFICIAL_DOCS.copy(),
        "diagnostics": diagnostics,
    }


def main():
    import argparse
    from .datasets import real_dataset

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("RF_DATA_DIR", ROOT / "recommender/data/market")))
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset = None
    try:
        dataset = real_dataset(args.data_dir)
    except (OSError, ValueError):
        pass
    base = args.data_dir.parent.parent if args.data_dir.parent.name == "snapshots" else args.data_dir
    summary = coverage_summary(dataset, args.catalog or base / "catalog.json")
    encoded = json.dumps(summary, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps({"status": summary["status"], "counts": summary["counts"]}, allow_nan=False))
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
