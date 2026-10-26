"""Read-only Itausa corporate-action evidence, with no network or cache writes.

Run ``python -m recommender.itausa_primary_actions`` for JSON on stdout, or use
``primary_actions(data_root, identity, cutoff)`` for integration. Ratios include
exact integer fractions/decimal strings. Yahoo dates remain observations, never
primary effective dates.
"""

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from lxml import html


DEFAULT_ROOT = Path(__file__).parent / 'data/market'
CALENDAR_SHA = '24c4a8a0d8911941b62a3df8247379148d7d9ef16f36cca52a31fcaeb3fbec92'
CALENDAR_FILE = f'raw/primary-research/cash-calendars/itausa-{CALENDAR_SHA}.html'
CALENDAR_URL = 'https://ri.itausa.com.br/informacoes-financeiras/remuneracao-aos-acionistas/'
START = '2021-09-01'
END = '2026-09-30'
TICKERS = ('ITSA3', 'ITSA4')
BONUS_HEADERS = ('deliberado em', 'posicao acionaria', 'custo atribuido',
                 'proporcao', 'efeito', 'documento')
SUBSCRIPTION_HEADERS = ('deliberado em', 'posicao acionaria', 'preco de aquisicao',
                        '% de aumento do capital social', 'efeito', 'documento')
NOTICE_BASE = ('https://api.mziq.com/mzfilemanager/v2/d/'
               'afd9200b-9b01-4d1c-bdd9-9b2c1e9b3b4d/')


class EvidenceError(ValueError):
    """Missing, ambiguous or inconsistent explicit evidence."""


def _require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _text(node):
    return ' '.join(' '.join(node.itertext()).split())


def _norm(value):
    return ' '.join(unicodedata.normalize('NFKD', value).encode(
        'ascii', 'ignore').decode().lower().split())


def _day(value):
    _require(re.fullmatch(r'\d{2}/\d{2}/\d{4}', value) is not None,
             'explicit_DD_MM_YYYY_required')
    try:
        return datetime.strptime(value, '%d/%m/%Y').date().isoformat()
    except ValueError as error:
        raise EvidenceError('invalid_primary_calendar_date') from error


def _decimal(value):
    _require(isinstance(value, str) and re.fullmatch(r'\d+(?:,\d+)?', value) is not None,
             'explicit_nonnegative_decimal_required')
    return Decimal(value.replace(',', '.'))


def _path(root, name):
    _require(isinstance(name, str) and bool(name) and not Path(name).is_absolute()
             and not name.startswith('/')
             and '\\' not in name and ':' not in name and '..' not in Path(name).parts,
             'unsafe_evidence_path')
    path = (root / name).resolve()
    _require(path.is_relative_to(root.resolve()), 'evidence_outside_data_root')
    return path


def _read(root, name, expected=None):
    body = _path(root, name).read_bytes()
    digest = _sha(body)
    _require(expected is None or digest == expected, 'evidence_hash_mismatch:' + name)
    return body, {'file': name, 'sha256': digest, 'bytes': len(body),
                  'hash_basis': 'pinned' if expected else 'observed'}


def _located(node, source, *, attribute=None):
    return {'file': source['file'], 'sha256': source['sha256'],
            'xpath': node.getroottree().getpath(node) + ('/@' + attribute if attribute else ''),
            'line': node.sourceline,
            'literal': node.get(attribute) if attribute else _text(node)}


def _table(document, headers, section):
    matches = []
    for index, table in enumerate(document.xpath('//table')):
        rows = table.xpath('./thead/tr')
        if rows and tuple(_norm(_text(c)) for c in rows[0].xpath('./th|./td')) == headers:
            containers = table.xpath('ancestor::div[contains(concat(" ", normalize-space(@class), " "),'
                                     ' " accordion__item ")]')
            _require(bool(containers), 'action_table_section_missing')
            titles = containers[-1].xpath('.//div[contains(@class, "accordion__item__header")]')
            _require(len(titles) == 1 and section in _norm(_text(titles[0])),
                     'action_table_section_mismatch')
            matches.append((index, table))
    _require(len(matches) == 1, 'action_table_missing_or_ambiguous:' + section)
    return matches[0]


def _notice_link(cell, source):
    links = cell.xpath('.//a[@href]')
    _require(len(links) == 1, 'one_literal_notice_link_required')
    literal = links[0].get('href')
    _require(re.fullmatch(re.escape(NOTICE_BASE) + r'[a-f0-9-]{36}\?origin=[12]', literal)
             is not None, 'unexpected_literal_notice_link')
    return {'url': literal, 'evidence': _located(links[0], source, attribute='href')}


def _security_evidence(document, source):
    # These words explicitly bind the tickers to species in this primary page.
    # The ticker suffix and the generic phrase "mesma especie" are not mappings.
    result = {}
    for ticker in TICKERS:
        matches = []
        for paragraph in document.xpath('//p'):
            value = _norm(_text(paragraph))
            for species, pattern in (('PN', r'preferencialistas?\s*\(' + ticker + r'\)'),
                                     ('ON', r'ordinaristas?\s*\(' + ticker + r'\)')):
                if re.search(pattern, value, re.I):
                    matches.append((species, paragraph))
        species = {item[0] for item in matches}
        _require(len(species) <= 1, 'conflicting_explicit_security_species:' + ticker)
        result[ticker] = {'ticker': ticker, 'share_class': next(iter(species), None),
                          'evidence': [_located(p, source) for _, p in matches]}
    return result


def extract_primary_actions(body, source, start=START, end=END):
    """Extract explicit bonus terms and the distinct 2023 subscription context.

    Table indices are zero based: the local page has bonus table 12 and
    subscription table 13. XPath locators are executable and one based.
    """
    _require(_sha(body) == source['sha256'], 'primary_body_hash_mismatch')
    _require(date.fromisoformat(start) <= date.fromisoformat(end), 'invalid_window')
    document = html.document_fromstring(body.decode('utf-8-sig'),
                                       parser=html.HTMLParser(no_network=True))
    securities = _security_evidence(document, source)
    table_index, table = _table(document, BONUS_HEADERS, 'bonificacao em acoes')
    events = []
    for row_number, row in enumerate(table.xpath('./tbody/tr'), 1):
        cells = row.xpath('./td')
        _require(len(cells) == 6, 'incomplete_bonus_row')
        values = [_text(c) for c in cells]
        record = _day(values[1])
        if not start <= record <= end:
            continue
        declared = _day(values[0])
        _require(declared <= record, 'bonus_date_order_invalid')
        _require(re.fullmatch(r'\d+(?:,\d+)?%', values[3]) is not None,
                 'explicit_bonus_percentage_required')
        percent = _decimal(values[3][:-1])
        effect = re.fullmatch(r'(\d+) novas? a(?:coes|cao) para cada (\d+) possuidas? da mesma especie',
                              _norm(values[4]))
        _require(effect is not None, 'explicit_bonus_ratio_and_same_species_required')
        new, held = map(int, effect.groups())
        _require(new > 0 and held > 0 and Fraction(new, held) == Fraction(percent) / 100,
                 'bonus_percentage_effect_mismatch')
        cost = re.fullmatch(r'R\$\s*(\d+(?:,\d+)?)', values[2])
        _require(cost is not None, 'explicit_assigned_cost_required')
        events.append({
            'id': 'itausa:stock_bonus:' + record, 'kind': 'stock_bonus',
            'declared_date': declared, 'record_date': record,
            'primary_ex_date': None, 'primary_ex_date_evidence': None,
            'bonus_percent': str(percent),
            'bonus_ratio': {'new_shares': new, 'existing_shares': held},
            'share_count_factor': {'numerator': held + new, 'denominator': held},
            'species_rule': 'same_species_as_held', 'effect_literal': values[4],
            'securities': deepcopy(securities),
            'assigned_tax_cost_per_new_share': {
                'currency': 'BRL', 'amount': str(_decimal(cost[1])),
                'is_market_quote': False, 'evidence': _located(cells[2], source)},
            'primary_fields': {key: _located(cell, source) for key, cell in zip(
                ('declared_date', 'record_date', 'assigned_tax_cost', 'bonus_percent', 'effect'), cells)},
            'table_index': table_index, 'body_row': row_number,
            'row_evidence': _located(row, source), 'notice': _notice_link(cells[5], source),
        })
    events.sort(key=lambda event: event['record_date'])
    _require(len({e['record_date'] for e in events}) == len(events), 'duplicate_primary_bonus')

    subscription_index, subscription_table = _table(
        document, SUBSCRIPTION_HEADERS, 'subscricao em acoes')
    subscription = []
    for row in subscription_table.xpath('./tbody/tr'):
        cells = row.xpath('./td')
        _require(len(cells) == 6, 'incomplete_subscription_row')
        if _day(_text(cells[1])) != '2023-08-17':
            continue
        price = re.fullmatch(r'R\$\s*(\d+(?:,\d+)?)', _text(cells[2]))
        percentage = re.fullmatch(r'(\d+(?:,\d+)?)%', _text(cells[3]))
        _require(price is not None and percentage is not None,
                 'explicit_subscription_terms_required')
        subscription.append({
            'id': 'itausa:subscription:2023-08-17', 'kind': 'subscription',
            'declared_date': _day(_text(cells[0])), 'record_date': '2023-08-17',
            'primary_ex_date': None, 'share_classes': None,
            'subscription_price': {'currency': 'BRL', 'amount': str(_decimal(price[1])),
                                   'is_market_quote': False},
            'capital_increase_percent': str(_decimal(percentage[1])),
            'effect_literal': _text(cells[4]),
            'share_count_factor': None, 'price_adjustment_factor': None,
            'explains_price_discrepancy': None,
            'pending': ['exact_notice_body', 'explicit_rights_and_price_adjustment_terms'],
            'table_index': subscription_index, 'row_evidence': _located(row, source),
            'primary_fields': {key: _located(cell, source) for key, cell in zip(
                ('declared_date', 'record_date', 'subscription_price',
                 'capital_increase_percent', 'effect'), cells)},
            'notice': _notice_link(cells[5], source),
        })
    _require(len(subscription) == 1, '2023_subscription_missing_or_ambiguous')
    return {'events': events, 'subscription_context': subscription[0]}


def _provider_number(value):
    _require(type(value) in (int, float, str), 'invalid_Yahoo_ratio_number')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise EvidenceError('invalid_Yahoo_ratio_number') from error
    _require(result.is_finite() and result > 0, 'invalid_Yahoo_ratio_number')
    return result


def extract_yahoo_actions(body, ticker, source, start=START, end=END):
    """Read split observations only; do not inspect or fit any price series."""
    _require(ticker in TICKERS and _sha(body) == source['sha256'], 'Yahoo_source_mismatch')
    chart = json.loads(body)['chart']
    _require(not chart.get('error') and len(chart.get('result') or []) == 1,
             'one_successful_Yahoo_chart_required')
    result = chart['result'][0]
    meta = result['meta']
    _require(meta.get('symbol') == ticker + '.SA' and meta.get('instrumentType') == 'EQUITY'
             and meta.get('exchangeTimezoneName') == 'America/Sao_Paulo',
             'Yahoo_security_or_timezone_mismatch')
    offset = meta.get('gmtoffset')
    _require(type(offset) is int and -86400 < offset < 86400, 'Yahoo_explicit_offset_required')
    local = timezone(timedelta(seconds=offset))
    events = []
    for key, raw in result.get('events', {}).get('splits', {}).items():
        stamp = raw.get('date')
        _require(type(stamp) is int and key == str(stamp), 'Yahoo_event_timestamp_key_mismatch')
        observed = datetime.fromtimestamp(stamp, local).date().isoformat()
        if not start <= observed <= end:
            continue
        numerator = _provider_number(raw['numerator'])
        denominator = _provider_number(raw['denominator'])
        ratio = re.fullmatch(r'(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)', raw.get('splitRatio', ''))
        _require(ratio is not None and Decimal(ratio[2]) > 0 and
                 Fraction(numerator) / Fraction(denominator) ==
                 Fraction(Decimal(ratio[1])) / Fraction(Decimal(ratio[2])),
                 'Yahoo_splitRatio_inconsistent')
        events.append({'ticker': ticker, 'observed_date': observed,
                       'numerator': str(numerator), 'denominator': str(denominator),
                       'raw': deepcopy(raw), 'source': deepcopy(source),
                       'json_pointer': '/chart/result/0/events/splits/' + key,
                       'date_authority': 'Yahoo_observation_only',
                       'timezone': meta['exchangeTimezoneName'], 'gmtoffset': offset})
    events.sort(key=lambda event: event['observed_date'])
    _require(len({e['observed_date'] for e in events}) == len(events), 'duplicate_Yahoo_action_day')
    return events


def reconcile_actions(primary, yahoo_by_ticker, end=END):
    """Return candidate matches, retaining all unmatched/conflicting evidence.

    A match requires one observation after the record date and before the next
    declaration, with the independently extracted bonus factor. This bounds a
    candidate; it does not prove an ex date, price basis or other rights factor.
    """
    events = deepcopy(primary)
    _require([e['record_date'] for e in events] == sorted(e['record_date'] for e in events),
             'primary_actions_must_be_ordered')
    used = set()
    for index, event in enumerate(events):
        upper = events[index + 1]['declared_date'] if index + 1 < len(events) else None
        factor = event['share_count_factor']
        expected = Fraction(factor['numerator'], factor['denominator'])
        event['yahoo_reconciliation'] = {}
        blockers = ['primary_ex_date_unverified']
        for ticker in TICKERS:
            candidates = [observation for observation in yahoo_by_ticker.get(ticker, [])
                          if event['record_date'] < observation['observed_date'] <= end
                          and (upper is None or observation['observed_date'] < upper)]
            ratio_matches = [candidate for candidate in candidates
                             if Fraction(Decimal(candidate['numerator'])) /
                             Fraction(Decimal(candidate['denominator'])) == expected]
            matched = ratio_matches[0] if len(candidates) == len(ratio_matches) == 1 else None
            if matched:
                used.add((ticker, matched['json_pointer']))
            else:
                blockers.append('Yahoo_match_missing_ambiguous_or_ratio_mismatch:' + ticker)
            if event['securities'][ticker]['share_class'] is None:
                blockers.append('explicit_security_species_missing:' + ticker)
            event['yahoo_reconciliation'][ticker] = {
                'status': 'ratio_and_order_match_ex_date_unverified' if matched else
                          'missing_ambiguous_or_ratio_mismatch',
                'candidate': matched, 'observations_in_interval': candidates,
                'interval': {'after_record_date': event['record_date'],
                             'before_next_declaration': upper, 'through': end},
                'primary_ex_date_verified': False,
            }
        dates = {entry['candidate']['observed_date'] for entry in event['yahoo_reconciliation'].values()
                 if entry['candidate'] is not None}
        if len(dates) > 1:
            blockers.append('Yahoo_dates_disagree_between_tickers')
        event['normalization'] = {
            'kind': 'stock_bonus', 'share_count_factor': deepcopy(factor),
            'effective_date': None, 'ready': False, 'blockers': blockers,
            'price_adjustment_factor': None, 'other_rights_adjustment_verified': False,
        }
    unmatched = [deepcopy(event) for ticker in TICKERS for event in yahoo_by_ticker.get(ticker, [])
                 if (ticker, event['json_pointer']) not in used]
    return {'events': events, 'unmatched_yahoo_events': unmatched}


def _receipt_catalog(root):
    # Only original receipt locations, not derived reports, scratch or test copies.
    receipts = []
    directory = root / 'raw/primary-research'
    paths = set(directory.glob('*/*-source.json')) | set(directory.glob('*/response-*.json'))
    for path in sorted(paths):
        value = json.loads(path.read_bytes())
        if isinstance(value, dict) and isinstance(value.get('url'), str):
            receipts.append((path.relative_to(root).as_posix(), value))
    return receipts


def _cached_notice(root, notice, receipts):
    """Bind only the exact literal URL to original bytes, never a derived text/report.

    Presence and integrity do not certify an effective date. A cached native
    body, when available, still requires explicit date/rights clause review.
    """
    matches = []
    for name, receipt in receipts:
        if receipt['url'] != notice['url']:
            continue
        receipt_body, receipt_source = _read(root, name)
        _require(json.loads(receipt_body) == receipt, 'native_receipt_changed_during_verification')
        digest = receipt.get('sha256')
        _require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) is not None,
                 'native_notice_hash_required')
        body, source = _read(root, receipt['file'], digest)
        _require(len(body) == receipt.get('bytes'), 'native_notice_length_mismatch')
        success = (receipt.get('http_status', 200) == 200 and
                   receipt.get('final_url', notice['url']) == notice['url'])
        # PDF bytes only; an HTML download shell or denied response is not the notice.
        is_document = success and body.lstrip().startswith(b'%PDF-')
        matches.append({'receipt_file': name, 'receipt_sha256': receipt_source['sha256'],
                        'source': source, 'exact_native_body': is_document,
                        'status': 'cached_exact_pdf_requires_clause_review' if is_document else
                                  'cached_response_is_not_verified_native_pdf'})
    bodies = {m['source']['sha256'] for m in matches if m['exact_native_body']}
    return {'status': 'missing_exact_native_document' if not bodies else
                     'conflicting_exact_native_bodies' if len(bodies) > 1 else
                     'cached_exact_pdf_requires_clause_review',
            'matches': matches, 'primary_ex_date_verified': False}


def build_report(root=DEFAULT_ROOT):
    """Build reusable evidence without writing a report or changing any pointer."""
    root = Path(root).resolve()
    body, source = _read(root, CALENDAR_FILE, CALENDAR_SHA)
    receipt_name = CALENDAR_FILE[:-5] + '-source.json'
    receipt_body, receipt_source = _read(root, receipt_name)
    receipt = json.loads(receipt_body)
    _require(all(receipt.get(key) == expected for key, expected in (
        ('issuer', 'itausa'), ('file', CALENDAR_FILE), ('sha256', CALENDAR_SHA),
        ('bytes', len(body)), ('url', CALENDAR_URL), ('final_url', CALENDAR_URL)))
        and set(receipt.get('tickers', [])) == set(TICKERS), 'calendar_receipt_mismatch')
    source.update({'url': CALENDAR_URL, 'receipt': receipt_source,
                   'retrieved_at': receipt.get('retrieved_at')})
    primary = extract_primary_actions(body, source)
    _require(len(primary['events']) == 5, 'expected_five_primary_bonuses_in_window')
    yahoo, selected, missing = {}, {}, []
    for ticker in TICKERS:
        requested = f'raw/expanded/{ticker}-prices.json'
        actual = requested
        if not _path(root, requested).exists():
            missing.append(requested)
            actual = f'raw/inventory/daily-prices/{ticker}.json'
        if not _path(root, actual).exists():
            yahoo[ticker] = []
            selected[ticker] = {'requested_file': requested, 'actual_source': None,
                                'status': 'no_cached_Yahoo_source'}
            continue
        prices, price_source = _read(root, actual)
        selected[ticker] = {'requested_file': requested, 'actual_source': price_source,
                            'status': 'requested_cache' if requested == actual else
                                      'explicit_inventory_cache_substitution'}
        try:
            yahoo[ticker] = extract_yahoo_actions(prices, ticker, price_source)
        except (EvidenceError, KeyError, ValueError, TypeError) as error:
            # Comparison evidence cannot prevent returning the five primary rows.
            yahoo[ticker] = []
            selected[ticker].update({'status': 'invalid_cached_Yahoo_source', 'error': str(error)})
    report = reconcile_actions(primary['events'], yahoo)
    report.update({'schema_version': 1, 'window': {'start': START, 'end': END},
                   'primary_source': source, 'price_sources': selected,
                   'missing_requested_price_files': missing,
                   'subscription_context': primary['subscription_context'],
                   'receipt_search_patterns': ['raw/primary-research/*/*-source.json',
                                               'raw/primary-research/*/response-*.json'],
                   'missing_documents': [], 'pending_document_reviews': [],
                   'price_series_compared': False, 'fitted_factors': [],
                   'all_ready_for_normalization': False})
    receipts = _receipt_catalog(root)
    for event in [*report['events'], report['subscription_context']]:
        notice = event['notice']
        try:
            notice['cache_verification'] = _cached_notice(root, notice, receipts)
        except (EvidenceError, OSError, KeyError, ValueError) as error:
            notice['cache_verification'] = {
                'status': 'invalid_native_document_evidence', 'error': str(error),
                'primary_ex_date_verified': False, 'matches': []}
        item = {'event_id': event['id'], 'kind': event['kind'],
                'declared_date': event['declared_date'], 'record_date': event['record_date'],
                'url': notice['url'], 'discovery': notice['evidence'],
                'required_clauses': ['explicit_ex_or_trading_effective_date',
                                     'security_species_and_entitlement_terms']}
        if event['kind'] == 'subscription':
            item['required_clauses'].append('explicit_treatment_of_subscription_rights')
        if notice['cache_verification']['status'] == 'missing_exact_native_document':
            report['missing_documents'].append(item)
        else:
            report['pending_document_reviews'].append(item)
    return report


def primary_actions(folder, identity, cutoff):
    """Return five reviewable bonus actions and explicit normalization blockers.

    ``folder`` is the market data root; ``identity`` contains a ticker and may
    contain share_class. Only the primary page's explicit ticker/species binding
    is verified here; other identity fields are preserved as caller metadata.
    ``cutoff`` accepts an ISO date or date object. ``ratio`` is post/pre share
    count, with exact ratio_decimal and ratio_fraction beside it. It is not an
    independently established adjustment for subscriptions or other rights.

    Missing exact ex-date proof leaves action.date=None and blocked=True. Such
    actions are review inputs, not directly usable by dated normalization code.
    """
    result = {'schema_version': 1, 'identity': deepcopy(identity), 'actions': [],
              'sources': [], 'blocked': True, 'blockers': [], 'missing_documents': [],
              'pending_document_reviews': []}
    ticker = identity.get('ticker') if isinstance(identity, dict) else None
    if ticker not in TICKERS:
        result['blockers'] = ['identity_outside_ITSA3_ITSA4']
        return result
    try:
        cutoff_day = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
        _require(type(cutoff_day) is date, 'ISO_date_cutoff_required')
        end = cutoff_day.isoformat()
        result['cutoff'] = end
        report = build_report(folder)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as error:
        result['blockers'] = ['primary_evidence_unavailable_or_invalid']
        result['error'] = str(error)
        return result
    result['evidence_window'] = report['window']
    if not START <= end <= END:
        result['blockers'].append('cutoff_outside_bounded_review_window')
    included = set()
    sources = {report['primary_source']['file']: deepcopy(report['primary_source']),
               report['primary_source']['receipt']['file']: deepcopy(report['primary_source']['receipt'])}
    selected_prices = report['price_sources'][ticker]
    if selected_prices['actual_source'] is not None:
        price_source = selected_prices['actual_source']
        sources[price_source['file']] = deepcopy(price_source)
    result['price_source_selection'] = deepcopy(selected_prices)
    result['missing_requested_price_files'] = [name for name in report['missing_requested_price_files']
                                               if name == selected_prices['requested_file']]
    for event in report['events']:
        if event['record_date'] > end:
            continue
        included.add(event['id'])
        security = event['securities'][ticker]
        if identity.get('share_class') is not None and identity['share_class'] != security['share_class']:
            result['blockers'].append('identity_share_class_conflicts_with_primary:' + event['id'])
        factor = event['share_count_factor']
        ratio = Decimal(factor['numerator']) / Decimal(factor['denominator'])
        reconciliation = deepcopy(event['yahoo_reconciliation'][ticker])
        candidate = reconciliation['candidate']
        if candidate is not None and candidate['observed_date'] > end:
            reconciliation.update({'status': 'observation_after_cutoff', 'candidate': None})
        reconciliation['observations_in_interval'] = [row for row in
            reconciliation['observations_in_interval'] if row['observed_date'] <= end]
        located = deepcopy(event['row_evidence'])
        located['locator'] = located['xpath']
        result['actions'].append({
            'id': event['id'], 'date': None, 'ratio': float(ratio), 'ratio_decimal': str(ratio),
            'ratio_fraction': deepcopy(factor), 'kind': 'stock_bonus',
            'declared_date': event['declared_date'], 'record_date': event['record_date'],
            'bonus_percent': event['bonus_percent'], 'bonus_ratio': deepcopy(event['bonus_ratio']),
            'share_class': security['share_class'], 'species_rule': event['species_rule'],
            'security_evidence': deepcopy(security['evidence']),
            'assigned_tax_cost_per_new_share': deepcopy(event['assigned_tax_cost_per_new_share']),
            'source': located, 'primary_fields': deepcopy(event['primary_fields']),
            'notice': deepcopy(event['notice']), 'yahoo_reconciliation': reconciliation,
            'blocked': True, 'blockers': ['primary_ex_date_unverified'],
        })
    subscription = report['subscription_context']
    if subscription['record_date'] <= end:
        result['subscription_context'] = deepcopy(subscription)
        included.add(subscription['id'])
    result['missing_documents'] = [deepcopy(row) for row in report['missing_documents']
                                    if row['event_id'] in included]
    result['pending_document_reviews'] = [deepcopy(row) for row in report['pending_document_reviews']
                                          if row['event_id'] in included]
    for event in [*report['events'], subscription]:
        if event['id'] in included:
            for match in event['notice']['cache_verification']['matches']:
                native = deepcopy(match['source'])
                native['url'] = event['notice']['url']
                sources[native['file']] = native
                sources[match['receipt_file']] = {'file': match['receipt_file'],
                                                  'sha256': match['receipt_sha256']}
    result['sources'] = list(sources.values())
    if result['actions']:
        result['blockers'].append('primary_ex_dates_unverified')
    else:
        result['blockers'].append('no_primary_actions_through_cutoff')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    print(json.dumps(build_report(args.data_root), ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
