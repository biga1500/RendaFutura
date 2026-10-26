"""Offline yield evidence, synchronization, projection bounds and local-cache smoke."""

from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pytest

from . import treasury_yield_factors as module
from .macro_factors import factor_scenarios
from .simulation import block_indices


DATES = ['2026-07-31', '2026-08-31', '2026-09-30']
HEADER = ['Tipo Titulo', 'Data Vencimento', 'Data Base', 'Taxa Compra Manha',
          'Taxa Venda Manha', 'PU Compra Manha', 'PU Venda Manha', 'PU Base Manha']


def row(kind, day, rate, maturity='01/01/2035', buy='800,00'):
    return [kind, maturity, day, rate, '99,00', buy, '790,00', '795,00']


def source_rows():
    output = []
    for number, day in enumerate(('30/06/2026', '31/07/2026', '31/08/2026', '30/09/2026')):
        output.extend([
            row('Tesouro Prefixado', day, str(10 + number) + ',00'),
            row('Tesouro Prefixado com Juros Semestrais', day, str(12 + number) + ',00'),
            row('Tesouro IPCA+', day, str(5 + number) + ',00'),
            row('Tesouro IPCA+ com Juros Semestrais', day, str(7 + number) + ',00'),
            row('Tesouro Selic', day, f'0,{10 + number:02d}')])
    return output


def write_source(folder, rows=None, *, bundle_receipt=False):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(HEADER)
    writer.writerows(source_rows() if rows is None else rows)
    body = stream.getvalue().encode('utf-8')
    path = folder / 'raw/tesouro-precos.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    receipt = (folder / 'raw/official-treasury-bundle/price-history.csv.source.json'
               if bundle_receipt else path.with_name(path.name + '.meta.json'))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({'url': module.PRICE_URL, 'sha256': hashlib.sha256(body).hexdigest(),
        'bytes': len(body), 'retrieved_at': '2026-10-24T23:57:30+00:00',
        'origin': 'offline_test_fixture'}), encoding='utf-8')
    return path, receipt


@pytest.fixture
def local_history(tmp_path, monkeypatch):
    macro = {'dates': ['2026-06-30', *DATES], 'inflation': [.004, -.003, .002, .007],
             'selic': [.01, .011, .012, .013], 'sources': [{'role': 'macro_fixture'}]}
    monkeypatch.setattr(module, 'load_macro_factors', lambda _: (deepcopy(macro), 'macro-fixture-sha'))
    write_source(tmp_path)
    return tmp_path, macro


def test_observed_medians_units_dates_prior_and_macro_alignment(local_history):
    folder, _ = local_history
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['dates'] == DATES
    assert history['prior_yield_date'] == '2026-06-30'
    assert history['prior_levels'] == pytest.approx({'nominal_yield': .11, 'real_yield': .06, 'selic_spread': .001})
    np.testing.assert_allclose(history['nominal_yield'], [.12, .13, .14])
    np.testing.assert_allclose(history['real_yield'], [.07, .08, .09])
    np.testing.assert_allclose(history['selic_spread'], [.0011, .0012, .0013])
    np.testing.assert_allclose(history['nominal_yield_change'], [.01, .01, .01])
    assert history['inflation'] == [-.003, .002, .007]
    assert history['selic'] == [.011, .012, .013]
    assert history['monthly_aggregates'][-1]['families']['nominal_yield']['instrument_count'] == 2
    assert history['sources'][0]['hash_verified_against_cached_receipt']
    assert len(history['revision']) == 64
    assert not history['constant_tenor_curve']
    assert not history['missing_months_imputed']
    assert not history['bond_price_history_created']
    json.dumps(history, allow_nan=False)


def test_bundle_receipt_authenticates_identical_original_csv(local_history):
    folder, _ = local_history
    (folder / 'raw/tesouro-precos.csv.meta.json').unlink()
    path, receipt = write_source(folder, bundle_receipt=True)
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['sources'][0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert history['sources'][0]['receipt_file'] == receipt.relative_to(folder).as_posix()


def test_last_common_observed_day_not_different_family_dates(local_history):
    folder, _ = local_history
    rows = [item for item in source_rows() if item[2] != '30/09/2026']
    rows += [row(kind, '29/09/2026', rate) for kind, rate in
             [('Tesouro Prefixado', '12,00'), ('Tesouro IPCA+', '6,00'), ('Tesouro Selic', '0,10')]]
    rows += [row('Tesouro Prefixado', '30/09/2026', '50,00')]
    write_source(folder, rows)
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['nominal_yield'][-1] == .12
    assert history['monthly_aggregates'][-1]['observation_date'] == '2026-09-29'
    assert history['monthly_aggregates'][-1]['lag_calendar_days'] == 1


def test_unavailable_buy_quotes_other_indexers_and_future_rows_do_not_pollute_levels(local_history):
    folder, _ = local_history
    rows = source_rows() + [row('Tesouro Prefixado', '30/09/2026', '90,00', '01/01/2040', buy='0,00'),
        row('Tesouro IPCA+', '30/09/2026', '', '15/08/2040'),
        row('Tesouro Prefixado', '30/09/2026', '12,00', '30/09/2026'),
        row('Tesouro Prefixado', '30/09/2026', '1001,00', '01/01/2042'),
        row('Tesouro Renda+ Aposentadoria Extra', '30/09/2026', '90,00'),
        row('Tesouro Educa+', '30/09/2026', '90,00'),
        row('Tesouro IGPM+ com Juros Semestrais', '30/09/2026', '90,00'),
        row('Tesouro Prefixado', '15/10/2026', '90,00')]
    write_source(folder, rows)
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['nominal_yield'][-1] == pytest.approx(.14)
    assert history['real_yield'][-1] == pytest.approx(.09)
    assert history['excluded_from_family_aggregates'] == {'other_kind': 3,
        'missing_purchase_quote_or_yield': 2, 'invalid_annual_yield': 1, 'matured_on_observation': 1}
    assert history['source_diagnostics']['rows_after_cutoff'] == 1


@pytest.mark.parametrize('changes', [{'sha256': '0' * 64}, {'url': 'https://untrusted.invalid/file.csv'},
                                   {'bytes': 1}])
def test_integrity_and_official_source_identity_fail_closed(local_history, changes):
    folder, _ = local_history
    receipt = folder / 'raw/tesouro-precos.csv.meta.json'
    metadata = json.loads(receipt.read_text())
    receipt.write_text(json.dumps({**metadata, **changes}))
    with pytest.raises(ValueError, match='integrity_or_identity'):
        module.load_treasury_yield_factors(folder, DATES)


@pytest.mark.parametrize('deleted', ['raw/tesouro-precos.csv', 'raw/tesouro-precos.csv.meta.json'])
def test_missing_source_or_receipt_does_not_trigger_download(local_history, deleted, monkeypatch):
    folder, _ = local_history
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('network request'))
    (folder / deleted).unlink()
    with pytest.raises(ValueError, match='missing_or_oversized'):
        module.load_treasury_yield_factors(folder, DATES)


@pytest.mark.parametrize('dates', [[], ['2026-09-15'], ['2026-09-30', '2026-08-31'],
    ['2026-07-31', '2026-09-30'], ['2026-09-30', '2026-09-30'], ['2099-12-31'], ['bad']])
def test_nonconsecutive_partial_future_or_invalid_dates_are_rejected(local_history, dates):
    with pytest.raises(ValueError):
        module.load_treasury_yield_factors(local_history[0], dates)


@pytest.mark.parametrize('remove_day', ['30/06/2026', '31/08/2026'])
def test_missing_month_or_prior_anchor_not_imputed(local_history, remove_day):
    folder, _ = local_history
    write_source(folder, [item for item in source_rows() if item[2] != remove_day])
    with pytest.raises(ValueError, match='no_imputation'):
        module.load_treasury_yield_factors(folder, DATES)


def test_one_missing_family_does_not_reuse_previous_rate(local_history):
    folder, _ = local_history
    write_source(folder, [item for item in source_rows()
                          if not (item[0] == 'Tesouro Selic' and item[2] == '31/08/2026')])
    with pytest.raises(ValueError, match='no_imputation:2026-08'):
        module.load_treasury_yield_factors(folder, DATES)


def test_identical_duplicates_are_not_extra_members_but_conflicting_duplicates_fail(local_history):
    folder, _ = local_history
    rows = source_rows()
    write_source(folder, [*rows, rows[-5]])
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['monthly_aggregates'][-1]['families']['nominal_yield']['instrument_count'] == 2
    altered = rows[-5].copy()
    altered[3] = '44,00'
    write_source(folder, [*rows, altered])
    with pytest.raises(ValueError, match='conflicting_price_identity_and_date'):
        module.load_treasury_yield_factors(folder, DATES)


def test_unrecognized_csv_schema_is_not_read_positionally(local_history):
    folder, _ = local_history
    path, receipt = write_source(folder)
    content = path.read_bytes().replace(b'Taxa Compra Manha', b'Unknown new rate')
    path.write_bytes(content)
    metadata = json.loads(receipt.read_text())
    metadata.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
    receipt.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='schema_changed'):
        module.load_treasury_yield_factors(folder, DATES)


def test_stale_monthly_quote_does_not_silently_stand_for_month_end(local_history):
    folder, _ = local_history
    write_source(folder, [[*item[:2], '15/09/2026', *item[3:]] if item[2] == '30/09/2026' else item
                          for item in source_rows()])
    with pytest.raises(ValueError, match='stale_observed'):
        module.load_treasury_yield_factors(folder, DATES)


def test_missing_macro_month_fails_not_sliced_positionally(local_history):
    folder, macro = local_history
    macro['dates'].pop(2)
    with pytest.raises(ValueError, match='consecutive'):
        module.load_treasury_yield_factors(folder, DATES)


def test_missing_terminal_macro_month_is_not_forward_filled(local_history):
    folder, macro = local_history
    macro['dates'].pop()
    with pytest.raises(ValueError, match='macro_month_no_imputation'):
        module.load_treasury_yield_factors(folder, DATES)


def test_renda_educa_proxy_and_reserva_no_mtm_are_explicit(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    policies = history['instrument_factor_policy']
    for kind in ('Tesouro Renda+', 'Tesouro Educa+'):
        assert policies[kind]['yield_factor'] == 'real_yield'
        assert policies[kind]['proxy_source_family'] == 'IPCA'
        assert policies[kind]['family_factor_proxy']
        assert not policies[kind]['observed_instrument_history']
    assert policies['Tesouro Reserva']['yield_factor'] is None
    assert policies['Tesouro Reserva']['accrual_factor'] == 'selic'
    assert not policies['Tesouro Reserva']['mark_to_market']
    assert not policies['Tesouro Reserva']['spread_inferred']


def test_joint_scenarios_reuse_equity_blocks_and_original_macro_pairs(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    draws = block_indices(3, 13, 5, 2, 42)
    before = draws.copy()
    scenarios = module.yield_factor_scenarios(history, 13, 5, 2, 99, indices=draws, index_dates=DATES)
    macro = factor_scenarios(history, 13, 5, 2, 42)
    np.testing.assert_array_equal(scenarios['indices'], draws)
    np.testing.assert_array_equal(scenarios['inflation'], macro['inflation'])
    np.testing.assert_array_equal(scenarios['selic'], macro['selic'])
    np.testing.assert_array_equal(draws, before)
    for key in module.YIELD_KEYS:
        np.testing.assert_array_equal(scenarios['sampled_changes'][key],
                                      np.asarray(history[key + '_change'])[draws])
        assert scenarios[key].shape == (5, 13)
    assert not scenarios['future_values_observed']
    assert not scenarios['bond_price_history_created']
    assert not scenarios['maturity_curve_extrapolation_performed']


def test_generated_indices_reproducible_and_projection_capped_not_unbounded_random_walk(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    a, b = [module.yield_factor_scenarios(history, 120, 50, 2, 5) for _ in range(2)]
    np.testing.assert_array_equal(a['indices'], b['indices'])
    for key in module.YIELD_KEYS:
        np.testing.assert_array_equal(a[key], b[key])
        assert np.all(a[key] <= max(history[key]))
        assert np.all(a[key] >= history['prior_levels'][key])
        assert a['bound_hit_counts'][key] > 0
        np.testing.assert_allclose(a['applied_changes'][key], 0, atol=1e-15)
        assert np.all(a['sampled_changes'][key] > 0)
    assert not a['bounds_are_financial_guarantees']
    assert not a['drift_added']


def test_full_length_blocks_and_truncated_final_block_are_supported(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    draws = block_indices(3, 8, 2, 3, 42)
    result = module.yield_factor_scenarios(history, 8, 2, 3, 42, indices=draws, index_dates=DATES)
    np.testing.assert_array_equal(result['indices'], draws)


def test_future_factor_policies_do_not_mutate_loader_constants(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    result = module.yield_factor_scenarios(history, 2, 1, 1, 42)
    result['instrument_factor_policy']['Tesouro Reserva']['mark_to_market'] = True
    assert not history['instrument_factor_policy']['Tesouro Reserva']['mark_to_market']
    history['instrument_factor_policy']['Tesouro Reserva']['mark_to_market'] = True
    assert not module.INSTRUMENT_FACTOR_POLICY['Tesouro Reserva']['mark_to_market']


def test_negative_spreads_deflation_and_downward_yield_shocks_are_preserved(local_history):
    folder, _ = local_history
    rows = source_rows()
    for item in rows:
        if item[2] == '30/09/2026':
            item[3] = '-0,20' if item[0] == 'Tesouro Selic' else '1,00'
    write_source(folder, rows)
    history = module.load_treasury_yield_factors(folder, DATES)
    assert history['selic_spread'][-1] == -.002
    draws = np.array([[0, 1, 2, 0, 1, 2]])
    result = module.yield_factor_scenarios(history, 6, 1, 1, 0, indices=draws, index_dates=DATES)
    np.testing.assert_array_equal(result['inflation'], [[-.003, .002, .007, -.003, .002, .007]])
    assert result['nominal_yield'][0, 0] == pytest.approx(.02)
    assert result['nominal_yield'][0, 1] == pytest.approx(.03)
    assert result['nominal_yield'][0, 2] == pytest.approx(.01)
    assert result['selic_spread'][0, 2] == pytest.approx(-.002)
    assert result['applied_changes']['nominal_yield'][0, 2] < 0


@pytest.mark.parametrize('indices, dates, error', [
    (np.zeros((1, 2), dtype=float), DATES, 'indices'),
    (np.zeros((1, 2), dtype=bool), DATES, 'indices'),
    (np.array([[0, 3]]), DATES, 'indices'),
    (np.array([[-1, 0]]), DATES, 'indices'),
    (np.array([[0]]), DATES, 'indices'),
    (np.array([[0, 1]]), None, 'dates_mismatch'),
    (np.array([[0, 1]]), ['2026-06-30', '2026-07-31', '2026-08-31'], 'dates_mismatch'),
    (np.array([[1, 0]]), DATES, 'not_blocks')])
def test_external_indices_require_exact_dates_shape_integers_bounds_and_block_structure(
        local_history, indices, dates, error):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    with pytest.raises(ValueError, match=error):
        module.yield_factor_scenarios(history, 2, 1, 2, 42, indices=indices, index_dates=dates)


@pytest.mark.parametrize('options', [{'months': 0}, {'months': 1225}, {'count': 0}, {'count': 2001},
    {'block': 0}, {'block': 4}, {'block': True}, {'months': True}, {'count': 1.5}])
def test_scenario_resource_limits(local_history, options):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    with pytest.raises(ValueError, match='scenario_limit'):
        module.yield_factor_scenarios(history, **{'months': 2, 'count': 1, 'block': 1, 'seed': 42, **options})


def test_payment_date_padding_does_not_expand_the_permitted_planning_horizon(local_history):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    result = module.yield_factor_scenarios(history, 1224, 1, 1, 42, padding_months=2)
    assert result['inflation'].shape == (1, 1226)
    with pytest.raises(ValueError, match='scenario_limit'):
        module.yield_factor_scenarios(history, 1225, 1, 1, 42, padding_months=1)
    with pytest.raises(ValueError, match='scenario_limit'):
        module.yield_factor_scenarios(history, 12, 1, 1, 42, padding_months=3)


@pytest.mark.parametrize('key, value', [('inflation', [0, 0, float('nan')]),
    ('selic', [0, 0, -.01]), ('nominal_yield', [0, 0, -1]),
    ('real_yield_change', [1, 2, 3]), ('real_yield_change', ['bad', None, {}]),
    ('yield_unit', 'percent'), ('policy', 'other'), ('prior_levels', None)])
def test_invalid_factor_history_cannot_enter_projection(local_history, key, value):
    history = module.load_treasury_yield_factors(local_history[0], DATES)
    history[key] = value
    with pytest.raises(ValueError):
        module.yield_factor_scenarios(history, 2, 1, 1, 42)


def test_loader_does_not_write_or_change_active_pointer(local_history):
    folder, _ = local_history
    pointer = folder / 'current.json'
    pointer.write_bytes(b'{"snapshot":"unchanged"}')
    before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    module.load_treasury_yield_factors(folder, DATES)
    after = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    assert after == before


def test_actual_official_cache_sixty_months_when_available():
    folder = Path(__file__).parent / 'data/market'
    required = ['raw/tesouro-precos.csv', 'raw/official-treasury-bundle/price-history.csv.source.json',
                'raw/primary-research/macro/current.json']
    if not all((folder / name).is_file() for name in required):
        pytest.skip('Optional downloaded primary-source caches are not distributed with code.')
    dates = [module.month_end(number // 12, number % 12 + 1).isoformat()
             for number in range(2021 * 12 + 9, 2026 * 12 + 9)]
    history = module.load_treasury_yield_factors(folder, dates)
    assert len(history['dates']) == 60
    assert history['dates'][0] == '2021-10-31'
    assert history['dates'][-1] == '2026-09-30'
    assert history['sources'][0]['authority'] == 'primary'
    assert all(item['lag_calendar_days'] <= 7 for item in history['monthly_aggregates'])
    assert all(np.isfinite(history[key]).all() for key in (*module.YIELD_KEYS, 'inflation', 'selic'))
    result = module.yield_factor_scenarios(history, 720, 5, 3, 42)
    assert result['real_yield'].shape == (5, 720)
