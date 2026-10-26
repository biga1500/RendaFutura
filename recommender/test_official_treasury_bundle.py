"""Offline synthetic evidence: catalogue membership never depends on prepared models."""

import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request

import pytest

from . import official_treasury_bundle as bundle


NOW = datetime(2026, 10, 24, 23, tzinfo=timezone.utc)
AS_OF = date(2026, 10, 24)
PRICE_COLUMNS = ["Tipo Titulo", "Data Vencimento", "Data Base", "Taxa Compra Manha", "Taxa Venda Manha",
                 "PU Compra Manha", "PU Venda Manha", "PU Base Manha"]
COUPON_COLUMNS = ["Tipo Titulo", "Vencimento do Titulo", "Data Resgate", "PU", "Quantidade", "Valor"]
CLOSED = "<h2>Mercado fechado</h2><p>\u00daltima atualiza\u00e7\u00e3o: 23/10/2026 - 18h00 min</p>".encode()


def csv_bytes(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def current_offers():
    identities = [("Tesouro Selic 2031", "2031-03-01"), ("Tesouro Prefixado 2029", "2029-01-01"),
                  ("Tesouro Prefixado 2032", "2032-01-01"),
                  ("Tesouro Prefixado com Juros Semestrais 2037", "2037-01-01")]
    identities += [(f"Tesouro IPCA+ {year}", f"{year}-08-15") for year in (2032, 2040, 2050)]
    identities += [(f"Tesouro IPCA+ com Juros Semestrais {year}", f"{year}-{month}-15")
                   for year, month in [(2037, "05"), (2045, "05"), (2060, "08")]]
    identities += [(f"Tesouro Renda+ Aposentadoria Extra {2030 + i * 5}", f"{2049 + i * 5}-12-15") for i in range(8)]
    identities += [(f"Tesouro Educa+ {year}", f"{year + 4}-12-15") for year in range(2027, 2045)]
    records = [{"treasuryBondName": name, "maturityDate": maturity + "T00:00", "unitaryInvestmentValue": 1000 + i,
                "unitaryRedemptionValue": 990 + i, "investmentProfitabilityIndexerName": "6,50%",
                "investmentBondMinimumValue": 10, "lastMarketPricingDate": "2026-10-23T18:00:08.193",
                "treasuryBondCode": i + 1} for i, (name, maturity) in enumerate(identities)]
    reserve = {**records[0], "treasuryBondName": "Tesouro Reserva 2036", "maturityDate": "2036-01-01T00:00",
               "investmentProfitabilityIndexerName": "SELIC", "treasuryBondCode": 219,
               "lastMarketPricingDate": "2026-10-24T00:03:27.340"}
    return json.dumps({"TesouroLegado": records, "Tesouro24x7": [reserve]}).encode()


def prices():
    # Nine model-like historical instruments are all outside the current offer list.
    rows = [["Tesouro Prefixado", f"01/01/{2070 + i}", "15/10/2026", "12,00", "12,12", "1000,00", "990,00", "990,00"]
            for i in range(9)]
    for day in ("31/08/2026", "30/09/2026", "15/10/2026"):
        rows.append(["Tesouro Selic", "01/03/2031", day, "0,10", "0,11", "20000,00", "19990,00", "19990,00"])
    rows.append(["Tesouro Renda+ Aposentadoria Extra", "15/12/2049", "15/10/2026", "6,50", "6,60", "1000,00", "990,00", "990,00"])
    return csv_bytes(PRICE_COLUMNS, rows)


def coupons(extra=()):
    rows = [["Tesouro Prefixado com Juros Semestrais", "01/01/2037", "01/07/2026", "48,808850", "2,00", "97,62"],
            ["Tesouro IPCA+ com Juros Semestrais", "15/08/2060", "16/02/2026", "200,00", "2,00", "400,00"],
            ["Tesouro Prefixado com Juros Semestrais", "01/07/2026", "01/07/2026", "48,808850", "2,00", "97,62"]]
    return csv_bytes(COUPON_COLUMNS, [*rows, *extra])


@pytest.fixture
def official_sources(monkeypatch):
    calls = []
    bodies = {bundle.treasury_current.CURRENT_URL: current_offers(), bundle.PRICE_URL: prices(),
              bundle.COUPON_URL: coupons(), bundle.treasury_current.PAGE_URL: CLOSED}

    def download(url):
        calls.append(url)
        return bodies[url], {"http_status": 200, "headers": {"date": "Sat, 24 Oct 2026 23:00:00 GMT", "etag": "test-only"}}

    monkeypatch.setattr(bundle, "_utcnow", lambda: NOW)
    monkeypatch.setattr(bundle, "_download", download)
    return bodies, calls


def test_all_37_offers_remain_when_nine_prepared_models_are_outside_offer_and_history_is_short(tmp_path, official_sources):
    _, calls = official_sources
    pointer = tmp_path / "current.json"
    prepared = tmp_path / "assets.json"
    pointer.write_text('{"snapshot":"unchanged"}', encoding="utf-8")
    prepared.write_text(json.dumps({"assets": [{"ticker": f"MODEL-{i}", "maturity": f"{2070+i}-01-01"} for i in range(9)]}), encoding="utf-8")
    before = (pointer.read_bytes(), prepared.read_bytes())
    report = bundle.collect_treasury_bundle(tmp_path, AS_OF, refresh=True)
    assert len(calls) == len(set(calls)) == 4
    assert len(report["instruments"]) == report["counts"]["current_offers"] == 37
    assert report["catalogue_complete"] is True
    assert report["catalogue_requires_minimum_history"] is False
    assert all(item["catalogue_requires_minimum_history"] is False for item in report["instruments"])
    assert sum(item["observed_history_count"] == 0 for item in report["instruments"]) == 35
    selic = next(item for item in report["instruments"] if item["kind"] == "Tesouro Selic")
    assert selic["observed_history_count"] == selic["observed_history_month_count"] == 3
    assert selic["buy_price"] == 1000 and selic["buy_price"] != 19990
    assert selic["minimumInvestment"] == 10
    assert selic["history_last_date"] == "2026-10-15"
    assert selic["tradable"] is False
    assert selic["paid_coupon_status"] == "unknown_no_observed_coupon"
    assert selic["actual_coupon_events"] == [] and selic["missing_coupon_is_zero"] is False
    assert len(report["historical_instruments"]) == 11
    assert sum(not item["currently_offered"] for item in report["historical_instruments"]) == 9
    for item in report["instruments"]:
        if item["kind"] in bundle.UNVALIDATED_KINDS:
            assert item["contractual_model_status"] == "unvalidated"
            assert item["paid_coupon_status"] == "unknown_unvalidated_principal_interest_split"
            assert item["recommendation_supported"] is False
    assert next(item for item in report["instruments"] if item["kind"] == "Tesouro Reserva")["tradable"] is None
    assert (pointer.read_bytes(), prepared.read_bytes()) == before
    assert report["uses_synthetic_or_proxy_history"] is False and report["historical_quotes_are_total_returns"] is False
    assert json.loads(Path(report["report_path"]).read_bytes())["counts"] == report["counts"]


def test_history_keeps_all_dates_and_types_without_60_month_window_or_current_offer_filter():
    rows = []
    for i in range(85):
        year, month = divmod(2019 * 12 + i, 12)
        rows.append(["Tesouro IGPM+ com Juros Semestrais", "01/12/2040", f"01/{month + 1:02d}/{year}",
                     "6,00", "6,10", "1000,00", "990,00", "990,00"])
    report = bundle.parse_price_history(csv_bytes(PRICE_COLUMNS, rows), AS_OF)
    observations = report["series"][("Tesouro IGPM+ com Juros Semestrais", "2040-12-01")]
    assert len(observations) == 85 and observations[0]["date"] == "2019-01-01"
    assert observations[-1]["date"] == "2026-01-01"
    assert observations[0]["buy_price"] == 1000 and observations[0]["sell_price"] == 990


@pytest.mark.parametrize("status", [403, 429])
def test_single_denied_coupon_refresh_retains_july_cache_and_every_offer(tmp_path, official_sources, monkeypatch, status):
    bodies, calls = official_sources
    cached = tmp_path / "raw/income/treasury-coupons.csv"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(coupons())
    expected_hash = hashlib.sha256(cached.read_bytes()).hexdigest()
    original = bundle._download

    def download(url):
        if url == bundle.COUPON_URL:
            calls.append(url)
            raise urllib.error.HTTPError(url, status, "do-not-persist-secret", {"Retry-After": "3600"}, None)
        return original(url)

    monkeypatch.setattr(bundle, "_download", download)
    report = bundle.collect_treasury_bundle(tmp_path, "2026-10-24", refresh=True)
    assert calls.count(bundle.COUPON_URL) == 1
    assert report["counts"]["current_offers"] == 37
    source = next(item for item in report["sources"] if item["source_id"] == "paid_coupons")
    assert source["status"] == "cached_after_failed_refresh" and source["http_status"] == status
    assert source["headers"]["retry-after"] == "3600"
    assert source["last_observation_date"] == source["as_of"] == "2026-07-01"
    assert source["sha256"] == expected_hash and source["request_attempts"] == 1
    assert source["retrieved_at"] is None  # Legacy file mtime is not a retrieval timestamp.
    assert any(issue.get("http_status") == status for issue in report["issues"])
    assert not any(event["payment_date"].startswith("2026-08") for item in report["instruments"] for event in item["actual_coupon_events"])
    assert "do-not-persist-secret" not in json.dumps(report)
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == expected_hash
    second = bundle.collect_treasury_bundle(tmp_path, AS_OF)
    assert calls.count(bundle.COUPON_URL) == 1 and len(calls) == 4
    second_source = next(item for item in second["sources"] if item["source_id"] == "paid_coupons")
    assert second_source["last_request"]["http_status"] == status
    assert second_source["as_of"] == "2026-07-01"


def test_paid_coupons_keep_actual_final_coupon_but_never_classify_amortization_as_income(tmp_path, official_sources):
    bodies, _ = official_sources
    bodies[bundle.COUPON_URL] = coupons([["Tesouro Renda+ Aposentadoria Extra", "15/12/2049", "01/07/2026", "100,00", "2,00", "200,00"]])
    report = bundle.collect_treasury_bundle(tmp_path, AS_OF, refresh=True)
    prefix = next(item for item in report["instruments"] if item["kind"] == "Tesouro Prefixado com Juros Semestrais")
    assert prefix["paid_coupon_status"] == "observed_partial_coverage"
    assert prefix["actual_coupon_events"][0]["gross_coupon_per_unit"] == pytest.approx(48.80885)
    assert prefix["actual_coupon_events"][0]["observed_aggregate_amount"] == pytest.approx(97.62)
    final = next(item for item in report["paid_coupon_instruments"] if item["maturity"] == "2026-07-01")
    assert final["events"][0]["gross_coupon_per_unit"] == pytest.approx(48.80885)
    assert final["events"][0]["principal_counted_as_income"] is False
    unknown = report["unclassified_payments"][0]
    assert unknown["observed_unit_amount"] == 100 and unknown["income_per_unit"] is None
    renda = next(item for item in report["instruments"] if item["kind"] == "Tesouro Renda+" and item["maturity"] == "2049-12-15")
    assert renda["paid_coupon_count"] == 0 and renda["actual_coupon_events"] == []
    assert renda["paid_coupon_status"] == "unknown_unvalidated_principal_interest_split"
    assert prefix["coupon_coverage_complete"] is None


def test_independent_sources_survive_current_offer_denial_without_using_models_as_replacement(tmp_path, official_sources, monkeypatch):
    _, calls = official_sources
    original = bundle._download

    def download(url):
        if url == bundle.treasury_current.CURRENT_URL:
            calls.append(url)
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return original(url)

    monkeypatch.setattr(bundle, "_download", download)
    report = bundle.collect_treasury_bundle(tmp_path, AS_OF, refresh=True)
    assert len(calls) == 4 and calls.count(bundle.treasury_current.CURRENT_URL) == 1
    assert report["status"] == "current_offers_unavailable" and report["instruments"] == []
    assert report["counts"]["historical_instruments"] == 11
    assert report["counts"]["paid_coupon_events"] == 3
    assert all(source["status"] == "downloaded" for source in report["sources"] if source["source_id"] != "current_offers")


def test_missing_coupons_do_not_remove_any_current_instrument_or_become_zero_cash(tmp_path, official_sources, monkeypatch):
    original = bundle._download

    def download(url):
        if url == bundle.COUPON_URL:
            raise urllib.error.URLError("not available")
        return original(url)

    monkeypatch.setattr(bundle, "_download", download)
    report = bundle.collect_treasury_bundle(tmp_path, AS_OF, refresh=True)
    assert len(report["instruments"]) == 37
    assert all(item["paid_coupon_status"].startswith("unknown") for item in report["instruments"])
    assert all(item["actual_coupon_events"] == [] for item in report["instruments"])
    assert all(item["coupon_coverage_complete"] is None for item in report["instruments"])


def test_cache_hashes_and_dates_are_independent_and_nothing_is_redownloaded_by_default(tmp_path, official_sources):
    _, calls = official_sources
    report = bundle.collect_treasury_bundle(tmp_path, AS_OF, refresh=True)
    for source in report["sources"]:
        assert source["http_status"] == source["cached_http_status"] == 200
        assert source["headers"]["etag"] == "test-only"
        assert source["sha256"] == hashlib.sha256((tmp_path / source["file"]).read_bytes()).hexdigest()
        assert source["retrieved_at"] == NOW.isoformat()
    sources = {item["source_id"]: item for item in report["sources"]}
    assert sources["current_offers"]["as_of"].startswith("2026-10-24")
    assert sources["price_history"]["as_of"] == "2026-10-15"
    assert sources["paid_coupons"]["as_of"] == "2026-07-01"
    repeated = bundle.collect_treasury_bundle(tmp_path, AS_OF)
    assert len(calls) == 4
    assert all(item["request_attempts"] == 0 for item in repeated["sources"])


@pytest.mark.parametrize("mode", ["price", "coupon"])
def test_conflicting_evidence_cannot_silently_select_one_row(mode):
    if mode == "price":
        rows = [["Tesouro Selic", "01/03/2031", "15/10/2026", "0,1", "0,2", price, "990,0", "990,0"] for price in ("1000,0", "2000,0")]
        content, parser, columns = rows, bundle.parse_price_history, PRICE_COLUMNS
    else:
        rows = [["Tesouro Prefixado com Juros Semestrais", "01/01/2037", "01/07/2026", value, "1,0", value]
                for value in ("48,0", "49,0")]
        content, parser, columns = rows, bundle.parse_paid_coupons, COUPON_COLUMNS
    with pytest.raises(ValueError, match="conflicting"):
        parser(csv_bytes(columns, content), AS_OF)


def test_cutoff_applies_to_observations_without_shortening_catalogue(tmp_path, official_sources):
    bodies, _ = official_sources
    bodies[bundle.COUPON_URL] = coupons([["Tesouro Prefixado com Juros Semestrais", "01/01/2037", "01/01/2027", "50,0", "1,0", "50,0"]])
    report = bundle.collect_treasury_bundle(tmp_path, "2026-09-30", refresh=True)
    assert len(report["instruments"]) == 37
    assert all(item["buy_price"] is None and item["tradable"] is None for item in report["instruments"])
    assert all(observation["date"] <= "2026-09-30" for item in report["historical_instruments"] for observation in item["observations"])
    assert all(event["payment_date"] <= "2026-09-30" for item in report["paid_coupon_instruments"] for event in item["events"])
    assert any(issue["code"] == "quote_after_requested_as_of" for issue in report["issues"])


def test_future_cutoff_fails_before_any_network_or_cache_write(tmp_path, official_sources):
    _, calls = official_sources
    with pytest.raises(ValueError, match="future"):
        bundle.collect_treasury_bundle(tmp_path, "2026-10-25", refresh=True)
    assert calls == [] and not (tmp_path / "raw").exists()


def test_coupon_schema_with_unknown_principal_columns_is_not_assumed_interest():
    content = csv_bytes([*COUPON_COLUMNS, "Principal"], [["Tesouro Prefixado com Juros Semestrais", "01/01/2037",
                          "01/07/2026", "1048,8", "1,0", "1048,8", "1000,0"]])
    with pytest.raises(ValueError, match="schema"):
        bundle.parse_paid_coupons(content, AS_OF)


def test_actual_paid_date_after_nominal_maturity_is_preserved_without_contractual_assumptions():
    content = csv_bytes(COUPON_COLUMNS, [["Tesouro IPCA+ com Juros Semestrais", "15/05/2026", "18/05/2026",
                                         "200,0", "2,0", "400,0"]])
    report = bundle.parse_paid_coupons(content, AS_OF)
    event = report["series"][("Tesouro IPCA+ com Juros Semestrais", "2026-05-15")][0]
    assert event["payment_date"] == "2026-05-18" and event["after_nominal_maturity"] is True
    assert event["gross_coupon_per_unit"] == 200 and event["principal_counted_as_income"] is False
    assert report["included_row_count"] == 1 and report["rejected_row_count"] == 0


def test_tls_and_recorded_headers_are_verified_and_secrets_are_not_recorded(monkeypatch):
    class Response:
        status = 200
        headers = {"Content-Type": "text/csv", "Last-Modified": "Fri, 23 Oct 2026 00:00:00 GMT", "Set-Cookie": "private"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == bundle.MAX_BYTES + 1
            return coupons()

    class Opener:
        def open(self, request, timeout):
            assert "Authorization" not in request.headers and timeout == 25
            return Response()

    def build(*handlers):
        context = next(handler._context for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return Opener()

    monkeypatch.setattr(bundle.urllib.request, "build_opener", build)
    _, metadata = bundle._download(bundle.COUPON_URL)
    assert metadata["headers"]["last-modified"].startswith("Fri")
    assert "set-cookie" not in metadata["headers"]
