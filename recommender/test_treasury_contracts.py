"""Nominal contract fixtures only; no downloads, pricing, lots or activation."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, localcontext
import json

import numpy as np
import pytest

from .treasury_contracts import (
    EDUCA_SOURCE, RENDA_SOURCE, RESERVE_UNSUPPORTED_REASON,
    contract_from_offer, nominal_cash_schedule,
)


OFFER_SOURCE = "https://www.tesourodireto.com.br/o/rentabilidade/investir"
TERMS_SOURCE = "https://www.tesourodireto.com.br/documents/d/guest/calendario-da-emissao"


def offer(kind="Tesouro Renda+", maturity="2049-12-15", **extra):
    return {"kind": kind, "maturity": maturity, "source_url": OFFER_SOURCE, **extra}


def coupon_spec(kind="Tesouro IPCA+ com Juros Semestrais", maturity="2045-05-15",
                first="2026-05-15", **extra):
    return contract_from_offer(offer(kind, maturity, first_coupon_date=first,
                                     terms_source_url=TERMS_SOURCE, **extra))


@pytest.mark.parametrize("kind,maturity,conversion,count,regular,last,source", [
    ("Tesouro Renda+", "2049-12-15", "2030-01-15", 240, "0.416666", "0.416826", RENDA_SOURCE),
    ("Tesouro Educa+", "2031-12-15", "2027-01-15", 60, "1.666666", "1.666706", EDUCA_SOURCE),
])
def test_full_amortization_includes_first_and_last_without_inventing_income(
        kind, maturity, conversion, count, regular, last, source):
    original = offer(kind, maturity, buy_price=200, buy_rate=6.5)
    spec = contract_from_offer(original)
    assert spec["identity"] == {"kind": kind, "maturity": maturity}
    assert spec["terms_complete"] is True
    assert spec["terms"]["conversion_date"] == conversion != maturity
    assert spec["terms"]["nominal_base_date"] == "2000-07-15"
    assert spec["terms"]["nominal_base_value"] == "1000"
    assert spec["terms"]["indexer"] == "IPCA"
    rows = nominal_cash_schedule(spec, conversion, maturity, "1000")
    assert len(rows) == count
    assert rows[0]["contractual_date"] == conversion
    assert rows[-1]["contractual_date"] == maturity
    assert [r["installment"] for r in rows] == list(range(1, count + 1))
    assert all(date.fromisoformat(r["contractual_date"]).day == 15 for r in rows)
    assert Decimal(rows[0]["amortized_face_principal"]) == Decimal(regular) * 10
    assert Decimal(rows[-1]["amortized_face_principal"]) == Decimal(last) * 10
    assert sum(Decimal(r["amortized_face_principal"]) for r in rows) == Decimal(1000)
    for row in rows:
        assert Decimal(row["coupon_interest"]) == Decimal(row["income_cash"]) == 0
        assert row["gross_nominal_cash"] == row["amortized_face_principal"]
        assert row["inflation_adjustment"] is None
        assert row["economic_interest"] is row["cost_basis_return"] is None
        assert row["income_split_status"] == "requires_tax_or_lot_cost_basis"
        assert row["earnings_model_status"] == "pending"
        assert row["settled"] is row["adjusted_payment_date"] is None
        assert row["date_status"] == "contractual_unadjusted"
        assert row["provenance"]["methodology_sources"] == [source]
    assert original["buy_price"] == 200
    json.dumps({"spec": spec, "schedule": rows}, allow_nan=False)


def test_identity_uses_full_kind_and_maturity_and_never_the_commercial_year():
    a = contract_from_offer(offer(name="Tesouro Renda+ 2099"))
    b = contract_from_offer(offer(maturity="2049-11-15", name="Tesouro Renda+ 2099"))
    c = contract_from_offer(offer("Tesouro Educa+", "2049-12-15"))
    assert a["identity"] != b["identity"] != c["identity"]
    assert a["terms"]["conversion_date"] == "2030-01-15"
    assert b["terms"]["conversion_date"] == "2029-12-15"
    assert c["terms"]["conversion_date"] == "2045-01-15"


def test_window_is_inclusive_nominal_and_preserves_global_installment_numbers():
    spec = contract_from_offer(offer("Tesouro Educa+", "2031-12-15"))
    rows = nominal_cash_schedule(spec, date(2027, 2, 15), "2027-03-15", 1000)
    assert [r["installment"] for r in rows] == [2, 3]
    assert nominal_cash_schedule(spec, "2026-01-01", "2027-01-14", 1000) == []
    assert nominal_cash_schedule(spec, "2032-01-01", "2033-01-01", 1000) == []
    assert len(nominal_cash_schedule(spec, "2031-12-15", "2031-12-15", 1000)) == 1


def test_dated_vni_is_external_and_does_not_imply_computed_inflation():
    spec = contract_from_offer(offer("Tesouro Educa+", "2031-12-15"))
    supplied = {date(2027, 1, 15): Decimal("4000"), date(2027, 2, 15): Decimal("3900")}
    rows = nominal_cash_schedule(spec, "2027-01-01", "2027-02-28", supplied.get)
    assert [r["nominal_unit_value"] for r in rows] == ["4000", "3900"]
    assert Decimal(rows[1]["amortized_face_principal"]) < Decimal(rows[0]["amortized_face_principal"])
    assert all(r["inflation_adjustment"] is None for r in rows)
    assert all(r["nominal_value_basis"] == "caller_supplied_dated_value" for r in rows)
    with pytest.raises(ValueError, match="nominal_unit_value"):
        nominal_cash_schedule(spec, "2027-01-01", "2027-03-31", supplied.get)


@pytest.mark.parametrize("kind,maturity", [
    ("Tesouro Renda+", "2049-12-15"), ("Tesouro Educa+", "2031-12-15"),
    ("Tesouro IPCA+", "2032-08-15"), ("Tesouro Selic", "2031-03-01"),
])
def test_missing_indexed_nominal_is_never_replaced_by_zero_or_base_1000(kind, maturity):
    spec = contract_from_offer(offer(kind, maturity))
    with pytest.raises(ValueError, match="Missing VNI/VNA"):
        nominal_cash_schedule(spec, "2026-01-01", "2050-01-01", None)


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "NaN", "-Infinity", {}, None])
def test_invalid_amortizing_nominal_values_fail_closed(value):
    spec = contract_from_offer(offer())
    with pytest.raises(ValueError):
        nominal_cash_schedule(spec, "2030-01-15", "2030-01-15", value)


@pytest.mark.parametrize("rate", [-99.999, -1, 0, "6.5", None])
def test_negative_yield_above_minus_100_is_allowed_but_never_becomes_coupon(rate):
    spec = contract_from_offer(offer(buy_rate=rate, buy_price=1))
    rows = nominal_cash_schedule(spec, "2030-01-15", "2030-01-15", 1000)
    assert rows[0]["coupon_interest"] == "0"
    assert rows[0]["economic_interest"] is None


@pytest.mark.parametrize("rate", [-100, -101, True, float("nan"), float("inf"), "unknown"])
def test_invalid_rate_bounds_do_not_produce_a_contract(rate):
    with pytest.raises(ValueError, match="buy_rate"):
        contract_from_offer(offer(buy_rate=rate))


@pytest.mark.parametrize("kind,maturity,nominal", [
    ("Tesouro Prefixado", "2029-01-01", None),
    ("Tesouro IPCA+", "2032-08-15", "5000"),
    ("Tesouro Selic", "2031-03-01", "20000"),
])
def test_bullets_return_face_separately_without_imputing_economic_interest(kind, maturity, nominal):
    spec = contract_from_offer(offer(kind, maturity, buy_rate=-0.1, buy_price=700))
    rows = nominal_cash_schedule(spec, "2026-01-01", "2033-01-01", nominal)
    assert len(rows) == 1 and rows[0]["contractual_date"] == maturity
    assert Decimal(rows[0]["amortized_face_principal"]) == Decimal(nominal or 1000)
    assert rows[0]["coupon_interest"] == rows[0]["income_cash"] == "0"
    assert rows[0]["economic_interest"] is None
    assert rows[0]["inflation_adjustment"] == ("0" if kind == "Tesouro Prefixado" else None)


def test_purchase_price_cannot_replace_fixed_nominal_face():
    spec = contract_from_offer(offer("Tesouro Prefixado", "2029-01-01"))
    with pytest.raises(ValueError, match="Fixed nominal unit face"):
        nominal_cash_schedule(spec, "2029-01-01", "2029-01-01", 700)


@pytest.mark.parametrize("kind,maturity,first,annual,nominal", [
    ("Tesouro Prefixado com Juros Semestrais", "2037-01-01", "2026-07-01", "10", "1000"),
    ("Tesouro IPCA+ com Juros Semestrais", "2045-05-15", "2026-05-15", "6", "5000"),
    ("Tesouro IPCA+ com Juros Semestrais", "2060-08-15", "2026-02-15", "6", "5000"),
])
def test_semiannual_coupon_is_contract_rate_not_purchase_yield(kind, maturity, first, annual, nominal):
    spec = coupon_spec(kind, maturity, first, buy_rate=-1)
    rows = nominal_cash_schedule(spec, "2026-01-01", maturity, nominal)
    assert rows[0]["contractual_date"] == first
    assert rows[-1]["contractual_date"] == maturity
    with localcontext() as ctx:
        ctx.prec = 40
        expected = Decimal(nominal) * ((1 + Decimal(annual) / 100).sqrt() - 1)
    assert all(Decimal(r["coupon_interest"]) == expected for r in rows)
    assert all(Decimal(r["amortized_face_principal"]) == 0 for r in rows[:-1])
    assert Decimal(rows[-1]["amortized_face_principal"]) == Decimal(nominal)
    assert rows[-1]["income_cash"] == rows[-1]["coupon_interest"]
    assert Decimal(rows[-1]["gross_nominal_cash"]) > Decimal(rows[-1]["income_cash"])
    assert spec["provenance"]["additional_terms_source_url"] == TERMS_SOURCE


def test_incomplete_coupon_terms_do_not_invent_preissuance_cashflows():
    spec = contract_from_offer(offer("Tesouro IPCA+ com Juros Semestrais", "2045-05-15"))
    assert spec["supported"] and not spec["terms_complete"]
    assert spec["missing_terms"] == ["first_coupon_date", "terms_source_url"]
    with pytest.raises(ValueError, match="Incomplete contract terms"):
        nominal_cash_schedule(spec, "2000-01-01", "2045-05-15", 1000)
    complete = coupon_spec()
    assert nominal_cash_schedule(complete, "2000-01-01", "2026-05-14", 1000) == []


@pytest.mark.parametrize("extra", [
    {"first_coupon_date": "2026-06-15", "terms_source_url": TERMS_SOURCE},
    {"first_coupon_date": "2046-05-15", "terms_source_url": TERMS_SOURCE},
    {"first_coupon_date": "2026-05-16", "terms_source_url": TERMS_SOURCE},
    {"first_coupon_date": "2026-05-15", "terms_source_url": "http://example.com/terms"},
])
def test_bad_coupon_terms_are_rejected(extra):
    with pytest.raises(ValueError):
        contract_from_offer(offer("Tesouro IPCA+ com Juros Semestrais", "2045-05-15", **extra))


def test_reserve_is_recognized_but_has_no_invented_contract_or_default_rate():
    original = offer("Tesouro Reserva", "2036-01-01", buy_rate_text="SELIC", isin="BRSTNCT1D008")
    spec = contract_from_offer(original)
    assert spec["recognized"] is True and spec["supported"] is False
    assert spec["unsupported_reason"] == RESERVE_UNSUPPORTED_REASON
    assert spec["terms"] is None and spec["terms_complete"] is False
    assert spec["known_offer"]["buy_rate"] is None
    assert spec["known_offer"]["buy_rate_text"] == "SELIC"
    assert spec["provenance"]["offer_source_url"] == OFFER_SOURCE
    assert spec["provenance"]["methodology_sources"] == []
    with pytest.raises(ValueError, match=RESERVE_UNSUPPORTED_REASON):
        nominal_cash_schedule(spec, "2026-01-01", "2037-01-01", 1000)


def test_unknown_fields_cannot_override_contract_or_claim_validation():
    original = offer(terms={"installments": 1}, terms_complete=False, earnings_model_status="ready",
                     inflation_adjustment=0, nominal_unit_value=0, coupon_annual_rate_percent=99)
    frozen = deepcopy(original)
    spec = contract_from_offer(original)
    assert original == frozen
    assert spec["terms"]["installments"] == 240
    assert spec["terms"]["coupon_annual_rate_percent"] == "0"
    assert spec["earnings_model_status"] == "pending"
    assert "inflation_adjustment" not in spec and "nominal_unit_value" not in spec
    assert spec["known_offer"]["isin"] is None
    unknown = contract_from_offer(offer("Tesouro Inexistente", "2049-12-15"))
    assert not unknown["recognized"] and not unknown["supported"]


@pytest.mark.parametrize("mutate", [
    lambda s: s["terms"].update(installments=60),
    lambda s: s["terms"].update(coupon_annual_rate_percent="99"),
    lambda s: s["terms"].update(conversion_date="2029-01-15"),
    lambda s: s["identity"].update(maturity="2030-01-15"),
    lambda s: s.update(terms_complete=False),
    lambda s: s.update(provenance=None),
])
def test_modified_specs_cannot_bypass_contract_validation(mutate):
    spec = contract_from_offer(offer())
    mutate(spec)
    with pytest.raises(ValueError):
        nominal_cash_schedule(spec, "2030-01-01", "2050-01-01", 1000)


def test_calendar_requires_both_external_callback_and_provenance_and_never_proves_settlement():
    spec = contract_from_offer(offer("Tesouro Educa+", "2031-12-15"))
    # Synthetic calendar for adapter verification, not a Brazilian holiday source.
    calendar = np.busdaycalendar()

    def adjust(day):
        return date.fromisoformat(str(np.busday_offset(day.isoformat(), 0, roll="following", busdaycal=calendar)))

    provenance = {"library": "numpy.busday_offset", "version": np.__version__,
                  "source_url": "https://numpy.org/doc/stable/reference/generated/numpy.busday_offset.html"}
    rows = nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", 1000,
                                 business_day_adjuster=adjust, calendar_source=provenance)
    assert rows[0]["contractual_date"] == "2027-05-15"
    assert rows[0]["adjusted_payment_date"] == "2027-05-17"
    assert rows[0]["date_status"] == "externally_adjusted_projection"
    assert rows[0]["settled"] is None
    assert rows[0]["calendar_provenance"] == provenance
    with pytest.raises(ValueError, match="Calendar"):
        nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", 1000,
                              business_day_adjuster=adjust)
    with pytest.raises(ValueError, match="Calendar"):
        nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", 1000, calendar_source=provenance)
    with pytest.raises(ValueError, match="provenance"):
        nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", 1000,
                              business_day_adjuster=adjust, calendar_source={})
    with pytest.raises(ValueError, match="before"):
        nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", 1000,
                              business_day_adjuster=lambda d: date(2027, 5, 14), calendar_source=provenance)
    supplied = {date(2027, 5, 15): 1000, date(2027, 5, 17): 1100}
    valued = nominal_cash_schedule(spec, "2027-05-15", "2027-05-15", supplied.get,
                                  business_day_adjuster=adjust, calendar_source=provenance)[0]
    assert valued["nominal_unit_value"] == "1100"
    assert valued["nominal_value_date"] == "2027-05-17"
    assert valued["provenance"]["offer_source_url"] == OFFER_SOURCE
    assert valued["settled"] is None


@pytest.mark.parametrize("value", [None, {}, "2030-02-30", "20300115", datetime(2030, 1, 15)])
def test_invalid_maturity_never_falls_back_to_name_year(value):
    with pytest.raises(ValueError, match="maturity"):
        contract_from_offer(offer(maturity=value, name="Tesouro Renda+ 2030"))


def test_invalid_contract_dates_and_window_are_rejected():
    with pytest.raises(ValueError, match="day 15"):
        contract_from_offer(offer(maturity="2049-12-16"))
    with pytest.raises(ValueError, match="conversion_date"):
        contract_from_offer(offer(conversion_date="2049-12-15"))
    with pytest.raises(ValueError, match="date range"):
        contract_from_offer(offer(maturity="0010-12-15"))
    spec = contract_from_offer(offer())
    with pytest.raises(ValueError, match="fromdate"):
        nominal_cash_schedule(spec, "2050-01-01", "2030-01-01", 1000)
