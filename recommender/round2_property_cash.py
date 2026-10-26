"""Isolated property/education cash research. No production dispatch or publishing."""

from collections import defaultdict
from copy import deepcopy
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import re
import zipfile
from unittest.mock import patch
from urllib.parse import urljoin

from lxml import html

from . import cash_repair_proofs as proof
from . import pending_missing_b as widgets
from .bulk_pending_sources import PublicSourceCollector
from .round2_native_prices import read, receipt_source


CUTOFF = date(2026, 9, 30)
SCOPE = 'round2-property'
TARGETS = {
    'CURY3': (25100, 'BRCURYACNOR3', '08.797.760/0001-83',
              'a52d05d92fcf822ec8056f872111a54aff20fda6acc553007a66d53478d516a2'),
    'LAVV3': (25062, 'BRLAVVACNOR4', '26.462.693/0001-28',
              '98ef04322fdeafd14bdf25ce6e020acd1edc5abd86e767aa6fb2def1ea471672'),
    'MDNE3': (21067, 'BRMDNEACNOR9', '12.049.631/0001-84',
              '2e2b89128c9f2ef6b3210b544ea676a3d35d085d68c487086927d065f0ba65b6'),
    'MELK3': (25119, 'BRMELKACNOR8', '12.181.987/0001-77',
              'a3621f78b613cbc67f711cf7a756cc14dbe6e3610927e890dc3bfff0839ac79c'),
    'VITT3': (25763, 'BRVITTACNOR4', '45.365.558/0001-09',
              'c62eb79b386c4e9b97965f7c68cf1abfedc09ae9957f743595536fa77399b3be'),
    'CSED3': (25526, 'BRCSEDACNOR9', '62.984.091/0001-02',
              'c2478bf688890109c6ee7eb5e691d013c676c8ff826a2ddff796c2cc80cb06e8'),
}
SCRIPT_URL = 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js'
CSED_PAGE = 'https://ri.cruzeirodosuleducacional.com.br/pt'
CSED_COMPONENT = ('https://ri.cruzeirodosuleducacional.com.br/'
                  '_astro/csed-components-BELswIZM.Bhz-tWYi.js')
PACKET_SHA = '85e1324c488f2f82f6ac384f5e45f8f231db3b86aee301e9dacaab118c8c3807'
PAYMENT_SHA = '3bf6a8d708d479e9e206eef538f3908885d61689170c306560f6c5bd6416db3b'
REPAIR_ID = 'CSED3_full_dividend_payment_not_initial_deadline_v1'
SECTORS = {ticker: 1 if ticker == 'VITT3' else 4 for ticker in TARGETS}


def identity(ticker):
    code, isin, _, _ = TARGETS[ticker]
    return {'ticker': ticker, 'cvm': code, 'share_class': 'ON', 'isin': isin}


def compact(value):
    return ''.join(c.lower() for c in proof.text(value) if c.isalnum())


def csed_widget_request(page, component):
    tree = html.fromstring(page)
    stores = tree.xpath('//astro-island[contains(@component-url,"store.")]')
    proof.require(len(stores) == 1, 'property_CSED_unique_public_store_required')
    props = json.loads(stores[0].get('props'))
    proof.require(props.get('apiBaseUrl') == [0, 'https://api.solutions-ir.com'],
                  'property_CSED_public_API_base_changed')
    script = component.decode('utf-8')
    markers = ('getHistoricalInterday(t,i,a,o=!1)', '/v2/mktdata/candles/interday',
        '?ticker=${t}&start_date=${i}&end_date=${a}&rmv_after_market=${o}',
        'a("CSED3",_h(V),_h(Q))', 'return`${t}-${i}-${a}`', 'oe.close_price',
        'quote-history_${o}_${d}.csv')
    proof.require(all(marker in script for marker in markers),
                  'property_CSED_literal_history_template_or_price_field_changed')
    return ('https://api.solutions-ir.com/v2/mktdata/candles/interday'
        '?ticker=CSED3&start_date=2021-09-01&end_date=2026-09-30&rmv_after_market=false'), {
        'kind': 'issuer_published_SolutionsIR_history_template', 'ticker': 'CSED3',
        'identity': identity('CSED3'), 'page_sha256': hashlib.sha256(page).hexdigest(),
        'component_sha256': hashlib.sha256(component).hexdigest(), 'template_markers': list(markers),
        'price_field_rendered_and_exported_by_issuer': 'close_price',
        'rmv_after_market_is_not_a_share_adjustment_parameter': True,
        'share_adjustment_status_not_assumed': True}


def parse_calendar(body, ticker):
    """Keep native roles/quantities. A JCP value is never silently called gross."""
    proof.require(ticker in TARGETS, 'property_unsupported_security')
    tree = html.fromstring(body)
    labels = {n.text_content().strip() for n in tree.xpath('//p/strong')
              if re.fullmatch(r'[A-Z]{4}\d{1,2}', n.text_content().strip())}
    proof.require(labels == {ticker},
                  'property_provider_ticker_not_confirmed')
    proof.require('ultimodiacom' in compact(tree.text_content()), 'property_provider_com_date_role_missing')
    tables = [t for t in tree.xpath('//table') if t.xpath('.//tr') and
        [compact(c.text_content()) for c in t.xpath('.//tr')[0].xpath('./th|./td')] ==
        ['data', 'valor', 'tipo', 'datadepagamento', 'porquantasacoes']]
    proof.require(len(tables) == 1, 'property_unique_cash_table_required')
    events, seen = [], set()
    for number, tr in enumerate(tables[0].xpath('.//tr')[1:], 1):
        fields = [' '.join(c.text_content().split()) for c in tr.xpath('./td')]
        proof.require(len(fields) == 5, 'property_cash_row_shape_changed')
        proof.require(fields[2] in ('DIVIDENDO', 'JRS CAP PROPRIO'),
                      'property_non_income_distribution_requires_primary_review')
        try:
            record = datetime.strptime(fields[0], '%d/%m/%Y').date()
            payment = datetime.strptime(fields[3], '%d/%m/%Y').date()
            amount, quantity = [Decimal(v.replace('.', '').replace(',', '.')) for v in (fields[1], fields[4])]
        except (ValueError, InvalidOperation) as error:
            raise proof.ProofError('property_cash_date_or_quantity_unresolved') from error
        proof.require(amount.is_finite() and quantity.is_finite() and amount > 0 and quantity > 0
                      and record <= payment, 'property_cash_unit_or_date_invalid')
        key = tuple(fields)
        proof.require(key not in seen, 'property_duplicate_cash_row')
        seen.add(key)
        dividend = fields[2] == 'DIVIDENDO'
        events.append({'record_date': record.isoformat(), 'payment_date': payment.isoformat(),
            'reported_amount_decimal': str(amount), 'quoted_quantity_decimal': str(quantity),
            'reported_per_share_decimal': str(amount / quantity),
            'gross_per_share_decimal': str(amount / quantity) if dividend else None,
            'label': 'DIVIDENDO' if dividend else 'JCP', 'source_row': number,
            'original_reported_fields': fields,
            'amount_tax_basis': 'provider_dividend_not_JCP' if dividend else 'JCP_gross_or_net_unresolved',
            'published_at': None, 'settlement_verified': False})
    proof.require(bool(events), 'property_empty_cash_table_not_zero_income_proof')
    return events


def cached_receipts(folder):
    root = Path(folder).resolve()
    records = []
    for path in sorted((root / 'raw/primary-research/all-pending').glob('*/response-*.json')):
        proof.require(not path.is_symlink(), 'property_unsafe_receipt')
        record = json.loads(path.read_bytes())
        records.append(record)
    return records


def source_by_url(folder, url, digest=None):
    records = [r for r in cached_receipts(folder) if r.get('url') == url
               and (digest is None or r.get('sha256') == digest)]
    proof.require(bool(records), 'property_actual_receipt_missing:' + url)
    proof.require(len({r['sha256'] for r in records}) == 1, 'property_source_versions_require_review')
    record = records[0]
    body, source = receipt_source(Path(folder).resolve(), record, record['sha256'])
    return body, source, record


def save_report(path, payload):
    path = Path(path)
    workspace = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
    proof.require(path.name.startswith('property-') and path.parent.resolve() == workspace.resolve()
                  and not path.is_symlink(), 'property_report_scope_required')
    body = json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False,
                      default=lambda value: value.isoformat() if isinstance(value, date) else str(value)).encode()
    if path.exists():
        proof.require(path.read_bytes() == body, 'property_immutable_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return hashlib.sha256(body).hexdigest()


def seal_packet(folder):
    root = Path(folder).resolve()
    records = {ticker: source_by_url(root,
        'https://www.fundamentus.com.br/proventos.php?papel=' + ticker)[2] for ticker in TARGETS}
    attachments = [r for r in cached_receipts(root) if r.get('scope') == SCOPE]
    for url in (CSED_PAGE, SCRIPT_URL,
                'https://ri.lavvi.com.br/servicos-aos-investidores/cotacoes-e-graficos/',
                'https://ri.melnick.com.br/graficos-e-cotacoes'):
        attachments.append(source_by_url(root, url)[2])
    for receipt in attachments:
        if receipt['status'] == 200 and not receipt['error']:
            receipt_source(root, receipt, receipt['sha256'])
        else:
            read(root, receipt['file'], receipt['sha256'], receipt['bytes'])
    relative = 'raw/inventory/daily-prices/CSED3.json'
    comparison = (root / relative).read_bytes()
    packet = {'schema_version': 1, 'scope': SCOPE, 'cutoff': CUTOFF.isoformat(),
        'identities': {t: identity(t) for t in TARGETS}, 'cash_receipts': records,
        'attachments': attachments, 'model_admission': False,
        'comparison_price_source': {'file': relative, 'sha256': hashlib.sha256(comparison).hexdigest(),
            'bytes': len(comparison), 'url': None, 'retrieved_at': None,
            'authority': 'existing_Yahoo_comparison_cache_not_new_primary'},
        'derived_manifest_not_primary_document': True, 'source_bytes_modified': False}
    body = json.dumps(packet, ensure_ascii=True, indent=2, allow_nan=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    proof.require(PACKET_SHA is None or digest == PACKET_SHA, 'property_pinned_packet_cannot_be_resealed_changed')
    path = root / f'raw/primary-research/all-pending/{SCOPE}/property-evidence-{digest}.json'
    if path.exists():
        proof.require(path.read_bytes() == body, 'property_packet_immutable_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return {'file': path.relative_to(root).as_posix(), 'sha256': digest, 'bytes': len(body)}


def packet(folder, requested, cutoff):
    ticker = requested.get('ticker')
    proof.require(ticker in TARGETS and {k: requested.get(k) for k in identity(ticker)} == identity(ticker)
                  and requested.get('sector_code', SECTORS[ticker]) == SECTORS[ticker]
                  and cutoff == CUTOFF, 'property_exact_security_sector_or_cutoff_required')
    proof.require(isinstance(PACKET_SHA, str), 'property_packet_not_pinned')
    root = Path(folder).resolve()
    relative = f'raw/primary-research/all-pending/{SCOPE}/property-evidence-{PACKET_SHA}.json'
    body = read(root, relative, PACKET_SHA)
    data = json.loads(body)
    proof.require(data['identities'] == {t: identity(t) for t in TARGETS}
                  and data['cutoff'] == cutoff.isoformat(), 'property_packet_contract_changed')
    return root, data, {'file': relative, 'sha256': PACKET_SHA, 'bytes': len(body),
        'url': None, 'retrieved_at': None, 'authority': 'derived_immutable_source_binding_not_primary'}


def bound_source(root, record, authority):
    body, source = receipt_source(root, record, record['sha256'])
    parent = Path(record['file']).parent
    relative = (parent / ('response-' + hashlib.sha256(record['url'].encode()).hexdigest() + '.json')).as_posix()
    path = root / relative
    proof.require(not path.is_symlink() and path.resolve().is_relative_to(root), 'property_unsafe_transport_receipt')
    metadata = path.read_bytes()
    proof.require(json.loads(metadata) == record, 'property_actual_transport_receipt_changed')
    source.update(authority=authority, settlement_verified=False, point_in_time_archive_verified=False)
    return body, [source, {'file': relative, 'sha256': hashlib.sha256(metadata).hexdigest(),
        'bytes': len(metadata), 'url': None, 'retrieved_at': None,
        'authority': 'actual_HTTP_transport_receipt_not_primary_content'}]


def attachment(data, predicate):
    records = [r for r in data['attachments'] if predicate(r)]
    proof.require(len(records) == 1, 'property_one_exact_attachment_required')
    return records[0]


def load_cash_evidence(folder, requested, cutoff):
    root, data, seal = packet(folder, requested, cutoff)
    ticker = requested['ticker']
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    cache = _Cache(root, load_inventory(root))
    sources = proof.verified_identity(cache, requested, identity(ticker), TARGETS[ticker][3], TARGETS[ticker][2])
    receipt = data['cash_receipts'][ticker]
    proof.require(receipt['url'] == 'https://www.fundamentus.com.br/proventos.php?papel=' + ticker,
                  'property_cash_request_identity_changed')
    body, refs = bound_source(root, receipt, 'secondary_Fundamentus_reported_calendar_not_settlement')
    events = parse_calendar(body, ticker)
    proof.require(all(e['label'] == 'DIVIDENDO' and e['gross_per_share_decimal'] is not None for e in events),
                  'property_JCP_gross_or_net_requires_exact_primary_proof')
    expected = defaultdict(Decimal)
    for event in events:
        expected[date.fromisoformat(event['record_date'])] += Decimal(event['gross_per_share_decimal'])
    return {'events': events, 'expected_cash': {d: float(v) for d, v in expected.items()},
        'sources': proof.merge_sources(sources + [seal] + refs),
        'checks': {'cash_rows': len(events), 'all_DIVIDENDO': True,
            'first_entitlement_date': min(expected).isoformat(),
            'gross_reconstructed_from_net': False, 'provider_precision': 'four_reported_decimal_places',
            'source_history_complete_not_certified': True, 'ultimate_upstream_independence_verified': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False}}


def parse_csed_history(body, receipt):
    rows = json.loads(body)
    proof.require(isinstance(rows, list) and rows, 'property_CSED_history_list_required')
    monthly, dates = {}, set()
    for index, row in enumerate(rows):
        proof.require(isinstance(row, dict) and row.get('symbol') == 'CSED3', 'property_CSED_history_wrong_symbol')
        stamp = row.get('date', '')
        proof.require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T00:00:00', stamp)),
                      'property_CSED_native_date_required')
        day = date.fromisoformat(stamp[:10])
        proof.require('2021-09-01' <= day.isoformat() <= CUTOFF.isoformat()
                      and day not in dates, 'property_CSED_duplicate_or_outside_window')
        dates.add(day)
        proof.require(row.get('candle_time', '')[:10] == stamp[:10]
                      and row.get('close_time', '')[:10] == stamp[:10], 'property_CSED_date_roles_disagree')
        values = [row.get(k) for k in ('open_price', 'close_price', 'low_price', 'high_price')]
        proof.require(all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in values),
                      'property_CSED_native_positive_OHLC_required')
        opened, close, low, high = values
        proof.require(low <= min(opened, close) <= max(opened, close) <= high,
                      'property_CSED_OHLC_inconsistent')
        month = stamp[:7]
        if month not in monthly or stamp[:10] > monthly[month]['date']:
            monthly[month] = {'month': month, 'date': stamp[:10], 'close': close,
                             'locator': f'$[{index}].close_price'}
    expected = {f'{year:04d}-{month:02d}' for year in range(2021, 2027) for month in range(1, 13)
                if '2021-09' <= f'{year:04d}-{month:02d}' <= '2026-09'}
    proof.require(set(monthly) == expected, 'property_CSED_61_native_months_required')
    return {'monthly_prices': [monthly[m] for m in sorted(monthly)], 'daily_observations': len(rows),
        'monthly_observations': len(monthly), 'numeric_values_changed': False,
        'share_adjustment_basis': 'not_declared_by_API_do_not_assume_raw',
        'source_file': receipt['file'], 'source_sha256': receipt['sha256']}


def load_quote_observations(folder, requested, cutoff):
    root, data, seal = packet(folder, requested, cutoff)
    proof.require(requested['ticker'] == 'CSED3', 'property_native_quote_template_unavailable_or_access_stopped')
    page_record = attachment(data, lambda r: r['url'] == CSED_PAGE)
    component_record = attachment(data, lambda r: r['url'] == CSED_COMPONENT)
    page, page_refs = bound_source(root, page_record, 'issuer_published_public_widget_page')
    component, component_refs = bound_source(root, component_record, 'issuer_published_widget_source_code')
    url, discovery = csed_widget_request(page, component)
    quote_record = attachment(data, lambda r: r['url'] == url)
    expected_discovery = {**discovery, 'page': {k: page_record.get(k) for k in
        ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}, 'component': {k: component_record.get(k) for k in
        ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}}
    proof.require(quote_record['discovery'] == expected_discovery, 'property_CSED_literal_quote_request_binding_changed')
    body, refs = bound_source(root, quote_record, 'issuer_published_SolutionsIR_close_share_basis_unverified')
    result = parse_csed_history(body, quote_record)
    result['sources'] = proof.merge_sources([seal] + page_refs + component_refs + refs)
    return result


def load_evidence(folder, requested, cutoff):
    if requested.get('ticker') not in TARGETS:
        return None
    cash = load_cash_evidence(folder, requested, cutoff)
    from .collect_market_data import shift_month
    proof.require(any(day <= shift_month(cutoff, -60) for day in cash['expected_cash']),
                  'independent_cash_evidence_does_not_cover_window_start')
    observations = load_quote_observations(folder, requested, cutoff)
    quotes = {date.fromisoformat(r['date']): r['close'] for r in observations['monthly_prices']}
    root = Path(folder).resolve()
    from .inventory_candidates import split_price_history
    from .share_basis import reconcile_prices
    _, data, _ = packet(folder, requested, cutoff)
    source = data['comparison_price_source']
    comparison = read(root, source['file'], source['sha256'], source['bytes'])
    _, actions = split_price_history(comparison, requested['ticker'], cutoff, allow_recent_actions=True)
    # Do not call the issuer's display close a raw price until the ordinary
    # independent price/share-regime gate accepts all 61 native observations.
    reconcile_prices(comparison, requested['ticker'], actions, quotes, cutoff,
                     evidence_provider='Fundamentus_and_issuer_SolutionsIR')
    return {'provider': 'Fundamentus_and_issuer_SolutionsIR', 'expected_cash': cash['expected_cash'],
        'raw_quotes': quotes, 'sources': proof.merge_sources(cash['sources'] + observations['sources'] + [source]),
        'calendar': {'events': cash['events']}, 'checks': {**cash['checks'],
            'native_monthly_prices': 61, 'raw_price_basis_gate_passed': True,
            'no_adjustment_factor_guessed': True, 'synthetic_exchange_rows_created': False}}


def validate_primary_actions(folder, requested, cutoff, observed_actions):
    packet(folder, requested, cutoff)
    proof.require(all(date.fromisoformat(a['date']) < date(2021, 9, 1) for a in observed_actions),
                  'property_in_window_action_requires_exact_primary_proof')
    return {'sources': [], 'observed_pre_window_actions': deepcopy(observed_actions),
            'primary_action_absence_certified': False}


def load_csed_repair(folder, requested, cutoff, cache):
    root, data, seal = packet(folder, requested, cutoff)
    proof.require(requested['ticker'] == 'CSED3', 'property_CSED_repair_exact_security_required')
    sources = proof.verified_identity(cache, requested, identity('CSED3'), TARGETS['CSED3'][3], TARGETS['CSED3'][2])
    sources.append(seal)
    requirements = {
        '499436': ('2022-04-29', [
            'valor bruto de R$ 0,1309509891304993 por acao ordinaria',
            'posicao acionaria do dia 29 de abril de 2022',
            'ex-dividendos a partir de 02 de maio de 2022',
            'O pagamento dos dividendos sera realizado ate 28 de maio de 2022',
            'parcelas e datas a serem fixadas por deliberacao da Diretoria']),
        '509298': ('2022-05-24', [
            'em continuidade ao Aviso aos Acionistas divulgado em 29 de abril de 2022',
            'realizara, em 27 de maio de 2022, o pagamento integral dos dividendos',
            'valor bruto de R$ 0,1309509891304993 por acao ordinaria',
            'data-base de 29 de abril de 2022', 'exercicio social encerrado em 31 de dezembro de 2021']),
    }
    from pypdf import PdfReader
    for sequence, (reference, spans) in requirements.items():
        document, index = proof.index_document(cache, 2022, sequence, 1, 25526, TARGETS['CSED3'][2],
                                               reference, 'aviso aos acionistas')
        record = attachment(data, lambda r: r['url'] == document['url'])
        body, refs = bound_source(root, record, 'primary_CVM_issuer_declared_cash_schedule_not_settlement')
        proof.require(body.startswith(b'%PDF-'), 'property_CSED_primary_PDF_required')
        pdf = PdfReader(io.BytesIO(body), strict=True)
        proof.require(not pdf.is_encrypted and len(pdf.pages) == 2, 'property_CSED_notice_structure_changed')
        page = pdf.pages[0].extract_text()
        proof.require(TARGETS['CSED3'][2] in page and all(compact(s) in compact(page) for s in spans),
                      'property_CSED_literal_notice_roles_changed')
        refs[0]['source_locator'] = {'sequence': sequence, 'version': 1, 'page': 1,
                                    'literal_spans': spans, 'published_at': document['published_at']}
        sources.extend([index] + refs)
    sources = proof.merge_sources(sources)
    proof.register(cache, sources)
    fact = {**identity('CSED3'), 'record_date': '2022-04-29', 'approval_date': '2022-04-29',
        'ex_date': '2022-05-02', 'payment_date': '2022-05-27', 'label': 'DIVIDENDO',
        'gross_per_share_decimal': '0.1309509891304993', 'published_at': '2022-05-24',
        'source_locator_id': 'CSED3:CVM509298:v1:p1:full_payment_of_CVM499436_distribution',
        'source_locator': {'final_notice': '509298', 'initial_notice': '499436', 'page': 1}}
    return {'facts': [fact], 'sources': sources, 'contract': {
        'initial_payment_by_deadline_not_exact_payment_date': '2022-05-28',
        'final_full_payment_date': '2022-05-27', 'explicit_continuation_of_named_distribution': True,
        'whole_group_replaced': True, '2021_provider_row_not_invented_in_payment_calendar': True,
        'source_bytes_modified': False, 'settlement_verified': False}}


def apply_exact_repairs(parsed, requested, cutoff, cache):
    if requested.get('ticker') != 'CSED3':
        return parsed
    evidence = load_csed_repair(cache.folder, requested, cutoff, cache)
    return proof.apply_groups(parsed, requested, cutoff, evidence, repair_id=REPAIR_ID,
        payment_sha=PAYMENT_SHA, groups={'2022-04-29': {'expected_original': [
            {'label': 'DIVIDENDO', 'payment_date': '2022-05-28', 'gross_per_share': 0.13095,
             'source_row': 'table:2:row:8'}], 'facts': evidence['facts']}})


def replay(folder):
    """Exercise the real candidate and financial gates with only proposed hooks."""
    import socket
    from . import inventory_candidates as native
    from . import issuer_independent_evidence as dispatch
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    from .collect_market_data import financial_features, stock_observations
    root = Path(folder).resolve()
    inventory = load_inventory(root)
    selected = [s for s in inventory['stocks'] if s['ticker'] in TARGETS]
    proof.require(len(selected) == len(TARGETS), 'property_exact_six_inventory_rows_required')
    result = {'scope': SCOPE, 'cutoff': CUTOFF.isoformat(), 'results': [],
        'source_hooks_only_no_gate_patches': True, 'pointers_written': False,
        'production_core_changed': False, 'model_admission': False,
        'forecast_executed': False, 'own_training_rows_published': 0,
        'source_independence': 'distinct_declared_sources_ultimate_upstream_not_certified'}
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline replay')):
        statements, audit, financial_sources = prepare_financials(root, selected, CUTOFF)
        result.update(financial_audit=audit, financial_sources=financial_sources)
        for stock in selected:
            ticker = stock['ticker']
            cache, checks = native._Cache(root, inventory), {}
            row = {'ticker': ticker, 'ordinary_one_pass': False, 'financial_current_pass': False,
                   'training_rows': 0, 'admission_published': False}
            annual = financial_features(statements, {}, CUTOFF,
                (stock['cvm'], stock['share_class'], stock['sector_code']))
            row['annual_current_available_separately'] = annual is not None
            row['annual_current_filing'] = annual[1] if annual else None
            row['financial_candidate_audit'] = [a for a in audit if a.get('kind') == 'candidate'
                                                and a.get('ticker') == ticker]
            inspector = native.inspect_payment_page

            def repaired_inspector(body, code, cutoff, observed):
                parsed = inspector(body, code, cutoff, observed)
                return apply_exact_repairs(parsed, stock, cutoff, cache) if code == 'CSED3' else parsed

            try:
                with patch.object(dispatch, 'load_evidence', load_evidence), \
                        patch.object(dispatch, 'validate_primary_actions', validate_primary_actions), \
                        patch.object(native, 'inspect_payment_page', repaired_inspector):
                    candidate = native._one(stock, cache, CUTOFF, checks)
                row.update(ordinary_one_pass=True, prices=len(candidate['prices']),
                           payments=len(candidate['payments']))
                current = financial_features(statements, candidate['payments'], CUTOFF, candidate['statement_spec'])
                basis = date.fromisoformat(candidate['financial_basis_start'])
                row['financial_current_pass'] = current is not None and date.fromisoformat(current[1]['fiscal_end']) >= basis
                if row['financial_current_pass']:
                    observations, _ = stock_observations(ticker, candidate['prices'], candidate['payments'],
                        statements, CUTOFF, spec=candidate['statement_spec'])
                    row['training_rows'] = sum(date.fromisoformat(o['as_of']) >= candidate['training_since']
                        and date.fromisoformat(o['fiscal_end']) >= basis for o in observations)
            except (ValueError, KeyError, TypeError, IndexError, AssertionError) as error:
                row['ordinary_one_reason'] = str(error)
            row['checks'] = checks
            if ticker == 'CSED3':
                _, data, _ = packet(root, stock, CUTOFF)
                quotes = load_quote_observations(root, stock, CUTOFF)
                source = data['comparison_price_source']
                comparison = json.loads(read(root, source['file'], source['sha256'], source['bytes']))['chart']['result'][0]
                from datetime import timezone
                daily = {datetime.fromtimestamp(t, timezone.utc).date().isoformat(): v for t, v in
                    zip(comparison['timestamp'], comparison['indicators']['quote'][0]['close']) if v is not None}
                matches = [{'date': q['date'], 'issuer_close': q['close'], 'comparison_close': daily.get(q['date']),
                    'agrees': q['date'] in daily and math.isclose(q['close'], daily[q['date']], rel_tol=.006, abs_tol=.015)}
                    for q in quotes['monthly_prices']]
                row['native_quote_research'] = {k: v for k, v in quotes.items() if k != 'sources'}
                row['native_quote_research'].update(monthly_comparisons=matches,
                    conflicting_months=sum(not m['agrees'] for m in matches),
                    source_priority_not_overridden=True, fitted_price_factor_created=False)
                try:
                    with patch.object(native, 'inspect_payment_page', repaired_inspector):
                        native._payments(ticker, cache, CUTOFF, identity=stock)
                except (ValueError, KeyError, TypeError, IndexError) as error:
                    row['separate_repaired_payment_gate_reason'] = str(error)
                row['repair_proof'] = load_csed_repair(root, stock, CUTOFF, cache)['contract']
            result['results'].append(row)
            print(json.dumps({k: row[k] for k in ('ticker', 'ordinary_one_pass', 'financial_current_pass',
                'training_rows', 'ordinary_one_reason') if k in row}, ensure_ascii=True), flush=True)
    result['counts'] = {key: sum(bool(row[key]) for row in result['results'])
                        for key in ('ordinary_one_pass', 'financial_current_pass')}
    return result


def acquire_cash(folder):
    proof.require(PACKET_SHA is None, 'property_acquisition_frozen_packet_is_pinned')
    collector = PublicSourceCollector(folder, SCOPE)
    results = []
    # The shared collector respects robots/rates/stops; its default opener must
    # not inherit a machine proxy for this explicitly direct public collection.
    with patch('urllib.request.getproxies', return_value={}):
        for ticker in TARGETS:
            url = 'https://www.fundamentus.com.br/proventos.php?papel=' + ticker
            row = {'ticker': ticker, 'url': url, 'model_admission': False}
            try:
                receipt = collector.fetch(url, discovery={'kind': 'user_supplied_public_provider_template',
                    'ticker': ticker, 'purpose': 'literal_com_payment_quantity_and_cash_type_research'})
                row['receipt'] = receipt
                if receipt['status'] == 200 and not receipt['error']:
                    row['events'] = parse_calendar(collector.body(receipt), ticker)
            except (ValueError, OSError, KeyError) as error:
                row['reason'] = str(error)
            results.append(row)
            print(json.dumps({'ticker': ticker, 'rows': len(row.get('events', [])),
                'reason': row.get('reason'), 'status': row.get('receipt', {}).get('status')}, ensure_ascii=True), flush=True)
    return {'scope': SCOPE, 'cutoff': CUTOFF.isoformat(), 'results': results,
            'proxy': False, 'model_admission': False, 'pointers_written': False}


def acquire_quotes(folder):
    """Use the actual script and literal RI page configuration, never guessed IDs."""
    proof.require(PACKET_SHA is None, 'property_acquisition_frozen_packet_is_pinned')
    root = Path(folder).resolve()
    script, _, _ = source_by_url(root, SCRIPT_URL)
    collector = PublicSourceCollector(root, SCOPE)
    pages = {
        'LAVV3': ('https://ri.lavvi.com.br/servicos-aos-investidores/cotacoes-e-graficos/',
                  'https://ri.lavvi.com.br/central-de-analises/'),
        'MDNE3': ('https://ri.mouradubeux.com.br/servicos-a-investidores/cotacoes-e-graficos/',
                  'https://ri.mouradubeux.com.br/servicos-a-investidores/dividendos-2/'),
        'VITT3': ('https://ri.vittia.com.br/pt/servicos-aos-investidores/cotacoes-e-graficos',
                  'https://ri.vittia.com.br/pt'),
    }
    results = []
    with patch('urllib.request.getproxies', return_value={}):
        for ticker, (url, parent) in pages.items():
            row = {'ticker': ticker, 'model_admission': False}
            try:
                parent_body, parent_source, _ = source_by_url(root, parent)
                parsed = widgets.inspect_html(parent_body, parent_source)
                links = [link for link in parsed['links'] if link['url'] == url and link['tag'] == 'a']
                proof.require(bool(links), 'property_quote_page_not_published_by_issuer')
                try:
                    page, source, receipt = source_by_url(root, url)
                except proof.ProofError as error:
                    if not str(error).startswith('property_actual_receipt_missing:'):
                        raise
                    receipt = collector.fetch(url, discovery={'kind': 'issuer_published_quote_link',
                        'ticker': ticker, 'source': parent_source, 'locator': links[0]['locator']})
                    page, source = receipt_source(root, receipt, receipt['sha256'])
                row['page_receipt'] = receipt
                request, discovery = widgets.widget_request(page, source, script, identity(ticker))
                receipt = collector.fetch(request, discovery=discovery)
                row['quote_receipt'] = receipt
                body, source = receipt_source(root, receipt, receipt['sha256'])
                row['parsed'] = widgets.parse_widget_history(body, source, identity(ticker))
            except (ValueError, OSError, KeyError) as error:
                row['reason'] = str(error)
            results.append(row)
            print(json.dumps({'ticker': ticker, 'months': row.get('parsed', {}).get('monthly_observations'),
                'reason': row.get('reason')}, ensure_ascii=True), flush=True)
    return {'scope': SCOPE, 'cutoff': CUTOFF.isoformat(), 'results': results,
            'proxy': False, 'model_admission': False, 'pointers_written': False}


def acquire_published_assets(folder):
    proof.require(PACKET_SHA is None, 'property_acquisition_frozen_packet_is_pinned')
    root = Path(folder).resolve()
    collector = PublicSourceCollector(root, SCOPE)
    plans = [
        ('CSED3', 'https://ri.cruzeirodosuleducacional.com.br/pt',
         '(//astro-island)[position() <= 2]/@component-url'),
        ('VITT3', 'https://ri.vittia.com.br/pt/servicos-aos-investidores/cotacoes-e-graficos',
         '//script[contains(@src,"cotacoes-e-graficos/page-")]/@src'),
    ]
    results = []
    with patch('urllib.request.getproxies', return_value={}):
        for ticker, page_url, selector in plans:
            body, page_source, _ = source_by_url(root, page_url)
            tree = html.fromstring(body)
            for literal in tree.xpath(selector):
                url = urljoin(page_url, literal)
                row = {'ticker': ticker, 'url': url, 'model_admission': False}
                try:
                    row['receipt'] = collector.fetch(url, discovery={
                        'kind': 'issuer_published_script_or_component', 'ticker': ticker,
                        'source': page_source, 'selector': selector, 'literal': literal})
                except (ValueError, OSError) as error:
                    row['reason'] = str(error)
                results.append(row)
                print(json.dumps({'ticker': ticker, 'url': url,
                    'status': row.get('receipt', {}).get('status'), 'reason': row.get('reason')}), flush=True)
    return {'scope': SCOPE, 'results': results, 'proxy': False, 'model_admission': False}


def acquire_followon(folder):
    """Two published component imports and three exact indexed cash documents."""
    proof.require(PACKET_SHA is None, 'property_acquisition_frozen_packet_is_pinned')
    root = Path(folder).resolve()
    collector = PublicSourceCollector(root, SCOPE)
    plans = []
    page = 'https://ri.cruzeirodosuleducacional.com.br/_astro/_bird.YmpPFvAj.js'
    body, source, _ = source_by_url(root, page)
    imports = re.findall(r'from"(\./[^"\s]+\.js)"', body.decode('utf-8'))
    for literal in imports:
        if 'StoreProvider-' in literal or 'csed-components-' in literal:
            plans.append((urljoin(page, literal), {'kind': 'literal_public_ES_module_import',
                'ticker': 'CSED3', 'source': source, 'literal': literal}))
    manifest = json.loads((root / 'raw/primary-research/cvm-documents/index-sources.json').read_bytes())
    wanted = {2022: {'499436': 'CSED3'}, 2026: {'995937': 'LAVV3', '996192': 'LAVV3'}}
    for year, sequences in wanted.items():
        index, = [r for r in manifest if r['year'] == year]
        data = read(root, index['file'], index['sha256'], index['bytes'])
        member = f'ipe_cia_aberta_{year}.csv'
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            with archive.open(member) as stream:
                for line, row in enumerate(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'), 2):
                    from .cvm_proventos_documents import document_id
                    sequence, version = document_id(row['Link_Download'])
                    if sequence not in sequences:
                        continue
                    ticker = sequences[sequence]
                    proof.require(row['Codigo_CVM'] == str(TARGETS[ticker][0])
                        and row['CNPJ_Companhia'] == TARGETS[ticker][2]
                        and int(row['Versao']) == version, 'property_index_identity_conflict')
                    plans.append((row['Link_Download'], {'kind': 'exact_hashed_primary_CVM_IPE_row',
                        'ticker': ticker, 'sequence': sequence, 'version': version,
                        'index': index, 'member': member, 'csv_line': line, 'raw_row': row}))
    proof.require(len(plans) == 5, 'property_exact_bounded_followon_plan_required')
    results = []
    with patch('urllib.request.getproxies', return_value={}):
        for url, discovery in plans:
            row = {'ticker': discovery['ticker'], 'url': url, 'model_admission': False}
            try:
                row['receipt'] = collector.fetch(url, discovery=discovery)
            except (ValueError, OSError) as error:
                row['reason'] = str(error)
            results.append(row)
            print(json.dumps({'ticker': row['ticker'], 'url': url,
                'status': row.get('receipt', {}).get('status'), 'reason': row.get('reason')}), flush=True)
    return {'scope': SCOPE, 'results': results, 'proxy': False, 'model_admission': False}


def acquire_csed_final(folder):
    proof.require(PACKET_SHA is None, 'property_acquisition_frozen_packet_is_pinned')
    root = Path(folder).resolve()
    page, page_source, _ = source_by_url(root, CSED_PAGE)
    component, component_source, _ = source_by_url(root, CSED_COMPONENT)
    url, discovery = csed_widget_request(page, component)
    discovery.update(page=page_source, component=component_source)
    plans = [(url, discovery)]
    manifest = json.loads((root / 'raw/primary-research/cvm-documents/index-sources.json').read_bytes())
    source, = [r for r in manifest if r['year'] == 2022]
    data = read(root, source['file'], source['sha256'], source['bytes'])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with archive.open('ipe_cia_aberta_2022.csv') as stream:
            for line, row in enumerate(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'), 2):
                if row['Codigo_CVM'] == '25526' and 'numSequencia=509298&' in row['Link_Download']:
                    proof.require(row['CNPJ_Companhia'] == TARGETS['CSED3'][2] and row['Versao'] == '1',
                                  'property_CSED_final_notice_identity_changed')
                    plans.append((row['Link_Download'], {'kind': 'exact_hashed_primary_CVM_IPE_row',
                        'ticker': 'CSED3', 'index': source, 'member': 'ipe_cia_aberta_2022.csv',
                        'csv_line': line, 'raw_row': row}))
    proof.require(len(plans) == 2, 'property_CSED_exact_final_two_requests_required')
    collector, results = PublicSourceCollector(root, SCOPE), []
    with patch('urllib.request.getproxies', return_value={}):
        for url, discovery in plans:
            row = {'ticker': 'CSED3', 'url': url, 'model_admission': False}
            try:
                row['receipt'] = collector.fetch(url, discovery=discovery)
            except (ValueError, OSError) as error:
                row['reason'] = str(error)
            results.append(row)
            print(json.dumps({'url': url, 'status': row.get('receipt', {}).get('status'),
                'reason': row.get('reason')}), flush=True)
    return {'scope': SCOPE, 'results': results, 'proxy': False, 'model_admission': False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire-cash', action='store_true')
    parser.add_argument('--acquire-quotes', action='store_true')
    parser.add_argument('--acquire-assets', action='store_true')
    parser.add_argument('--acquire-followon', action='store_true')
    parser.add_argument('--acquire-csed-final', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if sum((args.acquire_cash, args.acquire_quotes, args.acquire_assets, args.acquire_followon,
            args.acquire_csed_final)) != 1 or args.report is None:
        parser.error('one explicit acquisition mode and --report required')
    result = (acquire_cash(args.folder) if args.acquire_cash else acquire_quotes(args.folder)
              if args.acquire_quotes else acquire_published_assets(args.folder) if args.acquire_assets
              else acquire_followon(args.folder) if args.acquire_followon else acquire_csed_final(args.folder))
    print('REPORT_SHA256', save_report(args.report, result), flush=True)
