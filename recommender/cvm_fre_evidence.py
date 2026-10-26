"""Bounded primary CVM FRE research; annual declarations are never paid cash."""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import ssl
import tempfile
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile


DATASET_URL = "https://dados.cvm.gov.br/dataset/cia_aberta-doc-fre"
DATA_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS/fre_cia_aberta_{year}.zip"
META_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/META/meta_fre_cia_aberta.zip"
MAX_FILE_BYTES = 150_000_000
MAX_TOTAL_BYTES = 200_000_000
HEADERS = {"content-length", "content-type", "date", "last-modified", "etag"}
TABLES = {
    "capital_social": "capital_snapshot_not_an_action",
    "capital_social_classe_acao": "capital_share_class_snapshot",
    "capital_social_titulo_conversivel": "conversion_terms_not_executed_conversion",
    "distribuicao_capital": "shareholder_and_free_float_distribution_not_dividends",
    "distribuicao_capital_classe_acao": "free_float_by_preferred_class_not_dividends",
    "capital_social_desdobramento": "reported_split_grouping_bonus",
    "capital_social_desdobramento_classe_acao": "reported_split_grouping_bonus_by_class",
    "capital_social_aumento": "reported_capital_increase_not_split_factor",
    "capital_social_aumento_classe_acao": "reported_capital_increase_by_class",
    "capital_social_reducao": "reported_capital_reduction_not_split_factor",
    "capital_social_reducao_classe_acao": "reported_capital_reduction_by_class",
    "distribuicao_dividendos": "reported_dividend_aggregate_not_paid_cash",
    "distribuicao_dividendos_classe_acao": "reported_dividend_aggregate_by_class_not_paid_cash",
}
IDENTITY_FIELDS = {"CNPJ_Companhia", "Data_Referencia", "Versao", "ID_Documento", "Nome_Companhia"}
LIMITATIONS = [
    "Only the exact downloaded CSV members establish structured table availability; a dictionary or dataset listing does not establish event coverage.",
    "Distribuicao de capital describes shareholders/free float, not dividends or corporate-action factors.",
    "Capital snapshots and differences between classes or years do not establish a dated split, conversion, bonus or adjustment factor.",
    "DT_RECEB is the filing receipt date; retrieval after as_of does not prove the archive was identical at that date.",
    "DT_REFER is a document reference cycle, which may end on December 31 after receipt; it is not an observed cash/payment date.",
    "Structured sections can contain only the latest revision; missing older sections are never reconstructed from a later revision.",
    "Annual aggregate distributions are not payment dates, paid cash, per-share income, or independent settlement evidence.",
    "Approval-date share-count ratios are not effective-date price-adjustment factors; no factor is applied to prices or income.",
]


def _utcnow():
    return datetime.now(timezone.utc)


def _path(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("research_path_outside_root")
    return path


def _write(path, content, root):
    _path(path, root)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fre-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        _path(path, root)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _allowed(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "dados.cvm.gov.br"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path.startswith("/dados/CIA_ABERTA/DOC/FRE/"))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not _allowed(newurl):
            raise ValueError("non_cvm_fre_redirect_rejected")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _archive(content):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("zip_file_size_limit")
    archive = zipfile.ZipFile(io.BytesIO(content))
    names = set()
    if len(archive.infolist()) > 1000:
        raise ValueError("zip_member_count_limit")
    if sum(item.file_size for item in archive.infolist()) > MAX_TOTAL_BYTES:
        raise ValueError("zip_expanded_size_limit")
    for item in archive.infolist():
        parts = PurePosixPath(item.filename)
        if (item.filename in names or parts.is_absolute() or ".." in parts.parts or "\\" in item.filename
                or "\\" in item.orig_filename or "\x00" in item.orig_filename
                or item.file_size > MAX_FILE_BYTES or item.flag_bits & 1):
            raise ValueError("unsafe_or_oversized_zip_member")
        names.add(item.filename)
    return archive


def _decode(content):
    try:
        return content.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        return content.decode("latin1"), "latin1"


def _day(value):
    parsed = date.fromisoformat(str(value)[:10])
    return parsed


def _integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("invalid_integer_identity")
    return int(value)


def _number(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ValueError("invalid_dot_decimal_number")
    parsed = Decimal(value)
    if not parsed.is_finite() or not math.isfinite(float(parsed)):
        raise ValueError("nonfinite_number")
    return int(parsed) if parsed == parsed.to_integral_value() else str(parsed)


def _normal(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value)
                            if not unicodedata.combining(c)).lower().split())


def _class(value):
    if not value:
        return None
    direct = value.strip().upper()
    if re.fullmatch(r"PN[A-Z]?|ON", direct):
        return direct
    name = _normal(value)
    if name == "preferencial":
        return "PN"
    match = re.fullmatch(r"preferencial classe ([a-z])", name)
    return "PN" + match[1].upper() if match else None


def _rows(archive, member, required):
    text, encoding = _decode(archive.read(member))
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not set(required) <= set(fields):
        raise ValueError("missing_or_duplicate_fre_columns:" + member)
    rows = []
    for number, row in enumerate(reader, 2):
        if number > 2_000_001 or None in row or any(value is None for value in row.values()):
            raise ValueError("invalid_or_oversized_fre_csv:" + member)
        rows.append((number, row))
    return fields, rows, encoding


def parse_dictionary(content):
    """Preserve the official TXT dictionary, including empty field descriptions."""
    result = {}
    with _archive(content) as archive:
        for member in archive.namelist():
            match = re.fullmatch(r"meta_fre_cia_aberta(?:_(.+))?\.txt", member)
            if not match:
                continue
            table = match[1] or "filing_index"
            if table not in TABLES and table != "filing_index":
                continue
            text, encoding = _decode(archive.read(member))
            fields, current = [], None
            for line in text.replace("\r", "\n").splitlines():
                if line.startswith("Campo:"):
                    current = {"field": line.split(":", 1)[1].strip()}
                    fields.append(current)
                elif current is not None and ":" in line:
                    key, value = line.split(":", 1)
                    key = {"descricao": "description", "dominio": "domain", "tipo dados": "data_type",
                           "tamanho": "size", "precisao": "precision", "scale": "scale"}.get(_normal(key.strip()))
                    if key:
                        current[key] = value.strip()
            result[table] = {"member": member, "encoding": encoding, "fields": fields,
                             "sha256": hashlib.sha256(archive.read(member)).hexdigest()}
    return result


def _targets(instruments):
    if not isinstance(instruments, list) or not 1 <= len(instruments) <= 2000:
        raise ValueError("explicit_target_instruments_required")
    seen, output = set(), []
    for row in instruments:
        if not isinstance(row, dict):
            raise ValueError("target_identity_must_be_an_object")
        ticker, cvm = row.get("ticker"), row.get("cvm")
        if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9.-]{1,30}", ticker) or ticker in seen:
            raise ValueError("missing_or_duplicate_target_ticker")
        if cvm is not None:
            cvm = _integer(cvm)
            if cvm <= 0:
                raise ValueError("exact_positive_CVM_code_required")
        if row.get("share_class") is not None and not isinstance(row["share_class"], str):
            raise ValueError("share_class_must_be_reported_text_or_unknown")
        seen.add(ticker)
        output.append({"ticker": ticker, "cvm": cvm, "share_class": row.get("share_class"),
                       "identity_status": "exact_CVM_code" if cvm is not None else "unresolved_CVM_code"})
    return sorted(output, key=lambda row: row["ticker"])


def load_targets(folder):
    """Read a hash-verified local inventory; never call a listing or B3 endpoint."""
    root = Path(folder).resolve()
    pointer_path = _path(root / "inventory/current.json", root)
    pointer = json.loads(pointer_path.read_bytes())
    relative = pointer.get("snapshot", "")
    if pointer.get("schema_version") != 1 or not re.fullmatch(r"snapshots/[A-Za-z0-9-]+", relative):
        raise ValueError("invalid_inventory_pointer")
    path = _path(root / "inventory" / relative / "market.json", root)
    if path.stat().st_size > 20_000_000:
        raise ValueError("inventory_size_limit")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != pointer.get("sha256"):
        raise ValueError("inventory_hash_mismatch")
    return _targets(json.loads(content)["stocks"])


def _latest_filings(rows, cutoff, wanted, source_ref, member, issues):
    by_id, latest, counters = {}, {}, defaultdict(int)
    for number, row in rows:
        try:
            cvm = _integer(row["CD_CVM"])
            if cvm not in wanted:
                continue
            received, reference = _day(row["DT_RECEB"]), _day(row["DT_REFER"])
            if received > cutoff or reference.year > cutoff.year:
                counters["filings_after_cutoff"] += 1
                continue
            version, identifier = _integer(row["VERSAO"]), str(_integer(row["ID_DOC"]))
            cnpj = row["CNPJ_CIA"]
            if not re.fullmatch(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", cnpj) or version <= 0:
                raise ValueError("invalid_filing_identity")
            filing = {"cvm": cvm, "cnpj": cnpj, "filing_id": identifier, "version": version,
                      "reference_date": reference.isoformat(), "published_at": row["DT_RECEB"],
                      "reference_date_is_future_cycle_label": reference > cutoff,
                      "publication_basis": "CVM_DT_RECEB_receipt_date", "issuer_name": row["DENOM_CIA"],
                      "document_url_reported": row.get("LINK_DOC") or None,
                      "source_ref": source_ref, "member": member, "row_number": number}
            if identifier in by_id:
                previous_identity = {key: value for key, value in by_id[identifier].items() if key != "row_number"}
                if previous_identity != {key: value for key, value in filing.items() if key != "row_number"}:
                    raise ValueError("conflicting_filing_id")
                counters["duplicate_filing_rows_removed"] += 1
                continue
            by_id[identifier] = filing
            key = (cvm, reference.isoformat())
            previous = latest.get(key)
            if previous is None or (version, received) > (previous["version"], _day(previous["published_at"])):
                latest[key] = filing
            elif version == previous["version"] and received == _day(previous["published_at"]) and identifier != previous["filing_id"]:
                raise ValueError("ambiguous_same_revision_filing")
        except (KeyError, ValueError) as error:
            if str(error) in {"conflicting_filing_id", "ambiguous_same_revision_filing"}:
                raise
            counters["invalid_filing_rows"] += 1
            if len(issues) < 100:
                issues.append({"code": "invalid_filing_index_row", "member": member, "row": number})
    counters["index_target_filings"] = len(by_id)
    counters["selected_filings"] = len(latest)
    counters["superseded_filing_revisions"] = len(by_id) - len(latest)
    return by_id, {row["filing_id"]: row for row in latest.values()}, dict(counters)


def _reported_ratios(record):
    row, results = record["fields"], []
    action_type = _normal(row.get("Tipo_Evento", ""))
    if action_type not in {"desdobramento", "grupamento", "bonificacao"}:
        return []
    approval = row.get("Data_Aprovacao")
    if not approval:
        return []
    approval = _day(approval).isoformat()
    for share, prefix in (("ON", "Quantidade_Acoes_Ordinarias"), ("PN_AGGREGATE", "Quantidade_Acoes_Preferenciais"),
                          ("ALL_SHARES", "Quantidade_Total_Acoes")):
        before = _number(row.get(prefix + "_Antes_Aprovacao"))
        after = _number(row.get(prefix + "_Depois_Aprovacao"))
        if before is None or after is None or Decimal(str(before)) <= 0 or Decimal(str(after)) <= 0:
            continue
        results.append({"cvm": record["cvm"], "action_type": row["Tipo_Evento"], "share_class": share,
                        "approval_date": approval, "effective_date": None, "before_shares": before, "after_shares": after,
                        "reported_count_ratio": str(Decimal(str(after)) / Decimal(str(before))),
                        "factor_status": "approval_counts_ratio_not_effective_adjustment_factor",
                        "usable_for_price_adjustment": False, "source_evidence": [record["evidence_id"]]})
    return results


def _reported_class_ratio(record, parent):
    row, approval = record["fields"], parent["fields"].get("Data_Aprovacao")
    share = _class(row.get("Tipo_Classe_Acao_Preferencial"))
    kind = parent["fields"].get("Tipo_Evento", "")
    if share is None or not approval or _normal(kind) not in {"desdobramento", "grupamento", "bonificacao"}:
        return []
    before = _number(row.get("Quantidade_Acoes_Antes_Aprovacao"))
    after = _number(row.get("Quantidade_Acoes_Depois_Aprovacao"))
    if before is None or after is None or Decimal(str(before)) <= 0 or Decimal(str(after)) <= 0:
        return []
    return [{"cvm": record["cvm"], "action_type": kind, "share_class": share,
             "approval_date": _day(approval).isoformat(), "effective_date": None,
             "before_shares": before, "after_shares": after,
             "reported_count_ratio": str(Decimal(str(after)) / Decimal(str(before))),
             "factor_status": "approval_counts_ratio_not_effective_adjustment_factor",
             "usable_for_price_adjustment": False,
             "source_evidence": [parent["evidence_id"], record["evidence_id"]]}]


def parse_archives(archives, instruments, as_of="2026-10-24"):
    targets, cutoff = _targets(instruments), _day(as_of)
    wanted = {row["cvm"] for row in targets if row["cvm"] is not None}
    total = sum(len(content) for content in archives.values())
    if total > MAX_TOTAL_BYTES:
        raise ValueError("research_total_size_limit")
    dictionary = parse_dictionary(archives["meta_fre_cia_aberta.zip"]) if "meta_fre_cia_aberta.zip" in archives else {}
    records, schemas, filings, issues, counts = defaultdict(list), [], {}, [], defaultdict(int)
    inventory, expanded = [], 0
    sources = []
    for name, content in sorted(archives.items()):
        match = re.fullmatch(r"fre_cia_aberta_(2025|2026)\.zip", name)
        if name == "meta_fre_cia_aberta.zip":
            digest = hashlib.sha256(content).hexdigest()
            source_ref = "cvm:FRE:META:" + digest
            sources.append({"id": source_ref, "archive_name": name, "url": META_URL,
                            "sha256": digest, "bytes": len(content)})
            for item in dictionary.values():
                item["source_ref"] = source_ref
            continue
        if not match:
            raise ValueError("unsupported_official_FRE_archive_name")
        year = int(match[1])
        digest = hashlib.sha256(content).hexdigest()
        source_ref = "cvm:FRE:" + str(year) + ":" + digest
        sources.append({"id": source_ref, "archive_name": name, "url": DATA_URL.format(year=year),
                        "sha256": digest, "bytes": len(content)})
        with _archive(content) as archive:
            expanded += sum(item.file_size for item in archive.infolist())
            if expanded > MAX_TOTAL_BYTES:
                raise ValueError("combined_expanded_size_limit")
            inventory.append({"archive_name": name, "member_count": len(archive.infolist()),
                              "members": [{"name": item.filename, "bytes": item.file_size,
                                           "compressed_bytes": item.compress_size} for item in archive.infolist()]})
            member = f"fre_cia_aberta_{year}.csv"
            fields, index_rows, encoding = _rows(archive, member, {"CD_CVM", "CNPJ_CIA", "DT_REFER", "VERSAO", "ID_DOC", "DT_RECEB", "DENOM_CIA"})
            known, selected, statistics = _latest_filings(index_rows, cutoff, wanted, source_ref, member, issues)
            for key, value in statistics.items():
                counts[key] += value
            filings.update(selected)
            schemas.append({"archive_name": name, "table": "filing_index", "member": member, "present": True,
                            "columns": fields, "encoding": encoding, "row_count": len(index_rows), "selected_rows": len(selected)})
            for table, role in TABLES.items():
                member = f"fre_cia_aberta_{table}_{year}.csv"
                schema = {"archive_name": name, "table": table, "role": role, "member": member,
                          "present": member in archive.namelist(), "dictionary_available": table in dictionary,
                          "selected_rows": 0}
                schemas.append(schema)
                if not schema["present"]:
                    issues.append({"code": "structured_table_not_in_archive", "archive_name": name,
                                   "table": table, "dictionary_available": table in dictionary})
                    continue
                fields, rows, encoding = _rows(archive, member, IDENTITY_FIELDS)
                schema.update(columns=fields, encoding=encoding, row_count=len(rows),
                              sha256=hashlib.sha256(archive.read(member)).hexdigest())
                seen = set()
                for number, row in rows:
                    identifier = row["ID_Documento"]
                    filing = selected.get(identifier)
                    if filing is None:
                        counts["section_rows_outside_selected_filings"] += 1
                        continue
                    if (row["CNPJ_Companhia"] != filing["cnpj"] or row["Data_Referencia"] != filing["reference_date"]
                            or _integer(row["Versao"]) != filing["version"]):
                        issues.append({"code": "section_filing_identity_mismatch", "member": member, "row": number})
                        continue
                    fingerprint = json.dumps(row, sort_keys=True, ensure_ascii=True)
                    if fingerprint in seen:
                        counts["duplicate_section_rows_removed"] += 1
                        continue
                    seen.add(fingerprint)
                    evidence_id = hashlib.sha256((source_ref + member + fingerprint).encode()).hexdigest()
                    record = {"evidence_id": evidence_id, "cvm": filing["cvm"], "cnpj": filing["cnpj"],
                              "filing_id": identifier, "version": filing["version"], "reference_date": filing["reference_date"],
                              "published_at": filing["published_at"], "source_ref": source_ref,
                              "table": table, "member": member, "row_number": number, "fields": row}
                    records[table].append(record)
                    schema["selected_rows"] += 1
    capital, class_by_capital = [], defaultdict(list)
    for record in records["capital_social_classe_acao"]:
        row = record["fields"]
        class_by_capital[(record["filing_id"], row.get("ID_Capital_Social"))].append({
            "share_class": _class(row.get("Tipo_Classe_Acao_Preferencial")),
            "reported_class": row.get("Tipo_Classe_Acao_Preferencial"),
            "quantity": _number(row.get("Quantidade_Acoes")), "evidence_id": record["evidence_id"]})
    for record in records["capital_social"]:
        row = record["fields"]
        capital.append({key: record[key] for key in ("cvm", "cnpj", "filing_id", "version", "published_at", "reference_date", "evidence_id")} | {
            "capital_id": row.get("ID_Capital_Social"), "capital_type": row.get("Tipo_Capital"),
            "approval_date_reported": row.get("Data_Autorizacao_Aprovacao") or None,
            "capital_amount_reported": _number(row.get("Valor_Capital")), "currency_not_inferred": True,
            "ordinary_shares": _number(row.get("Quantidade_Acoes_Ordinarias")),
            "preferred_shares_aggregate": _number(row.get("Quantidade_Acoes_Preferenciais")),
            "total_shares": _number(row.get("Quantidade_Total_Acoes")),
            "preferred_classes": class_by_capital[(record["filing_id"], row.get("ID_Capital_Social"))],
            "is_corporate_action": False, "adjustment_factor": None})
    exact_classes, issuer_records = defaultdict(set), defaultdict(int)
    snapshot_classes = defaultdict(lambda: defaultdict(set))
    for table_rows in records.values():
        for record in table_rows:
            issuer_records[record["cvm"]] += 1
    for snapshot in capital:
        if _normal(snapshot.get("capital_type") or "") != "capital emitido":
            continue
        classes = snapshot_classes[snapshot["cvm"]][snapshot["filing_id"]]
        if snapshot["ordinary_shares"] is not None and Decimal(str(snapshot["ordinary_shares"])) > 0:
            classes.add("ON")
        classes.update(row["share_class"] for row in snapshot["preferred_classes"] if row["share_class"] is not None)
        exact_classes[snapshot["cvm"]].update(classes)
    for record in records["distribuicao_capital_classe_acao"]:
        share = _class(record["fields"].get("Sigla_Classe_Acoes_Preferenciais"))
        if share:
            exact_classes[record["cvm"]].add(share)
    differences = []
    for cvm, by_document in snapshot_classes.items():
        ordered = sorted(by_document, key=lambda identifier: (filings[identifier]["reference_date"], filings[identifier]["published_at"]))
        for left, right in zip(ordered, ordered[1:]):
            if by_document[left] != by_document[right]:
                differences.append({"cvm": cvm, "from_filing": left, "to_filing": right,
                                    "from_classes": sorted(by_document[left]), "to_classes": sorted(by_document[right]),
                                    "status": "cross_reference_snapshot_difference_not_dated_conversion",
                                    "conversion_factor": None, "effective_date": None, "usable_for_price_adjustment": False})
    ratios, ratio_by_key, mentions = [], {}, []
    parents = {(row["filing_id"], row["fields"].get("ID_Capital_Social_Desdobramento")): row
               for row in records["capital_social_desdobramento"]}
    for record in records["capital_social_desdobramento"]:
        mentions.extend(_reported_ratios(record))
    for record in records["capital_social_desdobramento_classe_acao"]:
        parent = parents.get((record["filing_id"], record["fields"].get("ID_Capital_Social_Desdobramento")))
        if parent is None:
            issues.append({"code": "class_action_parent_not_in_same_filing", "evidence_id": record["evidence_id"]})
        else:
            mentions.extend(_reported_class_ratio(record, parent))
    for item in mentions:
        if _day(item["approval_date"]) > cutoff:
            continue
        key = (item["cvm"], item["action_type"], item["approval_date"], item["share_class"])
        previous = ratio_by_key.get(key)
        if previous is not None:
            if (previous["before_shares"], previous["after_shares"]) != (item["before_shares"], item["after_shares"]):
                issues.append({"code": "conflicting_reported_action_counts", "cvm": item["cvm"], "approval_date": item["approval_date"]})
                previous.update(reported_count_ratio=None, factor_status="conflicting_reported_counts")
            previous["source_evidence"].extend(item["source_evidence"])
            counts["duplicate_action_mentions_merged"] += 1
        else:
            ratio_by_key[key] = item
            ratios.append(item)
    annual = [{"cvm": row["cvm"], "filing_id": row["filing_id"], "version": row["version"],
               "published_at": row["published_at"], "evidence_id": row["evidence_id"], "fields": row["fields"],
               "payment_date": None, "cash_per_share": None, "is_paid_cash": False,
               "basis": "reported_aggregate_or_declared_fields_not_cash_event"}
              for table in ("distribuicao_dividendos", "distribuicao_dividendos_classe_acao") for row in records[table]]
    covered_issuers = {row["cvm"] for row in filings.values()}
    stocks = [row | {"has_received_filing": row["cvm"] in covered_issuers,
                     "has_structured_capital_evidence": issuer_records[row["cvm"]] > 0,
                     "has_exact_share_class_evidence": row["share_class"] in exact_classes[row["cvm"]],
                     "known_reported_share_classes": sorted(exact_classes[row["cvm"]]),
                     "share_class_evidence_basis": "selected_received_FRE_reference_cycles_not_current_trading_status",
                     "corporate_action_factor_status": "unknown_no_effective_date_validated_factor",
                     "annual_dividend_status": "aggregate_fields_only" if any(item["cvm"] == row["cvm"] for item in annual) else "unavailable_in_downloaded_tables",
                     "payment_dates_from_FRE": False} for row in targets]
    return {"schema_version": 1, "as_of": cutoff.isoformat(), "dataset_url": DATASET_URL,
            "sources": sources, "archive_inventory": inventory, "archive_schemas": schemas, "field_definitions": dictionary,
            "filings": sorted(filings.values(), key=lambda row: (row["cvm"], row["reference_date"])),
            "records": dict(records), "capital_snapshots": capital, "class_snapshot_differences": differences,
            "reported_approval_count_ratios": ratios, "annual_dividend_declarations": annual,
            "instruments": stocks, "issues": issues,
            "counts": {**counts, "target_instruments": len(targets), "exact_CVM_instruments": sum(row["cvm"] is not None for row in targets),
                       "target_issuers": len(wanted), "issuers_with_received_filing": len(covered_issuers),
                       "instruments_with_received_filing": sum(row["has_received_filing"] for row in stocks),
                       "instruments_with_structured_capital": sum(row["has_structured_capital_evidence"] for row in stocks),
                       "instruments_with_exact_share_class": sum(row["has_exact_share_class_evidence"] for row in stocks),
                       "selected_filing_count": len(filings), "capital_snapshots": len(capital),
                       "class_snapshot_differences": len(differences), "reported_approval_count_ratios": len(ratios),
                       "validated_effective_action_factors": 0, "annual_dividend_declarations": len(annual),
                       "paid_cash_events": 0, "source_bytes": total},
            "limitations": LIMITATIONS, "prices_or_cash_modified": False, "financially_validated": False}


def collect_fre_evidence(folder, instruments=None, *, as_of="2026-10-24", years=(2026, 2025), refresh=False):
    root = Path(folder).resolve()
    targets = load_targets(root) if instruments is None else _targets(instruments)
    sources = download_sources(root, years=years, refresh=refresh)
    archives = {source["archive_name"]: _path(root / source["file"], root).read_bytes()
                for source in sources if source["status"] in {"downloaded", "cached"}}
    report = parse_archives(archives, targets, as_of=as_of)
    source_by_name = {row["archive_name"]: row for row in sources}
    for row in report["sources"]:
        row.update(source_by_name[row["archive_name"]])
    report["download_sources"] = sources
    report["issues"].extend({"code": "source_unavailable", "url": row["url"], "http_status": row["http_status"]}
                            for row in sources if row["status"] not in {"downloaded", "cached"})
    cache = root / "raw/primary-research/fre"
    path = cache / ("evidence-" + _day(as_of).isoformat() + ".json")
    report.update(collected_at=_utcnow().isoformat(), report_path=str(path))
    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("evidence_file_size_limit")
    _write(path, encoded, root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--as-of", default="2026-10-24")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    report = collect_fre_evidence(args.folder, as_of=args.as_of, refresh=args.refresh)
    print(json.dumps({"counts": report["counts"], "issues": report["issues"], "report_path": report["report_path"]}, ensure_ascii=True))


def download_sources(folder, *, years=(2026, 2025), refresh=False):
    """HEAD then one GET per exact official URL; no retries or non-CVM requests."""
    years = tuple(years)
    if not years or len(set(years)) != len(years) or not set(years) <= {2025, 2026}:
        raise ValueError("research_years_must_be_2026_and_optionally_2025")
    root = Path(folder).resolve()
    cache = _path(root / "raw/primary-research/fre", root)
    cache.mkdir(parents=True, exist_ok=True)
    index_path = cache / "sources.json"
    previous = {}
    if index_path.is_file():
        try:
            _path(index_path, root)
            previous = {row["url"]: row for row in json.loads(index_path.read_bytes())}
        except (ValueError, TypeError, KeyError):
            previous = {}
    urls = [(f"fre_cia_aberta_{year}.zip", DATA_URL.format(year=year)) for year in years]
    urls.append(("meta_fre_cia_aberta.zip", META_URL))
    opener = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    total, sources, stopped = 0, [], False
    for name, url in urls:
        source = {"archive_name": name, "url": url, "authority": "CVM_open_data", "status": "unavailable",
                  "http_status": None, "head_status": None, "headers": {}, "retrieved_at": None}
        sources.append(source)
        if stopped:
            source["status"] = "stopped_after_access_response"
            continue
        try:
            cached = previous.get(url)
            if cached and not refresh and cached.get("status") in {"downloaded", "cached"}:
                path = _path(root / cached["file"], root)
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError("cached_file_size_limit")
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != cached["sha256"]:
                    raise ValueError("cached_zip_hash_mismatch")
                if total + len(content) > MAX_TOTAL_BYTES:
                    raise ValueError("research_total_size_limit")
                _archive(content).close()
                total += len(content)
                source.update(cached, status="cached")
                continue
            request_headers = {"User-Agent": "RendaFutura academic primary-data research", "Accept": "application/zip"}
            with opener.open(urllib.request.Request(url, method="HEAD", headers=request_headers), timeout=25) as response:
                source["head_status"] = response.status
                source["head_headers"] = {key.lower(): value for key, value in response.headers.items() if key.lower() in HEADERS}
                advertised = response.headers.get("Content-Length")
            if advertised is None or not advertised.isdigit():
                raise ValueError("official_content_length_required")
            advertised = int(advertised)
            source["advertised_bytes"] = advertised
            if not 0 < advertised <= MAX_FILE_BYTES or total + advertised > MAX_TOTAL_BYTES:
                raise ValueError("advertised_research_size_limit")
            with opener.open(urllib.request.Request(url, headers=request_headers), timeout=45) as response:
                source["http_status"] = response.status
                source["headers"] = {key.lower(): value for key, value in response.headers.items() if key.lower() in HEADERS}
                content = response.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
            total += len(content)
            if response.status != 200 or len(content) != advertised or len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("download_status_or_size_mismatch")
            _archive(content).close()
            digest = hashlib.sha256(content).hexdigest()
            path = cache / (Path(name).stem + "-" + digest[:20] + ".zip")
            if path.exists() and (_path(path, root).read_bytes() != content):
                raise ValueError("immutable_archive_conflict")
            if not path.exists():
                _write(path, content, root)
            source.update(status="downloaded", file=path.relative_to(root).as_posix(), sha256=digest,
                          bytes=len(content), retrieved_at=_utcnow().isoformat())
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
            source["error_type"] = type(error).__name__
            source["error_code"] = "official_download_or_cache_validation_failed"
            if isinstance(error, urllib.error.HTTPError):
                source["http_status"] = error.code
                if error.code in {403, 429}:
                    stopped = True
    _write(index_path, json.dumps(sources, ensure_ascii=True, indent=2).encode(), root)
    return sources


if __name__ == "__main__":
    main()
