"""Primary Schulz class-U/PN evidence; originals and registry labels stay intact."""

import argparse
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
import urllib.error
import urllib.request
import zipfile

from pypdf import PdfReader

from .dividend_scope import allowed_tickers
from .published_issuer_quotes import ROOT, MAX_BYTES, _NoRedirect
from .selected_issuer_sources import _dump, _require, _save, source_body


CACHE = 'raw/primary-research/schulz-share-class'
STATUTE_URL = 'https://api.mziq.com/mzfilemanager/v2/d/007e4c09-d021-4f52-95b7-a39e836a974c/90849c87-ba38-c1a0-c612-a81ee70fa7b8?origin=2'
STATUTE_SHA = '7eed7d70c942308b0a3dd56236c8d9a27efe75a865bfb8723cf5835f146df486'
IDENTITY = {'ticker': 'SHUL4', 'cvm': 14664, 'share_class': 'PNU', 'isin': 'BRSHULACNPR4'}
BINDING_ID = 'Schulz_registered_PNU_source_PN_exact_security_v1'


def _text(value):
    return ' '.join(unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower().split())


def _original(root):
    root = Path(root).resolve()
    path = root / CACHE / 'response.json'
    _require(not path.is_symlink() and path.resolve().is_relative_to(root), 'unsafe_statute_receipt')
    receipt = json.loads(path.read_bytes())
    relative = CACHE + '/original-' + STATUTE_SHA + '.pdf'
    _require(receipt['url'] == STATUTE_URL and receipt['file'] == relative
             and receipt['sha256'] == STATUTE_SHA and receipt['http_status'] == 200
             and receipt['valid_pdf_signature'] and not receipt['error'], 'statute_receipt_disagrees')
    path = root / relative
    _require(not path.is_symlink() and path.resolve().is_relative_to(root)
             and path.stat().st_size == receipt['bytes'] <= MAX_BYTES, 'unsafe_statute_original')
    body = path.read_bytes()
    _require(hashlib.sha256(body).hexdigest() == STATUTE_SHA, 'statute_original_changed')
    return body, receipt


def class_binding(root, identity, cutoff):
    """Bind one legal class to reported PN labels, without changing any value.

    Dated FCA declarations, not the undated statute, establish the historical
    registry binding. The statute is corroboration, not a PIT/publication claim.
    """
    from .selected_identity_research import research

    root = Path(root).resolve()
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    _require(cutoff == date(2026, 9, 30), 'unreviewed_class_binding_cutoff')
    _require(all(identity.get(key) == value for key, value in IDENTITY.items()), 'Schulz_exact_identity_required')
    _require('SHUL4' in (allowed_tickers(root) or ()), 'Schulz_outside_positive_yield_selection')
    body, statute = _original(root)
    pages = PdfReader(io.BytesIO(body)).pages
    text = _text(' '.join(page.extract_text() for page in pages[:2]))
    _require('84.693.183/0001-68' in text and '1466-4' in text
             and 'acoes preferenciais da classe existente' in text
             and 'dividendo 10% (dez por cento) maior' in text, 'statute_single_preferred_class_clause_missing')
    report = research(root, as_of=cutoff.isoformat(), tickers=('SHUL4',))
    _require(len(report['records']) == 1, 'Schulz_registry_evidence_ambiguous')
    record = report['records'][0]
    _require(record['cvm'] == 14664 and record['isin'] == IDENTITY['isin']
             and record['FCA_search_complete_for_2021_2026'], 'Schulz_registry_identity_disagrees')
    history, sources = [], {}
    rows = record['FCA_exact_code_records']
    _require({row['year'] for row in rows} == set(range(2021, 2027)), 'Schulz_FCA_class_history_incomplete')
    for row in sorted(rows, key=lambda item: item['year']):
        security, source = row['security'], row['source']
        _require(row['source_binding_valid'] and row['received_and_referenced_by_as_of']
                 and row['cvm'] == 14664 and security['Codigo_Negociacao'] == 'SHUL4'
                 and security['Sigla_Classe_Acao_Preferencial'] == 'PNU'
                 and security['Classe_Acao_Preferencial'] == 'Preferencial Classe U',
                 'Schulz_historical_class_disagrees')
        receipts = [item for item in source['existing_receipts'] if item.get('url') ==
                    f"https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{row['year']}.zip"]
        _require(bool(receipts) and all(item['sha256'] == source['sha256'] for item in receipts),
                 'Schulz_FCA_receipt_missing')
        archive = (root / source['file']).read_bytes()
        _require(hashlib.sha256(archive).hexdigest() == source['sha256'], 'Schulz_FCA_source_changed')
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            table = csv.DictReader(io.StringIO(bundle.read(row['security_member']).decode('latin1')), delimiter=';')
            preferred = [entry for entry in table if entry['ID_Documento'] == row['filing']['ID_DOC']
                         and _text(entry['Valor_Mobiliario']) == 'acoes preferenciais']
        _require(preferred and {entry['Sigla_Classe_Acao_Preferencial'] for entry in preferred} == {'PNU'},
                 'Schulz_multiple_preferred_classes_not_bound')
        receipt = {**receipts[0], 'retrieved_at': receipts[0].get('retrieved_at')}
        sources[receipt['file']] = receipt
        history.append({'year': row['year'], 'filing': row['filing'], 'member': row['security_member'],
                        'csv_record': row['csv_record'], 'registered_class': 'PNU',
                        'source_file': source['file'], 'source_sha256': source['sha256']})
    mappings = record['cached_exact_code_mappings']
    _require(mappings and all(item['cvm'] == 14664 and item['isin'] == IDENTITY['isin'] for item in mappings),
             'Schulz_exact_ISIN_mapping_disagrees')
    for item in mappings:
        source = item['source']
        sources[source['file']] = {**source, 'url': None, 'retrieved_at': None,
                                  'authority': 'existing_exchange_identity_cache_not_class_authority'}
    sources[statute['file']] = {key: statute[key] for key in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}
    return {'id': BINDING_ID, 'identity': IDENTITY.copy(), 'registered_class': 'PNU',
            'cash_source_class': 'PN', 'financial_source_class': 'PN',
            'financial_period_from': '2021-01-01', 'historical_registry': history,
            'sources': list(sources.values()), 'statute_pages': [1, 2],
            'statute_publication_verified': False, 'publication_date_inferred': False,
            'PNU_expanded_as_Unica': False, 'only_preferred_class_in_each_received_FCA': True,
            'EPS_computed': False, 'original_files_modified': False, 'model_admitted': False}


def cash_rows(rows, proof):
    _require(proof['id'] == BINDING_ID and proof['identity'] == IDENTITY, 'Schulz_cash_class_proof_required')
    result = deepcopy(rows)
    for row in result:
        if row.get('typeStock', '').strip() == 'PN':
            row.update(typeStock='PNU', reported_typeStock='PN', source_class_binding=BINDING_ID)
    return result


def acquire(root=ROOT):
    root = Path(root).resolve()
    _require('SHUL4' in (allowed_tickers(root) or ()), 'Schulz_outside_positive_yield_selection')
    base = root / CACHE
    base.mkdir(parents=True, exist_ok=True)
    _require(not base.is_symlink() and base.resolve().is_relative_to(root), 'unsafe_Schulz_cache')
    if (base / 'request.json').exists():
        _require((base / 'response.json').is_file(), 'interrupted_statute_acquisition_no_retry')
        receipt = json.loads((base / 'response.json').read_bytes())
        _require(receipt['url'] == STATUTE_URL and receipt['http_status'] == 200
                 and receipt['valid_pdf_signature'] and not receipt['error'], 'prior_statute_acquisition_stopped')
        return receipt
    robot = json.loads((root / 'raw/primary-research/selected-issuer-sources/response-006.json').read_bytes())
    _require(robot['url'] == 'https://api.mziq.com/robots.txt' and robot['status'] == 404
             and robot['completed_at'][:10] == datetime.now(timezone.utc).date().isoformat(),
             'fresh_provider_robots_observation_required')
    source_body(root, robot)
    request_record = {'url': STATUTE_URL, 'started_at': datetime.now(timezone.utc).isoformat(),
                      'discovery': 'public_primary_statute_search_not_claimed_as_literal_link_in_static_RI_HTML',
                      'discovery_context': 'https://ri.schulzsa.com/governanca-corporativa/estatuto-social-politicas-regimentos/',
                      'robots_observation': robot, 'automatic_retries': 0, 'redirects': False, 'authentication': 'none'}
    _save(base, 'request.json', _dump(request_record))
    status, body, error = None, b'', None
    try:
        request = urllib.request.Request(STATUTE_URL, headers={
            'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf', 'Accept-Encoding': 'identity'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=30) as response:
            status, body = response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as failure:
        status, body, error = failure.code, failure.read(MAX_BYTES + 1), type(failure).__name__
    except (ValueError, OSError) as failure:
        error = type(failure).__name__
    digest = hashlib.sha256(body).hexdigest()
    valid = len(body) <= MAX_BYTES and body.startswith(b'%PDF-')
    filename = f'original-{digest}.' + ('pdf' if valid else 'response.bin')
    _save(base, filename, body)
    receipt = {**request_record, 'http_status': status, 'valid_pdf_signature': valid, 'error': error,
               'file': CACHE + '/' + filename, 'sha256': digest, 'bytes': len(body),
               'retrieved_at': datetime.now(timezone.utc).isoformat(), 'published_at': None,
               'publication_date_inferred': False, 'model_admitted': False}
    _save(base, 'response.json', _dump(receipt))
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT)
    args = parser.parse_args()
    result = acquire(args.folder)
    print(json.dumps({key: result[key] for key in ('http_status', 'sha256', 'bytes', 'valid_pdf_signature')}, indent=2))
