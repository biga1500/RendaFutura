"""Hash-pinned, offline price-source recovery; never activates a dataset.

The reviewed adapter fixes unavailable-date parsing and one exact class label.
It never synthesizes a price, edits a raw cache, uses adjusted close, supplies a
missing split, or relaxes share_basis.reconcile_prices. Other cases remain
diagnostics. Run with --report for the immutable, machine-readable manifest.
"""

import argparse
from collections import defaultdict
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import zipfile


CACHE = 'raw/primary-research/price-basis'
CUTOFF = date(2026, 9, 30)
START = date(2021, 9, 30)
SNAPSHOT = 'snapshots/universe-20261025T082737-d133dcd2'
SNAPSHOT_PINS = {
    SNAPSHOT + '/candidate-source-audit.json': '8bf5a40e7f1d2647252c21b6144284e73dd4f0f459e801e265fdd32ef79d1073',
    SNAPSHOT + '/stock-universe-preparation.json': '7f8c3192892431b8c305dc8e7343bd670d8fc7239d5611aaed651afe6f4747fc',
}
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_HASH = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
REASONS = {
    'historical_quote_or_share_scale_disagrees_with_B3',
    'insufficient_independent_price_matches',
    'unverified_share_price_regime_requires_raw_history',
    'conflicting_B3_reference_quotes', 'stale_month_end_price',
    'unexplained_extreme_price_change',
}
REVIEWS = {
    'ISAE3': {
        'cvm': 18376, 'cnpj': '02998611000104', 'share_class': 'ON', 'isin': 'BRISAEACNOR2',
        'reference_share_class': 'ON', 'correction': 'unknown_date_sentinel_is_not_a_quote_date',
        'files': {
            'raw/expanded/b3-detail-18376.json': 'e4a32998424d7753becb28aeb6e7bdb617784ff4af3b588f7e8aba63b740d8f1',
            'raw/expanded/ISAE4-b3-cash-1.json': 'aa704ff265a4f0ff5384a46b03e8f0d267085a6b59c6d7c78f5032eb8d596efb',
            'raw/expanded/ISAE4-b3-cash-2.json': '74a7cc1d5809caac6537f24adbfc9a62117d45d1e71aa48d79b301e418595738',
            'raw/inventory/daily-prices/ISAE3.json': 'e79e7b072d9de8c09f6aac3b5aaf342ab3b39fff1196313f9ca902807b8b12bd',
        },
    },
    'UGPA3': {
        'cvm': 18465, 'cnpj': '33256439000139', 'share_class': 'ON', 'isin': 'BRUGPAACNOR8',
        'reference_share_class': 'ON', 'correction': 'unknown_date_sentinel_is_not_a_quote_date',
        'files': {
            'raw/expanded/b3-detail-18465.json': '508998aa7ce209d82ddf36e1436da55bb394b823c5da8fdb2bd33eb96c8fae5c',
            'raw/expanded/UGPA3-b3-cash-1.json': '4fd7525ca34093a397fb1da920e6638b65a52b2552d746cb6dbfd090e97af40f',
            'raw/expanded/UGPA3-prices.json': '8658fbc1e09d9b635addc4f1957e2cd88c5e7c7d644aa2b75c8128cdbc942be3',
        },
    },
    'CGAS5': {
        'cvm': 15636, 'cnpj': '61856571000117', 'share_class': 'PN', 'isin': 'BRCGASACNPA3',
        'reference_share_class': 'PNA', 'correction': 'exact_ISIN_source_class_label_crosswalk',
        'files': {
            'raw/expanded/b3-detail-15636.json': '7f3222ffa59f17fea03e6990c31338e9dba901914591f01a2b46441077ea6f11',
            'raw/expanded/CGAS5-b3-cash-1.json': '2227b5cb9b37f13ddd93b08175a7a87af1fd1313fddab326fb27cb6623da32d6',
            'raw/expanded/CGAS5-b3-cash-2.json': '58a7b1e3312e19b523e5dfcfe77d29ca34983186dc60f11f43901acbe59385ac',
            'raw/expanded/CGAS5-prices.json': 'a8b416f2a5c56630cbdc22cef653b7fc0420328121d5b34c2defb622407c174a',
        },
    },
}
CGAS_DOCUMENT = {
    'cvm': 15636, 'issuer_name': 'CIA GAS DE SAO PAULO - COMGAS',
    'sequence': '1061747', 'version': 2, 'published_at': '2026-06-22',
    'url': 'https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo=1537041&numSequencia=1061747&numVersao=2',
    'file': 'raw/primary-research/cvm-documents/1061747-v2-60c0a5f7e47765b175d2268e81514c8058eaf269ba7a4fd6082e8aaa249e0205.pdf',
    'sha256': '60c0a5f7e47765b175d2268e81514c8058eaf269ba7a4fd6082e8aaa249e0205',
    'index_file': 'raw/primary-research/cvm-documents/index-2026-d2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip',
    'index_sha256': 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _day(value):
    if type(value) is date:
        return value
    _require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'invalid_research_date')
    return date.fromisoformat(value)


def _path(folder, relative):
    root = Path(folder).resolve()
    _require(isinstance(relative, str) and '\\' not in relative
             and not PurePosixPath(relative).is_absolute() and ':' not in relative
             and '..' not in relative.split('/'), 'unsafe_price_proof_path')
    path = root / relative
    _require(path.resolve().is_relative_to(root)
             and not any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)),
             'unsafe_price_proof_path')
    return path


def _read(folder, relative, digest=None):
    path = _path(folder, relative)
    _require(path.stat().st_size <= 25_000_000, 'oversized_price_proof')
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(digest is None or actual == digest, 'unreviewed_price_source_hash')
    return raw, {'file': relative, 'sha256': actual, 'bytes': len(raw),
                 'access': 'existing_cache_read_only'}


def _dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')


def _brday(value):
    _require(isinstance(value, str) and re.fullmatch(r'\d{2}/\d{2}/\d{4}', value),
             'reference_date_unparseable')
    return datetime.strptime(value, '%d/%m/%Y').date()


def _reference_view(rows, share, start, cutoff):
    """Typed view: an unknown date cannot name an independent price observation.

    Keep every cash date/amount and the complete original unknown-date row. An
    unknown reference attached to an in-window right still blocks recovery.
    Never move a reference to the record date or choose a conflicting quote.
    """
    from .collect_market_data import normalize

    copied, corrections = deepcopy(rows), []
    for index, row in enumerate(copied):
        if (row.get('typeStock', '').strip() != share
                or normalize(row.get('corporateAction', '')) not in ('dividendo', 'jrs cap proprio')):
            continue
        if row.get('dateClosingPricePriorExDate') != '31/12/9999':
            continue
        record = _brday(row['lastDatePriorEx'])
        _require(record < start, 'unknown_reference_date_for_required_cash_right')
        corrections.append({'row_index': index, 'kind': 'unavailable_reference_date',
            'original_row': deepcopy(row), 'reference_date': None,
            'why': '9999-12-31 is outside the observation cutoff; distinct old rights do not share a dated quote',
            'cash_date_and_amount_preserved': True})
        row['closingPricePriorExDate'] = None
    return copied, corrections


def _fca_identity(raw, ticker, review, cutoff):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        def table(name):
            _require(archive.namelist().count(name) == 1
                     and archive.getinfo(name).file_size < 10_000_000, 'unsafe_FCA_table')
            return list(csv.DictReader(io.StringIO(archive.read(name).decode('latin1')), delimiter=';'))
        masters = table('fca_cia_aberta_2026.csv')
        securities = table('fca_cia_aberta_valor_mobiliario_2026.csv')
    matches = []
    for security in securities:
        if security['Codigo_Negociacao'].strip() != ticker:
            continue
        parents = [m for m in masters if m['ID_DOC'] == security['ID_Documento']]
        _require(len(parents) == 1, 'FCA_identity_filing_not_unique')
        parent = parents[0]
        _require(int(parent['CD_CVM']) == review['cvm']
                 and re.sub(r'\D', '', parent['CNPJ_CIA']) == review['cnpj']
                 and security['CNPJ_Companhia'] == parent['CNPJ_CIA']
                 and security['Versao'] == parent['VERSAO']
                 and security['Data_Referencia'] == parent['DT_REFER']
                 and _day(parent['DT_RECEB']) <= cutoff, 'FCA_identity_mismatch')
        from .security_registration import _kind
        kind, share = _kind(security)
        _require(kind in ('ordinary_share', 'preferred_share') and share == review['share_class'],
                 'FCA_exact_share_class_mismatch')
        matches.append({'master': parent, 'security': security})
    _require(bool(matches), 'FCA_exact_code_missing')
    return matches


def _verify_cgas_crosswalk(folder, rows, receipts):
    """Exact native CVM ISIN amounts bind PNA rows to the sole FCA preferred code."""
    from .cvm_proventos_documents import parse_pdf

    doc = CGAS_DOCUMENT
    raw, receipt = _read(folder, doc['file'], doc['sha256'])
    index_raw, index_receipt = _read(folder, doc['index_file'], doc['index_sha256'])
    with zipfile.ZipFile(io.BytesIO(index_raw)) as archive:
        index = csv.DictReader(io.StringIO(archive.read('ipe_cia_aberta_2026.csv').decode('latin1')), delimiter=';')
        selected = [r for r in index if r['Link_Download'] == doc['url']]
    _require(len(selected) == 1 and int(selected[0]['Codigo_CVM']) == 15636
             and re.sub(r'\D', '', selected[0]['CNPJ_Companhia']) == REVIEWS['CGAS5']['cnpj']
             and selected[0]['Versao'] == '2' and selected[0]['Data_Entrega'] == doc['published_at'],
             'CGAS_primary_index_identity_mismatch')
    parsed = parse_pdf(raw, doc, CUTOFF)
    events = [e for e in parsed['events'] if e['isin'] == REVIEWS['CGAS5']['isin']]
    _require(parsed['status'] == 'available' and not parsed['issues'] and len(events) == 2
             and {e['gross_per_unit_decimal'] for e in events} == {'1.86872579607', '2.51871737731'}
             and all(e['record_date'] == '2026-06-15' for e in events), 'CGAS_primary_ISIN_proof_failed')
    independent = [r for r in rows if r['typeStock'] == 'PNA' and r['lastDatePriorEx'] == '15/06/2026']
    values = {Decimal(r['valueCash'].replace(',', '.')) for r in independent}
    _require(values == {Decimal(e['gross_per_unit_decimal']) for e in events},
             'CGAS_source_class_amount_proof_failed')
    receipts.extend([receipt | {'url': doc['url']}, index_receipt])
    return {'basis': 'exact_FCA_code_B3_detail_ISIN_native_CVM_ISIN_and_amounts',
            'native_CVM_events': events, 'independent_PNA_rows': independent,
            'annual_financial_share_class_retained': 'PN', 'reference_class_only': 'PNA'}


def reviewed_inputs(folder, identity, cutoff=CUTOFF):
    """Return verified in-memory rows and original daily bytes for three codes.

    Parent can feed ``cash_rows`` consistently to its existing cash/price
    functions, retaining the FCA class for financial_features. Each view change
    is recorded with the original row; all original source bytes remain intact.
    The returned prices come from the unchanged share_basis gate.
    """
    from .broad_stocks import split_price_history
    from .share_basis import b3_evidence, reconcile_prices

    _require(_day(cutoff) == CUTOFF, 'unreviewed_price_cutoff')
    ticker = identity.get('ticker')
    _require(ticker in REVIEWS, 'code_outside_reviewed_price_recovery')
    review = REVIEWS[ticker]
    _require(type(identity.get('cvm')) is int
             and all(identity.get(k) == review[k] for k in ('cvm', 'isin', 'share_class')),
             'reviewed_price_instrument_identity_mismatch')
    if identity.get('cnpj') is not None:
        _require(re.sub(r'\D', '', identity['cnpj']) == review['cnpj'], 'reviewed_price_CNPJ_mismatch')
    bodies, receipts = {}, []
    for relative, digest in review['files'].items():
        bodies[relative], receipt = _read(folder, relative, digest)
        receipts.append(receipt)
    fca_raw, receipt = _read(folder, FCA, FCA_HASH)
    receipts.append(receipt)
    fca_proof = _fca_identity(fca_raw, ticker, review, CUTOFF)
    detail = json.loads(bodies[f'raw/expanded/b3-detail-{review["cvm"]}.json'])
    _require(int(detail['codeCVM']) == review['cvm'] and detail['cnpj'] == review['cnpj']
             and [r['isin'] for r in detail['otherCodes'] if r['code'] == ticker] == [review['isin']],
             'reviewed_price_detail_identity_mismatch')
    pages = [(name, json.loads(raw)) for name, raw in bodies.items() if '-b3-cash-' in name]
    total = pages[0][1]['page']['totalRecords']
    _require(len(pages) == pages[0][1]['page']['totalPages'], 'reviewed_cash_page_missing')
    original_rows = []
    for page, (_, data) in enumerate(pages, 1):
        _require(data['page']['pageNumber'] == page and data['page']['totalPages'] == len(pages)
                 and data['page']['totalRecords'] == total, 'reviewed_cash_pagination_mismatch')
        original_rows.extend(data['results'])
    _require(len(original_rows) == total, 'reviewed_cash_record_count_mismatch')
    rows, corrections = _reference_view(original_rows, review['reference_share_class'], START, CUTOFF)
    class_proof = None
    if ticker == 'CGAS5':
        class_proof = _verify_cgas_crosswalk(folder, original_rows, receipts)
        # Only the reviewed source label changes; financials keep the FCA class.
        for index, row in enumerate(rows):
            if row['typeStock'] == 'PNA':
                corrections.append({'row_index': index, 'kind': 'exact_ISIN_class_label',
                    'original_row': deepcopy(original_rows[index]), 'source_class': 'PNA',
                    'canonical_class': 'PN', 'isin': review['isin'], 'amount_and_dates_preserved': True})
                row['typeStock'] = 'PN'
    cash, quotes = b3_evidence(rows, review['share_class'])
    price_file = next(name for name in bodies if name.endswith('-prices.json') or '/daily-prices/' in name)
    price_content = bodies[price_file]
    _, actions = split_price_history(price_content, ticker, CUTOFF, allow_recent_actions=True)
    prices, matches, mode = reconcile_prices(price_content, ticker, actions, quotes, CUTOFF)
    return {'ticker': ticker, 'cash_rows': rows, 'original_cash_rows': original_rows,
            'cash_amounts': cash, 'reference_quotes': quotes, 'price_content': price_content,
            'prices': prices, 'actions': actions, 'price_matches': matches, 'price_mode': mode,
            'corrections': corrections, 'receipts': receipts, 'fca_proof': fca_proof,
            'class_proof': class_proof, 'price_file': price_file,
            'cash_files': [name for name, _ in pages]}


def replay_candidate(folder, identity, inventory=None):
    """Replay the complete existing source builder with a private cache view.

    No monkeypatching or parent edits. Raw reads and their receipts retain the
    pinned bytes; only this cache instance's parsed cash pages use the view.
    A source candidate is not an activated or financially approved model asset.
    """
    from .inventory_candidates import _Cache, _one
    from .market_inventory import load_inventory

    proof = reviewed_inputs(folder, identity)
    inventory = inventory or load_inventory(folder)
    selected = [r for r in inventory['stocks'] if r['ticker'] == identity['ticker']]
    _require(len(selected) == 1 and all(selected[0].get(k) == identity[k]
             for k in ('cvm', 'share_class')), 'replay_inventory_identity_mismatch')
    candidate_row = {**selected[0], 'isin': identity['isin']}
    cache = _Cache(folder, inventory)
    offset = 0
    for name in proof['cash_files']:
        data = cache.json(name, 'independent_cash')
        size = len(data['results'])
        cache.parsed[name] = {**data, 'results': deepcopy(proof['cash_rows'][offset:offset + size])}
        offset += size
    checks = {}
    try:
        candidate = _one(candidate_row, cache, CUTOFF, checks)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        audit = {'source_candidate_recovered': False, 'remaining_reason': str(error), 'checks': checks}
        if str(error) == 'B3_cash_event_missing_from_payment_table':
            from .inventory_candidates import _payments
            from .cash_calendar_research import apply_research_calendar
            primary = cache.primary.primary_events(candidate_row, CUTOFF) if cache.primary else {}
            original, _ = _payments(identity['ticker'], cache, CUTOFF, primary)
            original = apply_research_calendar(candidate_row, cache, CUTOFF, original,
                                                proof['cash_rows'], proof['actions'])
            calendar = {date.fromisoformat(r['record_date']) for r in original.installments}
            independent = set(proof['cash_amounts'])
            required = {d for d in independent if START <= d <= CUTOFF}
            required.update(date.fromisoformat(r['record_date']) for r in original.installments
                            if START <= date.fromisoformat(r['payment_date']) <= CUTOFF)
            audit['missing_calendar_record_dates'] = sorted(str(d) for d in required - calendar)
            audit['calendar_records_missing_independent_cash'] = sorted(str(d) for d in required - independent)
        audit['read_receipts'] = list(cache.receipts.values())
        return None, audit
    return candidate, {'source_candidate_recovered': True, 'remaining_reason': None,
                       'checks': checks, 'read_receipts': list(cache.receipts.values()),
                       'ledger_rows': len(candidate['ledger']),
                       'independent_cash_matches': len(candidate['independent_cash_matches']),
                       'independent_price_matches': len(candidate['independent_price_matches'])}


def _daily_diagnostic(raw, ticker, rows, share):
    """List every mismatch and missing regime without repairing any price."""
    from .collect_market_data import month_end, shift_month
    from .broad_stocks import split_price_history
    from .share_basis import b3_evidence, factor_after, reconcile_prices

    r = json.loads(raw)['chart']['result'][0]
    _require(r['meta']['symbol'] == ticker + '.SA' and r['meta']['currency'] == 'BRL'
             and r['meta'].get('dataGranularity') == '1d', 'diagnostic_daily_identity_mismatch')
    stamps, closes = r['timestamp'], r['indicators']['quote'][0]['close']
    _require(len(stamps) == len(closes), 'price_length_mismatch')
    days = [datetime.fromtimestamp(s, timezone.utc).date() for s in stamps]
    _require(len(set(days)) == len(days), 'duplicate_daily_price_dates')
    daily = {d: float(v) for d, v in zip(days, closes) if v is not None and d <= CUTOFF}
    _require(all(math.isfinite(v) and v > 0 for v in daily.values()), 'invalid_stock_price')
    actions = [{'date': datetime.fromtimestamp(e['date'], timezone.utc).date().isoformat(),
                'ratio': float(e['numerator']) / float(e['denominator'])}
               for e in r.get('events', {}).get('splits', {}).values()]
    clean, corrections = _reference_view(rows, share, START, CUTOFF)
    _, quotes = b3_evidence(clean, share)
    quotes = {d: q for d, q in quotes.items() if START <= d <= CUTOFF}
    anchors = []
    for d, q in sorted(quotes.items()):
        factor = factor_after(d, actions, CUTOFF)
        value = daily.get(d)
        anchors.append({'date': d.isoformat(), 'independent_raw_quote': q,
            'original_daily_close': value, 'factor_after': factor,
            'raw_match': value is not None and math.isclose(value, q, rel_tol=.006, abs_tol=.015),
            'split_adjusted_match': value is not None and math.isclose(value, q / factor, rel_tol=.006, abs_tol=.015)})
    months = {}
    for d, value in sorted(daily.items()):
        months[month_end(d.year, d.month)] = (d, value)
    required = [shift_month(CUTOFF, -i) for i in reversed(range(61))]
    observed = {round(factor_after(d, actions, CUTOFF), 12) for d in quotes}
    needed = {round(factor_after(months[m][0], actions, CUTOFF), 12) for m in required if m in months}
    out = {'share_actions_reported_by_original_Yahoo': actions, 'anchors': anchors,
        'valid_independent_quote_dates': len(anchors), 'unavailable_date_rows': corrections,
        'missing_months': [m.isoformat() for m in required if m not in months],
        'missing_independently_observed_share_factors': sorted(needed - observed),
        'last_observed_price_date': max(daily).isoformat() if daily else None}
    modes = [mode for mode in ('raw', 'split_adjusted')
             if anchors and all(r[mode + '_match'] for r in anchors)]
    extremes = []
    if modes:
        mode = modes[-1]
        monthly = sorted(months)
        for previous, current in zip(monthly, monthly[1:]):
            if current not in required:
                continue
            prev_day, before = months[previous]
            curr_day, after = months[current]
            if mode == 'raw':
                before /= factor_after(prev_day, actions, CUTOFF)
                after /= factor_after(curr_day, actions, CUTOFF)
            ratio = after / before
            if not .2 < ratio < 3:
                extremes.append({'previous_observed_on': str(prev_day), 'current_observed_on': str(curr_day),
                                 'previous_close_in_cutoff_units': before,
                                 'current_close_in_cutoff_units': after, 'ratio': ratio})
    out['unexplained_extreme_monthly_changes'] = extremes
    try:
        split_price_history(raw, ticker, CUTOFF, allow_recent_actions=True)
        prices, matches, mode = reconcile_prices(raw, ticker, actions, quotes, CUTOFF)
        out.update(price_history_reproduced=True, mode=mode, independent_matches=len(matches))
        out['required_monthly_prices'] = [dict(month=m.isoformat(), observed_on=prices[m][0].isoformat(),
                                              close_in_cutoff_units=prices[m][2]) for m in required]
    except ValueError as error:
        out.update(price_history_reproduced=False, remaining_reason=str(error))
    return out


def build_manifest(folder):
    from .inventory_candidates import _Cache, _cash_pages
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    from .collect_market_data import financial_features, shift_month, stock_observations

    snapshots, receipts = {}, []
    for name, digest in SNAPSHOT_PINS.items():
        raw, receipt = _read(folder, name, digest)
        snapshots[Path(name).name] = json.loads(raw)
        receipts.append(receipt)
    pointer_raw, pointer_receipt = _read(folder, 'current.json')
    pointer = json.loads(pointer_raw)
    receipts.append(pointer_receipt)
    inv = load_inventory(folder)
    inventory_pointer, receipt = _read(folder, 'inventory/current.json')
    receipts.append(receipt)
    inventory_ref = json.loads(inventory_pointer)
    _, receipt = _read(folder, 'inventory/' + inventory_ref['snapshot'] + '/market.json', inventory_ref['sha256'])
    receipts.append(receipt)
    cache = _Cache(folder, inv)
    pending = [r for r in snapshots['candidate-source-audit.json']['pending'] if r['reason'] in REASONS]
    prepared = {r['ticker']: r for r in snapshots['stock-universe-preparation.json']['stocks']}
    records, candidates = [], []
    for pending_row in pending:
        ticker = pending_row['ticker']
        row = next(r for r in inv['stocks'] if r['ticker'] == ticker)
        record = {'ticker': ticker, 'cvm': row.get('cvm'), 'isin': row.get('isin'),
            'share_class': row.get('share_class'), 'cutoff': CUTOFF.isoformat(), 'window_start': START.isoformat(),
            'original_pending_reason': pending_row['reason'], 'source_candidate_recovered': False,
            'model_admission_granted': False, 'sources': [],
            'baseline_preparation_reasons': prepared[ticker]['reason_codes']}
        try:
            detail_file = f'raw/expanded/b3-detail-{row["cvm"]}.json'
            codes = {}
            if _path(folder, detail_file).is_file():
                body, receipt = _read(folder, detail_file)
                record['sources'].append(receipt)
                detail = json.loads(body)
                _require(int(detail['codeCVM']) == row['cvm'], 'diagnostic_issuer_mismatch')
                codes = {r['code']: r['isin'] for r in detail['otherCodes']}
                record['isin'] = codes.get(ticker, row.get('isin'))
            price_file = f'raw/expanded/{ticker}-prices.json'
            if not _path(folder, price_file).is_file():
                price_file = f'raw/inventory/daily-prices/{ticker}.json'
            raw, receipt = _read(folder, price_file)
            record['sources'].append(receipt)
            try:
                rows = _cash_pages(ticker, codes, row['cvm'], cache)
            except ValueError:
                rows = []
            share = 'UNT' if ticker.endswith('11') else row['share_class']
            record['diagnostic'] = _daily_diagnostic(raw, ticker, rows, share)
            record['remaining_reason'] = record['diagnostic'].get('remaining_reason')
            if ticker in REVIEWS:
                identity = {**row, 'isin': record['isin']}
                proof = reviewed_inputs(folder, identity)
                record.update(correction=REVIEWS[ticker]['correction'], corrections=proof['corrections'],
                    fca_proof=proof['fca_proof'], class_proof=proof['class_proof'],
                    reference_source_class=REVIEWS[ticker]['reference_share_class'],
                    price_history_reproduced=True, price_mode=proof['price_mode'],
                    independent_price_matches=len(proof['price_matches']))
                record['sources'].extend(proof['receipts'])
                record['recovered_required_prices'] = [
                    {'month': shift_month(CUTOFF, -i).isoformat(),
                     'observed_on': proof['prices'][shift_month(CUTOFF, -i)][0].isoformat(),
                     'close_in_cutoff_units': proof['prices'][shift_month(CUTOFF, -i)][2]}
                    for i in reversed(range(61))]
                candidate, replay = replay_candidate(folder, identity, inv)
                record['source_replay'] = replay
                record['source_candidate_recovered'] = candidate is not None
                record['remaining_reason'] = replay['remaining_reason']
                if candidate is not None:
                    record['recovered_payment_ledger'] = candidate['ledger']
                    candidates.append((identity, candidate, record))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            record['remaining_reason'] = str(error)
        records.append(record)
    if candidates:
        statements, financial_audit, financial_sources = prepare_financials(folder, [r for r, _, _ in candidates], CUTOFF)
        receipts.extend(financial_sources)
        for identity, candidate, record in candidates:
            spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
            current = financial_features(statements, candidate['payments'], CUTOFF, spec)
            basis = _day(candidate['financial_basis_start'])
            valid = current is not None and _day(current[1]['fiscal_end']) >= basis
            training, _ = stock_observations(identity['ticker'], candidate['prices'], candidate['payments'], statements, CUTOFF, spec=spec)
            training = [r for r in training if _day(r['as_of']) >= candidate['training_since'] and _day(r['fiscal_end']) >= basis]
            record['financial_replay'] = {'valid': valid, 'exact_spec': list(spec),
                'annual_feature_source': current[1] if current else None,
                'training_observations': len(training), 'financial_basis_start': str(basis)}
            record['financial_audit'] = [r for r in financial_audit if r.get('cvm') == candidate['cvm']]
    # Shared cache receipts are also referenced by file in each diagnostic case.
    for record in records:
        cvm = record['cvm']
        related = cache.issuer_codes[cvm]
        record['sources'].extend(r for name, r in cache.receipts.items()
            if any(name.startswith(f'raw/expanded/{t}-b3-cash-') for t in related))
        record['sources'] = list({r['file']: r for r in record['sources']}.values())
    modules = ('price_basis_research.py', 'share_basis.py', 'inventory_candidates.py', 'broad_stocks.py',
               'cash_calendar_research.py', 'payment_sources.py', 'collect_market_data.py',
               'inventory_financials.py', 'cvm_proventos_documents.py')
    code_hashes = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in modules}
    return {'schema_version': 1, 'scope': 'isolated_reviewed_price_basis_recovery',
        'baseline_snapshot': SNAPSHOT, 'observed_current_snapshot': pointer['snapshot'],
        'cutoff': CUTOFF.isoformat(), 'window_start': START.isoformat(), 'records': records,
        'baseline_sources': receipts, 'producer_module_sha256': code_hashes,
        'network_requests': 0, 'B3_network_requests': 0,
        'thresholds_changed': False, 'raw_cache_modified': False, 'prices_imputed': False,
        'adjusted_close_used': False, 'dataset_activated': False, 'model_admissions': 0,
        'source_candidates_recovered': [r['ticker'] for r in records if r['source_candidate_recovered']],
        'integration': 'Use reviewed_inputs cash_rows in every cash-evidence consumer before b3_evidence; retain FCA class for annual financials. Never apply only the quote fix while another consumer reparses original sentinel rows. Re-run all parent admission checks.',
        'limitations': 'Retrospective source recovery; current purchase status, point-in-time availability and cash settlement are not established.'}


def write_manifest(folder):
    report = build_manifest(folder)
    raw = _dump(report)
    relative = f'{CACHE}/review-{CUTOFF.isoformat()}-{hashlib.sha256(raw).hexdigest()}.json'
    path = _path(folder, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == raw, 'immutable_price_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
    return path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.report:
        path, report = write_manifest(args.folder)
        print(json.dumps({'manifest': str(path), 'reviewed_codes': len(report['records']),
                          'source_candidates_recovered': report['source_candidates_recovered'],
                          'network_requests': 0}))


if __name__ == '__main__':
    main()
