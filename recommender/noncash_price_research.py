"""Read-only follow-on to corporate_action_research, never an admission path.

Replays exact-date raw quote anchors and genuine same-security bonuses. A
noncash principal distribution is neither income nor a change in origin units.
Only this module's bounded CVM acquisition and content-addressed research
directory may be written. No exchange, quote-provider or issuer HTTP client
is implemented. Run --report offline; --acquire selects exact CVM index rows.
"""

import argparse
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import zipfile

from . import corporate_action_research as corporate


CACHE = 'raw/primary-research/noncash-price'
START = date(2021, 9, 30)
CUTOFF = date(2026, 9, 30)
BR_TIME = timezone(timedelta(hours=-3))  # No Brazilian DST in the used window.
MAX_REQUESTS = 12
MAX_BYTES = 12_000_000
PRIMARY_SELECTION = ('441220', '452609', '451189', '446025', '439046')
FOLLOWON_PINS = {
    '441220': ('885f6eec7dec64bc479e0737b93215ddb13e8e824ea8b62916aa64d4d9ba7977', 'pdf'),
    '452609': ('7bbda58ef53f741438c4559b4bb9a88a9828fe81b1a55f63488bfd48b2ff5fcd', 'pdf'),
    '451189': ('8edce7fe70e6a1f48f9ee5885ea03c6878f0647c36c88dc85103d7ca5d4acf7a', 'bin'),
    '439046': ('b8417ccf54bf9fea8100c75e3e167c30435c5cba0036e4d1c9f6995e48aa8491', 'pdf'),
}
PRICE_PINS = {
    'BRAP3': '95aaaedf45ff344f58af6bc3917dd7881ad8b45d8ce7ebb8fc41799e3668f59e',
    'BRAP4': '47cce4bb989f283b6fe0c9b083f721fae2e683997b5b3cc6f0bc5f39e26011ab',
    'ITUB3': 'd78f563891a32194c2ddea886dcc542fe54c3aee6eed0713a932ef6b0a3e55c3',
    'ITUB4': 'cbe92b1dce61f5314cd0bbdae93508cecdc02ec367f474f0791a331d0148b592',
    'VALE3': '35179ca0049db9d2f02de90d83c2d7d87c977743f27e7d3ea48e82fe7b1591c0',
}
DETAIL_PINS = {
    18724: 'f60c9e8f036c0711a422a15a09739bb380e9bd796cc2cd58358a60284acf2a17',
    19348: 'e87b679964365e84747e162a1fb2a9997573aad12d9fe0962584c3e7b2bf04c5',
}
CASH_PINS = {
    'BRAP': ('64571fdf9b8fac6f4159414efea5b54dfd8b60225a69b01ed18117f33c8c4fb9',
             'fab098c7649e54c0b7a24d2832c8ac20077b503a4a70cc0dce6d54698f67fb59'),
    'ITUB': ('1a8a3df5382c33e5f4757a72db7ea1ae180a3389d6d11b2204af7f8479077e76',
             'a132cb3e462858713185faf26f66c3d72d6668544875b9442ce960fc90434977',
             '1a54b6257b7deede77a114dcb4e410bc4b691735825e2c4c7f3d57fe04861b15',
             'f0278ad7cdec82ff19bc15c7448bfa5a7da4ce908d815536fe3bbeef980c68e3',
             'ff361781a64fb12416a8d0dde6f371003b786fe4d28800a4e3ea8a19eb74a9f8',
             'b008a06917a31371d9cfa560f2289a7a013d0cfd99cbbb97eda37fc374c0443c',
             'fb71403a20c23e045af200e51540eb655781aa5f80cc791b0be72c6a329c1e0f',
             'f0458fa0acc2e238e001e7973087b90d33719d99be2b34248dd8a7b5c1c2ec2b'),
}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _day(value):
    if type(value) is date:
        return value
    _require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
             'invalid_noncash_price_date')
    return date.fromisoformat(value)


def _number(value, *, zero=False):
    _require(not isinstance(value, bool), 'invalid_economic_number')
    number = Decimal(str(value))
    _require(number.is_finite() and (number >= 0 if zero else number > 0),
             'invalid_economic_number')
    return number


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def _path(folder, relative):
    _require(isinstance(relative, str) and '\\' not in relative and ':' not in relative
             and not PurePosixPath(relative).is_absolute()
             and '..' not in relative.split('/'), 'unsafe_noncash_proof_path')
    root = Path(folder).resolve()
    path = root / relative
    _require(path.resolve().is_relative_to(root)
             and not any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)),
             'unsafe_noncash_proof_path')
    return path


def _write(folder, relative, raw):
    _require(relative.startswith(CACHE + '/'), 'write_outside_owned_noncash_cache')
    path = _path(folder, relative)
    corporate._immutable(path, raw)
    return path


def _read(folder, relative, digest):
    path = _path(folder, relative)
    _require(path.stat().st_size <= MAX_BYTES, 'oversized_noncash_proof')
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(actual == digest, 'noncash_source_hash_mismatch')
    return raw, {'file': relative, 'sha256': actual, 'bytes': len(raw),
                 'access': 'existing_cache_read_only'}


def identity(ticker):
    _require(ticker in corporate.IDENTITIES, 'unreviewed_noncash_instrument')
    cvm, cnpj, share, isin = corporate.IDENTITIES[ticker]
    return dict(ticker=ticker, cvm=cvm, cnpj=cnpj, share_class=share, isin=isin)


def _identity(value):
    expected = identity(value.get('ticker'))
    _require(type(value.get('cvm')) is int and value == expected,
             'noncash_exact_instrument_identity_mismatch')
    return expected


def select_document(folder, sequence):
    """Exact existing hash-pinned index selection, never URL discovery online."""
    _require(sequence in PRIMARY_SELECTION, 'outside_bounded_primary_selection')
    digest = corporate.INDEX_HASHES[2021]
    relative = f'raw/primary-research/cvm-documents/index-2021-{digest}.zip'
    raw, source = _read(folder, relative, digest)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = 'ipe_cia_aberta_2021.csv'
        _require(archive.getinfo(member).file_size <= 100_000_000, 'oversized_primary_index')
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        selected = [r for r in rows if parse_qs(urlsplit(r['Link_Download']).query)
                    .get('numSequencia') == [sequence]]
    _require(len(selected) == 1, 'primary_document_not_unique')
    row = selected[0]
    actual, version = corporate._document_identity(row['Link_Download'])
    _require(actual == sequence and version == int(row['Versao'])
             and int(row['Codigo_CVM']) == 18724
             and re.sub(r'\D', '', row['CNPJ_Companhia']) == '03847461000192',
             'primary_document_issuer_mismatch')
    return {'sequence': sequence, 'version': version, 'cvm': 18724,
            'cnpj': '03847461000192', 'published_at': row['Data_Entrega'],
            'url': row['Link_Download'], 'subject': row['Assunto'],
            'index_source': source, 'index_row': row}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('noncash_primary_redirect_not_followed')


def _attempts(folder):
    values = []
    for path in sorted(_path(folder, CACHE).glob('request-*.json')):
        _require(not path.is_symlink() and path.stat().st_size < 100_000, 'unsafe_request_receipt')
        receipt = json.loads(path.read_bytes())
        _require(path.name == f'request-{len(values) + 1:02d}.json', 'request_ledger_gap')
        values.append(receipt)
    return values


def acquire_document(folder, sequence):
    """At most twelve requests across restarts, no redirects, retries or bypass.

    An immutable intent is persisted BEFORE opening the exact CVM URL. Any
    failed or interrupted attempt stays consumed. A denial stops the session's
    entire acquisition lane, including subsequent commands.
    """
    document = select_document(folder, sequence)
    receipts = list(_path(folder, CACHE).glob(f'cvm-{sequence}-*-source.json'))
    if receipts:
        _require(len(receipts) == 1, 'ambiguous_followon_primary_document')
        receipt = json.loads(receipts[0].read_bytes())
        raw, _ = _read(folder, receipt['file'], receipt['sha256'])
        _require(all(receipt.get(k) == v for k, v in document.items())
                 and raw.startswith(b'%PDF-') and receipt['bytes'] == len(raw),
                 'followon_primary_receipt_mismatch')
        return raw, receipt
    attempts = _attempts(folder)
    _require(not list(_path(folder, CACHE).glob('denial-*.json')), 'access_denied_no_retry')
    _require(not any(r['sequence'] == sequence for r in attempts), 'previous_request_not_retried')
    _require(len(attempts) < MAX_REQUESTS, 'primary_request_budget_exhausted')
    # The existing chain already records a denied XP origin. This lane permits
    # only the exact CVM identity above, excluding every other host and proxy.
    attempt = {'sequence': sequence, 'url': document['url'], 'ordinal': len(attempts) + 1,
               'requested_at': datetime.now(timezone.utc).isoformat(), 'redirects_allowed': False}
    _write(folder, f'{CACHE}/request-{len(attempts) + 1:02d}.json', _json(attempt))
    request = urllib.request.Request(document['url'], headers={
        'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            _require(response.status == 200 and response.geturl() == document['url'],
                     'unexpected_followon_primary_response')
            raw = response.read(MAX_BYTES + 1)
            content_type = response.headers.get('Content-Type', '')
    except urllib.error.HTTPError as error:
        receipt = {**attempt, 'http_status': error.code, 'retry_performed': False}
        label = 'denial' if error.code in (401, 403, 429) else 'failure'
        _write(folder, f'{CACHE}/{label}-{sequence}.json', _json(receipt))
        raise
    digest = hashlib.sha256(raw).hexdigest()
    valid = len(raw) <= MAX_BYTES and raw.startswith(b'%PDF-')
    suffix = 'pdf' if valid else 'bin'
    relative = f'{CACHE}/cvm-{sequence}-{digest}.{suffix}'
    _write(folder, relative, raw[:MAX_BYTES])
    receipt = {**document, 'sha256': digest, 'file': relative, 'bytes': len(raw),
               'http_status': 200, 'content_type': content_type,
               'retrieved_at': datetime.now(timezone.utc).isoformat(),
               'status': 'primary_pdf' if valid else 'unexpected_format_not_proof',
               'point_in_time_archive_verified': False, 'settlement_confirmed': False}
    _write(folder, relative.rsplit('.', 1)[0] + '-source.json', _json(receipt))
    _require(valid, 'invalid_followon_primary_document')
    return raw, receipt


def _shift_month(day, months):
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    following = date(year + (month == 11), (month + 1) % 12 + 1, 1)
    return following - timedelta(days=1)


def _validate_actions(actions, origin_isin):
    days = []
    for action in actions:
        _require(action['kind'] == 'same_class_bonus' and action['isin'] == origin_isin
                 and action.get('same_security_ratio_primary_verified') is True,
                 'received_stock_cannot_be_origin_share_factor')
        _require(_number(action['ratio_decimal']) == _number(action['ratio'])
                 and _number(action['ratio_decimal']) > 1, 'conflicting_same_security_ratio')
        days.append(_day(action['date']))
    _require(len(set(days)) == len(days), 'duplicate_same_security_action')
    _require(all(day <= CUTOFF for day in days), 'post_cutoff_share_action_requires_reconciliation')


def _factor(day, actions, cutoff=CUTOFF):
    value = Decimal(1)
    for action in actions:
        _require(action['kind'] == 'same_class_bonus'
                 and action.get('same_security_ratio_primary_verified') is True,
                 'received_stock_cannot_be_origin_share_factor')
        if day < _day(action['date']) <= cutoff:
            value *= _number(action['ratio_decimal'])
    return value


def _daily(raw, ticker):
    chart = json.loads(raw)['chart']
    _require(chart.get('error') is None and len(chart['result']) == 1, 'invalid_price_chart')
    result = chart['result'][0]
    meta = result['meta']
    _require(meta.get('symbol') == ticker + '.SA' and meta.get('currency') == 'BRL'
             and meta.get('exchangeTimezoneName') == 'America/Sao_Paulo'
             and meta.get('dataGranularity') == '1d', 'price_instrument_currency_or_interval_mismatch')
    stamps, values = result['timestamp'], result['indicators']['quote'][0]['close']
    _require(len(stamps) == len(values), 'daily_array_length_mismatch')
    days, daily = set(), {}
    for stamp, value in zip(stamps, values):
        _require(type(stamp) is int, 'invalid_daily_timestamp')
        day = datetime.fromtimestamp(stamp, BR_TIME).date()
        _require(day not in days, 'duplicate_daily_price_dates')
        days.add(day)
        if value is not None and day <= CUTOFF:
            daily[day] = _number(value)
    return daily, result.get('events', {})


def _anchors(rows, share):
    anchors = {}
    for index, row in enumerate(rows):
        label = row.get('corporateAction', '').strip().upper()
        if row.get('typeStock', '').strip() != share or label not in ('DIVIDENDO', 'JRS CAP PROPRIO'):
            continue
        text = row.get('dateClosingPricePriorExDate')
        if not text or text == '31/12/9999':
            continue
        day = datetime.strptime(text, '%d/%m/%Y').date()
        if not START <= day <= CUTOFF:
            continue
        price = _number(str(row['closingPricePriorExDate']).replace('.', '').replace(',', '.'))
        _require(row.get('quotedPerShares') == '1', 'unreviewed_reference_quote_unit')
        if day in anchors:
            _require(math.isclose(float(price), float(anchors[day]['raw_close']),
                                 abs_tol=.015, rel_tol=.001), 'conflicting_B3_reference_quotes')
            anchors[day]['source_row_indexes'].append(index)
        else:
            anchors[day] = {'raw_close': price, 'source_row_indexes': [index],
                            'reference_date': text, 'reference_share_class': share,
                            'quoted_per_shares': row.get('quotedPerShares')}
    return anchors


def _load(folder, instrument, documents=None):
    instrument = _identity(instrument)
    ticker, cvm, share = instrument['ticker'], instrument['cvm'], instrument['share_class']
    sources = []

    def read(relative, digest):
        raw, receipt = _read(folder, relative, digest)
        sources.append(receipt)
        return raw

    detail = json.loads(read(f'raw/expanded/b3-detail-{cvm}.json', DETAIL_PINS[cvm]))
    _require(int(detail['codeCVM']) == cvm and detail['cnpj'] == instrument['cnpj']
             and [r['isin'] for r in detail['otherCodes'] if r['code'] == ticker] == [instrument['isin']],
             'cached_detail_exact_identity_mismatch')
    rows = []
    pins = CASH_PINS[ticker[:4]]
    total = None
    for page, digest in enumerate(pins, 1):
        data = json.loads(read(f'raw/expanded/{ticker}-b3-cash-{page}.json', digest))
        meta = data['page']
        total = meta['totalRecords'] if total is None else total
        _require(meta['pageNumber'] == page and meta['totalPages'] == len(pins)
                 and meta['totalRecords'] == total, 'cached_quote_pagination_mismatch')
        rows.extend(data['results'])
    _require(len(rows) == total, 'cached_quote_record_count_mismatch')
    raw = read(f'raw/expanded/{ticker}-prices.json', PRICE_PINS[ticker])
    daily, vendor_events = _daily(raw, ticker)
    supplement = read(f'raw/expanded/b3-supplement-{ticker[:4]}.json', corporate.SUPPLEMENT_HASHES[cvm])
    canonical = corporate.canonicalize_supplement(instrument, supplement,
        documents if documents is not None else corporate.load_documents(folder),
        CUTOFF, window_start=START)
    actions = canonical['canonical_share_actions']
    _validate_actions(actions, instrument['isin'])
    vendor_actions = []
    for entry in vendor_events.get('splits', {}).values():
        day = datetime.fromtimestamp(entry['date'], BR_TIME).date()
        if START < day <= CUTOFF:
            vendor_actions.append((day, _number(entry['numerator']) / _number(entry['denominator'])))
    _require(sorted(vendor_actions) == sorted((_day(a['date']), _number(a['ratio_decimal']))
                                            for a in actions), 'vendor_and_primary_share_actions_conflict')
    return {'identity': instrument, 'raw': raw, 'daily': daily, 'vendor_events': vendor_events,
            'anchors': _anchors(rows, share), 'corporate': canonical, 'sources': sources}


def audit_origin_basis(daily, anchors, actions, event_day, *, origin_isin):
    """Exact dates, all anchors, and all used share AND noncash regimes.

    The normal price-gate tolerances remain .006 / .015. This audit adds a
    noncash boundary to regime coverage; it cannot absorb one into share units.
    """
    _validate_actions(actions, origin_isin)
    _require(len(anchors) >= 4, 'insufficient_independent_price_matches')
    months = {}
    for day, value in sorted(daily.items()):
        if START <= day <= CUTOFF:
            months[_shift_month(day, 0)] = (day, value)
    required = [_shift_month(CUTOFF, -i) for i in reversed(range(61))]
    _require(all(month in months for month in required), 'less_than_61_consecutive_prices')
    _require((CUTOFF - months[CUTOFF][0]).days <= 7, 'stale_month_end_price')

    def regime(day):
        return str(_factor(day, actions)), 'cum_noncash' if day < event_day else 'ex_noncash'

    regimes = {regime(months[month][0]) for month in required}
    observed = {regime(day) for day in anchors}
    _require(regimes.issubset(observed), 'unverified_share_or_noncash_price_regime')
    comparisons = []
    for day, source in sorted(anchors.items()):
        factor = _factor(day, actions)
        actual = daily.get(day)
        expected = source['raw_close'] / factor
        matches = actual is not None and math.isclose(float(actual), float(expected),
                                                     abs_tol=.015, rel_tol=.006)
        comparisons.append({'date': day.isoformat(), 'raw_reference_close': str(source['raw_close']),
            'vendor_close': str(actual) if actual is not None else None,
            'same_security_factor': str(factor), 'expected_vendor_close': str(expected),
            'vendor_times_bonus_over_raw_diagnostic_only': str(actual * factor / source['raw_close'])
                if actual is not None else None,
            'matches_bonus_only_basis': matches, 'regime': list(regime(day)), **{
                key: value for key, value in source.items() if key != 'raw_close'}})
    matched = all(row['matches_bonus_only_basis'] for row in comparisons)
    normalized = [value for _, value in (months[month] for month in required)]
    _require(all(Decimal('.2') < b / a < 3 for a, b in zip(normalized, normalized[1:])),
             'unexplained_extreme_price_change')
    return {'bonus_only_basis_verified': matched, 'monthly_price_count': 61,
            'joint_return_periods_required': 60, 'joint_economic_returns_recovered': 0,
            'used_regimes': [list(r) for r in sorted(regimes)],
            'anchor_count': len(comparisons), 'anchors': comparisons,
            'thresholds': {'relative': .006, 'absolute': .015},
            'reason': None if matched else 'noncash_or_other_vendor_backadjustment_not_verified'}


def recover_origin_prices(folder, instrument):
    """Verified ITUB origin raw-basis VIEW only; no economic return or admission.

    Multiplication reverses only primary-proven same-class bonuses. No receipt
    ratio or adjustedClose is read. Research views never overwrite cached data.
    """
    loaded = _load(folder, instrument)
    action = loaded['corporate']['non_income_events'][0]
    actions = loaded['corporate']['canonical_share_actions']
    audit = audit_origin_basis(loaded['daily'], loaded['anchors'], actions, _day(action['ex_date']),
                               origin_isin=instrument['isin'])
    _require(audit['bonus_only_basis_verified'], 'original_price_basis_not_reconstructible_with_verified_terms')
    raw_daily = {day: value * _factor(day, actions) for day, value in loaded['daily'].items()
                 if START <= day <= CUTOFF}
    normalized = {}
    for day, value in sorted(raw_daily.items()):
        adjusted = value / _factor(day, actions)
        normalized[_shift_month(day, 0)] = (day, float(adjusted), float(adjusted))
    return {'identity': deepcopy(instrument), 'raw_daily_prices': raw_daily,
            'cutoff_share_monthly_prices': normalized, 'audit': audit, 'sources': loaded['sources'],
            'price_basis': 'reconstructed_origin_execution_price_scale_from_close_and_primary_bonuses',
            'cent_rounding_applied': False, 'economic_total_returns_available': False,
            'income_modified': False, 'noncash_principal_included_in_origin_price': False,
            'point_in_time_archive_verified': False, 'model_admission_granted': False}


def noncash_principal_receipt(canonical):
    """Financial boundary receipt: inherit, never rewrite, the existing proof."""
    instrument = _identity({key: canonical[key] for key in identity(canonical['ticker'])})
    event, = canonical['non_income_events']
    _require(event['isin'] == instrument['isin'] and event['holder_share_factor'] == '1'
             and event['is_own_share_split'] is False and event['cash_dividend_or_JCP'] is False
             and event['income_amount'] == '0', 'noncash_principal_or_share_basis_conflict')
    return {'identity': instrument, 'ex_date': event['ex_date'], 'kind': event['kind'],
            'cash_income_brl_per_origin_share': '0', 'principal_received_in_other_security': True,
            'origin_units_after_per_origin_unit': '1', 'income_ledger_entries_created': 0,
            'DFC_cash_outflow_inferred': False, 'profit_or_EPS_inferred': False,
            'NCI_attributed_to_origin_shareholders': False, 'cost_basis_is_FMV': False,
            'settlement_confirmed': False, 'point_in_time_archive_verified': False,
            'corporate_proof_sources': deepcopy(event['evidence']),
            'financial_and_cash_history_gates_still_required': True}


def validate_economic_bridge(instrument, canonical, receipt, *, valuation_day,
                             origin_raw_price, received_raw_price, origin_units, received_units,
                             receivable_units, cash_income, claimed_economic_value):
    """Pure guard for a FUTURE multi-asset ledger; does not authorize valuations.

    All prices must share a currency and valuation date and be on the units in
    this bridge. A receivable and delivered units partition the same principal.
    Parent-profit, NCI, book cost and an adjusted origin price cannot price it.
    Hash/source validation and full history gates remain the caller's work.
    """
    _identity(instrument)
    boundary = noncash_principal_receipt(canonical)
    _require(boundary['identity'] == instrument, 'economic_instrument_or_class_conflict')
    event, = canonical['non_income_events']
    expected_received_isin = event.get('received_isin') or event.get('final_received_isin')
    _require(expected_received_isin is not None, 'received_security_identity_not_verified')
    _require(receipt['origin_identity'] == instrument
             and receipt['received_isin'] == expected_received_isin
             and expected_received_isin != instrument['isin'], 'economic_instrument_or_class_conflict')
    day = _day(valuation_day)
    _require(day.isoformat() == event['ex_date']
             and receipt['valuation_date'] == day.isoformat()
             and receipt['origin_price_date'] == day.isoformat()
             and receipt['received_price_date'] == day.isoformat(), 'economic_valuation_date_conflict')
    _require(receipt['currency'] == 'BRL' and receipt['origin_price_basis'] == 'raw_origin_share'
             and receipt['received_price_basis'] == 'raw_received_security_unit'
             and receipt['origin_share_basis'] == receipt['entitlement_share_basis']
             and receipt['origin_share_basis'] == event['record_date']
             and receipt['principal_already_in_origin_price'] is False,
             'economic_price_or_share_basis_conflict')
    _require(receipt['valuation_kind'] == 'market_value'
             and _number(receipt['NCI_value_included'], zero=True) == 0,
             'NCI_or_nonmarket_value_conflict')
    _require(_number(cash_income, zero=True) == 0, 'noncash_principal_is_not_cash_income')
    units = _number(origin_units)
    delivered = _number(received_units, zero=True)
    pending = _number(receivable_units, zero=True)
    ratio = event.get('received_units_per_origin_unit') or event['final_received_units_per_origin_unit']
    _require(_number(receipt['received_units_per_origin_unit']) == _number(ratio),
             'noncash_entitlement_ratio_conflict')
    entitled = _number(ratio) * units
    _require(delivered + pending == entitled, 'double_count_or_missing_principal_units')
    value = units * _number(origin_raw_price) + (delivered + pending) * _number(received_raw_price)
    _require(value == _number(claimed_economic_value), 'economic_value_does_not_reconcile')
    return {'economic_value_brl': str(value), 'cash_income_brl': '0',
            'claim_is_observed_total_return': False, 'model_admission_granted': False}


def followon_documents(folder):
    """Verify the newly read sources without acquiring or rerunning old proof."""
    result = {}
    for sequence, (digest, suffix) in FOLLOWON_PINS.items():
        relative = f'{CACHE}/cvm-{sequence}-{digest}.{suffix}'
        raw, source = _read(folder, relative, digest)
        receipt_raw = _path(folder, relative.rsplit('.', 1)[0] + '-source.json').read_bytes()
        receipt = json.loads(receipt_raw)
        selected = select_document(folder, sequence)
        _require(all(receipt.get(k) == v for k, v in selected.items())
                 and receipt['file'] == relative and receipt['sha256'] == digest
                 and receipt['bytes'] == len(raw) and receipt['http_status'] == 200,
                 'followon_primary_receipt_mismatch')
        _require(_day(receipt['published_at']) <= CUTOFF
                 and datetime.fromisoformat(receipt['retrieved_at']).tzinfo is not None,
                 'invalid_followon_source_time')
        result[sequence] = {'source': {**receipt, 'access': source['access']},
                            'pages': corporate.extract_pages(raw) if suffix == 'pdf' else None}
    book = result['441220']['pages'][0]
    dividend = result['439046']['pages'][0]
    _require('R$40,258734277' in book and '30 de setembro de 2021' in book
             and 'R$5.260.000.000,00' in book, 'book_cost_reading_not_verified')
    _require('R$5,494409924' in dividend and 'R$6,043850916' in dividend
             and '29.12.2021' in dividend, 'separate_cash_dividend_reading_not_verified')
    return result


def recipient_quote_evidence(folder, vale_daily):
    """Cached rawClose corroboration, without assuming independent lineage."""
    relative = 'raw/income/brapi-raw-prices-VALE3.json'
    raw, source = _read(folder, relative,
        '086691d8254da21b44240b1244653c8620a233d9b656a0ddd6b594d3f5d51de8')
    payload = json.loads(raw)
    _require(len(payload['results']) == 1, 'ambiguous_recipient_quote_payload')
    result, = payload['results']
    _require(result['requestedSymbol'] == result['symbol'] == 'VALE3'
             and result['changed'] is False and result['data']['usedInterval'] == '1d',
             'recipient_quote_identity_or_interval_conflict')
    rows = {}
    for row in result['data']['historicalDataPrice']:
        day = datetime.fromtimestamp(row['date'], BR_TIME).date()
        _require(day not in rows, 'duplicate_recipient_quote_date')
        rows[day] = row
    observations = []
    for day in map(date.fromisoformat, ['2021-12-16', '2021-12-17', '2021-12-20', '2021-12-30']):
        row = rows.get(day, {})
        value = row.get('rawClose')
        value = _number(value) if value is not None else None
        vendor = vale_daily.get(day)
        observations.append({'date': day.isoformat(), 'brapi_rawClose': str(value) if value else None,
            'yahoo_close': str(vendor) if vendor else None,
            'rawClose_matches_close': value == _number(row['close']) if value else False,
            'providers_agree': value is not None and vendor is not None
                and math.isclose(float(value), float(vendor), abs_tol=.015, rel_tol=.006)})
    return {'source': source, 'observations': observations,
            'quote_field_used': 'rawClose', 'adjustedClose_consumed': False,
            'two_provider_agreement_is_primary_independent_proof': False,
            'upstream_independence_documented': False,
            'normal_recipient_price_and_action_regime_gate_completed': False,
            'reason': 'Dated rawClose exists, but independent primary anchors and the provider noncash close methodology remain unverified.'}


def _noncash_reversal_diagnostic(loaded, vale_daily, recipient):
    """A numerical candidate, deliberately NOT a reversal authorization.

    An independently reproducible value is necessary but is not sufficient to
    establish which field/provider methodology was applied. In particular a
    quote from the same provider cannot independently certify its own factor.
    """
    ticker = loaded['identity']['ticker']
    reference_day = date(2021, 12, 16)
    quote = loaded['anchors'][reference_day]['raw_close']
    vendor = loaded['daily'][reference_day]
    ratio = Decimal('0.332373453')
    cached_vale = vale_daily[reference_day]
    theoretical_value = ratio * cached_vale
    factor = (quote - theoretical_value) / quote
    book_value = ratio * Decimal('40.258734277')
    fit = []
    for day, anchor in sorted(loaded['anchors'].items()):
        candidate = anchor['raw_close'] * (factor if day < date(2021, 12, 17) else 1)
        observed = loaded['daily'].get(day)
        fit.append({'date': day.isoformat(), 'candidate_expected_vendor_at_known_raw_anchor': str(candidate),
                    'observed_vendor_close': str(observed) if observed else None,
                    'fits_numeric_candidate_only': observed is not None and math.isclose(
                        float(candidate), float(observed), abs_tol=.015, rel_tol=.006)})
    return {'ticker': ticker, 'status': 'unverified_numerical_candidate_do_not_apply',
            'reference_date': reference_day.isoformat(), 'ex_date': '2021-12-17',
            'origin_raw_reference': str(quote), 'vendor_close': str(vendor),
            'received_units_per_origin_unit': str(ratio),
            'vendor_VALE3_reference_close': str(cached_vale),
            'vendor_VALE3_reference_close_independently_verified_here': False,
            'cached_recipient_rawClose_corroboration': recipient['observations'],
            'implied_distribution_value_from_origin_mismatch_diagnostic': str(quote - vendor),
            'candidate_value_from_recipient_quote': str(theoretical_value),
            'candidate_proportional_price_factor': str(factor),
            'candidate_all_anchor_fit_diagnostics': fit,
            'candidate_formula': '(raw_cum_close - received_units * recipient_cum_close) / raw_cum_close',
            'book_cost_per_received_share': '40.258734277',
            'book_cost_reference_date': '2021-09-30',
            'book_cost_per_origin_share_not_FMV': str(book_value),
            'book_cost_cannot_replace_distribution_FMV': True,
            'provider_documented_noncash_close_adjustment': False,
            'independent_distribution_FMV_verified': False,
            'candidate_applied_to_any_price': False,
            'original_price_basis_reconstruction_available': False,
            'proof_missing': [
                'Yahoo documentation binding this noncash event/value/formula to the close field',
                'Independent primary dated VALE3 market-value reference actually used by the provider; secondary rawClose corroboration is insufficient',
                'Independent raw anchors in every used regime after applying that proven formula',
            ]}


def build_report(folder):
    documents = corporate.load_documents(folder)
    followon = followon_documents(folder)
    vale_raw, vale_source = _read(folder, 'raw/expanded/VALE3-prices.json', PRICE_PINS['VALE3'])
    vale_daily, _ = _daily(vale_raw, 'VALE3')
    recipient = recipient_quote_evidence(folder, vale_daily)
    records, sources = [], {s['file']: s for s in (vale_source, recipient['source'])}
    for ticker in corporate.IDENTITIES:
        loaded = _load(folder, identity(ticker), documents)
        canonical = loaded['corporate']
        event, = canonical['non_income_events']
        actions = canonical['canonical_share_actions']
        audit = audit_origin_basis(loaded['daily'], loaded['anchors'], actions, _day(event['ex_date']),
                                   origin_isin=loaded['identity']['isin'])
        for source in loaded['sources']:
            sources[source['file']] = source
        blocked = ['multi_asset_economic_history_not_recovered',
                   'normal_cash_financial_history_and_60_joint_return_gates_still_required']
        record = {'identity': loaded['identity'], 'original_basis_audit': audit,
            'noncash_financial_receipt': noncash_principal_receipt(canonical),
            'original_price_basis_reconstruction_available': audit['bonus_only_basis_verified'],
            'economic_total_returns_available': False, 'model_admission_granted': False,
            'monthly_economic_prices_produced': 0, 'joint_economic_returns_produced': 0,
            'cash_income_from_principal': '0', 'normal_gates_waived': [],
            'primary_same_security_actions': actions,
            'event_boundary_observations': [
                {'date': day.isoformat(), 'vendor_close': str(loaded['daily'][day]),
                 'origin_share_factor_to_cutoff': str(_factor(day, actions)),
                 'raw_origin_basis_view': str(loaded['daily'][day] * _factor(day, actions))
                    if audit['bonus_only_basis_verified'] else None,
                 'observed_independent_raw_anchor': str(loaded['anchors'][day]['raw_close'])
                    if day in loaded['anchors'] else None}
                for day in sorted(loaded['daily'])
                if (_day(event['record_date']) - timedelta(days=1)) <= day
                    <= (_day(event['ex_date']) + timedelta(days=3))]}
        if ticker.startswith('BRAP'):
            record['noncash_reversal_diagnostic'] = _noncash_reversal_diagnostic(loaded, vale_daily, recipient)
            record['separate_cash_distribution'] = {
                'gross_declared_per_origin_share': '5.494409924' if ticker == 'BRAP3' else '6.043850916',
                'record_date': '2021-12-16', 'ex_date': '2021-12-17', 'payment_date': '2021-12-29',
                'principal_included': False, 'source_sequence': '439046',
                'payment_source_calendar_not_overwritten': True, 'settlement_confirmed': False}
            blocked += ['undocumented_noncash_backadjustment_of_close',
                        'independent_dated_recipient_FMV_missing',
                        'announced_delivery_does_not_prove_fractional_proceeds_or_account_settlement']
        else:
            record['recovery_method'] = 'raw_origin_basis = vendor_close * product(primary_same_class_bonuses_after_date)'
            record['recovery_limits'] = 'Scale reconstruction from historical provider observations; not original tape bytes or a PIT archive.'
            record['issuer_theoretical_opening_reference'] = {
                'date': '2021-10-04', 'value_brl': event['announced_reference_opening_BRL'],
                'source_sequence': '429639', 'is_executed_quote': False,
                'is_XP_received_market_price_or_dividend': False, 'used_to_reconstruct_prices': False}
            blocked += ['final_XP_BDR_ISIN_and_delivery_fraction_dates_not_verified',
                        'XP_BDR_valuation_history_and_currency_unit_basis_missing']
        record['blockers'] = blocked
        records.append(record)
    return {'schema_version': 1, 'scope': 'noncash_origin_price_and_economic_return_followon_only',
            'window_start': START.isoformat(), 'cutoff': CUTOFF.isoformat(), 'records': records,
            'sources': list(sources.values()),
            'followon_primary_sources': [item['source'] for item in followon.values()],
            'recipient_quote_evidence': recipient,
            'acquisition': {'requests_used': len(_attempts(folder)), 'limit': MAX_REQUESTS,
                'attempts': _attempts(folder), 'report_network_requests': 0,
                'external_documentation_search_queries': 2,
                'B3_network_requests': 0, 'issuer_origin_requests': 0,
                'unexpected_HTML_response_sequence': '451189',
                'unexpected_HTML_response_retried': False,
                'not_requested_sequence': '446025',
                'not_requested_reason': 'Further bulletin probe not required after empty response; no replacement URL attempted.'},
            'provider_documentation_assessment': {
                'url': 'https://in.help.yahoo.com/kb/adjusted-close-sln28256.html',
                'discovery': 'public_search_result_read_2026-10-25_not_a_PIT_archive',
                'supports': 'General split and dividend multiplier explanation for adjusted close',
                'does_not_establish': 'Noncash capital-return adjustment of the cached close field',
                'raw_source_field_used': 'indicators.quote[0].close',
                'adjusted_close_consumed': False, 'provider_method_sufficient_for_BRAP_reversal': False},
            'economic_return_method_proposal': {
                'status': 'hypothetical_design_only_not_integrated_or_observed_returns',
                'proposed_marking': 'Mark origin plus separately identified noncash receivable or received asset; transfer receivable to asset on delivery without adding wealth twice.',
                'proposed_hold_policy': 'Retain received asset with its own independently reconciled prices, actions and income; specify tax, cost and fractional-principal treatment.',
                'alternative_index_policy': 'Sell or reinvest principal only under an explicitly hypothetical index convention with independently verified date, value and friction assumptions.',
                'income_policy': 'Received shares and any fractional-principal sale proceeds stay principal. Only independently proven cash dividends/JCP enter income.',
                'economic_period_return': '(ending marked wealth + externally paid cash - external contributions) / beginning marked wealth - 1; retained cash must not also be added externally.',
                'requirements': ['61 consecutive common month ends and 60 joint economic returns',
                    'Every origin and received-security share/currency regime independently reconciled',
                    'Declared, ex, delivery, valuation and cash-payment dates kept distinct',
                    'Record-date entitlements must not be multiplied by later origin bonuses',
                    'Class-specific EPS and parent-profit/NCI reconciliation remain independent gates'],
                'observed_index_created': False},
            'impossibility_scope': 'Current bounded public/cached evidence, not a claim that recovery is impossible from all possible future sources.',
            'new_admissions': 0, 'dataset_activated': False, 'parent_files_modified': False,
            'original_cache_modified': False, 'cash_income_modified': False,
            'point_in_time_archive_verified': False, 'missing_prices_imputed': False,
            'normal_financial_history_gates_waived': False}


def write_report(folder):
    report = build_report(folder)
    raw = _json(report)
    relative = f'{CACHE}/report-{CUTOFF.isoformat()}-{hashlib.sha256(raw).hexdigest()}.json'
    return _write(folder, relative, raw), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', nargs='+', choices=PRIMARY_SELECTION)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.acquire:
        for sequence in args.acquire:
            raw, receipt = acquire_document(args.folder, sequence)
            print(json.dumps({'sequence': sequence, 'sha256': receipt['sha256'],
                              'pages': corporate.extract_pages(raw)}, ensure_ascii=True), flush=True)
    if args.report:
        path, report = write_report(args.folder)
        print(json.dumps({'report': str(path), 'new_admissions': 0,
                          'joint_economic_returns_recovered': 0}, ensure_ascii=True))


if __name__ == '__main__':
    main()
