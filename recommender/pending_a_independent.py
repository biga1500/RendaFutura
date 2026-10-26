"""Strict, offline issuer evidence; no exchange-shaped synthetic inputs.

The evidence reader is intentionally separate from acquisition and from the
unchanged candidate/financial gates. Unknown native payment dates block use.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import zipfile

from .bulk_pending_sources import PublicSourceCollector
from .pending_missing_a import load_cash_rows, load_native_prices, norm


CUTOFF = date(2026, 9, 30)
IDENTITY = {'ticker': 'BLAU3', 'cvm': 24627, 'share_class': 'ON', 'isin': 'BRBLAUACNOR8'}
IDENTITIES = {row['ticker']: row for row in (
    IDENTITY,
    {'ticker': 'BMOB3', 'cvm': 25500, 'share_class': 'ON', 'isin': 'BRBMOBACNOR7'},
    {'ticker': 'CXSE3', 'cvm': 23795, 'share_class': 'ON', 'isin': 'BRCXSEACNOR7'},
)}
BASE = 'raw/primary-research/all-pending/missing-a'
COMPANY_ID = '2015ded1-d33a-4aad-b3e5-9673efb0e7ac'
NATIVE_URLS = {
    'bonus': f'https://api.mziq.com/mzfilemanager/v2/d/{COMPANY_ID}/df249198-f922-1d53-6ffa-ae53a519bf71?origin=2',
    'bonus_details': f'https://api.mziq.com/mzfilemanager/v2/d/{COMPANY_ID}/1804ee8f-cd6f-a9e1-078c-78458f2f2967?origin=2',
    'dividend_deadline': f'https://api.mziq.com/mzfilemanager/v2/d/{COMPANY_ID}/715e7d7b-f506-8f2d-4d49-e6f7e52806f7?origin=2',
    'annual_distribution_proposal': f'https://api.mziq.com/mzfilemanager/v2/d/{COMPANY_ID}/f8e6ead5-1073-fc31-a885-1455443bc4e9?origin=2',
}
TEMPLATE_SOURCE = {
    'url': 'https://ri.blau.com/informacoes-financeiras/aviso-aos-acionistas/',
    'file': BASE + '/body-77d9d56e997344575da4d2ac2ffa685615355ca68235df49e48d0164aab506dc.bin',
    'sha256': '77d9d56e997344575da4d2ac2ffa685615355ca68235df49e48d0164aab506dc',
}
NATIVE_SHA = {
    'bonus': '42c19600dee4e6c614a3655d7d32f356828d71dfef3ad47466b35916a97ca0cf',
    'bonus_details': 'c9596602ad66936ec08cef1b90ea63d9cc2b1d81cc97730a4a5335ccb593f452',
    'dividend_deadline': '9a293c154f7ace7b14d7931964065c098577d52fe849b3e6850e65386374f79c',
    'annual_distribution_proposal': '7f572f0b375f7e7ca0ebce38159715ed1610a86e88a4458a9d61cb6e620c58c9',
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read_original(folder, source):
    root = Path(folder).resolve()
    relative = source['file']
    require(isinstance(relative, str) and relative.startswith('raw/')
            and '\\' not in relative and ':' not in relative
            and not set(relative.split('/')) & {'', '.', '..'}, 'pending_a_unsafe_original')
    path = root / relative
    require(path.resolve().is_relative_to(root) and all(not node.is_symlink()
            and not getattr(node, 'is_junction', lambda: False)() for node in (path, *path.parents)
            if node.is_relative_to(root)), 'pending_a_unsafe_original')
    require(path.stat().st_size <= 20_000_000, 'pending_a_original_size_limit')
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == source['sha256']
            and source.get('bytes', len(body)) == len(body), 'pending_a_original_integrity_failure')
    return body


def acquire_native_documents(folder):
    """Bounded public GETs discovered by search and bound to issuer fmId/base."""
    root = Path(folder).resolve()
    page = read_original(root, TEMPLATE_SOURCE).decode('utf-8')
    require("var fmId = '" + COMPANY_ID + "';" in page
            and "var fmBase = 'https://api.mziq.com/mzfilemanager';" in page,
            'pending_a_issuer_does_not_publish_vendor_template')
    collector = PublicSourceCollector(root, 'missing-a')
    receipts = {}
    for kind, url in NATIVE_URLS.items():
        receipt = collector.fetch(url, discovery={
            'kind': 'fresh_web_discovered_primary_document_issuer_publishes_same_vendor_company_template',
            'source_url': TEMPLATE_SOURCE['url'], 'source_file': TEMPLATE_SOURCE['file'],
            'source_sha256': TEMPLATE_SOURCE['sha256'], 'issuer_fmId': COMPANY_ID,
            'search_query': 'site:api.mziq.com BLAU bonificacao dividendos 2028',
            'document_purpose': kind})
        receipts[kind] = receipt
        if receipt['status'] == 200 and not receipt['error']:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(collector.body(receipt)))
            print(json.dumps({'kind': kind, 'receipt': {key: receipt[key] for key in (
                'file', 'sha256', 'bytes', 'url', 'retrieved_at')},
                'page_count': len(reader.pages)},
                ensure_ascii=True), flush=True)
        else:
            print(json.dumps({'kind': kind, 'status': receipt['status'], 'error': receipt['error']}), flush=True)
    (root / BASE / 'pending-a-native-documents.json').write_text(
        json.dumps(receipts, ensure_ascii=True, indent=2), encoding='utf-8')
    return receipts


def _selected_identity(folder, identity, cutoff):
    expected = IDENTITIES.get(identity.get('ticker'))
    if expected is None or cutoff != CUTOFF:
        return False
    require(type(identity.get('cvm')) is int and all(identity.get(k) == v for k, v in expected.items()),
            'pending_a_exact_CVM_class_ISIN_mismatch')
    from .dividend_scope import allowed_tickers
    require(expected['ticker'] in (allowed_tickers(folder) or ()), 'pending_a_outside_selected_scope')
    return True


def class_proof(folder, identity, cutoff):
    """Prove the only equity class/code in all six actual annual FCA filings."""
    require(_selected_identity(folder, identity, cutoff), 'pending_a_unsupported_class_proof')
    from .selected_identity_research import research
    from .security_registration import _kind
    root = Path(folder).resolve()
    exact = IDENTITIES[identity['ticker']]
    report = research(root, as_of=cutoff.isoformat(), tickers=(exact['ticker'],))
    record, = report['records']
    require(record['status'] == 'primary_exact_identity_evidence'
            and record['FCA_search_complete_for_2021_2026']
            and all(record[k] == v for k, v in exact.items()), 'pending_a_FCA_identity_conflict')
    history = record['FCA_exact_code_records']
    require({entry['year'] for entry in history} == set(range(2021, 2027)), 'pending_a_FCA_window_incomplete')
    rows, sources = [], {}
    for entry in sorted(history, key=lambda item: item['year']):
        require(entry['source_binding_valid'] and entry['received_and_referenced_by_as_of']
                and entry['cvm'] == exact['cvm'], 'pending_a_FCA_source_join_invalid')
        source = entry['source']
        body = read_original(root, source)
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{entry["year"]}.zip'
        receipts = [r for r in source['existing_receipts'] if r.get('url') == url and r.get('sha256') == source['sha256']]
        require(receipts, 'pending_a_FCA_official_receipt_missing')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            require(sum(i.file_size for i in archive.infolist()) <= 80_000_000, 'pending_a_FCA_expansion_limit')
            masters = list(csv.DictReader(io.StringIO(archive.read(entry['master_member']).decode('latin1')), delimiter=';'))
            master, = [r for r in masters if r['ID_DOC'] == entry['filing']['ID_DOC']]
            require(int(master['CD_CVM']) == exact['cvm'] and master['DT_RECEB'] <= cutoff.isoformat()
                    and master['DT_REFER'] <= cutoff.isoformat(), 'pending_a_FCA_master_mismatch')
            securities = list(csv.DictReader(io.StringIO(archive.read(entry['security_member']).decode('latin1')), delimiter=';'))
            equity = [r for r in securities if r['ID_Documento'] == master['ID_DOC']
                      and _kind(r)[0] in ('ordinary_share', 'preferred_share')]
        require(len(equity) == 1 and _kind(equity[0]) == ('ordinary_share', 'ON')
                and equity[0]['Codigo_Negociacao'].strip() == exact['ticker'],
                'pending_a_multiple_equity_classes_or_codes')
        require(all(equity[0][child] == master[parent] for child, parent in (
            ('CNPJ_Companhia', 'CNPJ_CIA'), ('Data_Referencia', 'DT_REFER'), ('Versao', 'VERSAO'))),
            'pending_a_FCA_equity_child_mismatch')
        sources[source['file']] = {**receipts[0], 'published_at': None,
                                  'publication_basis': 'filing_received_date_is_separate_from_unknown_publication'}
        rows.append({'year': entry['year'], 'filing': entry['filing'], 'equity_classes': ['ON'],
                     'equity_codes': [exact['ticker']], 'member': entry['security_member'],
                     'csv_record': entry['csv_record'], 'source_file': source['file'], 'source_sha256': source['sha256']})
    mappings = record['cached_exact_code_mappings']
    require(mappings and all(m['cvm'] == exact['cvm'] and m['isin'] == exact['isin'] for m in mappings),
            'pending_a_cached_exact_ISIN_mismatch')
    for mapping in mappings:
        read_original(root, mapping['source'])
        sources[mapping['source']['file']] = {**mapping['source'], 'url': None, 'retrieved_at': None,
            'authority': 'existing_exact_ISIN_cache_not_legal_class_authority', 'published_at': None}
    return {'identity': dict(exact), 'years': rows, 'sole_equity_class_across_received_FCA_2021_2026': True,
            'generic_per_share_bound_to': 'ON', 'ISIN_basis': 'unchanged_existing_exact_code_registry_cache',
            'sources': list(sources.values()), 'point_in_time_archive_verified': False}


def native_quote_proof(folder, identity, cutoff, observed_actions):
    """Compare every model input, on the same last actual trade of each month.

    Off-grid comparisons are retained as an audit, never filtered into the grid.
    The shared reconciliation function and its tolerances remain unchanged.
    """
    require(_selected_identity(folder, identity, cutoff), 'pending_a_unsupported_quote_proof')
    from .collect_market_data import month_end, shift_month
    from .share_basis import factor_after, reconcile_prices
    root, ticker = Path(folder).resolve(), identity['ticker']
    prices = load_native_prices(root, identity)
    relative = f'raw/expanded/{ticker}-prices.json'
    if not (root / relative).exists():
        relative = f'raw/inventory/daily-prices/{ticker}.json'
    body = (root / relative).read_bytes()
    payload = json.loads(body)['chart']['result'][0]
    stamps, closes = payload['timestamp'], payload['indicators']['quote'][0]['close']
    require(len(stamps) == len(closes), 'pending_a_model_daily_length_mismatch')
    daily, months = {}, {}
    for stamp, value in zip(stamps, closes):
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        require(day not in daily, 'pending_a_duplicate_model_trade_date')
        if day <= cutoff and value is not None:
            require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                    'pending_a_invalid_model_close')
            daily[day] = float(value)
            key = month_end(day.year, day.month)
            months[key] = max(months.get(key, day), day)
    required = {shift_month(cutoff, -offset) for offset in range(61)}
    anchors = {date.fromisoformat(row['trade_date']): row['close'] for row in prices['monthly_anchors']}
    require(len(anchors) == 61 and {month_end(day.year, day.month) for day in anchors} == required,
            'pending_a_exact_61_month_grid_missing')
    require(all(months.get(month_end(day.year, day.month)) == day for day in anchors),
            'pending_a_native_and_model_last_trade_disagree')
    _, matches, mode = reconcile_prices(body, ticker, observed_actions, anchors, cutoff,
                                       evidence_provider='issuer_RI_and_Enfoque')
    require(len(matches) == 61 and set(matches) == {day.isoformat() for day in anchors},
            'pending_a_not_all_61_anchors_validated')
    discrepancies = []
    for row in prices['daily_observations']:
        day, raw = date.fromisoformat(row['date']), row['close']
        if day not in daily:
            continue
        expected = raw / factor_after(day, observed_actions, cutoff) if mode == 'split_adjusted' else raw
        if not math.isclose(daily[day], expected, rel_tol=.006, abs_tol=.015):
            discrepancy = {'trade_date': day.isoformat(), 'native_raw_close': raw,
                'model_source_close': daily[day], 'model_source_scale_expected': expected,
                'is_model_monthly_anchor': day in anchors}
            require(day not in anchors, 'pending_a_real_monthly_anchor_conflict')
            discrepancies.append(discrepancy)
    manifest = json.loads((root / BASE / f'quote-{ticker}.json').read_bytes())
    sources = [manifest[key] for key in ('page_receipt', 'bundle_receipt')]
    sources.append(manifest.get('graphic_receipt') or manifest['history_receipt'])
    for source in sources:
        read_original(root, source)
    sources.append({'file': relative, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                    'url': None, 'retrieved_at': None, 'published_at': None,
                    'authority': 'existing_original_model_quote_cache; unknown_retrieval_not_mtime'})
    return {'raw_quotes': anchors, 'sources': sources, 'checks': {
        'all_61_model_monthly_closes_validated': True, 'price_matches': matches, 'price_mode': mode,
        'same_actual_last_trade_dates': True, 'native_daily_count': prices['daily_count'],
        'monthly_raw_anchor_policy': 'all_61_last_actual_trades_of_calendar_months_no_conflict_filter',
        'required_minimum_anchor_count': 4, 'actual_anchor_count': 61,
        'unchanged_relative_epsilon': .006, 'unchanged_absolute_epsilon': .015,
        'off_grid_daily_discrepancies': discrepancies, 'off_grid_discrepancies_block_monthly_grid': False,
        'original_daily_sources_retained': True, 'native_values_changed': False,
        'imputed_prices': 0, 'synthetic_exchange_responses': False}}


def native_documents(folder):
    from pypdf import PdfReader
    root = Path(folder).resolve()
    index = json.loads((root / BASE / 'pending-a-native-documents.json').read_bytes())
    documents = {}
    for kind, url in NATIVE_URLS.items():
        receipt = index[kind]
        require(receipt.get('status') == 200 and not receipt.get('error')
                and receipt['url'] == url and receipt['sha256'] == NATIVE_SHA[kind],
                'pending_a_native_document_receipt_changed')
        body = read_original(root, receipt)
        reader = PdfReader(io.BytesIO(body))
        pages = ({43} if kind == 'annual_distribution_proposal' else set(range(1, len(reader.pages)+1)))
        text = {number: reader.pages[number-1].extract_text() or '' for number in pages}
        documents[kind] = {'source': receipt, 'pages': text, 'page_count': len(reader.pages)}
    return documents


def primary_action_proof(folder, identity, cutoff):
    require(_selected_identity(folder, identity, cutoff), 'pending_a_unsupported_action_proof')
    if identity['ticker'] != IDENTITY['ticker']:
        quote_proof = native_quote_proof(folder, identity, cutoff, [])
        return {'identity': dict(IDENTITIES[identity['ticker']]), 'actions': [],
                'sources': quote_proof['sources'], 'all_61_monthly_prices_agree_on_actual_scale': True,
                'empty_actions_basis': 'actual_price_scale_agreement_not_an_invented_action_document',
                'monthly_quote_checks': quote_proof['checks'], 'factor_fitted_from_prices': False,
                'legal_absence_of_all_actions_certified': False, 'point_in_time_archive_verified': False}
    documents = native_documents(folder)
    notice = norm(documents['bonus_details']['pages'][2])
    fact = norm(documents['bonus']['pages'][1])
    require('ticker: blau3' in fact and 'codigo cvm n' in fact and '2462-7' in fact,
            'pending_a_action_exact_issuer_missing')
    require('3 (tres) novas acoes' in notice and 'cada 10 (dez) acoes' in notice
            and '30% (trinta por cento)' in notice and 'novas acoes ordinarias' in notice,
            'pending_a_action_native_factor_or_species_unproven')
    require('05 de janeiro de 2026' in notice and 'partir de 06 de janeiro de 2026' in notice
            and '08 de janeiro de 2026' in notice, 'pending_a_action_date_roles_unproven')
    action = {'date': '2026-01-06', 'ratio': 1.3, 'ratio_decimal': '1.3',
              'kind': 'same_species_ordinary_share_bonus', 'record_date': '2026-01-05',
              'credit_date': '2026-01-08', 'date_role': 'ex_right_not_credit_date',
              'native_ratio': '3 new ordinary shares per 10 ordinary shares',
              'source_file': documents['bonus_details']['source']['file'], 'locator': 'PDF page 2, IV(b)'}
    return {'identity': dict(IDENTITY), 'actions': [action],
            'sources': [documents[k]['source'] for k in ('bonus', 'bonus_details')],
            'factor_fitted_from_prices': False, 'nonempty_actions_have_primary_documents': True,
            'point_in_time_archive_verified': False}


def validate_primary_actions(folder, identity, cutoff, observed_actions):
    """Exact native ex-date and factor, plus unchanged price-scale gates."""
    proof = primary_action_proof(folder, identity, cutoff)
    expected = [(a['date'], Decimal(a['ratio_decimal'])) for a in proof['actions']]
    actual = []
    for action in observed_actions:
        require(isinstance(action.get('date'), str) and type(action.get('ratio')) in (int, float)
                and math.isfinite(action['ratio']) and action['ratio'] > 0,
                'pending_a_invalid_observed_action')
        if date(2021, 9, 1) <= date.fromisoformat(action['date']) <= cutoff:
            actual.append((action['date'], Decimal(str(action['ratio']))))
    require(sorted(actual) == sorted(expected), 'pending_a_primary_actions_disagree')
    prices = native_quote_proof(folder, identity, cutoff, observed_actions)
    proof.update(prices['checks'])
    proof['sources'] = list({source['file']: source for source in proof['sources'] + prices['sources']}.values())
    return proof


def _inspect_blau(folder, identity, cutoff):
    """Return an audit, including unresolved positive rights; never admission."""
    require(_selected_identity(folder, identity, cutoff), 'pending_a_unsupported_inspection')
    root = Path(folder).resolve()
    classes = class_proof(root, identity, cutoff)
    documents = native_documents(root)
    actions = primary_action_proof(root, identity, cutoff)
    cash = load_cash_rows(root, identity)
    prices = load_native_prices(root, identity)
    deadline_text = norm(documents['dividend_deadline']['pages'][1])
    require('valor bruto a ser pago por acao e de r$0,56280720444' in deadline_text
            and '18 de dezembro de 2025' in deadline_text and 'ate 15 de dezembro de 2028' in deadline_text
            and 'datas de pagamento serao oportunamente' in deadline_text,
            'pending_a_native_deadline_reading_changed')
    proposal_text = norm(documents['annual_distribution_proposal']['pages'][43])
    require('valor liquido por acao' in proposal_text and 'conforme valor liquido pago' in proposal_text
            and '0,105526351' in proposal_text, 'pending_a_native_tax_label_reading_changed')
    declared, exact, unresolved = defaultdict(Decimal), [], []
    for observation in cash:
        require(observation['identity'] == IDENTITY and observation['source_class'] in (None, 'ON'),
                'pending_a_cash_identity_conflict')
        record = observation.get('record_date')
        require(record and observation['event_type'] in ('JCP', 'DIVIDENDO'), 'pending_a_cash_date_or_kind_unresolved')
        if record > cutoff.isoformat():
            continue
        amount = Decimal(observation['amount_decimal'])
        require(amount > 0, 'pending_a_nonpositive_native_cash')
        if observation['payment_date'] is None:
            require(record == '2025-12-18' and observation['event_type'] == 'DIVIDENDO',
                    'pending_a_additional_unknown_payment')
            # This more precise native declaration is a positive right, never
            # an invented zero or a payment at its latest possible deadline.
            amount = Decimal('0.56280720444')
            unresolved.append({'record_date': record, 'label': 'DIVIDENDO',
                'gross_per_share_decimal': str(amount), 'payment_date': None,
                'payment_deadline': '2028-12-15', 'date_role': 'latest_possible_payment_not_payment_date',
                'source': documents['dividend_deadline']['source'], 'locator': 'PDF page 1, paragraphs 2-3'})
        else:
            exact.append({**observation, 'source_class': 'ON', 'class_binding': classes['years'],
                          'amount_tax_basis': 'primary_history_has_total_gross_column_but_per_share_label_requires_conflict_check'})
        declared[date.fromisoformat(record)] += amount
    require(len(cash) == 24 and len(unresolved) == 1 and len(exact) == 23,
            'pending_a_native_calendar_shape_changed')
    quotes = {date.fromisoformat(row['trade_date']): row['close'] for row in prices['monthly_anchors']}
    require(len(quotes) == 61, 'pending_a_61_native_monthly_anchors_required')
    sources = {source['file']: source for source in classes['sources'] + actions['sources']}
    for document in documents.values():
        sources[document['source']['file']] = document['source']
    quote_manifest = json.loads((root / BASE / 'quote-BLAU3.json').read_bytes())
    for key in ('page_receipt', 'bundle_receipt', 'graphic_receipt'):
        source = quote_manifest[key]
        read_original(root, source)
        sources[source['file']] = source
    for document in json.loads((root / BASE / 'issuer-24627-inspection.json').read_bytes())['documents']:
        if document['receipt']['url'].endswith('/dividendos-e-jcp/'):
            read_original(root, document['receipt'])
            sources[document['receipt']['file']] = document['receipt']
    tax_conflict = {'kind': 'native_per_share_gross_net_label_conflict',
        'history_literal': '0,105526351', 'proposal_label': 'Valor Liquido por acao (R$)',
        'proposal_footnote': 'Considera o valor liquido por acao conforme Valor Liquido Pago',
        'source': documents['annual_distribution_proposal']['source'], 'locator': 'PDF page 43, Anexo II, table and footnote 1',
        'replacement_gross_created': False, 'net_gross_factor_applied': False}
    return {'provider': 'issuer_RI_native_calendar_and_Enfoque',
            'expected_cash': {day: float(value) for day, value in declared.items()},
            'raw_quotes': quotes, 'sources': list(sources.values()), 'primary_actions': actions,
            'checks': {'identity': dict(IDENTITY), 'class_proof': classes,
                       'native_calendar_rows': len(cash), 'exact_payment_rows': len(exact),
                       'raw_quote_rows': len(quotes), 'underlying_native_daily_rows': prices['daily_count'],
                       'quote_sampling_policy': 'all_61_calendar_month_ends_fixed_before_price_comparison',
                       'unresolved_payments': unresolved, 'native_tax_label_conflicts': [tax_conflict],
                       'blockers': ['pending_a_native_deadline_not_exact_payment', 'pending_a_native_gross_net_conflict'],
                       'deadline_retained_as_positive_entitlement': True,
                       'deadline_converted_to_payment_date': False, 'missing_cash_imputed': False,
                       'synthetic_exchange_responses': False, 'net_to_gross_inference': False,
                       'normal_financial_checks_required': True, 'settlement_verified': False,
                       'point_in_time_archive_verified': False, 'model_admission': False}}


BEMOBI_SHA = {
    '1': '6d45391c3458716b2e03ee7c85df91e39f0819ca48e94ab6e3d11aaf029acc46',
    '2': 'bb5e11bff37f551456badb55e3abea0252cab110787b6191714a28c7e9302d84',
    '3': '856da558803b556104dade92ee3a71c2dbb8d0196f0071e83cd4b082e736410d',
    '4': '6766f21d0b65e5dc6ccbd12f9fe330ac58c1f033c67b8ed55125a97c1e193124',
    '5': '75aee50910171e18aa8da3219ee344ba2df2ab076637aba056fc37235e244547',
    '7': '8175a70ec3d71168426c56522cfdeb81a0bec789a4ef085b313b54b20526af6a',
    '8': 'e19dc8aa77caab726da33363e3e9454cfe81b09c521c92cca5b147264f11d0bb',
    '9': 'b117430bbf76713c567ad97a4a740ddf91194851f251e76216d67caae156c525',
    '2022-original': '9596f4a1bf6208ff213b29663be1cfac5315737424ed1d05798c0e1c8d755cee',
    '2023-v1': '3cf9675dec52a929cb9ecb0607b0e92e959bfd037e278d36d9fce6499a2a4c41',
    '2023-v2': '9869400b83e12642583af4795f51316d0abd05417f8a96aad4b750ee0deffd23',
    '2025-v2': '3f6e27216207d13bad349ed41476920284f836b23c2ce93904360af294512bea',
    '2025-RI': '6d528c2a99d52b609be2ed6c59e03c91ff5a8ba2d59a23f473502955af361d39',
}


def bemobi_documents(folder):
    """Reopen exact original notices, including CVM versions and IPE joins."""
    from pypdf import PdfReader
    root = Path(folder).resolve()
    index = json.loads((root / BASE / 'pending-a-bemobi-documents.json').read_bytes())
    extra = json.loads((root / BASE / 'pending-a-bemobi-extra-documents.json').read_bytes())
    sources = []
    for item in extra:
        receipt = item.get('receipt')
        require(receipt is not None, 'pending_a_bemobi_CVM_acquisition_incomplete')
        if 'native_index_row' in item:
            source = {'file': item['index_file'], 'sha256': item['index_sha256'],
                      'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_'
                             + item['native_index_row']['Data_Entrega'][:4] + '.zip',
                      'retrieved_at': None, 'published_at': None}
            with zipfile.ZipFile(io.BytesIO(read_original(root, source))) as archive:
                rows = list(csv.DictReader(io.StringIO(archive.read(item['member']).decode('latin1')), delimiter=';'))
            native = rows[item['csv_record'] - 2]
            require(native == item['native_index_row'] and native['Codigo_CVM'] == '25500'
                    and native['Link_Download'] == receipt['url'] and native['Data_Entrega'] <= CUTOFF.isoformat(),
                    'pending_a_bemobi_IPE_index_binding_changed')
            if native['Data_Referencia'] == '2022-03-16':
                key = '2022-original'
            elif native['Data_Referencia'] == '2023-03-16':
                key = '2023-v' + native['Versao']
            else:
                require(native['Data_Referencia'] == '2025-08-14' and native['Versao'] == '2',
                        'pending_a_unexpected_bemobi_version')
                key = '2025-v2'
            sources.append(source)
        else:
            require(item['kind'] == 'bemobi_2025_JCP_notice', 'pending_a_unexpected_bemobi_document')
            key = '2025-RI'
        index[key] = receipt
    documents = {}
    for key, digest in BEMOBI_SHA.items():
        receipt = index[key]
        require(receipt['status'] == 200 and not receipt['error'] and receipt['sha256'] == digest,
                'pending_a_bemobi_document_changed')
        body = read_original(root, receipt)
        pages = [page.extract_text() or '' for page in PdfReader(io.BytesIO(body)).pages]
        require('ticker: bmob3' in norm(pages[0]) and '09.042.817/0001' in norm(pages[0]),
                'pending_a_bemobi_native_issuer_mismatch')
        documents[key] = {'source': receipt, 'pages': pages, 'compact': re.sub(r'\s+', '', norm(' '.join(pages)))}
        sources.append(receipt)
    return documents, sources


def bemobi_cash_rows(folder, identity, cutoff):
    """12 native components, not 9 HTML rows plus duplicate PDF components.

    Two notices give ex dates but no explicit record date. Keep None; subtracting
    a weekday from an ex date would not be an inspected primary record date.
    """
    require(_selected_identity(folder, identity, cutoff) and identity['ticker'] == 'BMOB3',
            'pending_a_unsupported_bemobi_cash')
    from .pending_missing_a import parse_bemobi_mixed_notice
    documents, sources = bemobi_documents(folder)
    classes = class_proof(folder, identity, cutoff)
    # Values below are exact native literals checked against pinned originals.
    specs = (
        ('1', 'DIVIDENDO', '0.20235653', None, '2022-03-22', '2022-04-12', None),
        ('2023-v2', 'DIVIDENDO', '0.23330815999', None, '2023-03-24', '2023-04-12', None),
        ('3', 'JCP', '0.52193343', '2023-12-18', '2023-12-19', '2024-05-02', '18dedezembrode2023'),
        ('5', 'DIVIDENDO', '0.69457324', '2025-04-24', '2025-04-25', '2025-05-09', '24deabrilde2025'),
        ('2025-v2', 'JCP', '0.35551784', '2025-08-21', '2025-08-22', '2025-09-01', '21deagostode2025'),
        ('8', 'JCP', '0.18818727094', '2026-05-15', '2026-05-18', '2026-05-27', '15demaiode2026'),
        ('9', 'JCP', '0.19178292', '2026-08-14', '2026-08-17', '2026-08-28', '14deagostode2026'),
    )
    rows = []
    for key, label, amount, record, ex, payment, record_literal in specs:
        document = documents[key]
        compact = document['compact']
        require(amount.replace('.', ',') in compact, 'pending_a_bemobi_native_amount_changed')
        if record_literal:
            require(record_literal in compact, 'pending_a_bemobi_native_record_date_changed')
        if label == 'JCP':
            require('valorbruto' in compact, 'pending_a_bemobi_gross_not_explicit')
        supporting = [document['source']]
        if key == '1':
            original = documents['2022-original']['compact']
            require('22demarcode2022' in original and '12deabrilde2022' in original
                    and 'ratificatodososdemaistermos' in compact, 'pending_a_bemobi_amendment_chain_unproven')
            supporting.append(documents['2022-original']['source'])
        rows.append({'identity': dict(IDENTITIES['BMOB3']), 'source_class': 'ON',
            'class_binding': 'single_equity_ON_proved_by_received_FCA_2021_2026',
            'label': label, 'gross_per_share_decimal': amount, 'gross_per_share': float(amount),
            'gross_basis': 'explicit_native_gross' if label == 'JCP' else 'native_declared_dividend_no_net_to_gross_conversion',
            'record_date': record, 'ex_date': ex, 'payment_date': payment,
            'record_date_derived_from_ex_date': False, 'published_at': None,
            'sources': supporting, 'locator': 'PDF page 1, native per-share and entitlement/payment paragraphs',
            'model_admission': False, 'settlement_verified': False})
    for key in ('4', '7'):
        document = documents[key]
        native = parse_bemobi_mixed_notice(read_original(folder, document['source']), document['source'], identity)
        for event in native:
            rows.append({**event, 'label': event['event_type'], 'gross_per_share_decimal': event['amount_decimal'],
                'gross_per_share': event['amount'], 'sources': [document['source']],
                'class_binding': 'single_equity_ON_proved_by_received_FCA_2021_2026',
                'record_date_derived_from_ex_date': False})
    require(len(rows) == 12 and sum(row['record_date'] is None for row in rows) == 2,
            'pending_a_bemobi_native_component_count_changed')
    return {'events': sorted(rows, key=lambda row: (row['payment_date'], row['label'], row['gross_per_share_decimal'])),
            'sources': sources, 'class_proof': classes,
            'HTML_mixed_totals_not_added': ['2025-01-07', '2025-12-22'],
            'source_conflicts': [
                {'kind': 'issuer_HTML_ex_date_vs_original_notice', 'HTML': '2022-03-21', 'notice': '2022-03-22',
                 'record_date_not_invented': True, 'sources': [documents['2022-original']['source']]},
                {'kind': 'issuer_HTML_payment_date_vs_CVM_version_2', 'HTML': '2025-12-01',
                 'CVM_version_2': '2025-09-01', 'source': documents['2025-v2']['source'],
                 'modelling_calendar_silently_changed': False},
                {'kind': 'CVM_version_revision_not_additional_cash', 'superseded_amount': '0.223542',
                 'version_2_amount': '0.23330815999',
                 'sources': [documents[key]['source'] for key in ('2023-v1', '2023-v2')]}]}


def _inspect_other(folder, identity, cutoff):
    ticker, root = identity['ticker'], Path(folder).resolve()
    classes = class_proof(root, identity, cutoff)
    quotes = native_quote_proof(root, identity, cutoff, [])
    action_proof = primary_action_proof(root, identity, cutoff)
    original = load_cash_rows(root, identity)
    if ticker == 'BMOB3':
        calendar = bemobi_cash_rows(root, identity, cutoff)
        events = calendar['events']
        blockers = ['pending_a_BMOB_no_native_cash_entitlement_at_window_start',
                    'pending_a_BMOB_two_native_record_dates_unresolved',
                    'pending_a_BMOB_secondary_calendar_does_not_span_window']
    else:
        events = original
        calendar = {'events': events, 'sources': [], 'source_conflicts': [],
                    'pre_IPO_dates_not_promoted_to_public_holder_record_dates': True}
        blockers = ['pending_a_CXSE_historical_gross_precision_unresolved',
                    'pending_a_CXSE_pre_IPO_record_dates_absent',
                    'pending_a_CXSE_secondary_calendar_does_not_span_window']
    declared = defaultdict(Decimal)
    if ticker == 'BMOB3':
        for event in events:
            if event['record_date']:
                declared[date.fromisoformat(event['record_date'])] += Decimal(event['gross_per_share_decimal'])
    sources = {source['file']: source for source in classes['sources'] + quotes['sources'] + calendar['sources']}
    inspection = json.loads((root / BASE / f'issuer-{identity["cvm"]}-inspection.json').read_bytes())
    receipt = inspection['documents'][0]['receipt']
    read_original(root, receipt)
    sources[receipt['file']] = receipt
    return {'provider': 'issuer_RI_CVM_native_notices_and_Enfoque',
        'expected_cash': {day: float(amount) for day, amount in declared.items()},
        'raw_quotes': quotes['raw_quotes'], 'sources': list(sources.values()), 'primary_actions': action_proof,
        'calendar': calendar, 'checks': {'identity': dict(IDENTITIES[ticker]), 'class_proof': classes,
            'quote_proof': quotes['checks'], 'blockers': blockers, 'model_admission': False,
            'expected_cash_is_incomplete_not_model_input': True,
            'record_dates_derived_from_ex_dates': False, 'missing_cash_imputed': False,
            'synthetic_exchange_responses_created': False, 'native_notices_are_not_bank_settlement': True}}


def inspect_evidence(folder, identity, cutoff):
    """Non-admitting audit; exposes every genuine conflict before integration."""
    require(_selected_identity(folder, identity, cutoff), 'pending_a_unsupported_inspection')
    if identity['ticker'] == 'BLAU3':
        evidence = _inspect_blau(folder, identity, cutoff)
        evidence['checks']['quote_proof'] = native_quote_proof(
            folder, identity, cutoff, evidence['primary_actions']['actions'])['checks']
        return evidence
    return _inspect_other(folder, identity, cutoff)


def load_evidence(folder, identity, cutoff):
    """None outside scope; return the parent contract only for unblocked proof.

    Supported, conflicting originals raise a specific ValueError. The current
    Audits with missing cash dates/precision are never returned as model input.
    """
    if not _selected_identity(folder, identity, cutoff):
        return None
    evidence = inspect_evidence(folder, identity, cutoff)
    blockers = evidence['checks']['blockers']
    require(not blockers, blockers[0] if blockers else 'pending_a_evidence_unavailable')
    return evidence


def _json_safe(value):
    if isinstance(value, dict):
        return {key.isoformat() if isinstance(key, date) else key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def financial_current(folder, row, evidence, prices, actions):
    """Normal financial functions on native cash, with real uncertainty retained.

    This is an isolated diagnostic, not an alternate candidate admission path.
    It never supplies an empty cash list as if unknown distributions were zero.
    """
    from .inventory_financials import prepare_financials
    from .collect_market_data import (DividendHistory, financial_features, shift_month,
                                      stock_observations)
    ticker, root = row['ticker'], Path(folder).resolve()
    statements, diagnostics, sources = prepare_financials(root, [row], CUTOFF)
    calendar = DividendHistory(CUTOFF, 0, basis='payment')
    if ticker == 'BMOB3':
        events = evidence['calendar']['events']
        for event in events:
            day = date.fromisoformat(event['payment_date'])
            if day <= CUTOFF:
                calendar.append((day, day, event['gross_per_share']))
        calendar.event_count = len(calendar)
        # No calendar completeness or zero distributions before the first
        # inspected payment is asserted by the positive post-IPO notices.
        calendar.issues.append({'event_index': 'before_first_native_payment',
            'interval_start': '2021-09-01', 'interval_end': '2022-04-12',
            'reason': 'no_native_window_start_cash_coverage_no_zero_inferred'})
        cash_basis = '12_exact_primary_notice_components_CVM_version2_priority_over_conflicting_HTML'
    else:
        calendar.issues.append({'event_index': 'native_cash_unresolved',
            'interval_start': '2020-09-01', 'interval_end': CUTOFF.isoformat(),
            'reason': evidence['checks']['blockers'][0]})
        cash_basis = 'unresolved_native_cash_blocks_features_not_an_empty_zero_calendar'
    spec = row['cvm'], row['share_class'], row['sector_code']
    current = financial_features(statements, calendar, CUTOFF, spec)
    observations, coverage = stock_observations(ticker, prices, calendar, statements, CUTOFF, spec=spec)
    basis = max((date.fromisoformat(action['date']) for action in actions), default=date(2014, 1, 1))
    latest = max((statement for statement in statements if statement['cvm'] == row['cvm']
                  and statement.get('share_class', row['share_class']) == row['share_class']
                  and statement['published_at'] <= CUTOFF.isoformat()),
                 key=lambda statement: (statement['fiscal_end'], int(statement['version'])), default=None)
    basis_passed = latest is not None and date.fromisoformat(latest['fiscal_end']) >= basis
    admitted_training = [observation for observation in observations
                         if date.fromisoformat(observation['as_of']) >= shift_month(shift_month(CUTOFF, -60), 12)
                         and date.fromisoformat(observation['fiscal_end']) >= basis]
    return {'unchanged_financial_features_called': True, 'unchanged_stock_observations_called': True,
        'features_present': current is not None, 'financial_current': current[0] if current else None,
        'financial_method': latest.get('financial_method') if latest else None,
        'filing_id': latest['filing_id'] if latest else None, 'fiscal_end': latest['fiscal_end'] if latest else None,
        'reported_EPS': latest['profit_per_share'] if latest else None,
        'financial_basis_start': basis.isoformat(), 'normal_financial_basis_gate_passed': basis_passed,
        'normal_financial_current_gate_passed': current is not None and basis_passed,
        'raw_diagnostic_training_rows': len(observations), 'training_rows': len(admitted_training),
        'statements': len(statements), 'sources': sources, 'source_diagnostics': diagnostics,
        'cash_basis': cash_basis, 'current_cash_blockers': calendar.blockers(shift_month(CUTOFF, -12), CUTOFF),
        'candidate_financial_admission': False, 'independent_model_candidate_required': True,
        'settlement_verified': False, 'EPS_imputed': False, 'old_filing_fallback_added': False}


def isolated_trial(folder, ticker='BMOB3'):
    """Patch only the two authorized issuer hooks, never any gate or source."""
    from unittest.mock import patch
    from . import issuer_independent_evidence as issuer
    from .inventory_candidates import _Cache, _one, _payments
    from .market_inventory import load_inventory
    from .broad_stocks import split_price_history
    root = Path(folder).resolve()
    protected_paths = [root / name for name in ('current.json', 'inventory/current.json', 'stock-selection/current.json')]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths}
    original_hooks = issuer.load_evidence, issuer.validate_primary_actions
    inventory = load_inventory(root)
    require(ticker in IDENTITIES, 'pending_a_trial_ticker_out_of_scope')
    row, = [row for row in inventory['stocks'] if row['ticker'] == ticker]
    cache = _Cache(root, inventory)
    checks = {}
    result = {'identity': dict(IDENTITIES[ticker]), 'only_monkeypatched_hooks': ['issuer_independent_evidence.load_evidence',
        'issuer_independent_evidence.validate_primary_actions'], 'core_source_edited': False,
        'candidate': False, 'model_admission': False, 'run_at': datetime.now(timezone.utc).isoformat()}
    try:
        with patch.object(issuer, 'load_evidence', load_evidence), patch.object(issuer, 'validate_primary_actions', validate_primary_actions):
            candidate = _one(row, cache, CUTOFF, checks)
            result.update(candidate=True, independent_cash_matches=len(candidate['independent_cash_matches']))
    except ValueError as failure:
        result['pending_reason'] = str(failure)
    require((issuer.load_evidence, issuer.validate_primary_actions) == original_hooks, 'pending_a_hooks_not_restored')
    native = inspect_evidence(root, row, CUTOFF)
    relative = f'raw/inventory/daily-prices/{ticker}.json'
    legacy = f'raw/expanded/{ticker}-prices.json'
    if (root / legacy).exists():
        relative = legacy
    price_body, _ = cache.read(relative, 'original_daily_prices')
    prices, actions = split_price_history(price_body, ticker, CUTOFF, allow_recent_actions=True)
    result['primary_action_and_monthly_price_gate'] = validate_primary_actions(root, row, CUTOFF, actions)
    primary = cache.primary.primary_events(row, CUTOFF) if cache.primary else {}
    try:
        calendar, _ = _payments(ticker, cache, CUTOFF, primary, identity=row)
        result['unchanged_secondary_calendar_gate'] = {'passed': True}
        if ticker == 'BLAU3':
            result['secondary_future_date_conflict'] = [event for event in calendar.installments
                                                       if event['record_date'] == '2025-12-18']
    except ValueError as failure:
        result['unchanged_secondary_calendar_gate'] = {'passed': False, 'reason': str(failure)}
    result['normal_financial_diagnostic_only'] = financial_current(root, row, native, prices, actions)
    result['checks'] = checks
    result['evidence_audit'] = native
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths}
    require(before == after, 'pending_a_protected_pointer_changed')
    result['protected_pointer_hashes_unchanged'] = after
    result['hooks_restored'] = True
    return _json_safe(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--market', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--trial-report', type=Path)
    parser.add_argument('--ticker', choices=tuple(IDENTITIES), default='BMOB3')
    parser.add_argument('--all-targets', action='store_true')
    args = parser.parse_args()
    if args.acquire:
        acquire_native_documents(args.market)
    if args.trial_report:
        if args.all_targets:
            trials = [isolated_trial(args.market, ticker) for ticker in IDENTITIES]
            report = {'scope': 'missing-a', 'model_admissions': 0, 'candidate_count': sum(r['candidate'] for r in trials),
                      'trials': trials, 'core_edited': False, 'network_during_replay': False,
                      'all_41_original_scope_report': str(args.trial_report.parent / 'missing-a-report.json')}
        else:
            report = isolated_trial(args.market, args.ticker)
        args.trial_report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
        print(json.dumps({'report_path': str(args.trial_report),
            'results': [{'ticker': trial['identity']['ticker'], 'candidate': trial['candidate'],
                         'pending_reason': trial.get('pending_reason'),
                         'monthly_price_matches': len(trial['primary_action_and_monthly_price_gate']['price_matches']),
                         'normal_financials': {key: trial['normal_financial_diagnostic_only'][key]
                           for key in ('features_present', 'filing_id', 'reported_EPS', 'training_rows',
                                       'normal_financial_current_gate_passed')}}
                         for trial in (report['trials'] if args.all_targets else [report])],
            'core_edited': False}), flush=True)


if __name__ == '__main__':
    main()
