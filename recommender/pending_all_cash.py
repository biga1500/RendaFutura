"""Isolated native cash research for the twenty ALL157 cash cases.

This module writes only its own immutable research cache. It never changes
inventory, model inputs, price history, source observations, or publication.
"""

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader
from lxml import html

from . import cvm_proventos_documents as native


CACHE = "raw/primary-research/all-pending/cash"
CUTOFF = "2026-09-30"
START = "2021-09-30"
GROUP = "cash_reconciliation"

# Reviewed interpretations remain separate from native observations and gates.
CASE_REVIEWS = {
    "ALOS3": ("revised_gross_amount_with_printed_year_conflict", ["841533", "842767", "842850"],
        "Record 2024-12-20: secondary 0.1195742; revised gross 0.120110445. "
        "Notice 842767 prints payment 7 January 2024, inconsistent with its December 2024 date; "
        "841533 and exact-ISIN form 842850 state 2025-01-07. Preserve the printed error. No issuer-origin retry."),
    "AZZA3": ("historical_ISIN_and_bilingual_conflict", ["683089", "684138"],
        "Record 2023-11-10: secondary 1.077379; Portuguese notice JCP 0.78532126295 plus dividend "
        "0.29737557246 = 1.08269683541 versus cached comparator 1.08269683540. English notice "
        "reports 0.78531497104 and 0.29737318992. Form uses historical BRARZZACNOR3, not BRAZZAACNOR9. "
        "Payment 2023-11-28; ex 2023-11-13 explicit. Historical security continuity remains unbound."),
    "BEES3": ("gross_net_and_missing_monthly_record_conflicts", ["517975", "565334", "565624", "468095"],
        "June 2022 gross intermediate JCP 0.0748258034, explicit net 0.0636019329, plus monthly gross "
        "0.02057529409 = 0.09540109749; secondary sum 0.084177 mixes net and gross. "
        "December intermediate gross 0.0833426245, net 0.0708412308; secondary 0.070842. "
        "Monthly December secondary/issuer webpage says record 2022-12-30; native annual schedule says "
        "2022-12-29. Issuer webpage also prints May payment 2021-07-01. Preserve both errors, not inferred repairs."),
    "BEES4": ("missing_intermediate_JCP_and_record_conflicts", ["517975", "565334", "565624", "468095"],
        "June 2022 secondary has only 0.020575 monthly; notice explicitly includes PN intermediate gross "
        "0.0748258034, net 0.0636019329, payable 2022-07-15 on record 2022-06-30, ex 2022-07-01. "
        "December intermediate 0.0833426245 missing; net 0.0708412308 payable 2022-12-29, "
        "record 2022-12-15, ex 2022-12-16. Monthly 29/30 December conflict remains explicit."),
    "BPAC5": ("payment_year_typo_recovered_from_native_notice", ["565973", "1078149"],
        "Source row table:2:row:13 says record 2022-12-16 and payment 2022-02-15 for 0.065353. "
        "Notice 565973 explicitly reports gross per preferred share 0.065353099, net 0.055550134, "
        "record 2022-12-16, ex 2022-12-19, payment 2023-02-15. Unit gross 0.196059297 remains distinct. "
        "Latest exact-ISIN PNA form reports 0.25889622100 payable 2026-08-14."),
    "CGAS3": ("preferred_amounts_in_ordinary_calendar", ["447514", "600420", "739201", "794236", "1061747"],
        "Record 2023-03-24 native ON dividend 2.95450718746 plus JCP 0.12707560066 = 3.08158278812. "
        "Secondary incorrectly carries PN dividend 3.249958 at that date and separates 0.139783 onto 2023-03-31. "
        "Record 2024-04-02 native ON 10.92894990568 versus secondary 12.021845; native PN is 12.02184489624. "
        "Class-specific native rows are retained; the new published remuneration workbook is supplementary evidence."),
    "CGAS5": ("exact_ISIN_PN_sources_recovered_comparator_label_unbound", ["600420", "739201", "794236", "1061747"],
        "Record 2023-03-24 native PN dividend 3.24995790621 plus JCP 0.13978316072 = 3.38974106693; "
        "secondary sum 3.38974107. Record 2024-04-02 native PN 12.02184489624 versus 12.0218449. "
        "Current registered class PN differs from cached comparator label PNA. This audit does not invent a crosswalk."),
    "DOHL4": ("older_native_cash_notices_recovered", ["404006", "428013", "505509", "919941", "1045983"],
        "Native PN gross JCP 0.088000 (2021-06-30) and 0.0946 (2021-09-30); 2022 notice explicitly "
        "distinguishes prior JCP net 0.07480/0.08041 from gross 0.08800/0.09460 and new dividend "
        "0.13200, record 2022-05-09. Exact-ISIN 2026 form reports 0.03630000000 payable 2026-05-27. "
        "Five original calendar/comparator groups have exact decimal equality; no matching-count gate is relaxed. "
        "Issuer-linked Prismic PDF was robots-disallowed and was not requested."),
    "ETER3": ("wrong_record_date_and_rounded_gross", ["683625", "683945", "824462"],
        "Native 2023 form: record 2023-11-10, gross JCP 0.07661590000, payment 2024-03-29; "
        "notice explicitly ex 2023-11-13. Secondary record 2023-11-12 is unsupported. "
        "2024 form: record 2024-11-08, gross 0.05202290000, payment 2025-03-31 versus secondary 0.05. "
        "No business-day or tax back-calculation was applied."),
    "EZTC3": ("successive_revision_still_missing_final_original", ["505848", "509117", "509579"],
        "2022-05-12 notice gross 0.112467632; 2022-05-23 update gross 0.11256951000. "
        "Both precede comparator 0.11269711721, while secondary is 0.112467. "
        "Record 2022-05-17, ex 2022-05-18, payment 2022-05-31 are explicit in initial notice. "
        "Final 2022-05-25 indexed original 509579 is cached empty ENET HTML, not a PDF. "
        "New issuer history page provides no native table. Do not replace the missing final revision with the comparator."),
    "HBTS5": ("duplicate_total_and_missing_first_installment", ["1033093"],
        "Issuer historical PDF states PNA 2022 installments 0.251495 on 2022-06-28 and 0.903705 "
        "on 2022-12-29, total 1.155200 versus secondary 1.406695. "
        "Issuer 2025 notice states first PNA installment 1.724846 payable 2025-06-20 on record 2025-04-23; "
        "secondary only has 4.173899 payable 2025-12-22. Their sum is declared total 5.898745. "
        "Historical PDF instead labels 2025 start 2025-06-06: retain this source date conflict. "
        "Later installment date is not independently confirmed by the first notice."),
    "ISAE4": ("pre_window_cash_and_both_species_board_evidence", ["435704", "435718"],
        "2021 board minutes explicitly declare dividend 0.529386 and gross JCP 0.780915 per share of "
        "both species (sum 1.310301), with ex 2021-11-04 and payment deadline 2021-11-17. "
        "A deadline is not an exact payment date. Current registered PNA versus cached PN label and "
        "historic TRPL ISIN require an explicit continuity/class proof. Official remuneration page was saved; "
        "linked investor workbook origin could not supply robots and was not accessed further."),
    "JHSF3": ("issuer_table_and_native_form_conflict", ["565462", "565690", "611118", "611834"],
        "Issuer table and December 2022 notice: 0.0480475919 on records 2023-01-17, 2023-02-14, "
        "2023-03-16, payments 2023-01-26/2023-02-27/2023-03-27; comparator instead 0.0479407236. "
        "April 2023 issuer table and board minutes say record 2023-04-18, ex 2023-04-19, payment "
        "2023-04-28 for 0.0479407236; native form 611834 says last with-rights 2023-04-19. "
        "Current fresh issuer table corroborates 18 April. Neither conflicting native record is overwritten."),
    "LUXM4": ("digit_transposition_and_payment_deadline", ["1035236", "1035306", "1035331"],
        "2026 native PN gross 0.13686000000 versus secondary 0.1386 (difference 0.00174000000). "
        "AGO says record 2026-04-29, ex 2026-04-30, payment date to be announced by year-end. "
        "Form version 2 fills 2026-12-31; this is a deadline, not settled cash. "
        "Declared Trevisa website robots policy was unavailable and no further website request was sent."),
    "MGLU3": ("early_native_notices_and_registered_class_conflict", ["382198", "404911", "1032845"],
        "2026 exact-ISIN form gross 0.08130019210, record 2026-04-24, payment 2026-05-08 "
        "versus secondary 0.08130019. Manifest class PNA conflicts with ON-labelled security/source. "
        "2021 notice 404911 reports gross 0.0154942583 on 6,454,003,634 ON shares; record 2021-07-05, "
        "ex 2021-07-06, payment conditional on the 2022 AGO plus 30 days. Historical share units must be "
        "documented before comparing secondary 0.15494. No exact payment date, class relabel or price imputation."),
    "POSI3": ("treasury_share_revision_and_installment_schedule", ["386062", "391777"],
        "Initial 2021 total 0.340860213471151 revised explicitly to 0.341616232023573. "
        "Secondary installment sum 0.34086016 versus comparator 0.34161623201. Initial notice "
        "states record 2021-04-30, ex 2021-05-03 and three dates 2021-05-17/2021-09-20/2021-12-20. "
        "Revised total does not state native per-share installment allocations; none are fabricated."),
    "RADL3": ("dividend_revision_and_missing_2023_JCP", ["447074", "450732", "605590", "643266"],
        "Record 2021-12-08 initial JCP 0.03521213027 plus dividend 0.02460248720 = 0.05981461747; "
        "issuer current table dividend 0.024891333000 leads to comparator total 0.06010346327. "
        "Later indexed revision 450732 was fetched; all its native bytes/status retained. "
        "Missing 2023 JCP native amounts 0.04853059700 (record 2023-04-04) and 0.05949664580 "
        "(record 2023-07-05) both state 2023-12-01. Fresh issuer table corroborates both schedules."),
    "ROMI3": ("missing_JCP_and_official_table_date_conflict", ["425712", "450949"],
        "Native record 2021-09-20 dividend 0.23 and JCP 0.13 = 0.36; secondary only 0.23. "
        "Dividend payable 2021-10-28. JCP initially due by year-end 2022; fresh issuer table says "
        "2022-12-28 but labels Data Com as 2021-09-21, contradicting original record/ex distinction. "
        "December original states record 2021-12-21, ex 2021-12-22, gross 0.1469, net 0.124865, "
        "payment 2022-01-26. Secondary orphan record 2021-12-27 amount 0.044883 is not corroborated."),
    "SANB4": ("native_PN_components_match_comparator", ["492293"],
        "Record 2022-04-20 native PN dividend 0.09839755610 plus gross JCP 0.14056793729 "
        "= 0.23896549339, exactly cached comparator, versus secondary 0.239504. "
        "Explicit JCP net 0.11948274669; ON and Unit columns remain distinct. "
        "Notice explicitly ex 2022-04-22 and payment starting 2022-05-16. Santander RI was not accessed."),
    "TPIS3": ("native_old_share_amounts_and_reverse_split", ["498195", "616048", "616207"],
        "Native 2022 gross 0.0186880553 versus secondary 0.074752; native 2023 gross "
        "0.0458966269653469 versus secondary 0.184. Cash notices give year-end deadlines, not "
        "settlement dates. Fresh issuer table reports 2022-06-29 and 2023-12-05 with rounded "
        "0.0186/0.0459. Official notice 616207 explicitly approves 4 old shares for 1 new share on "
        "record 2023-04-27. Diagnostic x4 gives 0.0747522212 and 0.1835865078613876; the latter "
        "still differs from rounded secondary 0.184 by 0.0004134921386124, exceeding the unchanged 0.2% gate."),
}

CASE_URLS = {
    "BEES3": ["https://ri.banestes.com.br/central-de-informacoes/dividendos-e-jcp"],
    "BEES4": ["https://ri.banestes.com.br/central-de-informacoes/dividendos-e-jcp"],
    "BPAC5": ["https://ri.btgpactual.com/"],
    "CGAS3": ["https://ri.comgas.com.br/informacoes-financeiras/dividendos/",
        "https://filemanager-cdn.mziq.com/published/14abe1c8-5911-4e94-a991-82f500120a89/5c6091fd-7474-448f-be70-adaf21022775"],
    "CGAS5": ["https://ri.comgas.com.br/informacoes-financeiras/dividendos/",
        "https://filemanager-cdn.mziq.com/published/14abe1c8-5911-4e94-a991-82f500120a89/5c6091fd-7474-448f-be70-adaf21022775"],
    "DOHL4": ["https://institucionaldohler.com.br/pt-br/investidores"],
    "ETER3": ["https://ri.eternit.com.br/"],
    "EZTC3": ["https://ri.eztec.com.br/relacoes-com-investidores/historico-de-dividendos/"],
    "HBTS5": ["https://ri.habitasul.com.br/informacoes-financeiras/",
        "https://ri.habitasul.com.br/wp-content/uploads/2023/06/historicodividendosdeclarados-2025.pdf",
        "https://ri.habitasul.com.br/wp-content/uploads/2025/06/aviso-de-pag-de-dividendos-1a-parcela-2025.pdf"],
    "ISAE4": ["https://ri.isaenergiabrasil.com.br/pt/informacoes-financeiras/remuneracao-aos-acionistas"],
    "JHSF3": ["https://ri.jhsf.com.br/informacoes-financeiras/dividendos-e-jcp/"],
    "MGLU3": ["https://ri.magazineluiza.com.br/"],
    "POSI3": ["https://ri.positivotecnologia.com.br/servicos-aos-investidores/dividendos-e-jcp/"],
    "RADL3": ["https://ri.rdsaude.com.br/jcp-e-dividendos"],
    "ROMI3": ["https://www.romi.com/investidores/acoes-da-romi/proventos/"],
    "TPIS3": ["https://www.triunfo.com/informacoes-financeiras/dividendos/"],
}


def sha(body):
    return hashlib.sha256(body).hexdigest()


def save(root, name, value):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
        raise ValueError("unsafe_research_name")
    body = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True).encode()
    folder = Path(root).resolve() / CACHE
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + "-" + sha(body) + ".json")
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError("immutable_research_conflict")
    else:
        with path.open("xb") as stream:
            stream.write(body)
    return path


def receipt(root, path, url=None, published_at=None):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()) or path.is_symlink():
        raise ValueError("unsafe_cached_source")
    body = path.read_bytes()
    return {"file": path.relative_to(root).as_posix(), "sha256": sha(body),
            "bytes": len(body), "url": url, "published_at": published_at,
            "retrieved_at": None, "access": "existing_cache_read_only",
            "retrieval_basis": "unknown_not_file_mtime"}


@dataclass(frozen=True)
class CashObservation:
    ticker: str
    cvm: int
    share_class: str
    isin: str
    record_date: str | None
    ex_date: str | None
    payment_date: str | None
    approval_date: str | None
    gross_decimal: str
    net_decimal: str | None
    label: str | None
    amount_unit: str
    published_at: str | None
    source: dict
    locator: dict
    payment_claim: str = "declared_schedule_not_settlement"
    model_admitted: bool = False


@dataclass(frozen=True)
class NativeCashEvidence:
    identity: dict
    observations: tuple[CashObservation, ...]
    case_review: dict
    comparisons: tuple[dict, ...]
    ledger_ready: bool = False
    model_admitted: bool = False


def load_native_evidence(root, report_file, identity):
    """Read a pinned research artifact; never turn observations into a ledger."""
    root = Path(root).resolve()
    report = (root / report_file).resolve()
    if not report.is_relative_to(root / CACHE) or report.is_symlink():
        raise ValueError("unsafe_cash_research_report")
    body = report.read_bytes()
    if report.name != "audit-" + sha(body) + ".json":
        raise ValueError("cash_research_report_hash_mismatch")
    data = json.loads(body)
    if (data.get("schema") != "ALL157-cash-native-research-v1"
            or data.get("reference") != CUTOFF
            or data.get("snapshot") != "universe-20261025T224724-fda3d4bd"
            or data.get("original_files_modified") is not False
            or data.get("model_or_pointer_changed") is not False):
        raise ValueError("cash_research_contract_changed")
    keys = ("ticker", "cvm", "share_class", "isin")
    supplied = {key: identity[key] for key in keys}
    matches = [r for r in data["outcomes"] if r["identity"] == supplied]
    if len(matches) != 1:
        raise ValueError("exact_cash_research_identity_required")
    outcome = matches[0]
    rows = []
    for item in outcome["observations"]:
        if any(item[key] != identity[key] for key in keys) or item["model_admitted"]:
            raise ValueError("native_observation_identity_or_admission_changed")
        source = item["source"]
        current = receipt(root, root / source["file"])
        if current["sha256"] != source["sha256"] or current["bytes"] != source["bytes"]:
            raise ValueError("native_observation_original_changed")
        rows.append(CashObservation(**item))
    return NativeCashEvidence(supplied, tuple(rows), outcome["case_review"],
                              tuple(outcome["comparisons"]))


def observations(parsed, identity, source):
    """Adapt exact native ISIN rows without borrowing class/label/date fields."""
    if parsed.get("cvm") != identity["cvm"]:
        raise ValueError("native_CVM_mismatch")
    rows = []
    for event in parsed.get("events", []):
        if event.get("isin") != identity["isin"]:
            continue
        amount = event["gross_per_unit_decimal"]
        if not Decimal(amount).is_finite() or Decimal(amount) < 0:
            raise ValueError("invalid_native_amount")
        rows.append(CashObservation(
            identity["ticker"], identity["cvm"], identity["share_class"],
            identity["isin"], event.get("record_date"), None,
            event.get("payment_date"), event.get("approval_date"), amount,
            None, event.get("label"), "BRL/ISIN-unit", event.get("published_at"),
            source, event.get("source_locator") or {
                "format": "native_CVM_PDF_table", "table": event["source_table"],
                "row": event["source_row"]}))
    return rows


def parse_reviewed_locale(body, document, source):
    """Reuse the established exact-ISIN adapter for one pinned en-US form."""
    pin = "c5016168b6bfba48354ad42268911b17ff3eba412a034fc449f28493e716b260"
    if document["sequence"] != "565624" or sha(body) != pin:
        raise ValueError("unreviewed_native_locale")
    from .older_cash_notice_research import native_facts
    facts = native_facts(body, document, source, locale="en-US")
    return {"cvm": document["cvm"], "status": "reviewed_native_en_US_locale",
            "events": [{"isin": f["isin"], "gross_per_unit_decimal": f["amount"],
                "record_date": f["record_date"], "payment_date": f["payment_date"],
                "approval_date": f["approval_date"], "published_at": document["published_at"],
                "label": None, "source_locator": f["source_locator"]} for f in facts],
            "original_wrapper_income_label_dispute_preserved": True,
            "gross_reconstructed_from_net": False}


def read_indices(root, identities):
    codes = {r["cvm"] for r in identities}
    manifest = root / "raw/primary-research/cvm-documents/index-sources.json"
    documents, sources = [], [receipt(root, manifest)]
    for item in json.loads(manifest.read_bytes()):
        if item.get("year") not in range(2021, 2027):
            continue
        path = root / item["file"]
        source = receipt(root, path, item["url"])
        if source["sha256"] != item["sha256"]:
            raise ValueError("native_IPE_hash_mismatch")
        source["retrieved_at"] = item.get("retrieved_at")
        source["retrieval_basis"] = "original_index_receipt"
        sources.append(source)
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
            member = f'ipe_cia_aberta_{item["year"]}.csv'
            with archive.open(member) as stream:
                for line, row in enumerate(csv.DictReader(io.TextIOWrapper(stream, encoding="latin1"), delimiter=";"), 2):
                    if not row["Codigo_CVM"].isdigit() or int(row["Codigo_CVM"]) not in codes:
                        continue
                    published = native.day(row["Data_Entrega"])
                    if not published or published > CUTOFF:
                        continue
                    sequence, version = native.document_id(row["Link_Download"])
                    documents.append({"cvm": int(row["Codigo_CVM"]),
                        "issuer_name": row["Nome_Companhia"], "url": row["Link_Download"],
                        "sequence": sequence, "version": version,
                        "published_at": published,
                        "reference_date": native.day(row["Data_Referencia"]),
                        "category": row["Categoria"], "type": row["Tipo"],
                        "subject": row["Assunto"], "index_evidence": {
                            **source, "member": member, "csv_line": line, "raw_row": row}})
    return documents, sources


def original_index(root):
    """Index cached originals by full digest and exact CVM sequence filename."""
    sequences, digests = defaultdict(list), defaultdict(list)
    base = root / "raw/primary-research"
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in (".pdf", ".bin"):
            continue
        match = re.search(r"(?:cvm)?(\d{5,8})(?:-v|v)(\d+)-([a-f0-9]{64})", path.name)
        if match:
            sequences[(match[1], int(match[2]))].append(path)
        hashes = re.findall(r"[a-f0-9]{64}", path.name)
        if hashes:
            digests[hashes[-1]].append(path)
    report = root / "raw/primary-research/cvm-documents/report.json"
    if report.exists():
        for row in json.loads(report.read_bytes())["results"]:
            path = root / row.get("file", "__missing__")
            if path.is_file() and row.get("sequence"):
                sequences[(row["sequence"], row["version"])].append(path)
    for path in base.rglob("*.json"):
        if path.stat().st_size > 600_000:
            continue
        try:
            row = json.loads(path.read_bytes())
        except (ValueError, OSError):
            continue
        if not isinstance(row, dict):
            continue
        url = row.get("url", "")
        if isinstance(url, str) and "www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?" in url:
            try:
                sequence, version = native.document_id(url)
            except ValueError:
                continue
            for candidate in digests.get(row.get("sha256"), []):
                sequences[(sequence, version)].append(candidate)
    return sequences


def inspect_public_sources(root):
    """Preserve semantic HTML table positions and PDF pages for new receipts."""
    output = []
    for path in sorted((root / CACHE).glob("response-*.json")):
        row = json.loads(path.read_bytes())
        if row.get("status") != 200 or row.get("error") or row["url"].endswith("/robots.txt"):
            continue
        content = (root / row["file"]).read_bytes()
        if sha(content) != row["sha256"]:
            raise ValueError("new_public_original_hash_mismatch")
        item = {"source": row, "format": "unparsed_original"}
        if content.lstrip().startswith(b"%PDF-"):
            item.update(format="PDF", pages=[{"page": i + 1, "text": page.extract_text()}
                for i, page in enumerate(PdfReader(io.BytesIO(content)).pages)])
        elif content.lstrip().startswith((b"<", b"\xef\xbb\xbf<")):
            tree = html.fromstring(content, parser=html.HTMLParser(no_network=True))
            item.update(format="HTML", title=tree.xpath("string(//title)"),
                tables=[{"table": i, "rows": [[" ".join(cell.itertext()).strip()
                    for cell in tr.xpath("./th|./td")] for tr in table.xpath(".//tr")]}
                    for i, table in enumerate(tree.xpath("//table"))],
                links=[{"href": a.get("href"), "text": " ".join(a.itertext()).strip()}
                       for a in tree.xpath("//a[@href]")])
        elif content.startswith(b"PK") and zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if "xl/workbook.xml" in archive.namelist():
                    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                    strings = []
                    if "xl/sharedStrings.xml" in archive.namelist():
                        tree = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                        strings = ["".join(node.itertext()) for node in tree]
                    sheets = []
                    for name in archive.namelist():
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                            tree = ET.fromstring(archive.read(name))
                            cells = []
                            for cell in tree.findall(".//s:c", ns):
                                value = cell.findtext("s:v", namespaces=ns)
                                kind = cell.get("t")
                                text = strings[int(value)] if kind == "s" and value is not None else None
                                cells.append({"cell": cell.get("r"), "type": kind,
                                    "style": cell.get("s"), "native_value": value,
                                    "shared_text": text, "inline_text": cell.findtext("s:is/s:t", namespaces=ns),
                                    "formula": cell.findtext("s:f", namespaces=ns)})
                            sheets.append({"member": name, "cells": cells})
                    item.update(format="native_XLSX_XML_read_only", sheets=sheets,
                        workbook_xml=archive.read("xl/workbook.xml").decode("utf-8"),
                        styles_xml=archive.read("xl/styles.xml").decode("utf-8"),
                        formulas_recalculated=False, native_numeric_strings_preserved=True)
        output.append(item)
    return output


def reviewed_case(ticker, inspected, public):
    status, sequences, finding = CASE_REVIEWS[ticker]
    sources = [{"document_sequence": d["document"]["sequence"],
        "status": d["status"], "source": d["source"],
        "page_citations": [{"page": p["page"], "text_sha256": sha(p["text"].encode())}
                           for p in d.get("pages", [])]}
        for d in inspected if d["document"]["sequence"] in sequences]
    sources.extend({"source": p["source"], "format": p["format"],
                    "table_citations": [t["table"] for t in p.get("tables", [])],
                    "page_citations": [{"page": page["page"]} for page in p.get("pages", [])]}
                   for p in public if p["source"]["url"] in CASE_URLS.get(ticker, []))
    return {"status": status, "finding": finding, "semantic_citations": sources,
            "recoverable_evidence": bool(sources), "model_admitted": False,
            "original_conflicts_preserved": True, "settlement_verified": False,
            "full_window_or_class_gate_pass_inferred": False}


def comparator_rows(root, identity, targets):
    codes = [identity["ticker"], *sorted({r["ticker"] for r in targets if r["cvm"] == identity["cvm"]})]
    path = next((root / f"raw/expanded/{code}-b3-cash-1.json" for code in codes
                 if (root / f"raw/expanded/{code}-b3-cash-1.json").is_file()), None)
    if path is None:
        return [], [], "cached_comparator_absent"
    first = json.loads(path.read_bytes())
    sources, rows = [], []
    for page in range(1, first["page"]["totalPages"] + 1):
        current = path.with_name(path.name.replace("-1.json", f"-{page}.json"))
        if not current.is_file():
            return rows, sources, "cached_pagination_incomplete"
        data = json.loads(current.read_bytes())
        sources.append(receipt(root, current))
        for line, row in enumerate(data["results"]):
            if row.get("typeStock", "").strip() == identity["share_class"]:
                rows.append({**row, "source": sources[-1], "array_index": line})
    return rows, sources, "cached_pagination_complete"


def exact_comparisons(identity, cached_rows, payment_events=None):
    groups = defaultdict(list)
    calendar = defaultdict(list)
    for row in cached_rows:
        if native.norm(row.get("corporateAction")) not in ("dividendo", "jrs cap proprio"):
            continue
        day = native.day(row.get("lastDatePriorEx"))
        if day:
            groups[day].append(row)
    if payment_events is None:
        payment_events = identity["source_inventory_record"].get("payments", {}).get("events", [])
    for event in payment_events:
        if event.get("record_date") and event.get("payment_date"):
            calendar[event["record_date"]].append(event)
    keys = {day for day in groups if START <= day <= CUTOFF}
    keys.update(day for day, rows in calendar.items()
                if any(START <= r["payment_date"] <= CUTOFF for r in rows))
    output = []
    for day in sorted(keys):
        independent = sum((native.gross(r["valueCash"]) / native.gross(r.get("quotedPerShares") or "1")
                           for r in groups[day]), Decimal(0)) if groups[day] else None
        observed = sum((Decimal(str(r["gross_per_share"])) for r in calendar[day]), Decimal(0)) if calendar[day] else None
        delta = observed - independent if observed is not None and independent is not None else None
        output.append({"record_date": day, "calendar_sum_decimal": str(observed) if observed is not None else None,
                       "comparator_sum_decimal": str(independent) if independent is not None else None,
                       "calendar_minus_comparator_decimal": str(delta) if delta is not None else None,
                       "comparator_rows": groups[day], "original_calendar_rows": calendar[day],
                       "exact_equal": delta == 0 if delta is not None else False,
                       "calendar_missing": not calendar[day], "comparator_missing": not groups[day]})
    return output


def build_audit(root, manifest):
    root = Path(root).resolve()
    mbody = Path(manifest).read_bytes()
    baseline = json.loads(mbody)
    targets = [r for r in baseline["records"] if r["task_group"] == GROUP]
    if len(targets) != 20 or baseline["snapshot"] != "universe-20261025T224724-fda3d4bd":
        raise ValueError("wrong_twenty_case_manifest")
    documents, index_sources = read_indices(root, targets)
    originals = original_index(root)
    public = inspect_public_sources(root)
    retrievals = {p["source"]["sha256"]: p["source"] for p in public}
    inspected = {}
    for doc in documents:
        key = (doc["sequence"], doc["version"])
        if key in inspected:
            continue
        paths = originals.get(key, [])
        if not paths:
            continue
        primary = next((p for p in paths if p.read_bytes().lstrip().startswith(b"%PDF-")), paths[0])
        body = primary.read_bytes()
        source = receipt(root, primary, doc["url"], doc["published_at"])
        if source["sha256"] in retrievals:
            acquisition = retrievals[source["sha256"]]
            source.update(retrieved_at=acquisition["retrieved_at"],
                          retrieval_basis="actual_public_GET_receipt")
        result = {"document": doc, "source": source, "status": "cached_non_PDF_no_retry"}
        if body.lstrip().startswith(b"%PDF-"):
            try:
                reader = PdfReader(io.BytesIO(body))
                pages = [{"page": i + 1, "text": page.extract_text()} for i, page in enumerate(reader.pages)]
                result.update(status="native_original_inspected", pages=pages)
                if native.norm(doc["category"]) == "relatorio proventos":
                    result["parsed"] = (parse_reviewed_locale(body, doc, source)
                        if doc["sequence"] == "565624" else
                        native.parse_pdf(body, doc, date.fromisoformat(CUTOFF)))
            except (ValueError, OSError, KeyError, TypeError) as error:
                result.update(status="native_parse_unresolved", error=str(error))
        inspected[key] = result
    outcomes = []
    for identity in targets:
        cached, sources, status = comparator_rows(root, identity, targets)
        payment = identity["source_inventory_record"].get("payments", {})
        events = payment.get("events", [])
        calendar_sources = []
        if payment.get("full_report"):
            full_path = root / payment["full_report"]
            full = json.loads(full_path.read_bytes())
            events = full.get("selected_cash_source", {}).get("events", events)
            calendar_sources.append(receipt(root, full_path, payment.get("source_url")))
        comparisons = exact_comparisons(identity, cached, events)
        docs = [d for d in inspected.values() if d["document"]["cvm"] == identity["cvm"]]
        obs = [asdict(o) for d in docs if d.get("parsed", {}).get("cvm") == identity["cvm"]
               for o in observations(d["parsed"], identity, d["source"])]
        for group in comparisons:
            group["native_observations"] = [o for o in obs if o["record_date"] == group["record_date"]]
        outcomes.append({"identity": {k: identity[k] for k in ("ticker", "cvm", "share_class", "isin")},
            "declared_website": identity["website_declared_in_FCA"], "baseline_reasons": identity["reasons"],
            "comparator_status": status, "comparator_sources": sources,
            "calendar_sources": calendar_sources, "calendar_event_count": len(events),
            "comparisons": comparisons, "observations": obs,
            "inspected_documents": [{k: v for k, v in d.items() if k not in ("pages", "parsed")} for d in docs],
            "case_review": reviewed_case(identity["ticker"], list(inspected.values()), public),
            "model_admitted": False, "original_conflicts_preserved": True})
    return {"schema": "ALL157-cash-native-research-v1", "snapshot": baseline["snapshot"],
        "reference": CUTOFF, "window_start": START, "manifest_sha256": sha(mbody),
        "outcomes": outcomes, "inspected_originals": list(inspected.values()),
        "indexed_documents": documents, "index_sources": index_sources,
        "new_public_sources": public,
        "original_files_modified": False, "model_or_pointer_changed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("recommender/data/market"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    result = build_audit(args.root, args.manifest)
    output = save(args.root, "audit", result)
    if args.report_dir:
        summary = {k: result[k] for k in ("schema", "snapshot", "reference", "window_start", "manifest_sha256")}
        summary.update(research_artifact=output.relative_to(args.root.resolve()).as_posix(),
            research_sha256=sha(output.read_bytes()),
            cases=[{"identity": r["identity"], "baseline_reasons": r["baseline_reasons"],
                    "native_observations": len(r["observations"]), "case_review": r["case_review"]}
                   for r in result["outcomes"]],
            original_files_modified=False, model_or_pointer_changed=False)
        body = json.dumps(summary, ensure_ascii=True, indent=2).encode()
        destination = args.report_dir.resolve() / ("cash-report-" + sha(body) + ".json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != body:
            raise ValueError("cash_report_immutable_conflict")
        if not destination.exists():
            with destination.open("xb") as stream:
                stream.write(body)
        print(json.dumps({"parent_report": str(destination)}))
    print(json.dumps({"output": str(output), "outcomes": len(result["outcomes"]),
                      "inspected": len(result["inspected_originals"])}))


if __name__ == "__main__":
    main()
