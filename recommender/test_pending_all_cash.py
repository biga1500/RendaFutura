"""Cash evidence must preserve identity, native decimals, dates and conflicts."""

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from .pending_all_cash import (CACHE, CASE_REVIEWS, CUTOFF, CashObservation,
    exact_comparisons, load_native_evidence, observations, parse_reviewed_locale,
    receipt, save, sha)


IDENTITY = {"ticker": "BEES4", "cvm": 1155, "share_class": "PN", "isin": "BRBEESACNPR6"}


def parsed_event(**overrides):
    return {"isin": IDENTITY["isin"], "gross_per_unit_decimal": "0.08334262450",
        "record_date": "2022-12-15", "payment_date": "2022-12-29",
        "approval_date": "2022-12-12", "published_at": "2022-12-13",
        "source_table": 1, "source_row": 3, **overrides}


def test_preserves_exact_class_gross_and_separate_unknown_fields():
    rows = observations({"cvm": 1155, "events": [parsed_event()]}, IDENTITY, {"sha256": "a" * 64})
    assert len(rows) == 1
    item = rows[0]
    assert item.gross_decimal == "0.08334262450"
    assert item.isin == "BRBEESACNPR6" and item.share_class == "PN"
    assert item.net_decimal is None and item.ex_date is None and item.label is None
    assert item.record_date == "2022-12-15" and item.payment_date == "2022-12-29"
    assert not item.model_admitted and item.payment_claim == "declared_schedule_not_settlement"


def test_does_not_guess_an_ISIN_or_predecessor_or_issuer():
    assert not observations({"cvm": 1155, "events": [parsed_event(isin="BRBEESACNOR9")]}, IDENTITY, {})
    with pytest.raises(ValueError, match="CVM_mismatch"):
        observations({"cvm": 22349, "events": [parsed_event()]}, IDENTITY, {})
    predecessor = {"ticker": "AZZA3", "cvm": 22349, "share_class": "ON", "isin": "BRAZZAACNOR9"}
    assert not observations({"cvm": 22349, "events": [parsed_event(isin="BRARZZACNOR3")]}, predecessor, {})


@pytest.mark.parametrize("value", ["-0.1", "NaN", "Infinity"])
def test_invalid_amounts_are_not_adapted(value):
    with pytest.raises(ValueError, match="invalid_native_amount"):
        observations({"cvm": 1155, "events": [parsed_event(gross_per_unit_decimal=value)]}, IDENTITY, {})


def test_unknown_payment_does_not_become_ex_plus_one_or_past_paid():
    item = observations({"cvm": 1155, "events": [parsed_event(payment_date=None)]}, IDENTITY, {})[0]
    assert item.payment_date is None and item.ex_date is None and not item.model_admitted


def test_compares_whole_installment_group_and_keeps_each_original():
    original = [{"record_date": "2022-04-29", "payment_date": "2022-06-28", "gross_per_share": 0.251495},
                {"record_date": "2022-04-29", "payment_date": "2022-12-29", "gross_per_share": 0.903705}]
    stock = {**IDENTITY, "source_inventory_record": {"payments": {"events": original}}}
    cached = [{"corporateAction": "DIVIDENDO", "lastDatePriorEx": "29/04/2022",
               "valueCash": "1,1552", "quotedPerShares": "1"}]
    result = exact_comparisons(stock, cached)[0]
    assert result["calendar_sum_decimal"] == "1.155200"
    assert result["comparator_sum_decimal"] == "1.1552" and result["exact_equal"]
    assert result["original_calendar_rows"] == original
    assert original[0]["payment_date"] == "2022-06-28"


def test_duplicate_total_is_reported_without_rewriting_it():
    stock = {**IDENTITY, "source_inventory_record": {"payments": {"events": [
        {"record_date": "2022-04-29", "payment_date": "2022-09-28", "gross_per_share": 0.251495},
        {"record_date": "2022-04-29", "payment_date": "2022-12-31", "gross_per_share": 1.1552}]}}}
    cached = [{"corporateAction": "DIVIDENDO", "lastDatePriorEx": "29/04/2022",
               "valueCash": "1,1552", "quotedPerShares": "1"}]
    result = exact_comparisons(stock, cached)[0]
    assert result["calendar_sum_decimal"] == "1.406695"
    assert result["calendar_minus_comparator_decimal"] == "0.251495"
    assert not result["exact_equal"] and len(result["original_calendar_rows"]) == 2


def report_fixture(tmp_path):
    base = tmp_path / CACHE
    base.mkdir(parents=True)
    original = base / "body.bin"
    original.write_bytes(b"native-source-test-only")
    source = receipt(tmp_path, original)
    item = asdict(observations({"cvm": 1155, "events": [parsed_event()]}, IDENTITY, source)[0])
    data = {"schema": "ALL157-cash-native-research-v1", "reference": CUTOFF,
        "snapshot": "universe-20261025T224724-fda3d4bd", "original_files_modified": False,
        "model_or_pointer_changed": False, "outcomes": [{"identity": IDENTITY,
            "observations": [item], "case_review": {"status": "source_conflict_preserved"},
            "comparisons": [{"calendar_sum_decimal": "0.070842", "comparator_sum_decimal": "0.0833426245"}]}]}
    path = save(tmp_path, "audit", data)
    return original, path


def test_pinned_adapter_returns_evidence_not_a_ledger_and_rehashes_original(tmp_path):
    original, path = report_fixture(tmp_path)
    result = load_native_evidence(tmp_path, path, IDENTITY)
    assert not result.ledger_ready and not result.model_admitted
    assert result.observations[0].net_decimal is None
    assert result.comparisons[0]["calendar_sum_decimal"] == "0.070842"
    with pytest.raises(ValueError, match="exact_cash_research_identity_required"):
        load_native_evidence(tmp_path, path, {**IDENTITY, "share_class": "PNA"})
    original.write_bytes(b"changed")
    with pytest.raises(ValueError, match="original_changed"):
        load_native_evidence(tmp_path, path, IDENTITY)


def test_pinned_adapter_rejects_modified_report(tmp_path):
    _, path = report_fixture(tmp_path)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="report_hash_mismatch"):
        load_native_evidence(tmp_path, path, IDENTITY)


def test_twenty_specific_reviews_and_no_unreviewed_locale_adapter():
    assert len(CASE_REVIEWS) == 20
    assert set(CASE_REVIEWS) == {"ALOS3", "AZZA3", "BEES3", "BEES4", "BPAC5", "CGAS3", "CGAS5",
        "DOHL4", "ETER3", "EZTC3", "HBTS5", "ISAE4", "JHSF3", "LUXM4", "MGLU3", "POSI3",
        "RADL3", "ROMI3", "SANB4", "TPIS3"}
    with pytest.raises(ValueError, match="unreviewed_native_locale"):
        parse_reviewed_locale(b"not a reviewed original", {"sequence": "565624"}, {})


def test_cached_reviewed_en_us_source_preserves_native_gross_and_ISIN():
    root = Path(__file__).parent / "data/market"
    paths = list((root / "raw/primary-research").glob("cash-calendars/cvm565624v1-*.pdf"))
    if not paths:
        pytest.skip("optional original research cache absent")
    body = paths[0].read_bytes()
    document = {"sequence": "565624", "version": 1, "cvm": 1155, "published_at": "2022-12-13"}
    parsed = parse_reviewed_locale(body, document, receipt(root, paths[0]))
    item = observations(parsed, IDENTITY, receipt(root, paths[0]))[0]
    assert item.gross_decimal == "0.08334262450"
    assert item.record_date == "2022-12-15" and item.payment_date == "2022-12-29"
    assert item.label is None and item.net_decimal is None
    assert item.locator["page"] == 2
