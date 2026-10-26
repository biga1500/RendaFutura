"""Synthetic FRE ZIPs validate identities, revisions and source limitations offline."""

import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request
import zipfile

import pytest

from . import cvm_fre_evidence as fre


CNPJ = "00.000.001/0001-00"
TARGETS = [{"ticker": "AAAA3", "cvm": 1, "share_class": "ON"},
           {"ticker": "AAAA4", "cvm": 2, "share_class": "PN"},
           {"ticker": "UNRES3", "cvm": None, "share_class": "ON"}]


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def csv_content(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("latin1")


def filing(identifier="102", version="2", published="2026-09-01", year=2026):
    return {"CNPJ_CIA": CNPJ, "DT_REFER": f"{year}-12-31", "VERSAO": version,
            "DENOM_CIA": "Synthetic issuer", "CD_CVM": "000001", "CATEG_DOC": "FRE WEB",
            "ID_DOC": identifier, "DT_RECEB": published, "LINK_DOC": "http://www.rad.cvm.gov.br/filing"}


def section(identifier="102", version="2", year=2026, **values):
    return {"CNPJ_Companhia": CNPJ, "Data_Referencia": f"{year}-12-31", "Versao": version,
            "ID_Documento": identifier, "Nome_Companhia": "Synthetic issuer", **values}


def capital(identifier="102", version="2", year=2026, **values):
    return section(identifier, version, year, ID_Capital_Social="20", Tipo_Capital="Capital Emitido",
                   Data_Autorizacao_Aprovacao="2024-04-01", Valor_Capital="1000000.00",
                   Quantidade_Acoes_Ordinarias="200", Quantidade_Acoes_Preferenciais="100",
                   Quantidade_Total_Acoes="300", **values)


def archive(year=2026, *, index=None, tables=None):
    files = {f"fre_cia_aberta_{year}.csv": csv_content(index or [filing(year=year)])}
    for name, rows in (tables or {}).items():
        files[f"fre_cia_aberta_{name}_{year}.csv"] = csv_content(rows)
    return zipped(files)


def dictionary():
    return zipped({"meta_fre_cia_aberta_capital_social.txt": (
        "Campo: Quantidade_Acoes_Ordinarias\r\n   Descri\u00e7\u00e3o : \r\n   Tipo Dados: numeric\r\n   Scale : 0\r\n"
        "Campo: Data_Autorizacao_Aprovacao\r\n   Descri\u00e7\u00e3o : Data declarada\r\n   Tipo Dados: date\r\n").encode("latin1"),
        "meta_fre_cia_aberta_capital_social_desdobramento.txt": b"Campo: Tipo_Evento\n   Tipo Dados: varchar\n"})


def test_latest_received_revision_is_selected_without_rejecting_current_year_reference_label():
    index = [filing("101", "1", "2026-05-01"), filing(), filing("103", "3", "2026-11-01")]
    latest = capital()
    data = archive(index=index, tables={"capital_social": [capital("101", "1"), latest, dict(latest), capital("103", "3")]})
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data, "meta_fre_cia_aberta.zip": dictionary()}, TARGETS)
    assert len(report["capital_snapshots"]) == 1
    selected = report["filings"][0]
    assert selected["filing_id"] == "102" and selected["version"] == 2
    assert selected["published_at"] == "2026-09-01"
    assert selected["reference_date"] == "2026-12-31" and selected["reference_date_is_future_cycle_label"] is True
    assert report["counts"]["filings_after_cutoff"] == 1
    assert report["counts"]["superseded_filing_revisions"] == 1
    assert report["counts"]["duplicate_section_rows_removed"] == 1
    assert report["capital_snapshots"][0]["adjustment_factor"] is None
    assert report["counts"]["paid_cash_events"] == 0
    assert report["instruments"][0]["has_structured_capital_evidence"] is True
    assert report["instruments"][1]["has_received_filing"] is False  # Shared ticker prefix is not identity.
    assert report["instruments"][2]["identity_status"] == "unresolved_CVM_code"


def test_later_receipt_breaks_same_version_tie_but_same_date_ambiguity_is_rejected():
    index = [filing("102", "1", "2026-05-01"), filing("101", "1", "2026-03-01")]
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": archive(index=index)}, TARGETS)
    assert report["filings"][0]["filing_id"] == "102"
    index[1]["DT_RECEB"] = "2026-05-01"
    with pytest.raises(ValueError, match="ambiguous_same_revision"):
        fre.parse_archives({"fre_cia_aberta_2026.zip": archive(index=index)}, TARGETS)


def test_identical_filing_rows_are_deduplicated_without_treating_row_numbers_as_revisions():
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": archive(index=[filing(), filing()])}, TARGETS)
    assert report["counts"]["selected_filing_count"] == 1
    assert report["counts"]["duplicate_filing_rows_removed"] == 1


def test_later_revision_only_structured_data_are_not_backfilled_into_earlier_as_of():
    data = archive(index=[filing("101", "1", "2026-05-01"), filing("102", "2", "2026-09-01")],
                   tables={"capital_social": [capital()]})
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data}, TARGETS, as_of="2026-08-01")
    assert report["filings"][0]["filing_id"] == "101"
    assert report["capital_snapshots"] == []
    assert report["instruments"][0]["has_received_filing"] is True
    assert report["instruments"][0]["has_structured_capital_evidence"] is False


def test_section_requires_same_cnpj_reference_version_and_document_identity():
    wrong = capital()
    wrong["CNPJ_Companhia"] = "00.000.002/0001-00"
    data = archive(tables={"capital_social": [wrong]})
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data}, TARGETS)
    assert report["capital_snapshots"] == []
    assert any(issue["code"] == "section_filing_identity_mismatch" for issue in report["issues"])


def test_actual_missing_tables_stay_unavailable_even_when_dictionary_describes_them():
    data = archive(tables={"capital_social": [capital()], "distribuicao_capital": [section(
        Quantidade_Acoes_Ordinarias_Circulacao="100", Data_Ultima_Assembleia="2026-04-01")]})
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data, "meta_fre_cia_aberta.zip": dictionary()}, TARGETS)
    split = next(row for row in report["archive_schemas"] if row["table"] == "capital_social_desdobramento")
    assert split["dictionary_available"] is True and split["present"] is False
    assert report["annual_dividend_declarations"] == report["reported_approval_count_ratios"] == []
    assert report["instruments"][0]["annual_dividend_status"] == "unavailable_in_downloaded_tables"
    assert any(issue["code"] == "structured_table_not_in_archive" for issue in report["issues"])
    metadata = report["field_definitions"]["capital_social"]
    assert metadata["fields"][0]["description"] == ""
    assert metadata["fields"][1]["description"] == "Data declarada"
    assert metadata["source_ref"].startswith("cvm:FRE:META:")
    assert any(source["url"] == fre.META_URL for source in report["sources"])


def test_preferred_class_quantities_join_only_their_own_capital_record():
    details = [section(ID_Capital_Social="20", Tipo_Classe_Acao_Preferencial="Preferencial Classe A", Quantidade_Acoes="100"),
               section(ID_Capital_Social="99", Tipo_Classe_Acao_Preferencial="Preferencial Classe B", Quantidade_Acoes="999")]
    data = archive(tables={"capital_social": [capital()], "capital_social_classe_acao": details})
    targets = [{"ticker": "TEST5", "cvm": 1, "share_class": "PNA"}, {"ticker": "TEST4", "cvm": 1, "share_class": "PN"}]
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data}, targets)
    preferred = report["capital_snapshots"][0]["preferred_classes"]
    assert len(preferred) == 1 and preferred[0]["share_class"] == "PNA" and preferred[0]["quantity"] == 100
    stocks = {row["ticker"]: row for row in report["instruments"]}
    assert stocks["TEST5"]["has_exact_share_class_evidence"] is True
    assert stocks["TEST4"]["has_exact_share_class_evidence"] is False


def test_class_inventory_differences_are_not_conversions_or_price_adjustment_factors():
    archives = {}
    for year, share in [(2025, "A"), (2026, "C")]:
        archives[f"fre_cia_aberta_{year}.zip"] = archive(year, index=[filing(str(year), year=year, published=f"{year}-09-01")], tables={
            "capital_social": [capital(str(year), year=year)], "capital_social_classe_acao": [section(str(year), year=year,
                ID_Capital_Social="20", Tipo_Classe_Acao_Preferencial=f"Preferencial Classe {share}", Quantidade_Acoes="100")]})
    report = fre.parse_archives(archives, TARGETS)
    change = report["class_snapshot_differences"][0]
    assert change["from_classes"] == ["ON", "PNA"] and change["to_classes"] == ["ON", "PNC"]
    assert change["conversion_factor"] is change["effective_date"] is None
    assert change["usable_for_price_adjustment"] is False


def test_split_approval_ratios_and_class_details_deduplicate_mentions_without_inventing_effective_dates():
    archives = {}
    for year in (2025, 2026):
        identifier = str(year)
        parent = section(identifier, "1", year, ID_Capital_Social_Desdobramento="1", Tipo_Evento="Desdobramento",
                         Data_Aprovacao="2024-02-01", Quantidade_Acoes_Ordinarias_Antes_Aprovacao="100",
                         Quantidade_Acoes_Ordinarias_Depois_Aprovacao="300", Quantidade_Acoes_Preferenciais_Antes_Aprovacao="100",
                         Quantidade_Acoes_Preferenciais_Depois_Aprovacao="300", Quantidade_Total_Acoes_Antes_Aprovacao="200",
                         Quantidade_Total_Acoes_Depois_Aprovacao="600")
        child = section(identifier, "1", year, ID_Capital_Social_Desdobramento="1",
                        Tipo_Classe_Acao_Preferencial="Preferencial Classe A",
                        Quantidade_Acoes_Antes_Aprovacao="50", Quantidade_Acoes_Depois_Aprovacao="100")
        archives[f"fre_cia_aberta_{year}.zip"] = archive(year, index=[filing(identifier, "1", f"{year}-06-01", year)],
            tables={"capital_social_desdobramento": [parent], "capital_social_desdobramento_classe_acao": [child]})
    report = fre.parse_archives(archives, TARGETS)
    ratios = report["reported_approval_count_ratios"]
    assert len(ratios) == 4 and report["counts"]["duplicate_action_mentions_merged"] == 4
    assert next(row for row in ratios if row["share_class"] == "ON")["reported_count_ratio"] == "3"
    assert next(row for row in ratios if row["share_class"] == "PNA")["reported_count_ratio"] == "2"
    assert all(row["effective_date"] is None and row["usable_for_price_adjustment"] is False for row in ratios)
    assert report["counts"]["validated_effective_action_factors"] == 0


def test_annual_aggregate_and_capital_increase_do_not_become_cash_income_or_split_factor():
    data = archive(tables={"distribuicao_dividendos": [section(Exercicio_Social="2025-12-31", Valor_Total="100000.00")],
                          "capital_social_aumento": [section(Data_Deliberacao="2026-03-01", Quantidade_Total_Acoes="1000")]})
    report = fre.parse_archives({"fre_cia_aberta_2026.zip": data}, TARGETS)
    aggregate = report["annual_dividend_declarations"][0]
    assert aggregate["fields"]["Valor_Total"] == "100000.00"
    assert aggregate["cash_per_share"] is aggregate["payment_date"] is None
    assert aggregate["is_paid_cash"] is False
    assert report["reported_approval_count_ratios"] == []
    assert report["counts"]["paid_cash_events"] == 0


@pytest.mark.parametrize("member", ["../outside.csv", "/outside.csv", "nested\\outside.csv"])
def test_zip_traversal_members_are_never_extracted_or_accepted(member):
    content = zipped({member: b"test"})
    if "\\" in member:
        content = content.replace(member.replace("\\", "/").encode(), member.encode())
    with pytest.raises(ValueError, match="unsafe"):
        fre._archive(content)


def test_inventory_targets_are_hash_verified_and_pointers_are_unchanged(tmp_path, monkeypatch):
    root = tmp_path
    path = root / "inventory/snapshots/test/market.json"
    path.parent.mkdir(parents=True)
    content = json.dumps({"stocks": TARGETS}).encode()
    path.write_bytes(content)
    pointer_path = root / "inventory/current.json"
    pointer = {"schema_version": 1, "snapshot": "snapshots/test", "sha256": hashlib.sha256(content).hexdigest()}
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    assert fre.load_targets(root) == fre._targets(TARGETS)
    archives = {"fre_cia_aberta_2026.zip": archive(tables={"capital_social": [capital()]}), "meta_fre_cia_aberta.zip": dictionary()}
    sources = []
    cache = root / "raw/primary-research/fre"
    cache.mkdir(parents=True)
    for name, body in archives.items():
        (cache / name).write_bytes(body)
        sources.append({"archive_name": name, "file": (cache / name).relative_to(root).as_posix(),
                        "url": fre.META_URL if name.startswith("meta") else fre.DATA_URL.format(year=2026),
                        "status": "cached", "http_status": 200, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
    monkeypatch.setattr(fre, "download_sources", lambda *args, **kwargs: sources)
    before = pointer_path.read_bytes()
    report = fre.collect_fre_evidence(root)
    assert pointer_path.read_bytes() == before
    assert Path(report["report_path"]).is_relative_to(cache)
    assert report["counts"]["target_instruments"] == 3
    path.write_bytes(content + b" ")
    with pytest.raises(ValueError, match="hash"):
        fre.load_targets(root)


def install_download(monkeypatch, *, denied=False, oversize=False):
    calls = []
    data = archive()
    metadata = dictionary()

    class Response:
        status = 200

        def __init__(self, body, method):
            self.body, self.method = body, method
            self.headers = {"Content-Length": str(fre.MAX_FILE_BYTES + 1 if oversize else len(body)), "Content-Type": "application/zip"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert self.method == "GET" and limit <= fre.MAX_FILE_BYTES
            return self.body[:limit]

    class Opener:
        def open(self, request, timeout):
            assert fre._allowed(request.full_url)
            assert "b3.com.br" not in request.full_url
            calls.append((request.get_method(), request.full_url))
            if denied:
                raise urllib.error.HTTPError(request.full_url, 403, "private-debug", None, None)
            return Response(metadata if request.full_url == fre.META_URL else data, request.get_method())

    def build(*handlers):
        context = next(handler._context for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return Opener()

    monkeypatch.setattr(fre.urllib.request, "build_opener", build)
    return calls


def test_download_checks_official_lengths_limits_hashes_and_cache_without_b3_calls(tmp_path, monkeypatch):
    calls = install_download(monkeypatch)
    sources = fre.download_sources(tmp_path)
    assert len(calls) == 6 and len(sources) == 3
    for source in sources:
        assert source["http_status"] == source["head_status"] == 200
        assert source["advertised_bytes"] == source["bytes"] < 150_000_000
        assert source["sha256"] == hashlib.sha256((tmp_path / source["file"]).read_bytes()).hexdigest()
    assert sum(row["bytes"] for row in sources) < 200_000_000
    assert all(row["status"] == "cached" for row in fre.download_sources(tmp_path))
    assert len(calls) == 6


def test_oversized_official_head_never_starts_get(tmp_path, monkeypatch):
    calls = install_download(monkeypatch, oversize=True)
    sources = fre.download_sources(tmp_path)
    assert all(method == "HEAD" for method, _ in calls)
    assert all(row["status"] == "unavailable" for row in sources)


def test_access_denial_stops_without_retry_or_request_to_another_service(tmp_path, monkeypatch):
    calls = install_download(monkeypatch, denied=True)
    sources = fre.download_sources(tmp_path)
    assert len(calls) == 1 and sources[0]["http_status"] == 403
    assert all(row["status"] == "stopped_after_access_response" for row in sources[1:])
    assert "private-debug" not in json.dumps(sources)


@pytest.mark.parametrize("url", ["http://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/test.zip",
                                 "https://www.b3.com.br/test", "https://example.com/test",
                                 "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/test.zip?token=secret"])
def test_redirect_cannot_leave_official_fre_open_files_or_bypass_access(url):
    with pytest.raises(ValueError, match="redirect"):
        fre._Redirect().redirect_request(urllib.request.Request(fre.META_URL), None, 302, "", {}, url)
