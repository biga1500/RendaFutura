"""Pricing proofs, lot conservation and immutable current-offer integration.

Synthetic index inputs below are explicitly scenario fixtures. They are never
claimed to be observed VNA, Selic fixings, paid cash or prior market prices.
"""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from .treasury_contracts import contract_from_offer, nominal_cash_schedule
from .treasury_valuation import (
    BCB_METHODOLOGY, BrazilCalendar, contract_for_offer, lot_cashflows,
    lot_economic_cash, offer_scenario, price_contract, pricing_family_registry,
    quote_implied_nominal_value,
)


SOURCE = "https://www.tesourodireto.com.br/o/rentabilidade/investir"
AS_OF = "2026-10-23T18:00:08.193000-03:00"
EVIDENCE = Path(__file__).parent / "data/market/raw/primary-research/treasury"


@pytest.fixture
def calendar():
    pytest.importorskip("QuantLib")
    return BrazilCalendar()


def make_offer(kind="Tesouro Prefixado", maturity="2029-01-01", **extra):
    return {"kind": kind, "maturity": maturity, "source_url": SOURCE,
            "as_of": AS_OF, "buy_rate": "12.76", "buy_price": "771.26",
            "rate_basis": "nominal_annual", "rate_unit": "percentage_points_per_year", **extra}


def index_value(day, value="1000", indexer="IPCA", unit="VNA", **extra):
    return {"value": value, "value_date": day, "indexer": indexer, "unit": unit,
            "basis": "scenario", "scenario_id": "synthetic_unit_test_not_market_data",
            "source_url": BCB_METHODOLOGY, "as_of": AS_OF, **extra}


def make_lot(spec, cost="700", quantity="1", settlement="2026-10-26", **extra):
    return {"identity": deepcopy(spec["identity"]), "isin": spec["known_offer"].get("isin"),
            "cost_basis_total": cost, "quantity": quantity, "settlement_date": settlement,
            "source_url": SOURCE, "as_of": AS_OF,
            "basis": "hypothetical_purchase_at_reference_quote", **extra}


def external_values(spec, settlement, calendar, acquisition="1000", payment="1100"):
    indexer = spec["terms"]["indexer"]
    unit = "VNI" if spec["terms"]["conversion_date"] else "VNA"
    rows = nominal_cash_schedule(spec, settlement, spec["maturity"], "1000")
    result = {settlement: index_value(settlement, acquisition, indexer, unit)}
    for row in rows:
        day = calendar.following(row["contractual_date"]).isoformat()
        if day > settlement:
            result[day] = index_value(day, payment, indexer, unit)
    return result


def assert_cash_conservation(result):
    assert result["supported"], result
    with localcontext() as ctx:
        ctx.prec = 50
        for row in result["cashflows"]:
            assert abs(Decimal(row["gross_nominal_cash"]) - Decimal(row["cost_basis_return"])
                       - Decimal(row["income_cash"])) < Decimal("1e-30")
        returned = sum((Decimal(r["cost_basis_return"]) for r in result["cashflows"]), Decimal(0))
        assert abs(returned + Decimal(result["remaining_cost_basis"])
                   - Decimal(result["original_cost_basis"])) < Decimal("1e-30")


def test_registry_is_documentary_and_cannot_be_mutated_by_callers():
    registry = pricing_family_registry()
    assert len(registry) == 8
    assert sum(r["pricing_formula_verified"] for r in registry.values()) == 7
    reserve = registry["Tesouro Reserva"]
    assert not reserve["pricing_formula_verified"]
    assert reserve["verified_product_facts"]["mark_to_market"] is False
    registry["Tesouro Prefixado"]["pricing_formula_verified"] = False
    assert pricing_family_registry()["Tesouro Prefixado"]["pricing_formula_verified"]


def test_quantlib_brazil_calendar_is_settlement_business252_and_tracks_new_holiday(calendar):
    assert calendar.provenance["market"] == "Brazil.Settlement"
    assert calendar.business_days("2012-01-04", "2015-01-01") == 755
    assert calendar.business_days("2026-10-23", "2026-10-26") == 1
    assert calendar.business_days("2026-10-26", "2026-10-26") == 0
    assert calendar.is_business_day("2023-11-20")
    assert not calendar.is_business_day("2026-11-20")
    assert not calendar.is_business_day("2026-02-17")  # Carnival.
    assert calendar.following("2029-01-01") == date(2029, 1, 2)
    assert calendar.provenance["settlement_evidence"] is False


def test_discount_is_annual_effective_exact_yield_and_not_monthly_coupon(calendar):
    days = calendar.business_days("2026-10-26", "2032-01-02")
    with localcontext() as ctx:
        ctx.prec = 40
        expected = (1 + Decimal("0.12891234")) ** (-Decimal(days) / 252)
    factor = calendar.discount("12.891234", "2026-10-26", "2032-01-02")
    assert abs(factor - expected) < Decimal("1e-14")
    assert factor != calendar.discount("12.89", "2026-10-26", "2032-01-02")


def test_official_ltn_worked_example_755_business_days_and_733_86_execution_floor(calendar):
    spec = contract_from_offer(make_offer(maturity="2015-01-01"))
    result = price_contract(spec, "2012-01-04", "10.88", calendar=calendar)
    assert result["cashflows"][0]["business_days"] == 755
    assert Decimal(result["unit_price"]).quantize(Decimal("0.01"), rounding=ROUND_DOWN) == Decimal("733.86")
    assert result["execution_price"] is None
    assert result["price_basis"] == "theoretical_full_dirty_pv"


def test_official_ntnf_example_matches_all_twelve_day_counts_and_unrounded_price(calendar):
    # STN 29-Apr-2024 methodology, p.12: coupon cash rounded to 5 decimals there.
    spec = contract_for_offer(make_offer("Tesouro Prefixado com Juros Semestrais", "2014-01-01"), "2008-05-21")
    result = price_contract(spec, "2008-05-21", "13.66", calendar=calendar)
    assert [r["business_days"] for r in result["cashflows"]] == [
        28, 159, 281, 409, 532, 660, 784, 911, 1036, 1162, 1285, 1415]
    assert abs(Decimal(result["unit_price"]) - Decimal("903.075616")) < Decimal("0.0001")


@pytest.mark.parametrize("kind,maturity,annual", [
    ("Tesouro Prefixado com Juros Semestrais", "2037-01-01", "10"),
    ("Tesouro IPCA+ com Juros Semestrais", "2037-05-15", "6"),
])
def test_coupon_is_contract_rate_while_yield_only_discounts(kind, maturity, annual, calendar):
    spec = contract_for_offer(make_offer(kind, maturity), "2026-10-26")
    nominal = index_value("2026-10-26", "1000") if "IPCA" in kind else None
    low = price_contract(spec, "2026-10-26", "1.23", nominal, calendar)
    high = price_contract(spec, "2026-10-26", "13.456789", nominal, calendar)
    assert Decimal(low["unit_price"]) > Decimal(high["unit_price"])
    assert [r["reference_cash"] for r in low["cashflows"]] == [r["reference_cash"] for r in high["cashflows"]]
    with localcontext() as ctx:
        ctx.prec = 40
        coupon = Decimal(1000) * ((1 + Decimal(annual) / 100).sqrt() - 1)
    assert abs(Decimal(low["cashflows"][0]["reference_cash"]) - coupon) < Decimal("1e-30")
    assert len(low["cashflows"]) < 2 * (date.fromisoformat(maturity).year - 2026 + 1)


@pytest.mark.parametrize("kind,maturity,count,regular,last", [
    ("Tesouro Renda+", "2049-12-15", 240, "0.416666", "0.416826"),
    ("Tesouro Educa+", "2031-12-15", 60, "1.666666", "1.666706"),
])
def test_b1_pricing_uses_sourced_truncated_face_fractions_and_exact_yield(
        kind, maturity, count, regular, last, calendar):
    spec = contract_from_offer(make_offer(kind, maturity))
    nominal = index_value("2026-10-26", "4000", unit="VNI")
    result = price_contract(spec, "2026-10-26", "6.691234", nominal, calendar)
    assert len(result["cashflows"]) == count
    assert Decimal(result["cashflows"][0]["reference_cash"]) == 40 * Decimal(regular)
    assert Decimal(result["cashflows"][-1]["reference_cash"]) == 40 * Decimal(last)
    with localcontext() as ctx:
        ctx.prec = 40
        expected = sum((Decimal(r["reference_cash"]) * Decimal("1.06691234") **
                        (-Decimal(r["business_days"]) / 252) for r in result["cashflows"]), Decimal(0))
    assert abs(Decimal(result["unit_price"]) - expected) < Decimal("1e-10")
    assert sum(Decimal(r["reference_cash"]) for r in result["cashflows"]) == 4000


def test_lft_yield_is_spread_not_selic_total_rate_and_needs_external_vna(calendar):
    spec = contract_from_offer(make_offer("Tesouro Selic", "2031-03-01"))
    assert price_contract(spec, "2026-10-26", ".0989", calendar=calendar)["unsupported_reason"] == "missing_external_settlement_vna"
    nominal = index_value("2026-10-26", "20000", "SELIC")
    result = price_contract(spec, "2026-10-26", ".0989", nominal, calendar)
    assert result["yield_basis"] == "spread_over_selic"
    assert Decimal(result["unit_price"]) < 20000
    assert price_contract(spec, "2026-10-26", 0, nominal, calendar)["unit_price"] == "20000.0"
    values = external_values(spec, "2026-10-26", calendar, "20000", "25000")
    flows = lot_cashflows(spec, make_lot(spec, "19000"), nominal_values=values, calendar=calendar)
    row = flows["cashflows"][0]
    assert row["economic_interest_nominal"] == "6000"
    assert row["economic_interest_real_base_brl"] is row["inflation_adjustment"] is None


@pytest.mark.parametrize("cost,income", [("700", "300"), ("1000", "0"), ("1100", "-100")])
def test_zero_coupon_returns_actual_lot_cost_and_signed_interest(cost, income, calendar):
    spec = contract_from_offer(make_offer())
    result = lot_cashflows(spec, make_lot(spec, cost), calendar=calendar)
    assert len(result["cashflows"]) == 1
    row = result["cashflows"][0]
    assert Decimal(row["coupon_interest"]) == 0
    assert row["cost_basis_return"] == cost
    assert row["income_cash"] == income
    assert Decimal(row["amortized_face_principal"]) == 1000
    assert result["remaining_cost_basis"] == "0"
    assert_cash_conservation(result)


@pytest.mark.parametrize("kind,maturity,count", [
    ("Tesouro Renda+", "2049-12-15", 240),
    ("Tesouro Educa+", "2031-12-15", 60),
])
def test_amortizing_discount_earnings_exclude_original_principal(kind, maturity, count, calendar):
    spec = contract_from_offer(make_offer(kind, maturity))
    values = external_values(spec, "2026-10-26", calendar, "4000", "4000")
    result = lot_cashflows(spec, make_lot(spec, "2000"), nominal_values=values, calendar=calendar)
    assert len(result["cashflows"]) == count
    assert Decimal(result["income_cash"]) == 2000
    assert Decimal(result["gross_nominal_cash"]) == 4000
    assert Decimal(result["cost_basis_returned"]) == 2000
    assert Decimal(result["remaining_cost_basis"]) == 0
    for row in result["cashflows"]:
        assert Decimal(row["coupon_interest"]) == 0
        assert 0 < Decimal(row["income_cash"]) < Decimal(row["gross_nominal_cash"])
        assert Decimal(row["inflation_adjustment"]) == 0
    assert_cash_conservation(result)


@pytest.mark.parametrize("payment,nominal_income,inflation", [("1100", "300", "100"), ("900", "100", "-100")])
def test_ipca_nominal_income_equals_real_base_income_plus_inflation(payment, nominal_income, inflation, calendar):
    spec = contract_from_offer(make_offer("Tesouro IPCA+", "2032-08-15"))
    values = external_values(spec, "2026-10-26", calendar, "1000", payment)
    result = lot_cashflows(spec, make_lot(spec, "800"), nominal_values=values, calendar=calendar)
    row = result["cashflows"][0]
    assert row["economic_interest_real_base_brl"] == "200"
    assert row["inflation_adjustment"] == inflation
    assert row["income_cash"] == nominal_income
    assert_cash_conservation(result)


def test_coupon_lot_basis_is_returned_once_at_maturity_and_premium_is_not_hidden(calendar):
    spec = contract_for_offer(make_offer("Tesouro Prefixado com Juros Semestrais", "2027-01-01"), "2026-10-26")
    result = lot_cashflows(spec, make_lot(spec, "1200"), calendar=calendar)
    row = result["cashflows"][0]
    assert row["cost_basis_return"] == "1200"
    assert Decimal(row["coupon_interest"]) > 48
    assert Decimal(row["income_cash"]) < 0
    with localcontext() as ctx:
        ctx.prec = 40
        assert Decimal(result["income_cash"]) == Decimal(result["gross_nominal_cash"]) - 1200
    assert_cash_conservation(result)


def test_fractional_quantity_and_cost_include_supplied_acquisition_costs(calendar):
    spec = contract_from_offer(make_offer())
    result = lot_cashflows(spec, make_lot(spec, "203", ".25"), calendar=calendar)
    assert result["gross_nominal_cash"] == "250.00"
    assert Decimal(result["income_cash"]) == 47
    assert_cash_conservation(result)


def test_nominal_weekend_date_and_adjusted_receipt_never_prove_actual_settlement(calendar):
    spec = contract_from_offer(make_offer("Tesouro Educa+", "2031-12-15"))
    values = external_values(spec, "2026-10-26", calendar)
    result = lot_cashflows(spec, make_lot(spec), nominal_values=values, horizon="2027-05-17", calendar=calendar)
    row = result["cashflows"][-1]
    assert row["contractual_date"] == "2027-05-15"
    assert row["adjusted_payment_date"] == "2027-05-17"
    assert row["settled"] is None
    before = lot_cashflows(spec, make_lot(spec), nominal_values=values, horizon="2027-05-16", calendar=calendar)
    assert len(before["cashflows"]) == len(result["cashflows"]) - 1
    assert not result["contract_ended_by_horizon"]


def test_maturity_stops_income_and_does_not_roll_over_or_earn_on_idle_cash(calendar):
    spec = contract_from_offer(make_offer(maturity="2027-01-01"))
    lot = make_lot(spec)
    end = lot_cashflows(spec, lot, horizon="2027-01-04", calendar=calendar)
    later = lot_cashflows(spec, lot, horizon="2037-01-01", calendar=calendar)
    assert end["cashflows"] == later["cashflows"]
    assert end["income_cash"] == later["income_cash"] == "300"
    assert later["post_maturity_income"] == "0"
    assert later["contract_ended_by_horizon"]
    assert later["reinvestment"] == "requires_separate_explicit_new_lot"
    assert price_contract(spec, "2027-01-04", 10, calendar=calendar)["status"] == "matured"
    assert not lot_cashflows(spec, make_lot(spec, settlement="2027-01-04"), calendar=calendar)["supported"]


def test_mid_amortization_acquisition_allocates_cost_only_across_remaining_face(calendar):
    spec = contract_from_offer(make_offer("Tesouro Educa+", "2031-12-15"))
    settlement = "2031-11-17"  # Excludes the November nominal payment on a holiday.
    values = external_values(spec, settlement, calendar, "4000", "4000")
    result = lot_cashflows(spec, make_lot(spec, "50", settlement=settlement), nominal_values=values, calendar=calendar)
    assert len(result["cashflows"]) == 1
    assert result["cashflows"][0]["installment"] == 60
    assert result["cost_basis_returned"] == "50"
    assert Decimal(result["remaining_cost_basis"]) == 0
    assert_cash_conservation(result)


def test_empty_horizon_retains_entire_lot_basis_and_has_no_imputed_income(calendar):
    spec = contract_from_offer(make_offer())
    result = lot_cashflows(spec, make_lot(spec), horizon="2027-01-01", calendar=calendar)
    assert result["cashflows"] == []
    assert result["remaining_cost_basis"] == "700"
    assert result["income_cash"] == "0"
    assert result["remaining_market_value"] is None


def test_missing_index_dates_fail_closed_without_scalar_or_forward_fill(calendar):
    spec = contract_from_offer(make_offer("Tesouro Educa+", "2031-12-15"))
    values = external_values(spec, "2026-10-26", calendar)
    del values["2027-05-17"]
    result = lot_cashflows(spec, make_lot(spec), nominal_values=values, calendar=calendar)
    assert result["unsupported_reason"] == "missing_external_payment_index_values"
    assert result["cashflows"] == []
    assert result["missing_value_dates"] == ["2027-05-17"]
    with pytest.raises(ValueError, match="dated external"):
        price_contract(spec, "2026-10-26", 6, 4000, calendar)


@pytest.mark.parametrize("patch", [
    {"value_date": "2026-10-23"}, {"unit": "VNA"}, {"indexer": "SELIC"},
    {"value": 0}, {"value": True}, {"value": "NaN"}, {"basis": "observed"},
    {"basis": "constant_default"}, {"scenario_id": None}, {"as_of": "2026-10-23"},
    {"source_url": None},
])
def test_bad_or_falsely_observed_external_index_inputs_are_rejected(patch, calendar):
    spec = contract_from_offer(make_offer("Tesouro Renda+", "2049-12-15"))
    nominal = index_value("2026-10-26", unit="VNI")
    nominal.update(patch)
    with pytest.raises(ValueError):
        price_contract(spec, "2026-10-26", 6, nominal, calendar)


@pytest.mark.parametrize("rate", [-100, "-101", True, "NaN", float("inf")])
def test_invalid_yields_fail_before_discounting(rate, calendar):
    with pytest.raises(ValueError):
        price_contract(contract_from_offer(make_offer()), "2026-10-26", rate, calendar=calendar)


def test_negative_yield_is_a_premium_and_preserves_economic_loss(calendar):
    result = price_contract(contract_from_offer(make_offer()), "2026-10-26", -1, calendar=calendar)
    assert Decimal(result["unit_price"]) > 1000
    assert Decimal(lot_economic_cash(1000, result["unit_price"])["income_cash"]) < 0


def test_calendar_dates_and_coupon_gaps_fail_closed(calendar):
    spec = contract_from_offer(make_offer())
    for day in ("2026-10-25", "2026-11-20", datetime(2026, 10, 26)):
        with pytest.raises(ValueError):
            price_contract(spec, day, 6, calendar=calendar)
    with pytest.raises(ValueError, match="BrazilCalendar"):
        price_contract(spec, "2026-10-26", 6, calendar=lambda d: d)
    offer = make_offer("Tesouro Prefixado com Juros Semestrais", "2037-01-01",
                       first_coupon_date="2030-01-01", terms_source_url=SOURCE)
    with pytest.raises(ValueError, match="omits a payment"):
        price_contract(contract_from_offer(offer), "2026-10-26", 6, calendar=calendar)


def test_tampered_contract_and_lot_identity_cannot_override_verified_family(calendar):
    spec = contract_from_offer(make_offer())
    lot = make_lot(spec)
    lot["identity"]["maturity"] = "2030-01-01"
    with pytest.raises(ValueError, match="identity"):
        lot_cashflows(spec, lot, calendar=calendar)
    spec["terms"]["fixed_face_value"] = "700"
    with pytest.raises(ValueError, match="registered family"):
        price_contract(spec, "2026-10-26", 6, calendar=calendar)


def test_simple_cash_helper_requires_real_reference_and_retains_signed_losses():
    assert lot_economic_cash(1000, 700)["income_cash"] == "300"
    assert lot_economic_cash(0, 700)["income_cash"] == "-700"
    assert lot_economic_cash(1100, 800, reference_cash=1000, indexer="IPCA")["inflation_adjustment"] == "100"
    with pytest.raises(ValueError, match="reference_cash"):
        lot_economic_cash(1100, 800, indexer="IPCA")


def test_current37_immutable_quotes_remain_conditional_without_invented_indices(calendar):
    paths = list(EVIDENCE.glob("current_offers_20261023-*.json"))
    assert len(paths) == 1
    content = paths[0].read_bytes()
    assert hashlib.sha256(content).hexdigest().startswith(paths[0].stem.rsplit("-", 1)[1])
    offers = json.loads(content)["instruments"]
    assert len(offers) == 37
    original = deepcopy(offers)
    outcomes = []
    for offer in offers:
        result = offer_scenario(offer, "2026-10-26", calendar=calendar)
        assert result["recommendation_ready"] is False
        assert result["historical_series_created"] is False
        outcomes.append(result)
        if result["status"] != "unsupported":
            assert result["offer"]["isin"] == offer["isin"]
            assert result["offer"]["as_of"] == AS_OF
            assert result["offer"]["tradable"] is False
            assert result["purchase_executed"] is False
            assert result["lot_cash"].get("provenance", {}).get("lot", {}).get("basis") in (
                None, "hypothetical_purchase_at_reference_quote")
    assert sum(r["status"] == "scenario" for r in outcomes) == 3
    assert sum(r["status"] == "incomplete" for r in outcomes) == 33
    assert sum(r["status"] == "unsupported" for r in outcomes) == 1
    assert offers == original
    json.dumps(outcomes, allow_nan=False)


def test_composed_index_scenario_preserves_quote_and_uses_quote_price_as_cost(calendar):
    offer = make_offer("Tesouro Educa+", "2031-12-15", buy_rate="6.69", buy_price="4034.27",
                       rate_basis="real_above_ipca", isin="BRSTNCNAP0P9", market_status="closed", tradable=False)
    spec = contract_for_offer(offer, "2026-10-26")
    values = external_values(spec, "2026-10-26", calendar, "4800", "5000")
    original = deepcopy(values)
    result = offer_scenario(offer, "2026-10-26", nominal_value=values["2026-10-26"],
                            nominal_values=values, calendar=calendar)
    assert result["status"] == "scenario"
    assert result["lot_cash"]["original_cost_basis"] == "4034.27"
    assert Decimal(result["lot_cash"]["income_cash"]) == Decimal("5000") - Decimal("4034.27")
    assert values == original
    assert result["pricing"]["nominal_value_provenance"]["basis"] == "scenario"
    assert_cash_conservation(result["lot_cash"])


def test_unsupported_reserva_and_unknown_family_do_not_borrow_lft_formula():
    for kind in ("Tesouro Reserva", "Unknown family"):
        spec = contract_from_offer(make_offer(kind))
        result = price_contract(spec, "2026-10-26", None)
        assert result["status"] == "unsupported"
        assert result["unit_price"] is None
        assert result["cashflows"] == []


def test_current_quote_cannot_become_a_fabricated_prior_purchase(calendar):
    with pytest.raises(ValueError, match="backdate"):
        offer_scenario(make_offer(), "2020-10-26", calendar=calendar)


def test_observed_inputs_use_brasilia_day_even_when_timestamp_is_utc(calendar):
    spec = contract_from_offer(make_offer("Tesouro IPCA+", "2032-08-15"))
    nominal = index_value("2026-10-26", basis="observed", as_of="2026-10-26T01:00:00+00:00")
    with pytest.raises(ValueError, match="future index"):
        price_contract(spec, "2026-10-26", 6, nominal, calendar)
    with pytest.raises(ValueError, match="future acquisition"):
        lot_cashflows(spec, make_lot(spec, basis="observed_acquisition",
                                    as_of="2026-10-26T01:00:00+00:00"), calendar=calendar)


def test_evidence_manifest_matches_all_archived_source_bytes():
    manifest = json.loads((EVIDENCE / "valuation-research-20261025-v1.json").read_text(encoding="utf-8"))
    for item in manifest["sources"]:
        content = (EVIDENCE / item["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        assert len(content) == item["bytes"]


def test_quote_implied_index_is_explicitly_derived_and_does_not_supply_future_values(calendar):
    offers = json.loads(next(EVIDENCE.glob("current_offers_20261023-*.json")).read_bytes())["instruments"]
    for offer in offers:
        spec = contract_for_offer(offer, "2026-10-26")
        if not spec["supported"] or not spec["terms"]["indexer"]:
            continue
        implied = quote_implied_nominal_value(
            spec, "2026-10-26", offer["buy_rate"], offer["buy_price"],
            source_url=offer["source_url"], as_of=offer["as_of"], calendar=calendar)
        assert implied["basis"] == "derived_from_price_yield"
        assert implied["observed_index_value"] is False
        assert implied["derivation"]["execution_rounding_inverted"] is False
        priced = price_contract(spec, "2026-10-26", offer["buy_rate"], implied, calendar)
        assert abs(Decimal(priced["unit_price"]) - Decimal(str(offer["buy_price"]))) < Decimal("1e-25")
        assert priced["nominal_value_provenance"]["observed_index_value"] is False
        result = offer_scenario(offer, "2026-10-26", nominal_value=implied, calendar=calendar)
        assert result["status"] == "incomplete"
        assert result["lot_cash"]["unsupported_reason"] == "missing_external_payment_index_values"
        assert result["lot_cash"]["cashflows"] == []


def test_quote_derived_proxy_cannot_be_relabelled_observed_future_or_changed_silently(calendar):
    spec = contract_from_offer(make_offer("Tesouro Educa+", "2031-12-15"))
    implied = quote_implied_nominal_value(spec, "2026-10-26", "6.69", "4034.27",
                                        source_url=SOURCE, as_of=AS_OF, calendar=calendar)
    implied["value"] = str(Decimal(implied["value"]) + 100)
    with pytest.raises(ValueError, match="disagrees"):
        price_contract(spec, "2026-10-26", "6.69", implied, calendar)
    implied["basis"] = "observed"
    with pytest.raises(ValueError, match="future index"):
        price_contract(spec, "2026-10-26", "6.69", implied, calendar)


def test_quote_derivation_cannot_be_laundered_as_an_observed_index_on_the_same_day(calendar):
    spec = contract_from_offer(make_offer("Tesouro Educa+", "2031-12-15"))
    implied = quote_implied_nominal_value(spec, "2026-10-23", "6.69", "4034.27",
                                        source_url=SOURCE, as_of=AS_OF, calendar=calendar)
    implied["basis"] = "observed"
    with pytest.raises(ValueError, match="relabelled"):
        price_contract(spec, "2026-10-23", "6.69", implied, calendar)


def test_multiple_coupons_never_return_the_acquisition_principal_more_than_once(calendar):
    spec = contract_for_offer(make_offer("Tesouro Prefixado com Juros Semestrais", "2037-01-01"), "2026-10-26")
    result = lot_cashflows(spec, make_lot(spec, "1100"), calendar=calendar)
    assert len(result["cashflows"]) == 21
    for row in result["cashflows"][:-1]:
        assert Decimal(row["cost_basis_return"]) == 0
        assert row["gross_nominal_cash"] == row["income_cash"]
        assert row["remaining_cost_basis"] == "1100"
    assert result["cashflows"][-1]["cost_basis_return"] == "1100"
    assert_cash_conservation(result)


def test_ipca_coupon_cash_has_separate_real_base_and_index_movement(calendar):
    spec = contract_for_offer(make_offer("Tesouro IPCA+ com Juros Semestrais", "2037-05-15"), "2026-10-26")
    values = external_values(spec, "2026-10-26", calendar, "1000", "1100")
    result = lot_cashflows(spec, make_lot(spec), nominal_values=values, horizon="2026-11-16", calendar=calendar)
    row = result["cashflows"][0]
    with localcontext() as ctx:
        ctx.prec = 40
        real = Decimal(1000) * (Decimal("1.06").sqrt() - 1)
        assert abs(Decimal(row["economic_interest_real_base_brl"]) - real) < Decimal("1e-30")
        assert abs(Decimal(row["income_cash"]) - real * Decimal("1.1")) < Decimal("1e-30")
        assert abs(Decimal(row["inflation_adjustment"]) - real * Decimal(".1")) < Decimal("1e-30")
    assert_cash_conservation(result)


def test_buying_on_coupon_settlement_does_not_create_ownership_of_that_coupon(calendar):
    spec = contract_for_offer(make_offer("Tesouro Prefixado com Juros Semestrais", "2037-01-01"), "2027-07-01")
    priced = price_contract(spec, "2027-07-01", 10, calendar=calendar)
    assert priced["cashflows"][0]["contractual_date"] == "2028-01-01"


def test_missing_quantlib_fails_closed_but_pure_accounting_stays_available(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "QuantLib", None)
    with pytest.raises(RuntimeError, match="QuantLib is required"):
        BrazilCalendar()
    assert lot_economic_cash(1000, 700)["income_cash"] == "300"
