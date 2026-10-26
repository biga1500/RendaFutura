"""Coverage tests use synthetic diagnostics; no network or local credentials."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from . import data_coverage as coverage


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def market(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    folder = root / "recommender/data/market"
    folder.mkdir(parents=True)
    monkeypatch.setattr(coverage, "ROOT", root)
    monkeypatch.setattr(coverage, "brapi_token", lambda: "test-only-credential-do-not-emit")
    monkeypatch.setenv("RF_DATA_DIR", str(folder))
    return folder


def fixture_dataset(folder):
    names = ["PETR4", "VALE3", "ITUB4", "MGLU3"]
    stocks = [{"ticker": t, "subType": "stock"} for t in names]
    stocks += [{"ticker": f"STOCK{i}", "subType": "stock"} for i in range(762)]
    stocks += [{"ticker": f"UNIT{i}", "subType": "unit"} for i in range(12)]
    bonds = [{"ticker": "TD-SELIC-2027", "kind": "Tesouro Selic", "maturity": "2027-03-01"}]
    bonds += [{"ticker": f"TD-IPCA-{year}", "kind": "Tesouro IPCA+", "maturity": f"{year}-05-15"}
              for year in range(2035, 2043)]
    treasury = [{"name": b["kind"], "maturity": b["maturity"]} for b in bonds]
    excluded = [{"kind": "Tesouro Prefixado", "maturity": f"{year}-01-01",
                 "reason": "less_than_61_consecutive_prices"} for year in range(2043, 2061)]
    excluded += [{"kind": "Tesouro IPCA+ com Juros Semestrais", "maturity": f"{year}-08-15",
                  "reason": "missing_observed_coupon", "months": ["2026-08-31"]} for year in range(2061, 2064)]
    treasury += [{"name": b["kind"], "maturity": b["maturity"]} for b in excluded]
    treasury += [{"name": "Tesouro Educa+", "maturity": f"{year}-12-15"} for year in range(2064, 2092)]
    catalogue = {"stocks": stocks, "treasury": treasury, "collectedAt": "2026-10-23T20:21:32-03:00",
                 "treasuryReference": "2026-10-15",
                 "sources": [{"file": "catalog.json", "retrieved_at": "2026-10-23T23:21:28Z"}]}
    assets = [{"ticker": t, "asset_class": "acoes", "as_of": "2026-09-30"} for t in names[:2]]
    assets += [{"ticker": b["ticker"], "asset_class": "liquidez" if b["kind"] == "Tesouro Selic" else "renda_fixa",
                "as_of": "2026-09-30"} for b in bonds]
    metadata = {"assets": assets, "bonds": bonds, "reference_date": "2026-09-30",
                "forecast_target": {"id": "cash_payments_12m_v2"}, "financially_validated": False,
                "collected_at": "2026-10-23T23:21:42Z", "sources": [
                    {"file": "older.json", "retrieved_at": "2026-10-23T23:00:00Z"},
                    {"file": "newer.json", "retrieved_at": "2026-10-23T20:21:20-03:00",
                     "url": "https://user:password@brapi.dev/api?token=hidden-query-secret#hidden-fragment"}]}
    report = {"assets": [a["ticker"] for a in assets], "reference_date": "2026-09-30",
              "excluded_bonds": excluded, "source_manifest": metadata["sources"]}
    snapshot = folder / "snapshots/test-snapshot"
    write_json(snapshot / "assets.json", metadata)
    write_json(snapshot / "collection-report.json", report)
    write_json(folder / "current.json", {"schema_version": 1, "status": "ready", "snapshot": "snapshots/test-snapshot"})
    write_json(folder / "collection-report.json", {"assets": ["STALE"], "reference_date": "2000-01-01"})
    write_json(folder / "catalog.json", catalogue)
    for ticker in names:
        actions = [{"label": "BONIFICACAO", "factor": 1.1, "lastDatePrior": "2025-03-17"}] if ticker == "ITUB4" else [
            {"label": "GRUPAMENTO", "factor": .1, "lastDatePrior": "2024-05-24"}] if ticker == "MGLU3" else []
        write_json(folder / "coverage" / ("sandbox-" + ticker + ".json"), {
            "results": [{"symbol": ticker, "historicalDataPrice": [],
                         "dividendsData": {"stockDividends": actions, "cashDividends": []}}]})
    write_json(coverage.ROOT / ".sites-runtime/brapi-access.json", {
        "credentialConfigured": True,
        "checks": [{"name": name, "status": status} for name, status in [
            ("basic_quote", 200), ("short_history", 200), ("long_history", 400),
            ("dividends", 403), ("sandbox_raw", 200)]]})
    dataset = SimpleNamespace(assets=assets, dates=["2021-10-31", "2026-09-30"],
                              metadata=metadata, mode="real")
    return dataset, catalogue, report


def test_catalogue_836_vs_11_and_disjoint_reasons(market):
    dataset, _, _ = fixture_dataset(market)
    result = coverage.coverage_summary(dataset)
    assert result["counts"] == {"catalogue": 836, "modelReady": 11, "catalogueReady": 11,
                                "catalogueGap": 825, "readyOutsideCatalogue": 0}
    assert result["catalogue"]["stockSubtypes"] == {"stock": 766, "unit": 12}
    assert result["byAssetClass"]["acoes"] == {"catalogue": 778, "modelReady": 2, "catalogueReady": 2,
                                                 "catalogueGap": 776, "readyOutsideCatalogue": 0}
    assert result["byAssetClass"]["renda_fixa"]["modelReady"] == 8
    assert result["byAssetClass"]["liquidez"]["modelReady"] == 1
    reasons = {row["reason"]: row["count"] for row in result["gaps"]}
    assert reasons == {"outside_collector_universe": 774, "share_scale_normalization_required": 2,
                       "less_than_61_consecutive_prices": 18, "missing_observed_coupon": 3,
                       "unsupported_treasury_structure": 28}
    assert sum(reasons.values()) == result["counts"]["catalogueGap"]
    assert len(result["treasuryGaps"]) == 49
    assert result["readiness"]["snapshotId"] == "test-snapshot"
    assert result["readiness"]["financiallyValidated"] is False
    assert result["dates"]["referenceDate"] == "2026-09-30"
    assert result["dates"]["latestSnapshotSourceRetrievedAt"] == "2026-10-23T23:21:20+00:00"
    assert result["dates"]["latestSourceRetrievedAt"] == "2026-10-23T23:21:28+00:00"
    assert result["status"] == "partial"
    assert result["diagnostics"] == []


def test_probe_and_sandbox_are_observations_without_secrets(market):
    dataset, _, _ = fixture_dataset(market)
    result = coverage.coverage_summary(dataset)
    assert result["credentialConfigured"] is True
    assert result["accessProbe"]["checkedAt"] is None
    assert result["accessProbe"]["fileModifiedAt"] is not None
    checks = {row["name"]: row for row in result["accessProbe"]["checks"]}
    assert checks["basic_quote"]["symbol"] == "BBAS3"
    assert checks["short_history"]["available"] is True
    assert checks["long_history"]["httpStatus"] == 400
    assert checks["dividends"]["httpStatus"] == 403
    for row in result["sandbox"]:
        if row["ticker"] in ("ITUB4", "MGLU3"):
            assert row["evidenceStatus"] == "cached_provider_payload"
            assert "share_scale_normalization_required" in row["blockers"]
            assert "outside_collector_universe" in row["blockers"]
    encoded = json.dumps(result, allow_nan=False)
    for secret in ("test-only-credential", "password", "hidden-query-secret", "hidden-fragment", "?token="):
        assert secret not in encoded


@pytest.mark.parametrize("bad", [None, "{", "[]", '{"stocks":null,"treasury":42}'])
def test_missing_or_malformed_catalogue_retains_loaded_readiness(market, bad):
    dataset, _, _ = fixture_dataset(market)
    path = market / "catalog.json"
    if bad is None:
        path.unlink()
    else:
        path.write_text(bad, encoding="utf-8")
    result = coverage.coverage_summary(dataset)
    assert result["counts"]["catalogue"] is None
    assert result["counts"]["catalogueGap"] is None
    assert result["counts"]["catalogueReady"] is None
    assert result["counts"]["readyOutsideCatalogue"] is None
    assert result["counts"]["modelReady"] == 11
    assert result["status"] == "unavailable"
    assert result["diagnostics"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [None, "{", "[]", '{"checks":[{"name":"long_history","status":true},{"name":[],"status":200}]}'])
def test_missing_or_malformed_probe_does_not_claim_no_access(market, bad):
    dataset, _, _ = fixture_dataset(market)
    path = coverage.ROOT / ".sites-runtime/brapi-access.json"
    if bad is None:
        path.unlink()
    else:
        path.write_text(bad, encoding="utf-8")
    result = coverage.coverage_summary(dataset)
    assert result["accessProbe"]["status"] != "available"
    assert result["credentialConfigured"] is True
    assert result["counts"]["modelReady"] == 11
    assert any(d["source"] == "accessProbe" for d in result["diagnostics"])
    json.dumps(result, allow_nan=False)


def test_failed_local_credential_is_sanitized_and_saved_bool_is_historical(market, monkeypatch):
    dataset, _, _ = fixture_dataset(market)

    def failed():
        raise subprocess.TimeoutExpired("hidden-command-secret", 20)

    monkeypatch.setattr(coverage, "brapi_token", failed)
    result = coverage.coverage_summary(dataset)
    assert result["credentialConfigured"] is False
    assert result["accessProbe"]["credentialConfiguredAtProbe"] is True
    assert "hidden-command-secret" not in json.dumps(result)
    assert {"source": "credential", "code": "unavailable_or_unreadable"} in result["diagnostics"]


@pytest.mark.parametrize("relative", ["../outside", "/outside", "snapshots/../../outside", "snapshots/\u0000invalid"])
def test_unsafe_snapshot_pointer_never_falls_back_to_stale_root_report(market, relative):
    dataset, _, _ = fixture_dataset(market)
    write_json(market / "current.json", {"schema_version": 1, "status": "ready", "snapshot": relative})
    result = coverage.coverage_summary(dataset)
    assert result["readiness"]["snapshotId"] is None
    assert not any(g["reason"] in ("less_than_61_consecutive_prices", "missing_observed_coupon") for g in result["gaps"])
    assert any(d["code"] == "unsafe_snapshot_path" for d in result["diagnostics"])


def test_loaded_snapshot_wins_over_a_republished_pointer(market):
    dataset, _, _ = fixture_dataset(market)
    stored = market / "snapshots/test-snapshot/assets.json"
    replacement = dict(dataset.metadata, collected_at="2026-10-24T00:00:00Z")
    write_json(stored, replacement)
    result = coverage.coverage_summary(dataset)
    assert result["counts"]["modelReady"] == 11
    assert result["readiness"]["snapshotId"] is None
    assert any(d["code"] == "does_not_match_loaded_dataset" for d in result["diagnostics"])


@pytest.mark.parametrize("bad", [None, "{", "[]", '{"assets":null,"excluded_bonds":{}}'])
def test_missing_or_malformed_snapshot_manifest_is_explicitly_unknown(market, bad):
    dataset, _, _ = fixture_dataset(market)
    path = market / "snapshots/test-snapshot/collection-report.json"
    if bad is None:
        path.unlink()
    else:
        path.write_text(bad, encoding="utf-8")
    result = coverage.coverage_summary(dataset)
    assert result["counts"]["modelReady"] == 11
    assert result["dates"]["referenceDate"] == "2026-09-30"
    assert any(d["source"] == "snapshotReport" for d in result["diagnostics"])
    assert any(g["reason"] == "not_evaluated_or_diagnostics_unavailable" for g in result["gaps"])
    assert not any(g["reason"] == "missing_observed_coupon" for g in result["gaps"])
    json.dumps(result, allow_nan=False)


def test_explicit_catalogue_and_direct_snapshot_directory(market, monkeypatch):
    dataset, _, _ = fixture_dataset(market)
    monkeypatch.setenv("RF_DATA_DIR", str(market / "snapshots/test-snapshot"))
    result = coverage.coverage_summary(dataset, market / "catalog.json")
    assert result["readiness"]["snapshotId"] == "test-snapshot"
    assert result["counts"]["catalogueReady"] == 11


def test_duplicates_wrong_symbols_and_malformed_action_fields(market):
    dataset, catalogue, report = fixture_dataset(market)
    catalogue["stocks"] += [catalogue["stocks"][0], None, {"ticker": []}]
    catalogue["treasury"] += [catalogue["treasury"][0], {"name": "bad", "maturity": "bad"}]
    write_json(market / "catalog.json", catalogue)
    write_json(market / "coverage/sandbox-ITUB4.json", {"results": [{"symbol": "WRONG"}]})
    write_json(market / "coverage/sandbox-MGLU3.json", {"results": [{"symbol": "MGLU3", "dividendsData": {
        "stockDividends": [None, {"label": [], "lastDatePrior": "2024-01-01"},
                           {"label": "BONIFICACAO", "factor": float("nan"), "lastDatePrior": "2025-01-01"},
                           {"label": "BONIFICACAO", "lastDatePrior": "2099-01-01"}]}}]})
    report["excluded_bonds"][0]["reason"] = []
    write_json(market / "snapshots/test-snapshot/collection-report.json", report)
    result = coverage.coverage_summary(dataset)
    assert result["counts"]["catalogue"] == 836
    assert result["counts"]["catalogueReady"] == 11
    sandbox = {row["ticker"]: row for row in result["sandbox"]}
    assert sandbox["ITUB4"]["evidenceStatus"] == "unavailable"
    assert sandbox["MGLU3"]["shareEventsInCollectorWindow"] == [
        {"label": "BONIFICACAO", "recordDate": "2025-01-01", "factor": None}]
    assert result["diagnostics"]
    json.dumps(result, allow_nan=False)


def test_demo_and_absent_dataset_are_not_market_ready(market):
    dataset, _, _ = fixture_dataset(market)
    dataset.mode = "demo"
    assert coverage.coverage_summary(dataset)["counts"]["modelReady"] == 0
    result = coverage.coverage_summary(None)
    assert result["counts"]["modelReady"] is None
    assert result["counts"]["catalogueReady"] is None
    assert result["counts"]["catalogueGap"] is None
    assert result["status"] == "unavailable"


def test_unknown_asset_is_outside_catalogue_and_treasury_needs_exact_maturity(market):
    dataset, _, _ = fixture_dataset(market)
    dataset.assets.append({"ticker": "OTHER", "asset_class": "acoes", "as_of": "2026-09-30"})
    dataset.metadata["bonds"][0]["maturity"] = "2027-12-01"
    result = coverage.coverage_summary(dataset)
    assert result["counts"]["modelReady"] == 12
    assert result["counts"]["catalogueReady"] == 10
    assert result["counts"]["readyOutsideCatalogue"] == 2
    assert result["byAssetClass"]["liquidez"]["catalogueReady"] == 0
