"""Synthetic current-offer fixtures; tests never access external services."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request

import pytest

from . import treasury_current as current


NOW = datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
CLOSED = "<html><h2>Mercado fechado</h2><p>\u00daltima atualiza\u00e7\u00e3o: 23/10/2026 - 18h00 min</p></html>".encode("utf-8")


def payload():
    names = [("Tesouro Selic 2031", "2031-03-01", "SELIC + 0,0989%"),
             ("Tesouro Prefixado 2029", "2029-01-01", "12,76%"),
             ("Tesouro Prefixado com Juros Semestrais 2037", "2037-01-01", "12,88%"),
             ("Tesouro IPCA+ 2032", "2032-08-15", "IPCA + 6,72%"),
             ("Tesouro IPCA+ com Juros Semestrais 2060", "2060-08-15", "IPCA + 6,46%"),
             ("Tesouro Renda+ Aposentadoria Extra 2030", "2049-12-15", "IPCA + 6,57%"),
             ("Tesouro Educa+ 2027", "2031-12-15", "IPCA + 6,69%")]
    rows = [{"treasuryBondName": name, "maturityDate": maturity + "T00:00",
             "unitaryInvestmentValue": 1000 + i, "unitaryRedemptionValue": 990 + i,
             "investmentProfitabilityIndexerName": rate, "investmentBondMinimumValue": 10,
             "lastMarketPricingDate": "2026-10-23T18:00:08.193", "treasuryBondCode": i + 1}
            for i, (name, maturity, rate) in enumerate(names)]
    reserve = {**rows[0], "treasuryBondName": "Tesouro Reserva 2036", "maturityDate": "2036-01-01T00:00",
               "unitaryInvestmentValue": 11.140827, "unitaryRedemptionValue": 11.140827,
               "investmentProfitabilityIndexerName": "SELIC", "investmentBondMinimumValue": 1,
               "lastMarketPricingDate": "2026-10-24T00:03:27.340", "treasuryBondCode": 219}
    return {"TesouroLegado": rows, "Tesouro24x7": [reserve]}


def encoded(value):
    return json.dumps(value, ensure_ascii=True).encode()


def test_all_offered_structures_include_real_maturity_purchase_minimum_and_unknown_availability():
    report = current.parse_official_json(encoded(payload()), now=NOW)
    assert report["catalogue_complete"] is True and report["failures"] == []
    rows = {item["kind"]: item for item in report["instruments"]}
    assert len(rows) == 8
    assert rows["Tesouro Renda+"]["maturity"] == "2049-12-15"
    assert rows["Tesouro Educa+"]["maturity"] == "2031-12-15"
    selic = rows["Tesouro Selic"]
    assert selic["buy_price"] == selic["buyPrice"] == 1000
    assert selic["sell_price"] == 990
    assert selic["buy_rate"] == pytest.approx(.0989)
    assert selic["rate_basis"] == "spread_over_selic"
    assert selic["minimumInvestment"] == selic["minimum_investment"] == 10
    assert selic["minimumFraction"] is None
    assert selic["as_of"] == "2026-10-23T18:00:08.193000-03:00"
    reserve = rows["Tesouro Reserva"]
    assert reserve["buy_rate"] is None and reserve["buy_rate_text"] == "SELIC"
    assert reserve["minimumInvestment"] == 1
    for item in rows.values():
        assert item["tradable"] is None and item["offered_for_purchase"] is True
        assert item["source_url"] == current.CURRENT_URL
        assert item["recommendation_supported"] is False
        assert item["support_status"].startswith("supported_structure" if item["kind"] in current.SUPPORTED_KINDS else "unsupported_")


@pytest.mark.parametrize("updates,reason", [
    ({"lastMarketPricingDate": "2026-10-25T00:00"}, "Future"),
    ({"lastMarketPricingDate": "2026-09-30T18:00"}, "Stale"),
    ({"lastMarketPricingDate": None}, "timestamp"),
    ({"maturityDate": "2026-10-24T00:00"}, "Matured"),
    ({"maturityDate": "2030-02-30"}, "maturity"),
    ({"unitaryInvestmentValue": -1}, "negative"),
    ({"unitaryInvestmentValue": True}, "numeric"),
    ({"unitaryRedemptionValue": float("nan")}, "Nonfinite"),
    ({"investmentProfitabilityIndexerName": "IPCA + unknown"}, "quote"),
    ({"investmentProfitabilityIndexerName": float("inf")}, "Nonfinite"),
    ({"investmentProfitabilityIndexerName": "1001%"}, "Implausible"),
    ({"minimumInvestmentFraction": 2}, "fraction"),
    ({"isinCode": "not-an-isin"}, "ISIN"),
    ({"treasuryBondCode": True}, "bond code"),
    ({"selicCode": []}, "Selic identity"),
])
def test_bad_quotes_and_identities_are_rejected_with_row_diagnostics(updates, reason):
    data = payload()
    data["TesouroLegado"][0].update(updates)
    report = current.parse_official_json(encoded(data), now=NOW)
    assert len(report["instruments"]) == 7
    assert report["catalogue_complete"] is False
    assert reason in report["failures"][0]["reason"]
    assert all(row["kind"] != "Tesouro Selic" for row in report["instruments"])


def test_zero_and_absent_prices_are_unknown_and_explicit_zero_rate_is_preserved():
    data = payload()
    data["TesouroLegado"][0].update(unitaryInvestmentValue=0, unitaryRedemptionValue=None,
                                  investmentProfitabilityIndexerName="0,00%", investmentBondMinimumValue=0)
    row = next(item for item in current.parse_official_json(encoded(data), now=NOW)["instruments"]
               if item["kind"] == "Tesouro Selic")
    assert row["buy_price"] is row["sell_price"] is row["minimumInvestment"] is None
    assert row["tradable"] is None and row["buy_rate"] == 0


@pytest.mark.parametrize("value", [[], {}, {"response": {}}, {"TesouroLegado": {}}, {"TesouroLegado": [None]}])
def test_schema_drift_does_not_become_an_empty_complete_catalogue(value):
    with pytest.raises(ValueError):
        current.parse_official_json(encoded(value), now=NOW)


def test_duplicate_or_oversized_current_catalogue_is_rejected():
    data = payload()
    data["TesouroLegado"].append(dict(data["TesouroLegado"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        current.parse_official_json(encoded(data), now=NOW)
    data["TesouroLegado"] = [data["TesouroLegado"][0]] * 1001
    with pytest.raises(ValueError, match="limit"):
        current.parse_official_json(encoded(data), now=NOW)


def test_legacy_json_has_separate_buy_and_sell_and_explicit_market_state():
    body = {"response": {"TrsrBondMkt": {"sts": "Fechado", "qtnDtTm": "2026-10-23T18:00:00"},
                         "TrsrBdTradgList": [{"TrsrBd": {"cd": 1, "nm": "Tesouro Selic 2031",
                             "mtrtyDt": "2031-03-01T00:00", "untrInvstmtVal": 20000,
                             "untrRedVal": 19990, "anulInvstmtRate": .1, "anulRedRate": .11}}]}}
    row = current.parse_official_json(encoded(body), source_url=current.LEGACY_URL, now=NOW)["instruments"][0]
    assert row["buy_price"] == 20000 and row["sell_price"] == 19990
    assert row["tradable"] is False and row["market_status"] == "closed"


def test_public_html_reads_explicit_state_and_real_quotes_without_running_scripts():
    body = CLOSED.replace(b"</html>", b"""<script>document.write('Mercado aberto')</script>
      <table><tr><th>Titulo</th><th>Vencimento</th><th>Preco de compra</th><th>Preco de venda</th><th>Taxa de compra</th></tr>
      <tr><td>Tesouro Selic 2031</td><td>01/03/2031</td><td>R$ 20.000,00</td><td>R$ 19.990,00</td><td>SELIC + 0,10%</td></tr></table></html>""")
    report = current.parse_public_html(body, now=NOW)
    assert report["market_status"]["state"] == "closed"
    assert report["instruments"][0]["buy_price"] == 20000
    assert report["instruments"][0]["sell_price"] == 19990
    assert report["instruments"][0]["tradable"] is False
    assert report["catalogue_complete"] is False
    shell = current.parse_public_html(CLOSED, now=NOW)
    assert shell["instruments"] == [] and shell["market_status"]["state"] == "closed"
    missing_time = current.parse_public_html(b"<h2>Mercado aberto</h2>", now=NOW)
    assert missing_time["market_status"]["state"] == "unknown"


def test_brapi_daily_quotes_are_partial_and_never_purchase_availability():
    body = {"results": [{"bondType": "Tesouro Selic", "maturityDate": "2031-03-01",
                         "buyPrice": 20000, "sellPrice": 19990, "buyRate": .1, "baseDate": "2026-10-23"}],
            "requestedAt": NOW.isoformat()}
    report = current.parse_brapi_sandbox(encoded(body), now=NOW)
    assert report["catalogue_complete"] is False
    assert report["instruments"][0]["tradable"] is None
    body["results"][0]["baseDate"] = "2026-08-21"
    stale = current.parse_brapi_sandbox(encoded(body), now=NOW)
    assert stale["instruments"] == [] and "Stale" in stale["failures"][0]["reason"]


def write_manual(path, **updates):
    row = {"kind": "Tesouro Selic", "name": "Tesouro Selic 2031", "maturity": "2031-03-01",
           "buy_price": "20000.50", "sell_price": "", "buy_rate": "SELIC + 0,10%",
           "as_of": "2026-10-23T18:00:00-03:00", "tradable": "unknown", "source_url": current.CURRENT_URL,
           **updates}
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)


def test_manual_csv_is_explicit_offline_and_preserves_missing_quotes(tmp_path, monkeypatch):
    path = tmp_path / "quotes.csv"
    write_manual(path)
    monkeypatch.setattr(current, "_download", lambda *args: pytest.fail("Manual import must be offline"))
    report = current.import_current_csv(path, now=NOW)
    row = report["instruments"][0]
    assert row["buy_price"] == 20000.5 and row["sell_price"] is None
    assert row["tradable"] is None and row["minimumFraction"] is None
    assert report["catalogue_complete"] is False
    assert report["sources"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    write_manual(path, buy_price="20.000,50", buy_rate="0,10")
    assert current.import_current_csv(path, now=NOW, decimal_format="comma")["instruments"][0]["buy_rate"] == .1


@pytest.mark.parametrize("updates", [{"as_of": "2026-09-30"}, {"tradable": "yes"},
                                      {"buy_price": "=1+2"}, {"source_url": "http://example.com"},
                                      {"source_url": "https://user:secret@example.com"},
                                      {"source_url": "https://example.com?token=secret"},
                                      {"kind": "Tesouro Prefixado"}])
def test_manual_csv_rejects_stale_ambiguous_or_secret_containing_evidence(tmp_path, updates):
    path = tmp_path / "quotes.csv"
    write_manual(path, **updates)
    with pytest.raises(ValueError):
        current.import_current_csv(path, now=NOW)


def test_collect_caches_verified_evidence_preserves_pointer_and_scopes_closed_market(tmp_path, monkeypatch):
    calls = []
    primary = encoded(payload())

    def download(url):
        calls.append(url)
        assert url in {current.CURRENT_URL, current.PAGE_URL}
        return (primary, 200, "application/json") if url == current.CURRENT_URL else (CLOSED, 200, "text/html")

    monkeypatch.setattr(current, "_download", download)
    monkeypatch.setattr(current, "_now", lambda value=None: NOW)
    pointer = tmp_path / "current.json"
    pointer.write_text('{"snapshot":"do-not-touch"}', encoding="utf-8")
    report = current.collect_current(tmp_path)
    assert report["status"] == "ready" and report["catalogue_complete"] is True
    assert report["counts"]["instruments"] == report["counts"]["with_buy_quote"] == 8
    assert report["counts"]["not_tradable"] == 7 and report["counts"]["availability_unknown"] == 1
    assert report["counts"]["tradable"] == 0
    assert report["counts"]["supported_structure"] == 5 and report["counts"]["unsupported_structure"] == 3
    assert report["failures"] == [] and len(report["sources"]) == 2
    assert pointer.read_text(encoding="utf-8") == '{"snapshot":"do-not-touch"}'
    assert Path(report["report_path"]).is_relative_to(tmp_path / "raw/current")
    for source in report["sources"]:
        body = (tmp_path / source["file"]).read_bytes()
        assert source["sha256"] == hashlib.sha256(body).hexdigest()
        assert source["http_status"] == 200 and source["cache_hit"] is False
    second = current.collect_current(tmp_path)
    assert len(calls) == 2 and all(source["cache_hit"] for source in second["sources"])
    assert second["instruments"] == report["instruments"]
    current.collect_current(tmp_path, refresh=True)
    assert len(calls) == 4
    (tmp_path / "raw/current/official-investir.json").write_bytes(b"tampered")
    assert current.collect_current(tmp_path)["counts"]["instruments"] == 8
    assert len(calls) == 5


def test_denied_official_access_falls_back_once_to_public_sandbox_without_claiming_full_catalogue(tmp_path, monkeypatch):
    calls = []
    sandbox = encoded({"results": [{"bondType": "Tesouro Selic", "maturityDate": "2031-03-01",
                                    "baseDate": "2026-10-23", "buyPrice": 20000}]})

    def download(url):
        calls.append(url)
        if url in {current.CURRENT_URL, current.LEGACY_URL}:
            raise urllib.error.HTTPError(url, 403 if url == current.CURRENT_URL else 410,
                                         "do-not-log-secret", None, io.BytesIO(b"do-not-log-secret"))
        return (CLOSED, 200, "text/html") if url == current.PAGE_URL else (sandbox, 200, "application/json")

    monkeypatch.setattr(current, "_download", download)
    monkeypatch.setattr(current, "_now", lambda value=None: NOW)
    report = current.collect_current(tmp_path)
    assert calls == [current.CURRENT_URL, current.LEGACY_URL, current.PAGE_URL, current.SANDBOX_URL]
    assert report["status"] == "partial" and report["catalogue_complete"] is False
    assert report["counts"]["instruments"] == 1 and report["counts"]["tradable"] == 0
    assert report["instruments"][0]["tradable"] is None
    assert [row["http_status"] for row in report["failures"]] == [403, 410]
    assert "do-not-log-secret" not in json.dumps(report)


def test_all_network_failures_produce_explicit_unavailable_report(tmp_path, monkeypatch):
    monkeypatch.setattr(current, "_now", lambda value=None: NOW)

    def fail(url):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("private debug text"))

    monkeypatch.setattr(current, "_download", fail)
    report = current.collect_current(tmp_path)
    assert report["status"] == "unavailable" and report["instruments"] == []
    assert len(report["failures"]) == 4
    assert all(row["code"] == "tls_verification_failed" for row in report["failures"])
    assert "private debug text" not in json.dumps(report)


@pytest.mark.parametrize("url", ["http://www.tesourodireto.com.br/data", "https://example.com/data",
                                 "https://www.tesourodireto.com.br:8443/data", "https://secret@www.tesourodireto.com.br/data"])
def test_redirects_cannot_downgrade_tls_change_origin_or_embed_credentials(url):
    with pytest.raises(ValueError, match="origin"):
        current._PublicRedirects().redirect_request(urllib.request.Request(current.CURRENT_URL), None, 302, "", {}, url)


def test_download_uses_verified_tls_and_bounded_read_without_credentials(monkeypatch):
    class Response:
        status, headers = 200, {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == current.MAX_BYTES + 1
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == current.CURRENT_URL and timeout == 25
            assert "Authorization" not in request.headers
            return Response()

    def build(*handlers):
        context = next(handler._context for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED
        return Opener()

    monkeypatch.setattr(current.urllib.request, "build_opener", build)
    assert current._download(current.CURRENT_URL)[0] == b"{}"
    root = Path.cwd().resolve()
    with pytest.raises(ValueError, match="escapes"):
        current._contained(root.parent / "outside-current-cache", root)
