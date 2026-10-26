"""Current Treasury identities and forward contracts, never fabricated history.

Quoted indexed prices/yields imply a scenario reference VNA/VNI; that derived
reference is not authenticated as an observed official index value. Historical
models remain separate. The Reserva return rule is 100% Selic, without MTM;
its quoted unit value is the starting capital unit, not an inferred LFT face.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path

from .contracts import Asset
from .treasury_current import BRT, CURRENT_URL, parse_official_json
from .treasury_valuation import (BrazilCalendar, contract_for_offer, price_contract,
    pricing_family_registry, quote_implied_nominal_value)

RESERVA_SOURCE = 'https://www.tesourodireto.com.br/tesouro-reserva'
RESERVA_RELEASE = ('https://www.gov.br/tesouronacional/pt-br/noticias/'
    'tesouro-nacional-b3-e-bb-lancam-tesouro-reserva-com-operacao-24h-nos-7-dias-da-semana')
KINDS = {'Tesouro Selic': 'SELIC', 'Tesouro Prefixado': 'PRE', 'Tesouro IPCA+': 'IPCA',
    'Tesouro Prefixado com Juros Semestrais': 'PRE-CUPOM',
    'Tesouro IPCA+ com Juros Semestrais': 'IPCA-CUPOM',
    'Tesouro Renda+': 'RENDA', 'Tesouro Educa+': 'EDUCA', 'Tesouro Reserva': 'RESERVA'}


@dataclass
class TreasuryInstrument:
    asset: Asset
    offer: dict
    spec: dict | None
    settlement: date
    nominal: dict | None
    pricing: dict
    duration_years: float
    calendar: BrazilCalendar


def instrument_from_offer(offer, *, calendar=None):
    calendar = calendar or BrazilCalendar()
    if not isinstance(offer, dict) or offer.get('source_url') != CURRENT_URL:
        raise ValueError('current_treasury_requires_official_offer')
    kind, maturity = offer['kind'], date.fromisoformat(offer['maturity'])
    if kind not in KINDS:
        raise ValueError('unrecognized_treasury_kind')
    stamp = datetime.fromisoformat(offer['as_of'])
    if stamp.tzinfo is None:
        raise ValueError('treasury_quote_requires_timezone')
    quote_day = stamp.astimezone(BRT).date()
    settlement = calendar.following(quote_day)
    if maturity <= settlement:
        raise ValueError('treasury_offer_matured')
    price = offer.get('buy_price')
    if type(price) not in (int, float) or not 0 < price < 1_000_000_000:
        raise ValueError('treasury_purchase_quote_unavailable')
    nominal, spec = None, None
    if kind == 'Tesouro Reserva':
        if offer.get('buy_rate_text') != 'SELIC' or offer.get('catalogue_group') != 'Tesouro24x7':
            raise ValueError('reserve_official_return_rule_unresolved')
        if offer.get('isin') != 'BRSTNCT1D008':
            raise ValueError('reserve_exact_security_not_verified')
        pricing = {'status': 'quoted_indexed_account_unit', 'unit_price': str(price),
            'price_basis': 'observed_current_unit_value_no_mark_to_market',
            'return_basis': '100_percent_effective_selic_no_mtm',
            'spread_over_selic': None, 'hypothetical_spread_inferred': False,
            'methodology_sources': [RESERVA_SOURCE, RESERVA_RELEASE],
            'institution_restriction': 'Banco do Brasil', 'cashflows': [],
            'quoted_maturity': maturity.isoformat(), 'periodic_coupon': False}
        duration = 0.
    else:
        spec = contract_for_offer(offer, settlement)
        if not spec['supported'] or not spec['terms_complete']:
            raise ValueError('treasury_contract_terms_incomplete')
        family = pricing_family_registry()[kind]
        if offer.get('rate_basis') != family['yield_basis'] or offer.get('rate_unit') != 'percentage_points_per_year':
            raise ValueError('treasury_purchase_yield_basis_mismatch')
        rate = offer.get('buy_rate')
        if type(rate) not in (int, float) or not -100 < rate < 1000:
            raise ValueError('treasury_purchase_yield_unavailable')
        if spec['terms']['indexer']:
            nominal = quote_implied_nominal_value(spec, settlement, rate, price,
                source_url=offer['source_url'], as_of=offer['as_of'], calendar=calendar)
        pricing = price_contract(spec, settlement, rate, nominal, calendar)
        total = Decimal(pricing['unit_price'])
        if total <= 0:
            raise ValueError('treasury_theoretical_price_invalid')
        duration = float(sum(Decimal(row['present_value']) * Decimal(row['business_days']) / 252
            for row in pricing['cashflows']) / total / (1 + Decimal(str(rate)) / 100))
    ticker = 'TD-' + KINDS[kind] + '-' + maturity.isoformat().replace('-', '')
    liquid = kind in ('Tesouro Selic', 'Tesouro Reserva')
    source = ('Oferta atual oficial do Tesouro; cenarios contratuais prospectivos, sem criar historico '
        'do titulo. Referencia nominal de titulos indexados derivada explicitamente do preco e taxa '
        'cotados; nao e VNA/VNI oficial observado. Juros e devolucao de custo de aquisicao separados.')
    quote = {'price': price, 'as_of': offer['as_of'], 'source_url': offer['source_url'],
        'purchase_available': offer.get('offered_for_purchase') is True,
        'basis': 'official_current_purchase_catalogue_quote_not_executed_order',
        'market_status': offer.get('market_status') or 'unknown',
        'minimum_fraction': None if kind == 'Tesouro Reserva' else .01,
        'minimum_investment': offer.get('minimum_investment')}
    asset = Asset(ticker=ticker, name=offer['name'][:100], issuer_id='TESOURO-NACIONAL',
        asset_class='liquidez' if liquid else 'renda_fixa', risk_level=0 if liquid else 1,
        liquidity_days=0 if kind == 'Tesouro Reserva' else 1,
        minimum_years=0 if liquid else 3, complex=False, price=price,
        annual_cash_yield=0, current_quote=quote, as_of=quote_day.isoformat(), source=source)
    return TreasuryInstrument(asset, offer, spec, settlement, nominal, pricing, duration, calendar)


def read_current_instruments(folder, *, now=None):
    folder = Path(folder).resolve()
    source = folder / 'raw/current/official-investir.json'
    if source.is_symlink() or not source.resolve().is_relative_to(folder) or source.stat().st_size > 4_000_000:
        raise ValueError('unsafe_current_treasury_source')
    body = source.read_bytes()
    result = parse_official_json(body, now=now)
    if not result.get('catalogue_complete') or result.get('failures'):
        raise ValueError('incomplete_current_treasury_catalogue')
    calendar = BrazilCalendar()
    instruments = [instrument_from_offer(offer, calendar=calendar) for offer in result['instruments']]
    identities = [(row.offer['kind'], row.offer['maturity']) for row in instruments]
    if len(identities) != len(set(identities)) or not instruments:
        raise ValueError('duplicate_current_treasury_identity')
    receipt = {'file': source.relative_to(folder).as_posix(), 'url': CURRENT_URL,
        'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
        'retrieved_at': datetime.fromtimestamp(source.stat().st_mtime, timezone.utc).isoformat(),
        'authority': 'primary', 'provider': 'Tesouro Nacional', 'role': 'current_purchase_catalogue'}
    return instruments, receipt
