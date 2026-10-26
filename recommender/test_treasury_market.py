from datetime import datetime, timezone

import pytest

from .treasury_market import CURRENT_URL, instrument_from_offer


def offer(kind='Tesouro Prefixado', **changes):
    return {'kind': kind, 'maturity': '2030-01-01', 'name': kind + ' 2030',
        'buy_price': 800., 'buy_rate': 7., 'rate_basis': 'nominal_annual',
        'rate_unit': 'percentage_points_per_year', 'source_url': CURRENT_URL,
        'as_of': '2026-10-23T18:00:00-03:00', 'market_status': 'closed',
        'offered_for_purchase': True, **changes}


def test_fixed_current_offer_is_not_historical_series_or_execution():
    model = instrument_from_offer(offer())
    assert model.asset.ticker == 'TD-PRE-20300101'
    assert model.asset.current_quote.purchase_available
    assert model.asset.current_quote.market_status == 'closed'
    assert model.asset.price == 800
    assert model.pricing['execution_price'] is None
    assert model.nominal is None and model.duration_years > 0


def test_indexed_nominal_anchor_is_derived_not_observed():
    model = instrument_from_offer(offer('Tesouro IPCA+', maturity='2035-05-15',
        rate_basis='real_above_ipca', buy_price=3000.))
    assert model.nominal['basis'] == 'derived_from_price_yield'
    assert not model.nominal['observed_index_value']
    assert float(model.pricing['unit_price']) == pytest.approx(3000.)


def test_reserva_uses_explicit_selic_rule_not_fabricated_spread():
    model = instrument_from_offer(offer('Tesouro Reserva', maturity='2036-01-01',
        buy_rate=None, buy_rate_text='SELIC', isin='BRSTNCT1D008', catalogue_group='Tesouro24x7'))
    assert model.asset.asset_class == 'liquidez' and model.asset.liquidity_days == 0
    assert model.pricing['spread_over_selic'] is None
    assert model.pricing['institution_restriction'] == 'Banco do Brasil'
    assert model.pricing['periodic_coupon'] is False


@pytest.mark.parametrize('changes', [{'source_url': 'https://other.test'}, {'buy_price': None},
    {'buy_rate': None}, {'rate_basis': 'real_above_ipca'}, {'maturity': '2025-01-01'}])
def test_unknown_or_wrong_offer_inputs_fail_closed(changes):
    with pytest.raises(ValueError):
        instrument_from_offer(offer(**changes))


def test_unknown_reserva_identity_does_not_share_the_verified_product_rule():
    with pytest.raises(ValueError, match='security'):
        instrument_from_offer(offer('Tesouro Reserva', buy_rate_text='SELIC',
            isin='other', catalogue_group='Tesouro24x7'))
