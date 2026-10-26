"""Isolated primary share-action and native quote evidence for seven securities.

Acquisition uses only PublicSourceCollector's round2-actions scope. Offline
loaders and replay never change core code, original data, or published pointers.
"""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path
import re
import socket
import unicodedata
import zipfile
from urllib.parse import parse_qs, urlsplit

from lxml import html
from pypdf import PdfReader

from .bulk_pending_sources import PublicSourceCollector
from .pending_all_prices import primary_index_candidates, published_history_request, vendor_rows


ROOT = Path(__file__).parent / 'data/market'
CACHE = 'raw/primary-research/all-pending/round2-actions'
CUTOFF = date(2026, 9, 30)
START = date(2021, 9, 30)
IDENTITIES = {
    'ITSA3': (7617, 'ON', 'BRITSAACNOR0'),
    'ITSA4': (7617, 'PN', 'BRITSAACNPR7'),
    'BMEB3': (1325, 'ON', 'BRBMEBACNOR8'),
    'BMEB4': (1325, 'PN', 'BRBMEBACNPR5'),
    'DEXP3': (16632, 'ON', 'BRDEXPACNOR1'),
    'DEXP4': (16632, 'PNA', 'BRDEXPACNPR8'),
    'IRBR3': (24180, 'ON', 'BRIRBRACNOR4'),
}
PROVIDER = 'round2_primary_actions_and_issuer_published_native_Enfoque'
INDEX_SHA = '2c5c6cddbe0028afe7399457e254c84e11dcb42dc99bd1eb19c4b669a6cfc1db'
READINGS = (
    # Literal ratios are additional shares for bonuses, total shares otherwise.
    (7617, '2021-12-21', 'same_species_bonus', 5, 100,
     '365a2eee5330153c1e7e0eca4cc2b61d6ed71a5b6bf9d028fa01c39a39b3156e', 450473,
     ((2, '5 (cinco) acoes novas para cada 100 (cem) acoes da mesma especie'),
      (2, 'a partir de 21.12.2021, as acoes passarao a ser negociadas'))),
    (7617, '2022-11-11', 'same_species_bonus', 1, 10,
     '2448479ce6d70409682dfdd2ddb93fa745208367a5c9517870ae3b85acf8dfac', 559339,
     ((2, '1 (uma) acao nova para cada 10 (dez) acoes da mesma especie'),
      (2, 'a partir de 11.11.2022, as acoes passarao a ser negociadas'))),
    (7617, '2023-11-28', 'same_species_bonus', 5, 100,
     '3073f62261c9b7d21f69b4243bbe461e716483588d182124550deef6529e6929', 690534,
     ((2, '5 (cinco) acoes novas para cada 100 (cem) acoes da mesma especie'),
      (2, 'a partir de 28.11.2023, as acoes passarao a ser negociadas'))),
    (7617, '2024-12-03', 'same_species_bonus', 5, 100,
     '4e2fe4e92d8fe5e27563423b748e58e860422d49b23299b46879ef28ac3e69ec', 827097,
     ((1, '5 (cinco) acoes novas para cada 100 (cem) acoes da mesma especie'),
      (2, 'a partir de 03.12.2024, as acoes passarao a ser negociadas'))),
    (7617, '2025-12-19', 'same_species_bonus', 2, 100,
     '6416d5797960e11efaef846aa3063f0102db942b13819b4d93d3dafbc15ae5ce', 982208,
     ((1, '2 (duas) acoes novas para cada 100 (cem) acoes da mesma especie'),
      (1, 'a partir de 19.12.2025, as acoes passarao a ser negociadas'))),
    (1325, '2021-12-03', 'simultaneous_reverse_split_and_split', 200, 100,
     'ba945d4e2c486a708b4ec76a4cb2233acc89d7afe86dddaf7e9c2094b6c0ca9a', 432674,
     ((1, 'cada grupo de 100 (cem) acoes sera grupada em 01 (uma) acao da mesma especie'),
      (1, 'cada acao sera desdobrada em 200 (duzentas) acoes da mesma especie'),
      (1, 'serao negociadas a partir do dia 03 de dezembro'))),
    (16632, '2025-12-22', 'same_species_bonus', 1, 8,
     '70935fbb94f00837bd1d52456644dbb7300e19ef78615e81ce333f04241e07fc', 980692,
     ((2, 'proporcao de 1 nova acao para cada 8 acoes existentes'),
      (2, 'A partir do dia 22 de dezembro de 2025'),
      (2, 'As novas acoes ordinarias e preferenciais a serem bonificadas'))),
    (24180, '2023-01-25', 'reverse_split', 1, 30,
     '7768766559ab1fe3d24f62de505f1f19a393b57b8aeb17ddd8721a9ee8640e03', 568804,
     ((1, '30 (trinta) acoes convertidas em 1 (uma) acao'),
      (1, 'A partir do dia 25 de janeiro de 2023'))),
)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True,
                       allow_nan=False) + '\n').encode('ascii')


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKD', value).encode(
        'ascii', 'ignore').decode().lower().split())


def read(root, relative, digest):
    root = Path(root).resolve()
    require(isinstance(relative, str) and relative.startswith('raw/') and ':' not in relative
            and '\\' not in relative and not set(relative.split('/')) & {'', '.', '..'},
            'round2_unsafe_evidence_path')
    path = root / relative
    require(path.resolve().is_relative_to(root) and path.is_file()
            and all(not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)()
                    for p in (path, *path.parents) if p.is_relative_to(root)),
            'round2_unsafe_or_missing_original')
    require(path.stat().st_size <= 30_000_000, 'round2_original_size_limit')
    body = path.read_bytes()
    require(sha(body) == digest, 'round2_original_hash_changed')
    return body


def receipt_index(root):
    """Enumerate metadata only; callers hash-check the chosen native original."""
    base = Path(root) / 'raw/primary-research'
    results = []
    for pattern in ('*/response-*.json', 'all-pending/*/response-*.json'):
        for path in sorted(base.glob(pattern)):
            if path.stat().st_size > 2_000_000:
                continue
            value = json.loads(path.read_bytes())
            if isinstance(value, dict) and all(k in value for k in ('file', 'sha256', 'url')):
                results.append({**value, 'receipt_file': path.relative_to(root).as_posix(),
                                'receipt_sha256': sha(path.read_bytes())})
    return results


def existing(root, url, receipts):
    matches = [r for r in receipts if r['url'] == url]
    good = [r for r in matches if r.get('status', r.get('http_status')) == 200
            and not r.get('error') and not r.get('stop')]
    if not good:
        return None
    chosen = good[0]
    read(root, chosen['file'], chosen['sha256'])
    return chosen


def save(root, kind, value):
    body = encoded(value)
    relative = f'{CACHE}/{kind}-{sha(body)}.json'
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'round2_immutable_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return relative


def acquire(root=ROOT):
    root = Path(root).resolve()
    collector = PublicSourceCollector(root, 'round2-actions')
    receipts = receipt_index(root)
    results = []

    def fetch(url, discovery):
        prior = existing(root, url, receipts)
        if prior:
            result = prior
        else:
            try:
                result = collector.fetch(url, discovery=discovery)
            except ValueError as error:
                result = {'url': url, 'error': str(error), 'discovery': discovery}
        results.append(result)
        print(json.dumps({k: result.get(k) for k in ('url', 'status', 'http_status', 'error', 'sha256')}), flush=True)
        return result

    sequences = {450478, 553889, 690546, 827101, 982224, 568804,
                 450473, 553885, 559339, 690534, 827097, 982208}
    for item in primary_index_candidates(root, {7617, 24180}, sequences):
        fetch(item['url'], {'kind': 'literal_CVM_final_published_notice', **item})
    fetch('https://api.mziq.com/mzfilemanager/v2/d/afd9200b-9b01-4d1c-bdd9-9b2c1e9b3b4d/26e834b2-7fca-13e0-f41a-dfebd48c7df9?origin=2',
          {'kind': 'indexed_literal_issuer_final_board_minutes', 'cvm': 7617,
           'meeting_date': '2025-12-15', 'CVM_sequence': 982208,
           'discovery_query': 'Itausa 19.12.2025 ata',
           'CVM_response_was_HTML_not_PDF': True})
    fetch('https://api.mziq.com/mzfilemanager/v2/d/afd9200b-9b01-4d1c-bdd9-9b2c1e9b3b4d/8df7a32b-5a88-1114-6f2f-89c9b9013c73?origin=1',
          {'kind': 'literal_issuer_subscription_calendar_link', 'cvm': 7617,
           'source_sha256': '6632ca2356abc6900c846e12238060344d727d2c4a9f8673e3628ae087178c59',
           'declaration_date': '2025-02-10', 'record_date': '2025-02-17'})
    mercantil = 'https://ri.bancomercantil.com.br/informacoes-aos-investidores/graficos-e-cotacoes-bmeb4/'
    fetch(mercantil, {'kind': 'literal_link_from_cached_issuer_BMEB3_page',
                      'source_sha256': '653dfa88a4491f01b6e65081cf076e39990ad59d13f8373df723ed2ff97d76a8'})
    dex_file = 'raw/primary-research/issuer-raw-quotes/body-6a4feaba5443a2313d413eb61d724d4c5e411e5b472ff4ad5c3d2564baf105bd.bin'
    tree = html.fromstring(read(root, dex_file, '6a4feaba5443a2313d413eb61d724d4c5e411e5b472ff4ad5c3d2564baf105bd'))
    for node in tree.xpath('//a[@href]'):
        label = ' '.join(node.text_content().split())
        if re.search('cotac|gr.fico', normalized(label)) or re.search('/1094[0-4]-2/', node.get('href')):
            fetch(node.get('href'), {'kind': 'literal_issuer_navigation', 'source_file': dex_file,
                  'xpath': tree.getroottree().getpath(node), 'label': label})
    return save(root, 'acquisition', {'results': results, 'prior_stops': sorted(collector.prior_stops),
                                     'model_admission': False})


def acquire_quotes(root=ROOT):
    """New date coverage comes from the provider's literal from/to controls."""
    root = Path(root).resolve()
    collector = PublicSourceCollector(root, 'round2-actions')
    receipts = receipt_index(root)
    script = next(r for r in receipts if r['sha256'] ==
                  '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
                  and r['url'] == 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js')
    results = []
    for ticker in ('BMEB4', 'BMEB3'):
        page_url = ('https://ri.bancomercantil.com.br/informacoes-aos-investidores/'
                    + ('graficos-e-cotacoes-bmeb4/' if ticker == 'BMEB4' else 'graficos-e-cotacoes/'))
        page = existing(root, page_url, receipts)
        require(page is not None, 'round2_published_widget_page_missing')
        url, proof = published_history_request(read(root, page['file'], page['sha256']), page,
                    read(root, script['file'], script['sha256']), script, ticker)
        # The prior five-year BMEB3 request timed out. Annual inputs are a new
        # supported coverage request, with the exact published provider ID.
        ranges = [('2021-09-01', '2026-09-30')] if ticker == 'BMEB4' else [
            ('2021-09-01', '2021-12-31'), ('2022-01-01', '2022-12-31'),
            ('2023-01-01', '2023-12-31'), ('2024-01-01', '2024-12-31'),
            ('2025-01-01', '2025-12-31'), ('2026-01-01', '2026-09-30')]
        for first, last in ranges:
            target = url.replace('from=2021-09-01&to=2026-09-30', f'from={first}&to={last}')
            try:
                result = existing(root, target, receipts) or collector.fetch(target, discovery={
                    **proof, 'coverage_from': first, 'coverage_to': last,
                    'coverage_policy': 'native_provider_from_to_template_no_denied_URL_retry'})
            except ValueError as error:
                result = {'url': target, 'error': str(error)}
            results.append(result)
            print(json.dumps({k: result.get(k) for k in ('url', 'status', 'error', 'sha256')}), flush=True)
            if result.get('status', result.get('http_status')) != 200 or result.get('error'):
                break
    return save(root, 'quote-acquisition', {'results': results, 'model_admission': False})


def seal(root=ROOT):
    """Create a reviewable index; loading still requires its code-pinned hash."""
    from .market_inventory import load_inventory
    from .pending_b_independent import FCA_PINS
    from .published_issuer_quotes import PAGE_URL
    root = Path(root).resolve()
    receipts = receipt_index(root)
    inventory = load_inventory(root)
    sources = {}

    def add_file(relative, **metadata):
        body = (root / relative).read_bytes()
        ref = {'file': relative, 'sha256': sha(body), 'bytes': len(body),
               'url': None, 'retrieved_at': None, 'published_at': None, **metadata}
        read(root, relative, ref['sha256'])
        return ref

    def add_receipt(receipt):
        if receipt is None:
            return None
        ref = {k: receipt[k] for k in ('file', 'sha256', 'bytes', 'url',
                                        'receipt_file', 'receipt_sha256')}
        ref['retrieved_at'] = receipt.get('retrieved_at', receipt.get('completed_at'))
        ref['published_at'] = None
        read(root, ref['file'], ref['sha256'])
        sources[ref['sha256'] + ':' + ref['url']] = ref
        return ref

    needed_hashes = {row[5] for row in READINGS}
    needed_hashes.update({
        'e4f2ee5a5c736669deef9744b0814c6066410c59220abeb80a70208099cd3c68',
        '7f4bb90dcc1d9182cde2db8e14b4315ef470d2688525b9d0acdf742e680d17e5',
        '104ca2fb50b35b5890bbc8bd0bb7d33d5e3cb38293a8adf1e215337c9216c1c0',
        'bbe33b1e48eeb385efba4ddeca000c96609983d13a8de43e9fd33948cc7f7ec9',
        '6632ca2356abc6900c846e12238060344d727d2c4a9f8673e3628ae087178c59',
    })
    for digest in sorted(needed_hashes):
        found = next((r for r in receipts if r['sha256'] == digest
                      and r.get('status', r.get('http_status')) == 200), None)
        require(found is not None, 'round2_required_original_missing:' + digest)
        add_receipt(found)
    for receipt in receipts:
        if '8df7a32b-5a88-1114-6f2f-89c9b9013c73' in receipt['url']:
            add_receipt(receipt)
    script = add_receipt(next(r for r in receipts if r['sha256'] ==
        '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
        and r['url'] == 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js'))
    fca = []
    for year, digest in FCA_PINS.items():
        relative = ('raw/inventory/fca_cia_aberta_2026.zip' if year == 2026 else
                    f'raw/primary-research/units/fca_cia_aberta_{year}-{digest}.zip')
        ref = add_file(relative, year=year, authority='primary_CVM_FCA')
        require(ref['sha256'] == digest, 'round2_FCA_pin_disagrees')
        fca.append(ref)
    targets = {}
    urls = {
        'ITSA3': PAGE_URL, 'ITSA4': PAGE_URL,
        'BMEB3': 'https://ri.bancomercantil.com.br/informacoes-aos-investidores/graficos-e-cotacoes/',
        'BMEB4': 'https://ri.bancomercantil.com.br/informacoes-aos-investidores/graficos-e-cotacoes-bmeb4/',
        'IRBR3': 'https://ri.irbre.com/servicos-aos-investidores/cotacoes-e-graficos/',
    }
    for ticker, (cvm, share, isin) in IDENTITIES.items():
        stock, = [r for r in inventory['stocks'] if r['ticker'] == ticker]
        require((stock['cvm'], stock['share_class'], stock['isin']) == (cvm, share, isin),
                'round2_inventory_identity_changed')
        quotes = {}
        for receipt in receipts:
            if ('/history?' in receipt['url'] and f'&tickers={ticker}&adjusted=false' in receipt['url']
                    and receipt.get('status', receipt.get('http_status')) == 200 and not receipt.get('error')):
                quotes.setdefault(receipt['url'], add_receipt(receipt))
        cash = [add_file(p.relative_to(root).as_posix(), authority='existing_native_exchange_cash_cache')
                for p in sorted((root / 'raw/expanded').glob(ticker + '-b3-cash-*.json'))]
        targets[ticker] = {'identity': dict(ticker=ticker, cvm=cvm, share_class=share, isin=isin),
            'detail': add_file(f'raw/expanded/b3-detail-{cvm}.json', authority='cached_exact_ISIN_identity'),
            'comparison': add_file(stock['history']['source_file'],
                                   url=stock['history']['source_url'], authority='original_comparison_feed'),
            'cash': cash, 'quotes': sorted(quotes.values(), key=lambda r: r['url']),
            'configuration': add_receipt(existing(root, urls[ticker], receipts)) if ticker in urls else None}
    indices = primary_index_candidates(root, {7617, 1325, 16632, 24180}, {r[6] for r in READINGS})
    packet = {'schema_version': 1, 'cutoff': CUTOFF.isoformat(), 'required_months': 61,
              'targets': targets, 'sources': list(sources.values()), 'fca': fca,
              'CVM_index_rows': indices, 'script': script, 'model_admission': False}
    relative = save(root, 'index', packet)
    return {'file': relative, 'sha256': sha((root / relative).read_bytes())}


def packet(root):
    require(INDEX_SHA is not None, 'round2_reviewed_index_not_pinned')
    value = json.loads(read(root, f'{CACHE}/index-{INDEX_SHA}.json', INDEX_SHA))
    require(value['schema_version'] == 1 and value['cutoff'] == CUTOFF.isoformat()
            and value['required_months'] == 61 and set(value['targets']) == set(IDENTITIES),
            'round2_index_contract_changed')
    return value


def checked_source(root, ref):
    body = read(root, ref['file'], ref['sha256'])
    require(len(body) == ref['bytes'], 'round2_original_length_changed')
    if 'receipt_file' in ref:
        receipt = json.loads(read(root, ref['receipt_file'], ref['receipt_sha256']))
        require(all(receipt.get(k) == ref[k] for k in ('url', 'file', 'sha256', 'bytes'))
                and receipt.get('status', receipt.get('http_status')) == 200
                and not receipt.get('error') and not receipt.get('stop'),
                'round2_native_receipt_contract_changed')
        timestamp = receipt.get('retrieved_at', receipt.get('completed_at'))
        require(timestamp == ref['retrieved_at'] and timestamp is not None,
                'round2_native_retrieval_missing')
        observed = datetime.fromisoformat(timestamp)
        require(observed.utcoffset() is not None and observed <= datetime.now(timezone.utc),
                'round2_native_retrieval_invalid')
    return body


def exact_identity(root, identity, data):
    ticker = identity['ticker']
    spec = data['targets'][ticker]
    require(type(identity.get('cvm')) is int and all(identity.get(k) == v for k, v in spec['identity'].items()),
            'round2_exact_CVM_class_ISIN_mismatch')
    from .dividend_scope import allowed_tickers
    require(ticker in (allowed_tickers(root) or ()), 'round2_outside_selected_scope')
    detail = json.loads(checked_source(root, spec['detail']))
    require(int(detail['codeCVM']) == identity['cvm'] and not detail.get('hasBDR')
            and [r['isin'] for r in detail['otherCodes'] if r['code'] == ticker] == [identity['isin']],
            'round2_cached_exact_security_identity_disagrees')
    proof = []
    for ref in data['fca']:
        year = ref['year']
        with zipfile.ZipFile(io.BytesIO(checked_source(root, ref))) as archive:
            def rows(member):
                return list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
            masters = [r for r in rows(f'fca_cia_aberta_{year}.csv')
                       if int(r['CD_CVM']) == identity['cvm'] and r['DT_RECEB'] <= CUTOFF.isoformat()]
            require(masters, 'round2_FCA_issuer_missing')
            master = max(masters, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            member = f'fca_cia_aberta_valor_mobiliario_{year}.csv'
            security = [(line, r) for line, r in enumerate(rows(member), 2)
                        if r['ID_Documento'] == master['ID_DOC'] and r['Codigo_Negociacao'] == ticker]
            require(security, 'round2_FCA_exact_ticker_missing')
            for _, r in security:
                share = ('ON' if normalized(r['Valor_Mobiliario']) == 'acoes ordinarias' else
                         r['Sigla_Classe_Acao_Preferencial'] or 'PN')
                require(share == identity['share_class']
                        and re.sub(r'\D', '', r['CNPJ_Companhia']) == detail['cnpj'],
                        'round2_FCA_CNPJ_or_share_class_disagrees')
            proof.append({'year': year, 'filing_id': master['ID_DOC'], 'received_at': master['DT_RECEB'],
                          'source': ref, 'member': member,
                          'rows': [{'csv_line': line, 'native_row': r} for line, r in security]})
    return proof


def compact(value):
    return re.sub('[^a-z0-9]', '', normalized(value))


def clause(text, fragment, source, page):
    """Map whitespace-fragmented PDF text back to literal native offsets."""
    characters, offsets = [], []
    for index, char in enumerate(text):
        for folded in compact(char):
            characters.append(folded)
            offsets.append(index)
    target = compact(fragment)
    start = ''.join(characters).find(target)
    require(start >= 0, 'round2_literal_primary_clause_changed:' + fragment)
    first, last = offsets[start], offsets[start + len(target) - 1] + 1
    return {'file': source['file'], 'sha256': source['sha256'], 'page': page,
            'text_start': first, 'text_end': last, 'literal': text[first:last],
            'page_text_sha256': sha(text.encode('utf-8'))}


def _primary_actions(root, identity, cutoff, data, class_proof):
    spec = data['targets'][identity['ticker']]
    prices, observed = _comparison(root, spec)
    actions, readings, sources = [], [], [spec['detail'], spec['comparison'], *data['fca']]
    for cvm, ex_date, kind, new, old, digest, sequence, fragments in READINGS:
        if cvm != identity['cvm']:
            continue
        refs = [r for r in data['sources'] if r['sha256'] == digest]
        require(len(refs) == 1, 'round2_primary_notice_ambiguous')
        ref = refs[0]
        body = checked_source(root, ref)
        require(body.startswith(b'%PDF-'), 'round2_primary_native_PDF_required')
        indexed = [r for r in data['CVM_index_rows'] if f'numSequencia={sequence}&' in r['url']]
        require(len(indexed) == 1, 'round2_primary_CVM_index_row_missing')
        index = indexed[0]
        with zipfile.ZipFile(io.BytesIO(checked_source(root, index['index_source']))) as archive:
            rows = csv.DictReader(io.StringIO(archive.read(index['member']).decode('latin1')), delimiter=';')
            row = next(r for line, r in enumerate(rows, 2) if line == index['csv_line'])
        require(row == index['index_row'] and int(row['Codigo_CVM']) == identity['cvm']
                and row['Link_Download'] == index['url'] and row['Data_Entrega'] <= cutoff.isoformat(),
                'round2_primary_publication_or_CVM_index_changed')
        require(re.sub(r'\D', '', row['CNPJ_Companhia']) == class_proof[-1]['rows'][0]['native_row']['CNPJ_Companhia'].replace('.', '').replace('/', '').replace('-', ''),
                'round2_primary_notice_CNPJ_disagrees')
        if sequence != 982208:
            require(ref['url'] == index['url'], 'round2_primary_notice_index_URL_disagrees')
        else:
            require('/afd9200b-9b01-4d1c-bdd9-9b2c1e9b3b4d/26e834b2-' in ref['url'],
                    'round2_primary_issuer_document_binding_disagrees')
        pdf = PdfReader(io.BytesIO(body))
        locators = [clause(pdf.pages[page - 1].extract_text(), fragment, ref, page)
                    for page, fragment in fragments]
        if kind == 'same_species_bonus' and cvm == 7617:
            require('exdireitoabonificacao' in compact(pdf.pages[fragments[-1][0] - 1].extract_text()),
                    'round2_explicit_ex_bonus_negotiation_clause_required')
        factor = Fraction(old + new, old) if kind == 'same_species_bonus' else Fraction(new, old)
        actions.append({'date': ex_date, 'ratio': float(factor)})
        readings.append({'identity': spec['identity'], 'kind': kind, 'date': ex_date,
            'total_share_factor': {'numerator': factor.numerator, 'denominator': factor.denominator},
            'literal_ratio_units': {'new_or_bonus_shares': new, 'old_shares': old,
                                   'bonus_is_additional': kind == 'same_species_bonus'},
            'published_at': row['Data_Entrega'], 'publication_basis': 'native_CVM_index_delivery_date',
            'source': ref, 'locators': locators, 'CVM_index': index,
            'literal_ratio_and_effective_date_verified': True, 'factor_fitted_to_prices': False,
            'version': row['Versao'], 'receipt_file': ref['receipt_file'],
            'same_species_rule': 'explicit_same_species_or_issuer_ordinary_preferred_classes'})
        sources.extend([ref, index['index_source'], {'file': ref['receipt_file'],
                        'sha256': ref['receipt_sha256'], 'bytes': (root / ref['receipt_file']).stat().st_size,
                        'url': None, 'retrieved_at': None}])
    relevant = [r for r in observed if '2021-09-01' <= r['date'] <= cutoff.isoformat()]
    require(len(relevant) == len(actions) and all(a['date'] == b['date']
            and math.isclose(a['ratio'], b['ratio'], rel_tol=1e-12, abs_tol=0)
            for a, b in zip(sorted(relevant, key=lambda r: r['date']), sorted(actions, key=lambda r: r['date']))),
            'round2_observed_actions_disagree_with_primary_notices')
    return {'actions': sorted(actions, key=lambda r: r['date']), 'readings': readings,
        'observed_actions': observed, 'outside_window_observations_retained': [r for r in observed if r not in relevant],
        'sources': sources, 'identity': spec['identity'], 'class_history': class_proof,
        'blocked': False, 'window_start': '2021-09-01', 'window_end': cutoff.isoformat(),
        'record_or_credit_date_used_as_ex_date': False, 'factor_fitted_to_prices': False,
        'cash_or_EPS_adjustment_authorized': False, 'model_admission': False}


def primary_actions(folder, identity, cutoff=CUTOFF):
    root = Path(folder).resolve()
    require(cutoff == CUTOFF and identity.get('ticker') in IDENTITIES, 'round2_action_scope_unsupported')
    data = packet(root)
    class_proof = exact_identity(root, identity, data)
    return _primary_actions(root, identity, cutoff, data, class_proof)


def _comparison(root, spec):
    from .broad_stocks import split_price_history
    body = checked_source(root, spec['comparison'])
    result = json.loads(body)['chart']['result'][0]
    require(result['meta']['symbol'] == spec['identity']['ticker'] + '.SA'
            and result['meta']['currency'] == 'BRL' and result['meta']['dataGranularity'] == '1d',
            'round2_comparison_exact_daily_identity_disagrees')
    days = result['timestamp']
    require(len(days) == len(set(days)) == len(result['indicators']['quote'][0]['close']),
            'round2_comparison_duplicate_or_missing_timestamp')
    return split_price_history(body, spec['identity']['ticker'], CUTOFF, allow_recent_actions=True)


def _native_quotes(root, spec, data):
    ticker = spec['identity']['ticker']
    if not spec['quotes']:
        return {}, [], [], None
    config, script = spec['configuration'], data['script']
    require(config is not None, 'round2_native_issuer_configuration_missing')
    page_body, script_body = checked_source(root, config), checked_source(root, script)
    if ticker.startswith('ITSA'):
        from .published_issuer_quotes import request_proof
        request = request_proof(root, ticker, require_fresh_robots=False)
        require(request['issuer_configuration']['sha256'] == config['sha256']
                and request['public_provider_template']['sha256'] == script['sha256'],
                'round2_itausa_exact_published_template_changed')
        url, proof = request['url'], request
    else:
        url, proof = published_history_request(page_body, config, script_body, script, ticker)
    require('R$' in page_body.decode('utf-8'), 'round2_native_BRL_display_missing')
    quotes, inactive = {}, []
    for ref in spec['quotes']:
        # BMEB3 annual requests must differ only by the documented date inputs.
        if ref['url'] != url:
            actual, expected = urlsplit(ref['url']), urlsplit(url)
            query = parse_qs(actual.query, strict_parsing=True)
            require(ticker == 'BMEB3' and actual._replace(query='') == expected._replace(query='')
                    and set(query) == {'from', 'to', 'tickers', 'adjusted'}
                    and query['tickers'] == [ticker] and query['adjusted'] == ['false']
                    and len(query['from']) == len(query['to']) == 1
                    and (query['from'][0], query['to'][0]) in {
                        ('2021-09-01', '2021-12-31'), ('2022-01-01', '2022-12-31'),
                        ('2023-01-01', '2023-12-31'), ('2024-01-01', '2024-12-31'),
                        ('2025-01-01', '2025-12-31'), ('2026-01-01', '2026-09-30')},
                    'round2_native_history_URL_disagrees')
        body = checked_source(root, ref)
        rows, nontrades = vendor_rows(body, ticker)
        query = parse_qs(urlsplit(ref['url']).query, strict_parsing=True)
        require(all(query['from'][0] <= row['date'] <= query['to'][0] for row in rows + nontrades),
                'round2_native_quote_outside_actual_request_coverage')
        for row in rows:
            day = date.fromisoformat(row['date'])
            require(day not in quotes, 'round2_duplicate_native_quote_date')
            quotes[day] = float(Decimal(row['native_close_decimal']))
        inactive.extend(nontrades)
    return quotes, inactive, [config, script, *spec['quotes']], proof


def _monthly(quotes):
    from .collect_market_data import month_end
    result = {}
    for day, value in sorted(quotes.items()):
        if date(2021, 9, 1) <= day <= CUTOFF:
            result[month_end(day.year, day.month)] = (day, value)
    return result


def _quote_audit(root, spec, quotes, actions, prices):
    from .collect_market_data import shift_month
    from .share_basis import factor_after
    monthly = _monthly(quotes)
    checks = []
    for month in sorted(shift_month(CUTOFF, -i) for i in range(61)):
        observed, value = prices[month][:2]
        other_day, native = monthly.get(month, (None, None))
        factor = factor_after(observed, actions, CUTOFF)
        checks.append({'month': month.isoformat(), 'comparison_date': observed.isoformat(),
            'comparison_close': value, 'issuer_native_date': other_day.isoformat() if other_day else None,
            'issuer_native_close': native, 'documented_factor_after': factor,
            'date_matches': observed == other_day,
            'raw_matches': native is not None and math.isclose(value, native, rel_tol=.006, abs_tol=.015),
            'split_adjusted_matches': native is not None and math.isclose(value, native / factor, rel_tol=.006, abs_tol=.015)})
    modes = [mode for mode in ('split_adjusted', 'raw')
             if all(r['date_matches'] and r[mode + '_matches'] for r in checks)]
    original = json.loads(checked_source(root, spec['comparison']))['chart']['result'][0]
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): value for stamp, value in zip(
        original['timestamp'], original['indicators']['quote'][0]['close']) if value is not None
        and date(2021, 9, 1) <= datetime.fromtimestamp(stamp, timezone.utc).date() <= CUTOFF}
    daily_conflicts = []
    for day in sorted(set(daily) | set(quotes)):
        value, native = daily.get(day), quotes.get(day)
        factor = factor_after(day, actions, CUTOFF)
        raw_match = value is not None and native is not None and math.isclose(value, native, rel_tol=.006, abs_tol=.015)
        split_match = value is not None and native is not None and math.isclose(value, native / factor, rel_tol=.006, abs_tol=.015)
        if not raw_match or not split_match:
            daily_conflicts.append({'date': day.isoformat(), 'comparison_close': value,
                'issuer_native_close': native, 'raw_matches': raw_match, 'split_adjusted_matches': split_match,
                'documented_factor_after': factor})
    regimes = []
    for factor in sorted({r['documented_factor_after'] for r in checks}):
        rows = [r for r in checks if r['documented_factor_after'] == factor]
        regimes.append({'documented_factor_after': factor, 'model_months': [r['month'] for r in rows],
            'all_dates_agree': all(r['date_matches'] for r in rows),
            'all_raw_prices_agree': all(r['raw_matches'] for r in rows),
            'all_split_adjusted_prices_agree': all(r['split_adjusted_matches'] for r in rows)})
    return {'monthly_input_count': len(checks), 'native_month_count': len(monthly), 'monthly_inputs': checks,
        'modes_agreeing_all61_dates_and_prices': modes, 'used_regimes': regimes,
        'monthly_conflicts': [r for r in checks if not r['date_matches'] or not r['split_adjusted_matches']],
        'monthly_conflicts_by_mode': {mode: [r['month'] for r in checks
            if not r['date_matches'] or not r[mode + '_matches']] for mode in ('raw', 'split_adjusted')},
        'daily_conflict_counts_by_mode': {mode: sum(not r[mode + '_matches'] for r in daily_conflicts)
                                          for mode in ('raw', 'split_adjusted')},
        'all_daily_regime_comparisons_retained': daily_conflicts,
        'tolerances_unchanged': {'relative': .006, 'absolute_BRL': .015},
        'selection_rule': 'last_native_observation_independently_in_each_of_all_61_months',
        'favorable_months_selected': False, 'actions_fitted_to_quotes': False}


def inspect_evidence(folder, identity, cutoff=CUTOFF):
    """Return strict proof plus unresolved conflicts, without granting input use."""
    if identity.get('ticker') not in IDENTITIES or cutoff != CUTOFF:
        return None
    root, data = Path(folder).resolve(), packet(folder)
    class_proof = exact_identity(root, identity, data)
    spec = data['targets'][identity['ticker']]
    prices, observed = _comparison(root, spec)
    proof = _primary_actions(root, identity, cutoff, data, class_proof)
    quotes, inactive, sources, discovery = _native_quotes(root, spec, data)
    audit = _quote_audit(root, spec, quotes, observed, prices)
    blockers = []
    if not quotes:
        blockers.append('round2_native_history_not_published_or_recovered')
    elif audit['native_month_count'] != 61:
        blockers.append('round2_native_history_requires_all61_months')
    if not audit['modes_agreeing_all61_dates_and_prices']:
        blockers.append('round2_all61_model_monthly_dates_or_prices_disagree')
    audit.update(currency='BRL', currency_basis='literal_issuer_R$_display_exact_ISIN_and_FCA_binding',
                 vendor_currency_field_present=False, adjusted_parameter=False,
                 prices_transformed=False, paid_subscriptions_used_as_split_factors=False)
    evidence_sources = [*sources, *proof['sources']]
    for source in sources:
        if 'receipt_file' in source:
            receipt_body = read(root, source['receipt_file'], source['receipt_sha256'])
            evidence_sources.append({'file': source['receipt_file'], 'sha256': source['receipt_sha256'],
                                     'bytes': len(receipt_body), 'url': None, 'retrieved_at': None})
    return {'provider': PROVIDER, 'identity': spec['identity'], 'primary_actions': proof,
        'daily': quotes, 'raw_quotes': {day: value for day, value in _monthly(quotes).values()},
        'sources': evidence_sources, 'inactive_native_rows': inactive,
        'request_discovery': discovery, 'checks': audit, 'blockers': blockers,
        'model_admission': False, 'point_in_time_archive_verified': False,
        'settlement_verified': False, 'EPS_or_financial_scope_modified': False}


def validate_primary_actions(folder, identity, cutoff, observed_actions):
    proof = primary_actions(folder, identity, cutoff)
    require(observed_actions == proof['observed_actions'], 'round2_caller_action_list_disagrees')
    return proof


def load_evidence(folder, identity, cutoff, observed_actions=None,
                  independent_quotes=None, comparison_content=None):
    """Quote-provider contract for the unchanged comparison-feed price gate.

    All 61 months must share one documented raw/split-adjusted regime. Every
    original independent cash-page quote is retained and cross-checked too.
    This loader does not substitute native prices for conflicting model months.
    """
    evidence = inspect_evidence(folder, identity, cutoff)
    if evidence is None:
        return None
    require(not evidence['blockers'], evidence['blockers'][0] if evidence['blockers'] else 'round2_evidence_blocked')
    if observed_actions is not None:
        require(observed_actions == evidence['primary_actions']['observed_actions'],
                'round2_caller_action_list_disagrees')
    data = packet(folder)
    spec = data['targets'][identity['ticker']]
    if comparison_content is not None:
        require(sha(comparison_content) == spec['comparison']['sha256'], 'round2_caller_comparison_hash_changed')
    references = []
    if independent_quotes is not None:
        for day, value in sorted(independent_quotes.items()):
            if START <= day <= cutoff:
                other = evidence['daily'].get(day)
                agrees = other is not None and math.isclose(value, other, rel_tol=.006, abs_tol=.015)
                references.append({'date': day.isoformat(), 'original_independent_quote': value,
                                   'issuer_native_quote': other, 'agrees': agrees})
        require(all(r['agrees'] for r in references), 'round2_original_independent_quote_conflict')
    evidence['checks']['all_original_independent_quotes'] = references
    evidence['checks']['whole_61_month_comparison_gate_passed'] = True
    return evidence


def documentary_mismatches(folder):
    """Keep literal disagreements and paid subscription rights out of factors."""
    root, data = Path(folder).resolve(), packet(folder)

    def ref(digest):
        found = [r for r in data['sources'] if r['sha256'] == digest]
        require(len(found) == 1, 'round2_documentary_comparison_source_missing')
        return found[0]

    def pdf_clause(digest, page, fragment):
        source = ref(digest)
        document = PdfReader(io.BytesIO(checked_source(root, source)))
        return clause(document.pages[page - 1].extract_text(), fragment, source, page)

    calendar = ref('6632ca2356abc6900c846e12238060344d727d2c4a9f8673e3628ae087178c59')
    tree = html.fromstring(checked_source(root, calendar))
    rows = [n for n in tree.xpath('//table//tr') if '13/12/2021' in n.text_content()
            and '18,191662' in n.text_content()]
    require(len(rows) == 1, 'round2_original_calendar_tax_cost_discrepancy_changed')
    calendar_locator = {'file': calendar['file'], 'sha256': calendar['sha256'],
                        'xpath': tree.getroottree().getpath(rows[0]),
                        'literal': ' '.join(rows[0].text_content().split())}
    cost = pdf_clause('365a2eee5330153c1e7e0eca4cc2b61d6ed71a5b6bf9d028fa01c39a39b3156e',
                      2, 'R$ 18,891662 por acao')
    count_a = pdf_clause('104ca2fb50b35b5890bbc8bd0bb7d33d5e3cb38293a8adf1e215337c9216c1c0',
                         1, '516.407.741 novas acoes')
    count_b = pdf_clause('4e2fe4e92d8fe5e27563423b748e58e860422d49b23299b46879ef28ac3e69ec',
                         1, '516.407.471 novas acoes')
    subscriptions = []
    for digest, record, ex_date, page, ratio_page, ratio in (
        ('bbe33b1e48eeb385efba4ddeca000c96609983d13a8de43e9fd33948cc7f7ec9',
         '2023-08-17', '2023-08-18', 8, 8, '1,390757436%'),
        ('632dfb2785473c8208cb59ab014c1b089cc22dc6c51ae0b6bdc45415f5b57345',
         '2025-02-17', '2025-02-18', 8, 7, '1,3766678%'),
    ):
        literal_day = date.fromisoformat(ex_date).strftime('%d.%m.%Y')
        locators = [pdf_clause(digest, page, 'partir do dia ' + literal_day),
                    pdf_clause(digest, page, 'ex-direitos'), pdf_clause(digest, ratio_page, ratio)]
        subscriptions.append({'identity': {k: v for k, v in IDENTITIES.items() if k.startswith('ITSA')},
            'kind': 'paid_subscription_right_not_free_share_bonus', 'record_date': record,
            'ex_rights_date': ex_date, 'subscription_percent_literal': ratio,
            'source': ref(digest), 'locators': locators, 'split_factor': None,
            'price_adjustment_factor': None, 'holder_exercise_or_cash_entitlement_assumed': False,
            'economic_effect_on_comparison_prices_not_fitted_or_asserted': True})
    return {'discrepancies': [
        {'kind': '2021_issuer_calendar_tax_cost_typo', 'calendar_decimal': '18.191662',
         'final_notice_decimal': '18.891662', 'evidence': [calendar_locator, cost],
         'tax_cost_is_not_a_quote_or_action_ratio': True},
        {'kind': '2024_total_new_share_count_disagreement', 'fact_notice': 516407741,
         'board_minutes': 516407471, 'ordinary': 177465112, 'preferred': 338942359,
         'evidence': [count_a, count_b], 'ratio_5_per_100_agrees_in_both': True,
         'share_count_or_EPS_denominator_imputed': False},
        {'kind': '2022_board_minutes_version_2_supersedes_version_1',
         'initial_source': ref('e4f2ee5a5c736669deef9744b0814c6066410c59220abeb80a70208099cd3c68'),
         'final_source': ref('2448479ce6d70409682dfdd2ddb93fa745208367a5c9517870ae3b85acf8dfac'),
         'changed_fields': 'reserve_allocation_amounts_swapped_on_page_1',
         'ratio_and_ex_date_identical': True, 'final_published_version_used': 2}],
        'subscription_rights': subscriptions, 'price_or_EPS_changes_made': False}


def json_safe(value):
    if isinstance(value, dict):
        return {key.isoformat() if isinstance(key, date) else key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def protected_hashes(root):
    return {relative: sha((root / relative).read_bytes()) for relative in (
        'current.json', 'inventory/current.json', 'stock-selection/current.json')}


def _cash_failure_diagnostic(root, row, cache, actions):
    """Identify the next ordinary cash gate failure without repairing amounts."""
    from . import inventory_candidates as candidates
    from .cash_calendar_research import apply_research_calendar, deduplicate_independent_rows
    from .share_basis import b3_evidence, factor_after
    _, codes = candidates._identity(row, cache, CUTOFF)
    rows = candidates._cash_pages(row['ticker'], codes, row['cvm'], cache)
    rows = candidates._reviewed_cash_view(row, cache, CUTOFF, rows, {})
    rows, _ = deduplicate_independent_rows(row, cache, CUTOFF, rows)
    expected, _ = b3_evidence(rows, row['share_class'])
    primary = cache.primary.primary_events(row, CUTOFF) if cache.primary else {}
    original, _ = candidates._payments(row['ticker'], cache, CUTOFF, primary, identity=row)
    original = apply_research_calendar(row, cache, CUTOFF, original, rows, actions)
    conflicts = []
    for day, amount in sorted(expected.items()):
        if not START <= day <= CUTOFF:
            continue
        installments = [r for r in original.installments if r['record_date'] == day.isoformat()]
        reported = sum(r['gross_per_unit'] for r in installments)
        factors = {1.0, factor_after(day, actions, CUTOFF)}
        factors.update(factor_after(day, actions, date.fromisoformat(a['date']))
                       for a in actions if a['date'] >= day.isoformat())
        if not installments or not any(math.isclose(reported, amount / f, rel_tol=.002, abs_tol=.00002)
                                       for f in factors):
            conflicts.append({'record_date': day.isoformat(), 'independent_gross_amount': amount,
                'reported_installment_sum': reported if installments else None,
                'installments': installments, 'primary_documented_basis_factors': sorted(factors),
                'native_exchange_rows': [r for r in rows if r.get('typeStock') == row['share_class']
                    and r.get('lastDatePriorEx') == day.strftime('%d/%m/%Y')],
                'net_to_gross_inference_or_repair': False})
    return {'conflicts': conflicts, 'gate': 'ordinary_reconcile_payments',
            'cash_tolerances_unchanged': {'relative': .002, 'absolute_BRL': .00002},
            'source_receipts': cache.all_receipts(), 'amounts_or_dates_changed': False}


def isolated_one(folder, identity, *, financial_bundle=None):
    """Run ordinary _one with a quote-source dispatcher in this process only.

    The dispatcher calls the original reconcile_prices with every native model
    anchor after validating every supplied exchange anchor. No gate, original
    price body, exchange row, financial parser, or payment table is replaced.
    """
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from . import issuer_independent_evidence as issuer
    from .collect_market_data import (DividendHistory, financial_features, shift_month, stock_observations)
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    before = protected_hashes(root)
    inventory = load_inventory(root)
    row, = [r for r in inventory['stocks'] if r['ticker'] == identity['ticker']]
    require(all(row.get(k) == identity.get(k) for k in ('ticker', 'cvm', 'share_class', 'isin')),
            'round2_replay_inventory_identity_disagrees')
    cache, checks = candidates._Cache(root, inventory), {}
    old_prices, old_actions = candidates.reconcile_prices, issuer.validate_primary_actions
    quote_calls = []

    def dispatch(content, ticker, actions, quotes, cutoff, **kwargs):
        if ticker != row['ticker']:
            return old_prices(content, ticker, actions, quotes, cutoff, **kwargs)
        require(kwargs.get('coverage_start') in (None, START), 'round2_extended_window_not_proven')
        evidence = load_evidence(root, row, cutoff, actions, quotes, content)
        for source in evidence['sources']:
            cache.register_research_evidence(source)
        quote_calls.append({'provider': PROVIDER, 'checks': evidence['checks']})
        checks['round2_primary_and_native_quote_evidence'] = {
            'provider': PROVIDER, 'all61_model_months_verified': True,
            'primary_actions': evidence['primary_actions'], 'sources': evidence['sources'],
            'all_original_independent_quotes': evidence['checks']['all_original_independent_quotes']}
        return old_prices(content, ticker, actions, evidence['raw_quotes'], cutoff,
                          **{**kwargs, 'evidence_provider': PROVIDER})

    def action_dispatch(folder, security, cutoff, actions):
        if security.get('ticker') == row['ticker']:
            return validate_primary_actions(folder, security, cutoff, actions)
        return old_actions(folder, security, cutoff, actions)

    def forbidden(*args, **kwargs):
        raise RuntimeError('round2_offline_replay_network_forbidden')

    result = {'identity': identity, 'candidate': False, 'model_admission': False,
              'normal_financial_current_pass': False, 'source_result': None}
    candidate = None
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden):
        try:
            with patch.object(candidates, 'reconcile_prices', dispatch), patch.object(issuer, 'validate_primary_actions', action_dispatch):
                candidate = candidates._one(row, cache, CUTOFF, checks)
            result.update(candidate=True, source_result='candidate', ledger_rows=len(candidate['ledger']),
                          independent_price_matches=len(candidate['independent_price_matches']),
                          independent_cash_matches=len(candidate['independent_cash_matches']))
        except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
            result['source_result'] = str(error)
        statements, diagnostics, sources = (financial_bundle if financial_bundle is not None
                                            else prepare_financials(root, [row], CUTOFF))
        exact = [s for s in statements if s['cvm'] == row['cvm']
                 and s.get('share_class', row['share_class']) == row['share_class']
                 and s['published_at'] <= CUTOFF.isoformat()]
        latest = max(exact, key=lambda r: (r['fiscal_end'], int(r['version'])), default=None)
        prices, actions = _comparison(root, packet(root)['targets'][row['ticker']])
        if result['source_result'] == 'cash_amount_or_share_scale_disagrees_with_B3':
            result['cash_failure_diagnostic'] = _cash_failure_diagnostic(root, row, cache, actions)
        if candidate is not None:
            payments, prices = candidate['payments'], candidate['prices']
            cash_basis = 'ordinary_independently_reconciled_candidate_payments'
        else:
            payments = DividendHistory(CUTOFF, 0, basis='payment')
            # Unknown candidate cash is an explicit blocker, never a zero target.
            payments.issues.append({'event_index': 'round2_source_candidate_unresolved',
                'interval_start': '1900-01-01', 'interval_end': CUTOFF.isoformat(),
                'reason': result['source_result']})
            cash_basis = 'unresolved_source_candidate_cash_not_zero'
        spec = row['cvm'], row['share_class'], row['sector_code']
        current = financial_features(statements, payments, CUTOFF, spec)
        observations, coverage = stock_observations(row['ticker'], prices, payments, statements, CUTOFF, spec=spec)
        basis = max((a['date'] for a in actions), default='2014-01-01')
        admitted = [r for r in observations if r['as_of'] >= shift_month(START, 12).isoformat()
                    and r['fiscal_end'] >= basis]
        current_pass = (candidate is not None and current is not None
                        and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
        result['normal_financial_current_pass'] = current_pass
        result['financial_replay'] = {
            'unchanged_prepare_financials_called': True, 'unchanged_financial_features_called': True,
            'unchanged_stock_observations_called': True, 'latest_statement': latest,
            'current': current, 'latest_action_basis_start': basis,
            'cash_basis': cash_basis, 'source_candidate_required': True,
            'current_issues': payments.blockers(shift_month(CUTOFF, -12), CUTOFF),
            'training_rows': len(admitted), 'diagnostic_observation_count': len(observations),
            'training_filing_ids': sorted({r['filing_id'] for r in admitted}),
            'current_cash_window': coverage['current_cash_window'], 'EPS_imputed': False,
            'scope_mixed': False, 'latest_unusable_filing_replaced_with_older': False,
            'diagnostics': [d for d in diagnostics if d.get('cvm') in (None, row['cvm'])],
            'sources': sources}
    require(candidates.reconcile_prices is old_prices and issuer.validate_primary_actions is old_actions,
            'round2_replay_hooks_not_restored')
    after = protected_hashes(root)
    require(before == after, 'round2_replay_protected_pointer_changed')
    result.update(checks=checks, quote_dispatch_calls=quote_calls,
        sources=cache.all_receipts(), protected_pointer_hashes=after, hooks_restored=True,
        unchanged_ordinary_one_called=True, gates_patched=False, original_JSON_modified=False,
        core_source_edited=False, network_calls=0, source_and_current_financial_pass=bool(current_pass))
    return json_safe(result)


def replay(folder=ROOT):
    from unittest.mock import patch
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    rows = [r for r in load_inventory(root)['stocks'] if r['ticker'] in IDENTITIES]
    require(len(rows) == 7, 'round2_all_seven_replay_identities_required')

    def forbidden(*args, **kwargs):
        raise RuntimeError('round2_offline_replay_network_forbidden')

    results = []
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden):
        bundle = prepare_financials(root, rows, CUTOFF)
        for row in rows:
            print(json.dumps({'stage': 'ordinary_isolated_one_and_financial_replay', 'ticker': row['ticker']}), flush=True)
            results.append(isolated_one(root, {k: row[k] for k in ('ticker', 'cvm', 'share_class', 'isin')},
                                        financial_bundle=bundle))
    return {'cutoff': CUTOFF.isoformat(), 'required_months': 61, 'targets': results,
            'source_candidate_count': sum(r['candidate'] for r in results),
            'source_and_current_financial_pass_count': sum(r['source_and_current_financial_pass'] for r in results),
            'model_admissions': 0, 'published_pointers_written': 0, 'network_calls': 0,
            'core_modified': False, 'production_integration_by_parent_required': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', type=Path, default=ROOT)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--acquire-quotes', action='store_true')
    parser.add_argument('--seal', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--replay', action='store_true')
    args = parser.parse_args(argv)
    if args.acquire:
        print(acquire(args.market))
    if args.acquire_quotes:
        print(acquire_quotes(args.market))
    if args.seal:
        print(json.dumps(seal(args.market)))
    if args.report:
        require(args.report.name.startswith('actions-') and args.report.suffix == '.json',
                'round2_report_must_use_assigned_actions_prefix')
        before = protected_hashes(args.market)
        records = [json_safe(inspect_evidence(args.market, dict(ticker=t, cvm=c, share_class=s, isin=i)))
                   for t, (c, s, i) in IDENTITIES.items()]
        report = {'scope': 'round2_actions_seven_exact_equities', 'cutoff': CUTOFF.isoformat(),
                  'required_months': 61, 'records': records, 'documentary_mismatches': documentary_mismatches(args.market),
                  'replay': replay(args.market) if args.replay else None,
                  'producer_sha256': sha(Path(__file__).read_bytes()), 'index_sha256': INDEX_SHA,
                  'model_admissions': 0, 'published_pointers_written': 0,
                  'protected_pointer_hashes': before}
        require(protected_hashes(args.market) == before, 'round2_protected_pointer_changed')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(encoded(json_safe(report)))
        print(json.dumps({'report': str(args.report), 'replay': report['replay'] and {
            k: report['replay'][k] for k in ('source_candidate_count', 'source_and_current_financial_pass_count')}}))


if __name__ == '__main__':
    main()
