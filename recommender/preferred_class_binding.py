"""Bind generic PN source labels to an exact sole registered PNA class.

The FCA legal class remains PNA. This is a read-only source-format view, not
an issuer-wide PN/PNA equivalence or a replacement for numerical checks.
"""
from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
from pathlib import Path
import re
import zipfile

from .security_registration import FCA_URL, _kind


CUTOFF = date(2026, 9, 30)
BINDING_ID = 'exact_single_preferred_PNA_source_PN_2021_2026_v1'
IDENTITIES = {
    'ABCB4': {'cvm': 20958, 'share_class': 'PNA', 'isin': 'BRABCBACNPR4'},
    'CMIG4': {'cvm': 2453, 'share_class': 'PNA', 'isin': 'BRCMIGACNPR3'},
    'DEXP4': {'cvm': 16632, 'share_class': 'PNA', 'isin': 'BRDEXPACNPR8'},
    'RSUL4': {'cvm': 13439, 'share_class': 'PNA', 'isin': 'BRRSULACNPR3'},
    'TASA4': {'cvm': 6173, 'share_class': 'PNA', 'isin': 'BRTASAACNPR4'},
}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _read(root, source):
    relative = source['file']
    _require(isinstance(relative, str) and relative.startswith('raw/')
             and '\\' not in relative and ':' not in relative
             and not set(relative.split('/')) & {'', '.', '..'}, 'unsafe_preferred_class_evidence')
    path = root / relative
    _require(path.resolve().is_relative_to(root) and not path.is_symlink()
             and path.stat().st_size <= 20_000_000, 'unsafe_preferred_class_evidence')
    body = path.read_bytes()
    _require(hashlib.sha256(body).hexdigest() == source['sha256'], 'preferred_class_evidence_changed')
    return body


def class_binding(root, identity, cutoff):
    from .selected_identity_research import research

    root = Path(root).resolve()
    when = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    ticker = identity.get('ticker')
    _require(when == CUTOFF and ticker in IDENTITIES, 'unreviewed_preferred_class_binding')
    expected = IDENTITIES[ticker]
    _require(type(identity.get('cvm')) is int
             and all(identity.get(key) == value for key, value in expected.items()),
             'preferred_class_exact_identity_required')
    report = research(root, as_of=when.isoformat(), tickers=(ticker,))
    _require(len(report['records']) == 1, 'preferred_class_registry_ambiguous')
    record = report['records'][0]
    _require(record['status'] == 'primary_exact_identity_evidence'
             and record['cvm'] == expected['cvm'] and record['isin'] == expected['isin']
             and record['FCA_search_complete_for_2021_2026'], 'preferred_class_registry_conflict')
    history = record['FCA_exact_code_records']
    _require({r['year'] for r in history} == set(range(2021, 2027)), 'preferred_class_history_incomplete')
    sources, binding_rows = {}, []
    for entry in sorted(history, key=lambda r: r['year']):
        security, filing, source = entry['security'], entry['filing'], entry['source']
        _require(entry['source_binding_valid'] and entry['received_and_referenced_by_as_of']
                 and entry['cvm'] == expected['cvm']
                 and security['Codigo_Negociacao'] == ticker
                 and _kind(security) == ('preferred_share', 'PNA'), 'preferred_class_historical_identity_changed')
        receipts = [s for s in source['existing_receipts']
                    if s.get('url') == FCA_URL + f"fca_cia_aberta_{entry['year']}.zip"]
        _require(receipts and all(s['sha256'] == source['sha256'] for s in receipts),
                 'preferred_class_official_receipt_required')
        archive = _read(root, source)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            masters = csv.DictReader(io.StringIO(zipped.read(entry['master_member']).decode('latin1')), delimiter=';')
            master_rows = [r for r in masters if r['ID_DOC'] == filing['ID_DOC']]
            _require(len(master_rows) == 1 and int(master_rows[0]['CD_CVM']) == expected['cvm'],
                     'preferred_class_master_identity_mismatch')
            master = master_rows[0]
            table = csv.DictReader(io.StringIO(zipped.read(entry['security_member']).decode('latin1')), delimiter=';')
            preferred = [r for r in table if r['ID_Documento'] == filing['ID_DOC']
                         and _kind(r)[0] == 'preferred_share']
        _require(preferred and {_kind(r)[1] for r in preferred} == {'PNA'}
                 and {r['Codigo_Negociacao'].strip() for r in preferred} == {ticker},
                 'multiple_preferred_classes_or_codes_not_bound')
        _require(all(r['CNPJ_Companhia'] == master['CNPJ_CIA']
                     and r['Data_Referencia'] == filing['DT_REFER']
                     and r['Versao'] == filing['VERSAO'] for r in preferred),
                 'preferred_class_child_identity_mismatch')
        sources[source['file']] = {**receipts[0], 'retrieved_at': receipts[0].get('retrieved_at')}
        binding_rows.append({'year': entry['year'], 'filing': filing,
                             'member': entry['security_member'], 'csv_record': entry['csv_record'],
                             'registered_class': 'PNA', 'file': source['file'], 'sha256': source['sha256']})
    mappings = record['cached_exact_code_mappings']
    _require(mappings and all(r['cvm'] == expected['cvm'] and r['isin'] == expected['isin'] for r in mappings),
             'preferred_class_exact_ISIN_mapping_required')
    for mapping in mappings:
        source = mapping['source']
        _read(root, source)
        sources[source['file']] = {**source, 'url': None, 'retrieved_at': None,
                                  'authority': 'existing_exact_identity_cache_not_legal_class_authority'}
    return {'id': BINDING_ID, 'identity': {'ticker': ticker, **expected},
            'registered_class': 'PNA', 'cash_source_class': 'PN', 'financial_source_class': 'PN',
            'financial_period_from': '2021-01-01', 'historical_registry': binding_rows,
            'only_preferred_class_in_each_received_FCA': True, 'sources': list(sources.values()),
            'numeric_values_changed': False, 'EPS_computed': False, 'original_files_modified': False,
            'publication_date_inferred': False, 'point_in_time_archive_verified': False,
            'model_admitted': False}


def cash_rows(rows, proof):
    identity = proof['identity']
    ticker = identity['ticker']
    _require(proof['id'] == BINDING_ID and ticker in IDENTITIES
             and identity == {'ticker': ticker, **IDENTITIES[ticker]}, 'preferred_cash_binding_required')
    classes = {r.get('typeStock', '').strip() for r in rows}
    _require(not (classes - {'ON', 'PN', 'PNA'}), 'unexpected_preferred_cash_class')
    _require(not ({'PN', 'PNA'} <= classes), 'ambiguous_double_preferred_cash_class')
    output = deepcopy(rows)
    for row in output:
        if row.get('typeStock', '').strip() in ('PN', 'PNA'):
            _require(all(not row.get(field) or row[field] == identity['isin']
                         for field in ('isinCode', 'isin')), 'preferred_cash_ISIN_conflict')
        if row.get('typeStock', '').strip() == 'PN':
            row.update(typeStock='PNA', reported_typeStock='PN', source_class_binding=BINDING_ID)
    return output
