"""Offline Treasury pricing and acquisition-lot cash attribution, never history.

``price_contract(spec, valuation_date, annual_yield_percent, nominal_value=None)``
uses QuantLib's Brazil Settlement calendar and annually compounded Business/252.
valuation_date is an explicit purchase/valuation settlement date, not a trade
timestamp. The yield is in percentage points: nominal for LTN/NTN-F, real above
IPCA for NTN-B/Principal/B1, and *only* the spread over Selic for LFT.

Indexed nominal_value is a dated external record (see _nominal). No index path,
purchase settlement lag, monthly coupon, reinvestment or prior prices are inferred.
Prices are theoretical full/dirty PVs, without official execution truncations,
fees or tax. This intentionally does not promise an exchange/execution PU match.

``lot_cashflows`` allocates actual acquisition cost pro rata to the remaining
face amortizations. This is an explicit realization policy, not effective-interest
accounting or Brazilian tax basis. Coupons retain their contractual identity;
redemption earnings are cash less allocated cost, including signed losses. IPCA
earnings are split into acquisition-date purchasing-power BRL and index movement.
All projected payment dates remain unsettled. No function performs I/O or changes
dataset readiness, active pointers, global QuantLib settings or contract terms.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import math
from urllib.parse import parse_qsl, urlsplit

from . import treasury_contracts as contracts


BRT = timezone(timedelta(hours=-3))
STN_METHODOLOGY = (
    "https://cdn.tesouro.gov.br/sistemas-internos/apex/producao/sistemas/thot/"
    "arquivos/publicacoes/26310_1658601/Metodologia%20de%20Calculo%20-%20290424.pdf"
)
BCB_METHODOLOGY = (
    "https://www.bcb.gov.br/conteudo/depec/NotasTecnicas/"
    "2002nt12mercadotitulopublicop.pdf"
)
RESERVA_SOURCE = "https://www.tesourodireto.com.br/tesouro-reserva"
RESERVA_RELEASE = (
    "https://www.gov.br/tesouronacional/pt-br/noticias/"
    "tesouro-direto-tem-emissao-liquida-de-r-6-07-bi-em-maio"
)
QUANTLIB_CALENDAR_SOURCE = (
    "https://github.com/lballabio/QuantLib/blob/v1.44/ql/time/calendars/brazil.cpp"
)
QUANTLIB_DISCOUNT_SOURCE = (
    "https://github.com/lballabio/QuantLib/blob/v1.44/ql/interestrate.cpp"
)

_REGISTRY = {
    "Tesouro Prefixado": ("LTN", None, "nominal_annual", "discounted_fixed_face", contracts.PREFIXADO_SOURCE),
    "Tesouro Prefixado com Juros Semestrais": (
        "NTN-F", None, "nominal_annual", "discounted_coupon_and_face", STN_METHODOLOGY),
    "Tesouro IPCA+": ("NTN-B Principal", "IPCA", "real_above_ipca", "discounted_index_face", contracts.IPCA_SOURCE),
    "Tesouro IPCA+ com Juros Semestrais": (
        "NTN-B", "IPCA", "real_above_ipca", "discounted_coupon_and_index_face", contracts.IPCA_SOURCE),
    "Tesouro Renda+": ("NTN-B1", "IPCA", "real_above_ipca", "discounted_240_face_amortizations", contracts.RENDA_SOURCE),
    "Tesouro Educa+": ("NTN-B1", "IPCA", "real_above_ipca", "discounted_60_face_amortizations", contracts.EDUCA_SOURCE),
    "Tesouro Selic": ("LFT", "SELIC", "spread_over_selic", "indexed_face_discounted_by_spread", contracts.SELIC_SOURCE),
}


def pricing_family_registry():
    """Return fresh documentary family capabilities, not per-offer readiness."""
    result = {
        kind: {"family": values[0], "indexer": values[1], "yield_basis": values[2],
               "formula": values[3], "pricing_formula_verified": True,
               "methodology_sources": [values[4]],
               "requires_external_index_values": values[1] is not None}
        for kind, values in _REGISTRY.items()
    }
    result["Tesouro Reserva"] = {
        "pricing_formula_verified": False, "unsupported_reason": "reserve_pricing_formula_not_verified",
        "verified_product_facts": {"return_reference": "100_percent_selic_over",
                                   "mark_to_market": False, "initial_distributor": "Banco do Brasil"},
        "methodology_sources": [RESERVA_SOURCE, RESERVA_RELEASE],
    }
    return result


def _day(value, field):
    return contracts._day(value, field)


def _number(value, field, *, positive=False, nonnegative=False):
    number = contracts._decimal(value, field)
    if positive and number <= 0 or nonnegative and number < 0:
        raise ValueError(field + (" must be positive" if positive else " must be nonnegative"))
    return number


def _money(value):
    return format(value, "f")


def _source(value, field):
    if value is None:
        raise ValueError("Missing " + field)
    url = contracts._url(value, field)
    parsed = urlsplit(url)
    if any(key.lower() in {"token", "key", "api_key", "access_token"} for key, _ in parse_qsl(parsed.query)):
        raise ValueError(field + " must not contain query credentials")
    return url


def _timestamp(value, field):
    if not isinstance(value, str):
        raise ValueError("Missing " + field)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid " + field) from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(field + " requires a timezone")
    return stamp


class BrazilCalendar:
    """QuantLib Settlement (not exchange) calendar; future holidays are projections.

    QuantLib is optional at module import so unsupported families and the pure
    cost-basis helper remain usable without it. Pricing fails closed if absent.
    """

    def __init__(self):
        try:
            import QuantLib as ql
        except ImportError as error:
            raise RuntimeError("QuantLib is required for Treasury pricing/calendar") from error
        self._ql = ql
        self._calendar = ql.Brazil(ql.Brazil.Settlement)
        self._day_counter = ql.Business252(self._calendar)

    def _ql_date(self, value):
        value = _day(value, "calendar date")
        try:
            return self._ql.Date(value.day, value.month, value.year)
        except RuntimeError as error:
            raise ValueError("Date outside QuantLib supported range") from error

    def following(self, value):
        adjusted = self._calendar.adjust(self._ql_date(value), self._ql.Following)
        return date(adjusted.year(), adjusted.month(), adjusted.dayOfMonth())

    def is_business_day(self, value):
        return self._calendar.isBusinessDay(self._ql_date(value))

    def business_days(self, first, last):
        first, last = _day(first, "first"), _day(last, "last")
        if first > last:
            raise ValueError("Business/252 start must not follow end")
        return self._day_counter.dayCount(self._ql_date(first), self._ql_date(last))

    def discount(self, annual_yield_percent, first, last):
        annual = _number(annual_yield_percent, "annual_yield_percent")
        if annual <= -100:
            raise ValueError("annual_yield_percent must be greater than -100")
        days = self.business_days(first, last)
        rate = self._ql.InterestRate(float(annual / 100), self._day_counter,
                                     self._ql.Compounded, self._ql.Annual)
        try:
            factor = rate.discountFactor(days / 252)
        except (RuntimeError, OverflowError) as error:
            raise ValueError("Yield cannot be represented by QuantLib") from error
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Invalid QuantLib discount factor")
        return Decimal(str(factor))

    @property
    def provenance(self):
        version = self._ql.__version__
        return {"library": "QuantLib", "version": version, "market": "Brazil.Settlement",
                "day_counter": "Business/252", "compounding": "annual_effective",
                "day_interval": "settlement_inclusive_payment_exclusive",
                "payment_adjustment": "Following", "settlement_evidence": False,
                "source_url": QUANTLIB_CALENDAR_SOURCE.replace("v1.44/", "v" + version + "/")}


def _calendar(value):
    if value is not None and type(value) is not BrazilCalendar:
        raise ValueError("calendar must be a provenance-aware BrazilCalendar")
    return value if value is not None else BrazilCalendar()


def _nominal(record, expected_date, indexer, unit):
    """External record: value, value_date, indexer, unit, basis, source_url, as_of.

    basis is observed, projection, scenario, or derived_from_price_yield. A
    scenario requires scenario_id; quote inversion requires derivation metadata.
    VNI is integral, never the already amortized VNA of an NTN-B1. A scalar or
    missing date is insufficient. Records are attributed, not authenticated.
    """
    if not isinstance(record, dict):
        raise ValueError("Missing dated external " + unit + " record")
    value = _number(record.get("value"), "nominal value", positive=True)
    value_date = _day(record.get("value_date"), "nominal value_date")
    if value_date != expected_date:
        raise ValueError("nominal value_date must match its settlement/payment date")
    if record.get("indexer") != indexer or record.get("unit") != unit:
        raise ValueError("nominal indexer/unit mismatch; NTN-B1 requires integral VNI")
    basis = record.get("basis")
    if basis not in {"observed", "projection", "scenario", "derived_from_price_yield"}:
        raise ValueError("Invalid nominal basis")
    stamp = _timestamp(record.get("as_of"), "nominal as_of")
    if basis == "observed" and value_date > stamp.astimezone(BRT).date():
        raise ValueError("A future index value cannot be observed")
    if basis == "observed" and record.get("derivation") is not None:
        raise ValueError("A quote-derived index cannot be relabelled as observed")
    scenario = record.get("scenario_id")
    if basis == "scenario" and (not isinstance(scenario, str) or not scenario.strip()):
        raise ValueError("A scenario nominal value requires scenario_id")
    provenance = {"value": _money(value), "value_date": value_date.isoformat(),
                  "indexer": indexer, "unit": unit, "basis": basis,
                  "source_url": _source(record.get("source_url"), "nominal source_url"),
                  "as_of": stamp.isoformat(), "scenario_id": scenario,
                  "observed_index_value": basis == "observed",
                  "independently_authenticated": False}
    if basis == "derived_from_price_yield":
        derivation = record.get("derivation")
        if not isinstance(derivation, dict):
            raise ValueError("Derived nominal value requires quote derivation metadata")
        price = _number(derivation.get("quoted_unit_price"), "derived quoted_unit_price", positive=True)
        annual = _number(derivation.get("annual_yield_percent"), "derived annual_yield_percent")
        quote = _number(derivation.get("quotation_percent_unrounded"), "derived quotation", positive=True)
        if annual <= -100 or derivation.get("valuation_date") != value_date.isoformat():
            raise ValueError("Invalid derived yield or valuation date")
        with localcontext() as ctx:
            ctx.prec = 40
            if abs(value * quote / 100 - price) > max(price, Decimal(1)) * Decimal("1e-30"):
                raise ValueError("Derived nominal value disagrees with price/quotation")
        provenance["derivation"] = {
            "method": "quoted_price_divided_by_theoretical_quotation",
            "quoted_unit_price": _money(price), "annual_yield_percent": _money(annual),
            "quotation_percent_unrounded": _money(quote), "valuation_date": value_date.isoformat(),
            "execution_rounding_inverted": False,
            "index_proxy_may_include_execution_rounding_and_calendar_differences": True,
        }
    return value, provenance


def _unit(spec):
    return "VNI" if spec["terms"]["conversion_date"] else "VNA"


def _unsupported(spec, reason):
    return {"schema_version": 1, "status": "unsupported", "supported": False,
            "unsupported_reason": reason,
            "identity": deepcopy(spec.get("identity")), "unit_price": None,
            "cashflows": [], "recommendation_ready": False,
            "historical_series_created": False,
            "provenance": deepcopy(spec.get("provenance")),
            "family_capability": pricing_family_registry().get(spec.get("kind"))}


def _checked(spec):
    if not isinstance(spec, dict):
        raise ValueError("spec must be a Treasury contract dictionary")
    if spec.get("kind") not in _REGISTRY:
        return None
    return contracts._checked_spec(spec)


def contract_for_offer(offer, valuation_date):
    """Prepare a new-lot forward schedule using the verified coupon cycle.

    The derived first coupon is the next contractual coupon for this valuation,
    not an assertion about the title's first issuance. Exact kind/maturity remain
    authoritative; names and commercial years never determine identity.
    """
    settlement = _day(valuation_date, "valuation_date")
    spec = contracts.contract_from_offer(offer)
    if spec["supported"] and spec["terms"]["coupon_months"] and not spec["terms_complete"]:
        first = _day(spec["maturity"], "maturity")
        while contracts._shift(first, -6) > settlement:
            first = contracts._shift(first, -6)
        supplied = dict(offer)
        supplied.setdefault("first_coupon_date", first.isoformat())
        supplied.setdefault("terms_source_url", contracts.COUPON_CALENDAR_SOURCE)
        spec = contracts.contract_from_offer(supplied)
    return spec


def _schedule(spec, settlement, calendar):
    if not calendar.is_business_day(settlement):
        raise ValueError("valuation/settlement date must be a Brazil business day")
    maturity = _day(spec["maturity"], "maturity")
    if settlement >= calendar.following(maturity):
        return []
    terms = spec["terms"]
    if terms["first_coupon_date"]:
        previous = contracts._shift(_day(terms["first_coupon_date"], "first_coupon_date"), -6)
        if previous > settlement:
            raise ValueError("Coupon schedule omits a payment after settlement")
    # A reference unit constructs the contractual fractions, never an index forecast.
    rows = contracts.nominal_cash_schedule(
        spec, min(settlement, maturity), maturity, "1000",
        business_day_adjuster=calendar.following,
        calendar_source={key: calendar.provenance[key] for key in ("library", "version", "source_url")},
    )
    return [row for row in rows if _day(row["adjusted_payment_date"], "payment date") > settlement]


def price_contract(spec, valuation_date, annual_yield_percent, nominal_value=None, calendar=None):
    """Theoretical full PV per unit at explicitly supplied settlement and yield.

    IPCA PV holds today's VNA/VNI as the price reference and discounts by the
    real yield. LFT discounts today's external VNA by the quoted spread, without
    projecting Selic. No future index values are needed for current pricing.
    Missing external values produce unsupported; malformed supplied data raises.
    """
    checked = _checked(spec)
    if checked is None:
        return _unsupported(spec, "reserve_pricing_formula_not_verified" if spec.get("kind") == "Tesouro Reserva"
                            else "unverified_pricing_family")
    settlement = _day(valuation_date, "valuation_date")
    annual = _number(annual_yield_percent, "annual_yield_percent")
    if annual <= -100:
        raise ValueError("annual_yield_percent must be greater than -100")
    calendar = _calendar(calendar)
    rows = _schedule(checked, settlement, calendar)
    indexer = checked["terms"]["indexer"]
    value, nominal_provenance = Decimal(1000), None
    if indexer:
        if nominal_value is None and rows:
            return _unsupported(spec, "missing_external_settlement_" + _unit(checked).lower())
        if nominal_value is not None:
            value, nominal_provenance = _nominal(nominal_value, settlement, indexer, _unit(checked))
    elif nominal_value is not None and _number(nominal_value, "fixed face") != 1000:
        raise ValueError("Fixed face must be 1000, not a purchase price")
    discounted = []
    with localcontext() as ctx:
        ctx.prec = 40
        for row in rows:
            payment = _day(row["adjusted_payment_date"], "payment date")
            factor = calendar.discount(annual, settlement, payment)
            fraction = Decimal(row["gross_nominal_cash"]) / 1000
            discounted.append({"contractual_date": row["contractual_date"],
                               "adjusted_payment_date": payment.isoformat(), "settled": None,
                               "business_days": calendar.business_days(settlement, payment),
                               "discount_factor": _money(factor),
                               "reference_cash": _money(value * fraction),
                               "reference_basis": "settlement_index_unit_not_future_nominal_cash" if indexer else "fixed_brl",
                               "present_value": _money(value * fraction * factor)})
        total = sum((Decimal(row["present_value"]) for row in discounted), Decimal(0))
        quote = total / value * 100
    return {"schema_version": 1, "status": "priced" if rows else "matured", "supported": True,
            "unsupported_reason": None, "identity": deepcopy(spec["identity"]),
            "valuation_date": settlement.isoformat(), "date_basis": "explicit_settlement",
            "annual_yield_percent": _money(annual), "yield_basis": _REGISTRY[spec["kind"]][2],
            "unit_price": _money(total), "quotation_percent_unrounded": _money(quote),
            "price_basis": "theoretical_full_dirty_pv", "execution_price": None,
            "precision_policy": "QuantLib_binary64_discount_factors_decimal_cash_no_execution_truncation",
            "cashflows": discounted, "calendar_provenance": calendar.provenance,
            "nominal_value_provenance": nominal_provenance,
            "provenance": {"contract": deepcopy(spec["provenance"]),
                           "input_quote": deepcopy(spec.get("known_offer")),
                           "yield_input_basis": "explicit_caller_yield_not_authenticated_quote",
                           "methodology_sources": [_REGISTRY[spec["kind"]][4], BCB_METHODOLOGY],
                           "library_formula_source": QUANTLIB_DISCOUNT_SOURCE},
            "recommendation_ready": False, "historical_series_created": False}


def quote_implied_nominal_value(spec, valuation_date, annual_yield_percent, quoted_unit_price, *,
                                source_url, as_of, calendar=None):
    """Invert a supplied quote into a settlement index *proxy*, not observed VNA.

    Uses the verified family PV formula and the supplied annual quote yield.
    Execution truncations are not invertible, so the result may absorb their
    effects and calendar differences. This opt-in helper creates no future path.
    Its output is accepted by price_contract/lot_cashflows, with derivation flags.
    """
    checked = _checked(spec)
    if checked is None:
        return _unsupported(spec, "unverified_pricing_family")
    indexer = checked["terms"]["indexer"]
    if indexer is None:
        raise ValueError("Quote-implied nominal value applies only to indexed families")
    settlement = _day(valuation_date, "valuation_date")
    stamp = _timestamp(as_of, "quote as_of")
    if stamp.astimezone(BRT).date() > settlement:
        raise ValueError("Cannot backdate a derived index before its quote date")
    price = _number(quoted_unit_price, "quoted_unit_price", positive=True)
    source = _source(source_url, "quote source_url")
    reference = {"value": "1", "value_date": settlement.isoformat(), "indexer": indexer,
                 "unit": _unit(checked), "basis": "projection", "source_url": source,
                 "as_of": stamp.isoformat()}
    calendar = _calendar(calendar)
    unit_price = price_contract(spec, settlement, annual_yield_percent, reference, calendar)
    if unit_price["status"] == "matured":
        return _unsupported(spec, "cannot_infer_index_without_remaining_cashflows")
    with localcontext() as ctx:
        ctx.prec = 40
        reference["value"] = _money(price / Decimal(unit_price["unit_price"]))
    reference.update(
        basis="derived_from_price_yield", observed_index_value=False,
        independently_authenticated=False,
        derivation={"method": "quoted_price_divided_by_theoretical_quotation",
                    "quoted_unit_price": _money(price),
                    "annual_yield_percent": unit_price["annual_yield_percent"],
                    "quotation_percent_unrounded": unit_price["quotation_percent_unrounded"],
                    "valuation_date": settlement.isoformat(), "execution_rounding_inverted": False,
                    "index_proxy_may_include_execution_rounding_and_calendar_differences": True},
    )
    return reference


def lot_economic_cash(gross_cash, cost_basis_return, *, reference_cash=None, indexer=None):
    """Split one gross receipt from its allocated acquisition cost, including loss.

    For IPCA, reference_cash must be in acquisition-date purchasing-power BRL:
    nominal income = real-base income + index adjustment. Selic is interest,
    not an inflation index. This helper never treats face principal as income.
    """
    cash = _number(gross_cash, "gross_cash", nonnegative=True)
    basis = _number(cost_basis_return, "cost_basis_return", nonnegative=True)
    if indexer not in {None, "IPCA", "SELIC"}:
        raise ValueError("Unsupported indexer")
    with localcontext() as ctx:
        ctx.prec = 40
        nominal_income = cash - basis
        if indexer == "IPCA":
            if reference_cash is None:
                raise ValueError("IPCA split requires acquisition-date reference_cash")
            reference = _number(reference_cash, "reference_cash", nonnegative=True)
            real_income, inflation = reference - basis, cash - reference
        else:
            real_income, inflation = None, None if indexer == "SELIC" else Decimal(0)
    return {"gross_nominal_cash": _money(cash), "cost_basis_return": _money(basis),
            "economic_interest_nominal": _money(nominal_income), "income_cash": _money(nominal_income),
            "economic_interest_real_base_brl": _money(real_income) if real_income is not None else None,
            "inflation_adjustment": _money(inflation) if inflation is not None else None,
            "real_value_basis": "acquisition_date_purchasing_power_brl" if indexer == "IPCA" else None,
            "net_after_tax_cash": None, "income_basis": "gross_cash_less_allocated_acquisition_cost"}


def lot_cashflows(spec, lot, *, nominal_values=None, horizon=None, calendar=None):
    """Project one acquisition lot, with a complete original-cost reconciliation.

    lot requires identity={kind,maturity}, quantity, cost_basis_total (including
    any supplied acquisition costs), settlement_date, source_url and as_of.
    For indexed lots nominal_values maps exact ISO payment/settlement dates to
    external records described in _nominal; no interpolation/defaults are used.
    Only dates through horizon need future inputs. Remaining basis is not a
    market value. Reinvestment requires an independently specified new lot.
    """
    checked = _checked(spec)
    if checked is None:
        return _unsupported(spec, "reserve_pricing_formula_not_verified" if spec.get("kind") == "Tesouro Reserva"
                            else "unverified_pricing_family")
    if not isinstance(lot, dict) or lot.get("identity") != spec["identity"]:
        raise ValueError("Lot identity must match exact contract kind and maturity")
    expected_isin = spec.get("known_offer", {}).get("isin")
    if expected_isin is not None and lot.get("isin") != expected_isin:
        raise ValueError("Lot ISIN must match the offer ISIN")
    quantity = _number(lot.get("quantity"), "quantity", positive=True)
    cost = _number(lot.get("cost_basis_total"), "cost_basis_total", positive=True)
    settlement = _day(lot.get("settlement_date"), "settlement_date")
    stamp = _timestamp(lot.get("as_of"), "lot as_of")
    source = _source(lot.get("source_url"), "lot source_url")
    lot_basis = lot.get("basis", "observed_acquisition")
    if lot_basis not in {"observed_acquisition", "hypothetical_purchase_at_reference_quote"}:
        raise ValueError("Invalid lot basis")
    if lot_basis == "observed_acquisition" and settlement > stamp.astimezone(BRT).date():
        raise ValueError("A future acquisition cannot be observed")
    calendar = _calendar(calendar)
    all_rows = _schedule(checked, settlement, calendar)
    if not all_rows:
        return _unsupported(spec, "lot_has_no_remaining_payments_at_acquisition")
    maturity_payment = _day(all_rows[-1]["adjusted_payment_date"], "maturity payment")
    end = _day(horizon, "horizon") if horizon is not None else maturity_payment
    if end < settlement:
        raise ValueError("horizon must not precede acquisition settlement")
    indexer = checked["terms"]["indexer"]
    values = nominal_values if nominal_values is not None else {}
    if not isinstance(values, dict):
        raise ValueError("nominal_values must map exact ISO dates to external records")
    acquisition_nominal, acquisition_provenance = Decimal(1000), None
    if indexer:
        record = values.get(settlement.isoformat())
        if record is None:
            return _unsupported(spec, "missing_external_acquisition_" + _unit(checked).lower())
        acquisition_nominal, acquisition_provenance = _nominal(record, settlement, indexer, _unit(checked))
    output, missing = [], []
    with localcontext() as ctx:
        ctx.prec = 40
        remaining_face = sum((Decimal(row["amortized_face_principal"]) / 1000 for row in all_rows), Decimal(0))
        returned, gross_total, income_total = Decimal(0), Decimal(0), Decimal(0)
        for position, row in enumerate(all_rows):
            payment = _day(row["adjusted_payment_date"], "payment date")
            if payment > end:
                break
            nominal, nominal_provenance = Decimal(1000), None
            if indexer:
                record = values.get(payment.isoformat())
                if record is None:
                    missing.append(payment.isoformat())
                    continue
                nominal, nominal_provenance = _nominal(record, payment, indexer, _unit(checked))
            principal_fraction = Decimal(row["amortized_face_principal"]) / 1000
            coupon_fraction = Decimal(row["coupon_interest"]) / 1000
            allocated = cost * principal_fraction / remaining_face
            if position == len(all_rows) - 1:
                allocated = cost - returned
            face = quantity * nominal * principal_fraction
            coupon = quantity * nominal * coupon_fraction
            reference = quantity * acquisition_nominal * (principal_fraction + coupon_fraction)
            cash = face + coupon
            split = lot_economic_cash(cash, allocated, reference_cash=reference, indexer=indexer)
            returned += allocated
            gross_total += cash
            income_total += Decimal(split["income_cash"])
            output.append({**split, "contractual_date": row["contractual_date"],
                           "adjusted_payment_date": payment.isoformat(), "settled": None,
                           "date_status": "calendar_adjusted_projection_not_settlement_evidence",
                           "installment": row["installment"], "quantity": _money(quantity),
                           "amortized_face_principal": _money(face), "coupon_interest": _money(coupon),
                           "remaining_cost_basis": _money(cost - returned),
                           "nominal_value_provenance": nominal_provenance})
        if missing:
            result = _unsupported(spec, "missing_external_payment_index_values")
            result["missing_value_dates"] = missing
            return result
        remaining_cost = cost - returned
    return {"schema_version": 1, "status": "projected", "supported": True,
            "cashflow_basis": "contractual_scenario_not_observed_history",
            "identity": deepcopy(spec["identity"]), "lot_id": lot.get("lot_id"),
            "settlement_date": settlement.isoformat(), "horizon": end.isoformat(),
            "cashflows": output, "original_cost_basis": _money(cost),
            "cost_basis_returned": _money(returned), "remaining_cost_basis": _money(remaining_cost),
            "gross_nominal_cash": _money(gross_total), "income_cash": _money(income_total),
            "allocation_policy": "acquisition_cost_pro_rata_remaining_face_not_tax_or_effective_interest",
            "contract_ended_by_horizon": end >= maturity_payment,
            "post_maturity_income": "0", "reinvestment": "requires_separate_explicit_new_lot",
            "remaining_market_value": None, "net_after_tax_cash": None,
            "provenance": {"contract": deepcopy(spec["provenance"]),
                           "lot": {"source_url": source, "as_of": stamp.isoformat(), "basis": lot_basis,
                                   "independently_authenticated": False},
                           "acquisition_index": acquisition_provenance,
                           "calendar": calendar.provenance},
            "historical_series_created": False, "recommendation_ready": False}


def offer_scenario(offer, valuation_date, *, nominal_value=None, nominal_values=None,
                   quantity="1", horizon=None, calendar=None):
    """Compose a hypothetical current-quote lot, never an executed purchase.

    Closed-market quotes remain reference quotes. No freshness, tradability or
    37-offer completeness is certified by this offline adapter. Caller policy
    must decide whether a timestamp is suitable for its scenario.
    """
    spec = contract_for_offer(offer, valuation_date)
    _source(offer.get("source_url"), "offer source_url")
    stamp = _timestamp(offer.get("as_of"), "offer as_of")
    if stamp.astimezone(BRT).date() > _day(valuation_date, "valuation_date"):
        raise ValueError("Cannot backdate a current-quote purchase before its quote date")
    if not spec["supported"]:
        return _unsupported(spec, "reserve_pricing_formula_not_verified" if spec["kind"] == "Tesouro Reserva"
                            else "unverified_pricing_family")
    expected_basis = _REGISTRY[spec["kind"]][2]
    if offer.get("rate_basis") != expected_basis or offer.get("rate_unit") != "percentage_points_per_year":
        raise ValueError("Offer annual yield basis/unit does not match its family")
    if offer.get("buy_rate") is None:
        return _unsupported(spec, "missing_purchase_yield")
    if offer.get("buy_price") is None:
        return _unsupported(spec, "missing_purchase_price_for_lot_basis")
    calendar = _calendar(calendar)
    pricing = price_contract(spec, valuation_date, offer["buy_rate"], nominal_value, calendar)
    with localcontext() as ctx:
        ctx.prec = 40
        count = _number(quantity, "quantity", positive=True)
        cost = count * _number(offer["buy_price"], "buy_price", positive=True)
    lot = {"identity": spec["identity"], "isin": offer.get("isin"), "quantity": _money(count),
           "cost_basis_total": _money(cost), "settlement_date": _day(valuation_date, "valuation_date").isoformat(),
           "source_url": offer["source_url"], "as_of": offer["as_of"],
           "basis": "hypothetical_purchase_at_reference_quote"}
    values = deepcopy(nominal_values) if nominal_values is not None else {}
    if nominal_value is not None and isinstance(nominal_value, dict):
        key = _day(valuation_date, "valuation_date").isoformat()
        if key in values and values[key] != nominal_value:
            raise ValueError("Pricing and lot acquisition index inputs disagree")
        values[key] = nominal_value
    cash = lot_cashflows(spec, lot, nominal_values=values, horizon=horizon, calendar=calendar)
    return {"schema_version": 1, "status": "scenario" if pricing["supported"] and cash["supported"] else "incomplete",
            "supported": pricing["supported"] and cash["supported"],
            "model_basis": "current_quote_contractual_scenario_not_observed_history",
            "identity": deepcopy(spec["identity"]),
            "offer": {key: deepcopy(offer.get(key)) for key in
                      ("name", "isin", "treasury_bond_code", "selic_code", "buy_price", "buy_rate", "as_of",
                       "source_url", "tradable", "market_status", "offered_for_purchase")},
            "pricing": pricing, "lot_cash": cash, "recommendation_ready": False,
            "historical_series_created": False, "purchase_executed": False}
