"""Offline face-value contracts and nominal agendas; no pricing or activation.

Money and percentages are decimal strings in returned dictionaries. A supplied
indexed nominal value is a reference unit, not an inflation forecast; a callable
can supply a different VNI/VNA for each contractual date. Neither mode infers
inflation, lot earnings, taxes, position eligibility or actual settlement.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from urllib.parse import urlsplit


_DOCUMENTS = "https://www.tesourodireto.com.br/documents/d/guest/"
RENDA_SOURCE = _DOCUMENTS + "tesouro_renda_mais"
EDUCA_SOURCE = _DOCUMENTS + "tesouro_educa_mais"
IPCA_SOURCE = _DOCUMENTS + "tesouro_ipca_juros_semestrais"
SELIC_SOURCE = _DOCUMENTS + "tesouro_selic"
PREFIXADO_SOURCE = _DOCUMENTS + "tesouro_prefixado"
NTNF_SOURCE = ("https://cdn.tesouro.gov.br/sistemas-internos/apex/producao/sistemas/thot/"
               "arquivos/publicacoes/42536_1368112/PAF-PT_2021-2022_26-01-2022%20--%2011h20.pdf")
COUPON_CALENDAR_SOURCE = "https://tesourodireto.com.br/documents/d/guest/dataspag20250818"
RESERVE_UNSUPPORTED_REASON = "reserve_contract_terms_not_verified"

# Family terms only. Purchase yields are deliberately absent from these rules.
_FAMILIES = {
    "Tesouro Selic": ("LFT", "SELIC", 0, "0", SELIC_SOURCE),
    "Tesouro Prefixado": ("LTN", None, 0, "0", PREFIXADO_SOURCE),
    "Tesouro IPCA+": ("NTN-B Principal", "IPCA", 0, "0", IPCA_SOURCE),
    "Tesouro Prefixado com Juros Semestrais": ("NTN-F", None, 0, "10", NTNF_SOURCE),
    "Tesouro IPCA+ com Juros Semestrais": ("NTN-B", "IPCA", 0, "6", IPCA_SOURCE),
    "Tesouro Renda+": ("NTN-B1", "IPCA", 240, "0", RENDA_SOURCE),
    "Tesouro Educa+": ("NTN-B1", "IPCA", 60, "0", EDUCA_SOURCE),
}


def _day(value, field):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError(field + " must be a date or an ISO date string")
    try:
        result = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid " + field) from error
    if result.isoformat() != value:
        raise ValueError(field + " must use YYYY-MM-DD")
    return result


def _decimal(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid " + field)
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid " + field) from error
    if not number.is_finite():
        raise ValueError(field + " must be finite")
    return number


def _url(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid " + field)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(field + " must be public HTTPS without credentials")
    return value


def _shift(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    try:
        return day.replace(year=year, month=month + 1)
    except ValueError as error:
        raise ValueError("Contract dates exceed the supported date range") from error


def contract_from_offer(offer: dict) -> dict:
    """Register a normalized offer by exact kind/maturity, never by name/year.

    Semiannual agendas additionally need first_coupon_date and terms_source_url.
    These are caller-supplied documentary terms, not independently authenticated
    here. Unknown offer keys cannot override family terms or support flags.
    """
    if not isinstance(offer, dict):
        raise ValueError("Offer must be a dictionary")
    kind = offer.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("Missing kind")
    maturity = _day(offer.get("maturity"), "maturity")
    quoted_yield = offer.get("buy_rate")
    if quoted_yield is not None:
        quoted_yield = _decimal(quoted_yield, "buy_rate")
        if quoted_yield <= -100:
            raise ValueError("buy_rate must be greater than -100 percent")
        quoted_yield = format(quoted_yield, "f")
    known_offer = {key: offer.get(key) for key in
                   ("name", "isin", "treasury_bond_code", "selic_code", "as_of", "buy_rate_text")}
    known_offer["buy_rate"] = quoted_yield
    spec = {
        "schema_version": 1, "kind": kind, "maturity": maturity.isoformat(),
        "identity": {"kind": kind, "maturity": maturity.isoformat()},
        "recognized": kind in _FAMILIES or kind == "Tesouro Reserva",
        "supported": kind in _FAMILIES, "unsupported_reason": None,
        "terms": None, "terms_complete": False, "missing_terms": [],
        "known_offer": known_offer,
        "provenance": {"offer_source_url": _url(offer.get("source_url"), "source_url"),
                       "methodology_sources": [], "additional_terms_source_url": None},
        "business_day_adjustment": "requires_external_calendar",
        "income_split_status": "requires_tax_or_lot_cost_basis",
        "earnings_model_status": "pending", "pricing_supported": False,
    }
    if not spec["supported"]:
        spec["unsupported_reason"] = (RESERVE_UNSUPPORTED_REASON if kind == "Tesouro Reserva"
                                      else "unrecognized_kind")
        return spec

    family, indexer, installments, annual_coupon, source = _FAMILIES[kind]
    terms = {
        "family": family, "indexer": indexer,
        "nominal_base_date": "2000-07-15" if indexer == "IPCA" else None,
        "nominal_base_value": "1000" if indexer == "IPCA" else None,
        "fixed_face_value": "1000" if indexer is None else None,
        "installments": installments or 1, "conversion_date": None,
        "coupon_annual_rate_percent": annual_coupon,
        "coupon_months": [], "coupon_day": None, "first_coupon_date": None,
        "regular_amortization_percent": None, "last_amortization_percent": None,
    }
    spec["provenance"]["methodology_sources"] = [source]
    if installments:
        if maturity.day != 15:
            raise ValueError("Amortizing maturity must fall on contractual day 15")
        conversion = _shift(maturity, -(installments - 1))
        if offer.get("conversion_date") is not None and _day(offer["conversion_date"], "conversion_date") != conversion:
            raise ValueError("conversion_date disagrees with maturity and installment count")
        with localcontext() as ctx:
            ctx.prec = 40
            regular = (Decimal(100) / installments).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            last = Decimal(100) - regular * (installments - 1)
        terms.update(conversion_date=conversion.isoformat(),
                     regular_amortization_percent=format(regular, "f"),
                     last_amortization_percent=format(last, "f"))
    elif annual_coupon != "0":
        # This source supports the nominal cycle, not first issuance or holidays.
        spec["provenance"]["methodology_sources"].append(COUPON_CALENDAR_SOURCE)
        months = [1, 7] if family == "NTN-F" else [5, 11] if maturity.month in (5, 11) else [2, 8]
        coupon_day = 1 if family == "NTN-F" else 15
        if maturity.month not in months or maturity.day != coupon_day:
            raise ValueError("Maturity disagrees with the supported coupon calendar")
        terms.update(coupon_months=months, coupon_day=coupon_day)
        first = offer.get("first_coupon_date")
        terms_source = _url(offer.get("terms_source_url"), "terms_source_url")
        if first is None:
            spec["missing_terms"].append("first_coupon_date")
        else:
            first = _day(first, "first_coupon_date")
            if first > maturity or first.month not in months or first.day != coupon_day:
                raise ValueError("Invalid first_coupon_date for this maturity")
            terms["first_coupon_date"] = first.isoformat()
        if terms_source is None:
            spec["missing_terms"].append("terms_source_url")
        spec["provenance"]["additional_terms_source_url"] = terms_source
    spec["terms"] = terms
    spec["terms_complete"] = not spec["missing_terms"]
    return spec


def _checked_spec(spec):
    if not isinstance(spec, dict) or spec.get("supported") is not True:
        raise ValueError("Unsupported contract: " + str(spec.get("unsupported_reason") if isinstance(spec, dict) else None))
    terms = spec.get("terms")
    if not isinstance(terms, dict):
        raise ValueError("Missing contract terms")
    provenance = spec.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Missing contract provenance")
    expected = contract_from_offer({
        "kind": spec.get("kind"), "maturity": spec.get("maturity"),
        "source_url": provenance.get("offer_source_url"),
        "first_coupon_date": terms.get("first_coupon_date"),
        "terms_source_url": provenance.get("additional_terms_source_url"),
    })
    if spec.get("terms_complete") is not True or expected["terms_complete"] is not True:
        raise ValueError("Incomplete contract terms: " + ", ".join(expected["missing_terms"]))
    if spec.get("identity") != expected["identity"] or terms != expected["terms"]:
        raise ValueError("Contract terms disagree with the registered family")
    return expected


def _events(spec, first, last):
    terms, maturity = spec["terms"], _day(spec["maturity"], "maturity")
    if terms["conversion_date"]:
        conversion = _day(terms["conversion_date"], "conversion_date")
        for number in range(terms["installments"]):
            scheduled = _shift(conversion, number)
            if first <= scheduled <= last:
                yield scheduled, number + 1
    elif terms["coupon_months"]:
        inception = _day(terms["first_coupon_date"], "first_coupon_date")
        scheduled = inception
        while scheduled <= min(last, maturity):
            if scheduled >= first:
                yield scheduled, None
            if scheduled == maturity:
                break
            scheduled = _shift(scheduled, 6)
    elif first <= maturity <= last:
        yield maturity, 1


def nominal_cash_schedule(spec, fromdate, todate, nominal_unit_value, *,
                          business_day_adjuster=None, calendar_source=None):
    """Return face flows in an inclusive window of *contractual* dates.

    nominal_unit_value is a positive scalar reference, or callable(date)->value
    for supplied dated VNI/VNA at the adjusted date (nominal if unadjusted).
    None is rejected for indexed contracts; fixed
    nominal contracts can use their official face value of 1000. Amounts are
    decimal strings, without cent rounding or simulated inflation. Amortization
    percentages use the official six-decimal truncation and final remainder.

    Calendar adjustment requires a caller adapter backed by a calendar library
    and calendar_source={library, version, source_url}. Even adjusted dates are
    projections, never settlement evidence. The date window remains nominal.
    """
    spec = _checked_spec(spec)
    first, last = _day(fromdate, "fromdate"), _day(todate, "todate")
    if first > last:
        raise ValueError("fromdate must not be after todate")
    calendar = None
    if business_day_adjuster is not None or calendar_source is not None:
        if not callable(business_day_adjuster) or not isinstance(calendar_source, dict):
            raise ValueError("Calendar adjustment requires a callback and calendar provenance")
        if any(not isinstance(calendar_source.get(key), str) or not calendar_source[key].strip()
               for key in ("library", "version", "source_url")):
            raise ValueError("Calendar provenance requires library, version and source_url")
        calendar = {key: calendar_source[key] for key in ("library", "version", "source_url")}
        calendar["source_url"] = _url(calendar["source_url"], "calendar source_url")
    terms = spec["terms"]
    if nominal_unit_value is None:
        if terms["fixed_face_value"] is None:
            raise ValueError("Missing VNI/VNA nominal_unit_value; unknown is not zero")
        nominal_unit_value = terms["fixed_face_value"]
    provider = nominal_unit_value if callable(nominal_unit_value) else None
    if provider is None:
        _positive_nominal(nominal_unit_value, terms)
    output = []
    with localcontext() as ctx:
        ctx.prec = 40
        annual = Decimal(terms["coupon_annual_rate_percent"]) / 100
        coupon_factor = (1 + annual).sqrt() - 1 if annual else Decimal(0)
        for scheduled, installment in _events(spec, first, last):
            adjusted = None
            if business_day_adjuster is not None:
                adjusted = _day(business_day_adjuster(scheduled), "adjusted_payment_date")
                if adjusted < scheduled:
                    raise ValueError("Calendar callback must not move payment before its contractual date")
            value_date = adjusted or scheduled
            nominal = _positive_nominal(provider(value_date) if provider else nominal_unit_value, terms)
            if terms["conversion_date"]:
                key = "last_amortization_percent" if installment == terms["installments"] else "regular_amortization_percent"
                principal = nominal * Decimal(terms[key]) / 100
            else:
                principal = nominal if scheduled.isoformat() == spec["maturity"] else Decimal(0)
            coupon = nominal * coupon_factor
            output.append({
                "kind": spec["kind"], "maturity": spec["maturity"],
                "contractual_date": scheduled.isoformat(), "installment": installment,
                "adjusted_payment_date": adjusted.isoformat() if adjusted else None,
                "date_status": "externally_adjusted_projection" if adjusted else "contractual_unadjusted",
                "settled": None, "calendar_provenance": dict(calendar) if calendar else None,
                "nominal_unit_value": format(nominal, "f"),
                "nominal_value_date": value_date.isoformat(),
                "nominal_value_basis": "caller_supplied_dated_value" if provider else "constant_reference_unit_not_index_forecast",
                "amortized_face_principal": format(principal, "f"),
                "coupon_interest": format(coupon, "f"),
                "inflation_adjustment": None if terms["indexer"] else "0",
                "gross_nominal_cash": format(principal + coupon, "f"),
                "income_cash": format(coupon, "f"),
                "income_basis": "contractual_coupon_only_excludes_face_principal",
                "income_split_status": "requires_tax_or_lot_cost_basis",
                "earnings_model_status": "pending", "economic_interest": None,
                "cost_basis_return": None, "cashflow_basis": "nominal_contract_projection",
                "position_eligibility_verified": False,
                "provenance": {"methodology_sources": list(spec["provenance"]["methodology_sources"]),
                               "offer_source_url": spec["provenance"]["offer_source_url"],
                               "additional_terms_source_url": spec["provenance"]["additional_terms_source_url"]},
            })
    return output


def _positive_nominal(value, terms):
    nominal = _decimal(value, "nominal_unit_value")
    if nominal <= 0:
        raise ValueError("nominal_unit_value must be positive; missing is not zero")
    if terms["fixed_face_value"] is not None and nominal != Decimal(terms["fixed_face_value"]):
        raise ValueError("Fixed nominal unit face must equal 1000, not a purchase price")
    return nominal
