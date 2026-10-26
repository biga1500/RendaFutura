"""Exact Dexxos price-regime evidence, without rewriting any quote feed.

The original Yahoo series stays whole. Literal public-provider observations
extend the cached exchange anchors; no favorable dates are selected. DEXP4's
non-month-end conflict intentionally blocks the same ordinary price gate.
"""

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import re
import socket
from unittest.mock import patch
import zipfile

from lxml import html
from pypdf import PdfReader

from . import round2_share_actions as primary
from .collect_market_data import month_end, shift_month
from .share_basis import b3_evidence, factor_after, reconcile_prices


ROOT, CUTOFF, START = primary.ROOT, primary.CUTOFF, primary.START
TARGETS = ('DEXP3', 'DEXP4')
PROVIDER = 'original_exchange_and_literal_ibovx_daily_observations'
PINS = {
    'DEXP3': ('85d819fc0f1c17f99a938fbe8477017277b826e93647ed5d25a10dabeac9799e',
             '9989b48f3ad3e5550d6cc9c0553625ff991f0e4c6412ea8246cf85110c721f0f',
             'be2bfbe0bf2be49af55260b84a337759a511be8384d60a057cffd0f96150e313'),
    'DEXP4': ('c867d8e4d5a4d126b8fb2970c7e04208029a79c859bd0549c3a1d0e1cc7110ba',
             'e6d8d707e8899c089bda84971c095cce13bc7f8f71a187d286f9b147c3d34675',
             '87cf5927dd440a8f47a8943aa72752270c163741adbd5e4ee044025de215484c'),
    'release': ('bbb12a4aaa3bb1f120ddf26ccb47f75018c48b2b1b108b90318d87a719d04ad6',
                'fe54218ee02056741565be0e91a10a29781116a25d9df9bb3c8c39d9735daaf7',
                '1c2a0d482de11e77a0a4d9e7ab7a79a8cd42f3a58d1ae1dd0fa0f0d27f2c14e9'),
    'third': ('4d4de31f82e05ef605796c671da0601fe46f6d47b49062dfd8f8d951384ac9ef',
              'e281f507cad9a357f723472dacaa1ad496c84f616aed9f30eb0b6bddfb0f9fe0',
              '01b1a568402ecfeecbb3ef1b3e46ec1f7725176feb72eba28250bff4f4c5ce56'),
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def source(root, key):
    digest, request, receipt_digest = PINS[key]
    relative = f'{primary.CACHE}/response-{request}.json'
    receipt = json.loads(primary.read(root, relative, receipt_digest))
    require(receipt['sha256'] == digest, 'dexp_pinned_native_body_changed')
    ref = {k: receipt[k] for k in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}
    ref.update(receipt_file=relative, receipt_sha256=receipt_digest,
               authority='issuer_CVM_release' if key == 'release' else 'public_quote_provider_not_issuer')
    body = primary.checked_source(root, ref)
    return body, ref, receipt


def br_decimal(text):
    require(isinstance(text, str) and bool(re.fullmatch(r'\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}', text)),
            'dexp_invalid_literal_BR_price')
    value = Decimal(text.replace('.', '').replace(',', '.'))
    require(value.is_finite() and value > 0, 'dexp_nonpositive_literal_quote')
    return value


def parse_ibovx(body, ticker):
    """All 134 published sessions, including later observations kept in audit."""
    tree = html.fromstring(body.decode('utf-8'))
    title = ' '.join(tree.xpath('//title/text()'))
    require('ibovX - ' + ticker + '.' in title, 'dexp_native_page_security_mismatch')
    require(tree.xpath('//input[@value=$ticker]', ticker=ticker), 'dexp_native_form_security_mismatch')
    candidates = [t for t in tree.xpath('//table') if t.xpath('.//tr')
                  and [c.text_content().strip() for c in t.xpath('.//tr')[0].xpath('./td|./th')]
                  == ['Data', 'Varia\u00e7\u00e3o %', 'Varia\u00e7\u00e3o', 'Cota\u00e7\u00e3o',
                      'Abertura', 'M\u00ednimo', 'M\u00e1ximo', 'Volume', 'N\u00ba Neg\u00f3cios']]
    require(len(candidates) == 1, 'dexp_native_daily_table_schema_changed')
    table, = candidates
    result, previous = [], None
    for index, row in enumerate(table.xpath('.//tr')[1:], 1):
        cells = [' '.join(c.text_content().split()) for c in row.xpath('./td')]
        require(len(cells) == 9, 'dexp_native_daily_row_schema_changed')
        day = datetime.strptime(cells[0], '%d/%m/%Y').date()
        require(day.weekday() < 5 and (previous is None or day < previous),
                'dexp_duplicate_or_unordered_native_session')
        close, opened, low, high = (br_decimal(cells[i]) for i in (3, 4, 5, 6))
        require(low <= close <= high and low <= opened <= high, 'dexp_native_OHLC_inconsistent')
        require(cells[8].replace('.', '').isdigit() and int(cells[8].replace('.', '')) > 0,
                'dexp_native_inactive_or_unknown_session')
        result.append({'date': day.isoformat(), 'close_decimal': str(close),
            'literal_cells': cells, 'native_table_row': index,
            'xpath': tree.getroottree().getpath(row),
            'date_role': 'actual_reported_trading_session',
            'value_role': 'Cotacao_daily_close_not_Abertura_or_media'})
        previous = day
    require(len(result) == 134, 'dexp_native_pinned_session_count_changed')
    return result


def issuer_release(root):
    body, ref, receipt = source(root, 'release')
    discovery = receipt['discovery']
    index = discovery['index_source']
    with zipfile.ZipFile(io.BytesIO(primary.checked_source(root, index))) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read(discovery['member']).decode('latin1')),
                                   delimiter=';'))
    row = rows[discovery['csv_line'] - 2]
    require(row == discovery['index_row'] and row['Codigo_CVM'] == '16632'
            and row['CNPJ_Companhia'] == '02.193.750/0001-52'
            and row['Data_Referencia'] == '2026-06-30' and row['Data_Entrega'] == '2026-08-11'
            and row['Versao'] == '1' and row['Link_Download'] == ref['url'],
            'dexp_primary_release_index_binding_changed')
    version_key = ('CNPJ_Companhia', 'Data_Referencia', 'Categoria', 'Tipo', 'Especie', 'Assunto')
    require(max(int(r['Versao']) for r in rows
                if all(r[k] == row[k] for k in version_key)) == 1,
            'dexp_primary_release_not_final_index_version')
    text = PdfReader(io.BytesIO(body)).pages[10].extract_text()
    normalized = primary.normalized(text)
    fragments = ('acoes ordinarias da Dexxos Participacoes encerraram o pregao de 30 de junho de 2026',
                 'cotacao de R$ 7,11 por acao',
                 'Fonte: Infomoney e Investing.com.',
                 'Valores historicos ajustados para refletir os eventos de bonificacao de acoes e dividendos distribuidos.')
    require(all(primary.normalized(s) in normalized for s in fragments),
            'dexp_primary_release_literal_price_or_adjustment_note_changed')
    return {'source': ref, 'index_source': index, 'index_member': discovery['member'],
        'csv_line': discovery['csv_line'], 'native_index_row': row, 'page': 11,
        'published_at': row['Data_Entrega'], 'session_date': '2026-06-30',
        'share_class': 'ON', 'ticker': 'DEXP3', 'currency': 'BRL', 'literal_close': '7,11',
        'literal_fragments': fragments,
        'primary_report_is_not_a_raw_vendor_history': True,
        'historical_comparative_prices_rejected_as_raw': True,
        'adjustment_note_retained': True,
        'upstream_names_reported_by_issuer': ['Infomoney', 'Investing.com'],
        'used_as_extra_independent_gate_anchor': False,
        'role': 'issuer_published_current_quarter_close_corroboration_with_explicit_limits'}


def original_context(root, identity):
    from .inventory_candidates import _Cache, _identity
    from .market_inventory import load_inventory
    root = Path(root).resolve()
    inventory = load_inventory(root)
    row, = [r for r in inventory['stocks'] if r['ticker'] == identity['ticker']]
    require(all(row.get(k) == identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')),
            'dexp_inventory_exact_security_changed')
    cache = _Cache(root, inventory)
    _identity(row, cache, CUTOFF)
    spec = primary.packet(root)['targets'][identity['ticker']]
    original = sum([json.loads(primary.checked_source(root, ref))['results'] for ref in spec['cash']], [])
    binding = cache.exact_share_class_bindings.get(identity['ticker'])
    if binding is not None:
        from .preferred_class_binding import cash_rows
        original = cash_rows(original, binding)
    _, quotes = b3_evidence(original, identity['share_class'])
    return quotes, binding, spec, primary.checked_source(root, spec['comparison'])


def third_source_audit(root, daily, model_dates):
    body, ref, _ = source(root, 'third')
    tree = html.fromstring(body.decode('utf-8'))
    require('DEXP4' in ' '.join(tree.xpath('//h1//text()')), 'dexp_third_source_security_changed')
    tables = [t for t in tree.xpath('//table') if t.xpath('.//tr') and
        [c.text_content().strip() for c in t.xpath('.//tr')[0].xpath('./td|./th')] ==
        ['Data', 'Abertura', 'M\u00e1xima', 'M\u00ednima', 'Fechamento', 'Volume']]
    require(len(tables) == 1, 'dexp_third_source_daily_schema_changed')
    table, = tables
    observations, previous = [], None
    for index, row in enumerate(table.xpath('.//tr')[1:], 1):
        cells = [' '.join(c.text_content().split()) for c in row.xpath('./td')]
        require(len(cells) == 6, 'dexp_third_source_daily_row_changed')
        day = datetime.strptime(cells[0], '%d/%m/%Y').date()
        require(previous is None or day < previous, 'dexp_third_source_duplicate_session')
        require(all(c.startswith('R$ ') for c in cells[1:5]), 'dexp_third_source_currency_changed')
        opened, high, low, close = [br_decimal(c[3:]) for c in cells[1:5]]
        require(low <= opened <= high and low <= close <= high, 'dexp_third_source_OHLC_changed')
        included = START <= day <= CUTOFF
        observations.append({'date': day.isoformat(), 'literal_cells': cells,
            'close_decimal': str(close), 'table_row': index,
            'xpath': tree.getroottree().getpath(row), 'included_for_comparison': included,
            'comparison_close': daily.get(day), 'is_model_monthly_date': day in model_dates,
            'agrees': included and day in daily and math.isclose(float(close), daily[day],
                                                               rel_tol=.006, abs_tol=.015)})
        previous = day
    require(len(observations) == 50, 'dexp_third_source_pinned_row_count_changed')
    return {'source': ref, 'observations': observations,
        'conflicts': [r for r in observations if r['included_for_comparison'] and not r['agrees']],
        'upstream_independence_verified': False, 'official_exchange_authority_claimed': False,
        'raw_storage_basis_independently_certified': False,
        'role': 'third_publisher_corroboration_not_authorization_to_replace_one_day'}


def diagnose(folder, identity, cutoff=CUTOFF, observed_actions=None,
             independent_quotes=None, comparison_content=None):
    if identity.get('ticker') not in TARGETS:
        return None
    require(type(cutoff) is date and cutoff == CUTOFF, 'dexp_exact_cutoff_required')
    require(type(identity.get('cvm')) is int, 'dexp_exact_CVM_required')
    root, ticker = Path(folder).resolve(), identity['ticker']
    action_proof = primary.primary_actions(root, identity, cutoff)
    original, binding, spec, content = original_context(root, identity)
    if observed_actions is not None:
        require(observed_actions == action_proof['observed_actions'], 'dexp_caller_action_list_changed')
    if independent_quotes is not None:
        require(independent_quotes == original, 'dexp_caller_original_quote_set_changed')
    if comparison_content is not None:
        require(comparison_content == content, 'dexp_caller_original_feed_changed')
    actions = action_proof['observed_actions']
    body, ref, _ = source(root, ticker)
    rows = parse_ibovx(body, ticker)
    usable = [r for r in rows if START <= date.fromisoformat(r['date']) <= cutoff]
    future = [r for r in rows if date.fromisoformat(r['date']) > cutoff]
    new = {date.fromisoformat(r['date']): float(Decimal(r['close_decimal'])) for r in usable}
    require(all(factor_after(d, actions, cutoff) == 1 for d in new),
            'dexp_new_provider_observation_requires_additional_regime_proof')
    merged = dict(original)
    for day, value in new.items():
        require(day not in merged or merged[day] == value, 'dexp_independent_raw_sources_disagree')
        merged[day] = value
    payload = json.loads(content)['chart']['result'][0]
    daily = {datetime.fromtimestamp(t, timezone.utc).date(): float(v)
             for t, v in zip(payload['timestamp'], payload['indicators']['quote'][0]['close']) if v is not None}
    native_exchange_readings = []
    source_class = binding['cash_source_class'] if binding else identity['share_class']
    for reference in spec['cash']:
        for index, row in enumerate(json.loads(primary.checked_source(root, reference))['results']):
            if (row.get('typeStock', '').strip() != source_class
                    or row.get('dateClosingPricePriorExDate') in (None, '', '31/12/9999')):
                continue
            day = datetime.strptime(row['dateClosingPricePriorExDate'], '%d/%m/%Y').date()
            if day not in original:
                continue
            native_exchange_readings.append({'source': reference, 'json_pointer': f'/results/{index}',
                'actual_quote_date_field': 'dateClosingPricePriorExDate',
                'actual_quote_value_field': 'closingPricePriorExDate',
                'quote_date_read_from_actual_quote_field': True, 'native_row': row,
                'used_in_required_window': START <= day <= cutoff})
    comparisons = []
    for day, value in sorted(merged.items()):
        if not START <= day <= cutoff:
            continue
        factor = factor_after(day, actions, cutoff)
        comparisons.append({'date': day.isoformat(), 'raw_quote': value,
            'comparison_close': daily.get(day), 'documented_factor_after': factor,
            'split_adjusted_expected': value / factor,
            'split_adjusted_agrees': day in daily and math.isclose(daily[day], value / factor,
                                                                  rel_tol=.006, abs_tol=.015),
            'raw_agrees': day in daily and math.isclose(daily[day], value, rel_tol=.006, abs_tol=.015),
            'source_role': 'original_exchange' if day in original else 'literal_ibovx_close'})
    for row in comparisons:
        day = date.fromisoformat(row['date'])
        if not row['split_adjusted_agrees']:
            index = next(i for i, stamp in enumerate(payload['timestamp'])
                         if datetime.fromtimestamp(stamp, timezone.utc).date() == day)
            row['original_comparison_json_index'] = index
            row['original_comparison_timestamp'] = payload['timestamp'][index]
            row['original_comparison_OHLCV'] = {k: values[index]
                for k, values in payload['indicators']['quote'][0].items()}
    monthly = {}
    for day, value in sorted(daily.items()):
        if date(2021, 9, 1) <= day <= cutoff:
            monthly[month_end(day.year, day.month)] = (day, value)
    require(set(monthly) == {shift_month(cutoff, -i) for i in range(61)}, 'dexp_required61_months_changed')
    model_dates = {d for d, _ in monthly.values()}
    for row in comparisons:
        row['is_model_monthly_date'] = date.fromisoformat(row['date']) in model_dates
    try:
        prices, matches, mode = reconcile_prices(content, ticker, actions, merged, cutoff,
                                                evidence_provider=PROVIDER)
        gate = {'passed': True, 'mode': mode, 'matches': matches,
                'returned_history_months': len(prices), 'required_model_months': 61}
    except ValueError as error:
        prices, gate = None, {'passed': False, 'reason': str(error)}
    regimes = []
    for factor in sorted({factor_after(d, actions, cutoff) for d, _ in monthly.values()}):
        regimes.append({'factor_after': factor,
            'model_months': [m.isoformat() for m, (d, _) in sorted(monthly.items())
                             if factor_after(d, actions, cutoff) == factor],
            'all_independent_dates': [r['date'] for r in comparisons if r['documented_factor_after'] == factor],
            'original_exchange_dates': [r['date'] for r in comparisons
                if r['documented_factor_after'] == factor and r['source_role'] == 'original_exchange']})
    release = issuer_release(root)
    third = third_source_audit(root, daily, model_dates) if ticker == 'DEXP4' else None
    if ticker == 'DEXP3':
        require(new[date(2026, 6, 30)] == float(br_decimal(release['literal_close'])),
                'dexp_primary_current_quarter_close_disagrees_with_native_provider')
    proof = {'identity': {k: identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')},
        'cutoff': cutoff.isoformat(), 'selected_price_source': spec['comparison'],
        'price_selection_policy': 'whole_original_feed_no_day_replacement_all_original_and_new_anchors_required',
        'provider_observation_rows': rows, 'in_window_provider_rows': len(usable),
        'post_cutoff_rows_not_used': future, 'original_anchor_count_in_window': sum(
            START <= d <= cutoff for d in original),
        'independent_comparisons': comparisons,
        'native_exchange_quote_readings': native_exchange_readings,
        'original_quotes_outside_required_window_retained': [
            {'date': d.isoformat(), 'raw_quote': v} for d, v in sorted(original.items())
            if not START <= d <= cutoff],
        'all_split_adjusted_conflicts': [r for r in comparisons if not r['split_adjusted_agrees']],
        'all_raw_conflicts': [r for r in comparisons if not r['raw_agrees']],
        'used_regimes': regimes, 'original_price_gate': gate,
        'model_monthly_inputs': [{'month': m.isoformat(), 'observed_on': d.isoformat(),
            'comparison_close': v, 'documented_factor_after': factor_after(d, actions, cutoff)}
            for m, (d, v) in sorted(monthly.items())],
        'primary_share_actions': action_proof, 'exact_preferred_class_binding': binding,
        'issuer_release': release,
        'third_source_audit': third,
        'source_conflict_interpretation': (
            'No tested quote conflict remains under the documented split-adjusted mode.' if ticker == 'DEXP3'
            else 'The only tested conflict is in factor-one units. Changing raw/split mode cannot resolve it. '
                 'Two public publishers agree on 7.50 while original Yahoo OHLC is 7.40 throughout. '
                 'This favors an isolated feed disagreement, not a demonstrated share-adjustment problem. '
                 'Official same-session adjudication and upstream independence remain unproved; no day is replaced.'),
        'native61_issuer_history_recovered': False,
        'native61_not_present_in_sealed_prior_index': spec['configuration'] is None and not spec['quotes'],
        'provider_history_is_not_issuer_published': True,
        'provider_adjustment_flag_used_as_proof': False,
        'issuer_corroborated_post_bonus_session_dates': ['2026-06-30'] if ticker == 'DEXP3' else [],
        'all_available_original_exchange_quotes_retained': True,
        'all_new_provider_window_quotes_retained': True,
        'upstream_independence_verified': False, 'point_in_time_archive_verified': False,
        'primary_action_absence_certified': False, 'quote_cause_or_revision_chronology_proved': False,
        'factors_fitted_to_prices': False, 'synthetic_exchange_rows_created': False,
        'gates_relaxed': False, 'model_admission': False}
    return {'provider': PROVIDER, 'raw_quotes': merged, 'prices': prices,
        'sources': [ref, *spec['cash'], spec['comparison'], *action_proof['sources'], release['source'],
                    *([third['source']] if third else [])],
        'primary_actions': action_proof, 'proof': proof}


def load_evidence(folder, identity, cutoff, observed_actions=None,
                  independent_quotes=None, comparison_content=None):
    evidence = diagnose(folder, identity, cutoff, observed_actions, independent_quotes, comparison_content)
    if evidence is None:
        return None
    gate = evidence['proof']['original_price_gate']
    require(gate['passed'], gate.get('reason', 'dexp_price_evidence_blocked'))
    return evidence


def fingerprints(root):
    files = ['current.json', 'inventory/current.json', 'stock-selection/current.json']
    for t in TARGETS:
        files += [f'raw/expanded/{t}-prices.json', f'raw/expanded/{t}-payments.html',
                  f'raw/expanded/{t}-b3-cash-1.json']
    return {f: primary.sha((root / f).read_bytes()) for f in files if (root / f).is_file()}


def financial_blocker_rows(root, diagnostics):
    """Read literal current-period placeholders, never make them usable EPS."""
    relative = 'raw/official-financials/dfp_cia_aberta_2025.zip'
    digest = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
    body = primary.read(root, relative, digest)
    member, native = 'dfp_cia_aberta_DRE_con_2025.csv', []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        for line, row in enumerate(rows, 2):
            if (int(row['CD_CVM']) == 16632 and row['DT_REFER'] == '2025-12-31'
                    and row['VERSAO'] == '1' and row['DT_INI_EXERC'] == '2025-01-01'
                    and row['DT_FIM_EXERC'] == '2025-12-31' and row['CD_CONTA'].startswith('3.99')):
                native.append({'member': member, 'csv_line': line, 'native_row': row})
    latest = [r for r in diagnostics if r.get('kind') == 'filing'
              and r.get('filing_id') == '155652' and r.get('fiscal_end') == '2025-12-31']
    require(len(native) == 3 and all(Decimal(r['native_row']['VL_CONTA']) == 0 for r in native)
            and len(latest) == 1, 'dexp_financial_native_blocker_evidence_changed')
    return {'source': {'file': relative, 'sha256': digest, 'bytes': len(body)},
        'native_current_period_rows': native, 'ordinary_latest_filing_diagnostic': latest[0],
        'usable_class_EPS_from_these_rows': None, 'zero_roots_not_promoted_to_EPS': True,
        'claim': 'cached_consolidated_form_has_only_zero_roots_no_exact_class_basic_EPS_leaf',
        'printed_note_EPS_absence_certified': False,
        'required_next_evidence': 'same_filing_explicit_basic_ON_or_PNA_EPS_and_same_scope_weighted_share_basis'}


def isolated_one(folder, ticker, *, with_financial=True, baseline=False):
    """Call integrated production, optionally suppressing only verified note EPS."""
    from . import inventory_candidates as candidates
    from . import inventory_financials as financials
    from . import round2_dexp_eps as eps
    from .market_inventory import load_inventory
    from .collect_market_data import financial_features, stock_observations
    root = Path(folder).resolve()
    require(not baseline or with_financial, 'baseline_requires_financial_diagnostic')
    inventory = load_inventory(root)
    row, = [r for r in inventory['stocks'] if r['ticker'] == ticker]
    identity = {k: row[k] for k in ('ticker', 'cvm', 'share_class', 'isin')}
    before, original_one = fingerprints(root), candidates._one
    original_gate, original_research = candidates.reconcile_prices, financials._research
    original_getter = eps.get_dexp_eps_readings
    checks, candidate, counterfactual = {}, None, None
    result = {'ticker': ticker, 'source_candidate': False, 'financial_current_pass': None,
              'pending_reason': None, 'published': False, 'production_price_gate': None}

    def forbidden(*args, **kwargs):
        raise AssertionError('dexp_replay_network_forbidden')

    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden), \
         patch.object(socket, 'create_connection', forbidden), patch.object(socket, 'getaddrinfo', forbidden):
        if baseline:
            verified, _ = eps.get_dexp_eps_readings(root, CUTOFF, [identity])
            require(len(verified) == 1 and verified[0]['manifest_sha256'] == eps.REVIEW_SHA,
                    'counterfactual_EPS_proof_not_validated')
        cache = candidates._Cache(root, inventory)
        try:
            candidate = candidates._one(row, cache, CUTOFF, checks)
            result.update(source_candidate=True, prices=len(candidate['prices']), ledger_rows=len(candidate['ledger']),
                cash_matches=len(candidate['independent_cash_matches']),
                price_source_file=candidate['price_source_file'],
                share_mode=candidate['share_normalization']['price_source_basis'],
                financial_basis_start=candidate['financial_basis_start'],
                production_price_gate={'observation': 'ordinary_candidate_outputs_not_intercepted_call',
                    'all_match_dates': candidate['independent_price_matches'],
                    'required_months': 61, 'mode': candidate['share_normalization']['price_source_basis']})
        except (ValueError, KeyError, TypeError, IndexError) as error:
            result['pending_reason'] = str(error)
        if with_financial:
            if baseline:
                with patch.object(eps, 'get_dexp_eps_readings', return_value=([], [])) as suppressed:
                    statements, diagnostics, sources = financials.prepare_financials(root, [row], CUTOFF)
                require(suppressed.call_count == 1, 'production_EPS_hook_not_exercised')
                counterfactual = {'proof_validated_before_suppression': True,
                    'suppressed_reader': 'recommender.round2_dexp_eps.get_dexp_eps_readings',
                    'production_reader_calls': suppressed.call_count,
                    'manifest_sha256': eps.REVIEW_SHA}
            else:
                statements, diagnostics, sources = financials.prepare_financials(root, [row], CUTOFF)
            current, coverage, observations = None, None, []
            if candidate is not None:
                spec = row['cvm'], row['share_class'], row['sector_code']
                current = financial_features(statements, candidate['payments'], CUTOFF, spec)
                observations, coverage = stock_observations(ticker, candidate['prices'], candidate['payments'],
                                                            statements, CUTOFF, spec=spec)
                admitted = [r for r in observations if date.fromisoformat(r['as_of']) >= candidate['training_since']
                            and r['fiscal_end'] >= candidate['financial_basis_start']]
                result['financial_current_pass'] = (current is not None
                    and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
                result['training_rows'] = len(admitted)
                if not result['financial_current_pass']:
                    result['pending_reason'] = 'ordinary_financial_current_or_share_basis_gate_failed'
            result['financial_replay'] = {'prepare_financials_called': True, 'diagnostics': diagnostics,
                'sources': sources, 'current': current, 'coverage': coverage,
                'financial_features_called': candidate is not None,
                'stock_observations_called': candidate is not None, 'EPS_imputed': False,
                'financial_scopes_mixed': False, 'unknown_cash_replaced_with_zero': False,
                'latest_2025_records': [r for r in statements if r.get('cvm') == row['cvm']
                    and r.get('share_class') == row['share_class'] and r.get('fiscal_end') == '2025-12-31'],
                'exact_native_current_filing_blocker': financial_blocker_rows(root, diagnostics) if baseline else None}
    require(candidates._one is original_one and candidates.reconcile_prices is original_gate
            and financials._research is original_research and eps.get_dexp_eps_readings is original_getter,
            'dexp_hooks_not_restored')
    require(fingerprints(root) == before, 'dexp_original_or_pointer_mutated')
    result.update(checks=checks, price_gate_calls=[], source_receipts=cache.all_receipts(),
        protected_hashes_before=before, protected_hashes_after=fingerprints(root),
        network_calls=0, original_caches_modified=False, core_edited=False, gates_relaxed=False,
        normal_one_called=True, cash_inspector_patched=False, cash_gate_patched=False,
        production_price_hook_unmodified=True, production_research_unmodified=True,
        counterfactual=counterfactual, financial_hooks_overridden=['get_dexp_eps_readings'] if baseline else [])
    return primary.json_safe(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--replay', action='store_true')
    parser.add_argument('--baseline-eps', action='store_true')
    args = parser.parse_args(argv)
    require(not args.baseline_eps or args.replay, 'baseline_requires_explicit_replay')
    require(args.report.name.startswith('actions-dexp-') and args.report.suffix == '.json',
            'dexp_report_prefix_required')
    report = {'schema_version': 1, 'cutoff': CUTOFF.isoformat(), 'required_months': 61,
              'producer_sha256': primary.sha(Path(__file__).read_bytes()), 'records': []}
    for ticker in TARGETS:
        identity = primary.packet(ROOT)['targets'][ticker]['identity']
        evidence = diagnose(ROOT, identity)
        result = {'ticker': ticker, 'evidence': evidence,
                  'replay': isolated_one(ROOT, ticker, baseline=args.baseline_eps) if args.replay else None}
        report['records'].append(result)
        gate = evidence['proof']['original_price_gate']
        print(json.dumps({'ticker': ticker, 'price_gate': {k:v for k,v in gate.items() if k != 'matches'},
            'independent_price_matches': len(gate.get('matches', [])),
            'replay': {k: result['replay'][k] for k in ('source_candidate', 'financial_current_pass', 'pending_reason')}
            if result['replay'] else None}), flush=True)
    args.report.write_bytes(primary.encoded(primary.json_safe(report)))
    print(json.dumps({'report': str(args.report)}))


if __name__ == '__main__':
    main()
