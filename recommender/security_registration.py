"""Offline, exact-code security evidence; never a current trading-status feed.

security_status(row, cachefolder, as_of) accepts a market root or a reusable
SecurityRegistry. Reads only existing files. No suffix, issuer-active, missing
quote, related ticker or ISIN-pattern inference establishes a security type.
FCA segment episodes are retained separately from whole-security delisting.
The optional CLI writes one new content-addressed evidence report, never an
inventory, model, source cache, existing report or active pointer.
"""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
import zipfile

from .issuer_registration import load_registration, registration_status
from .market_inventory import load_inventory, market_root


POLICY = 'RF-SECURITY-REGISTRATION-1'
EVIDENCE_ROOT = 'raw/primary-research/security-registration'
FCA_URL = 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/'
MASTER_FIELDS = ('CNPJ_CIA', 'DT_REFER', 'VERSAO', 'CD_CVM', 'ID_DOC', 'DT_RECEB')
SECURITY_FIELDS = ('CNPJ_Companhia', 'Data_Referencia', 'Versao', 'ID_Documento',
    'Nome_Empresarial', 'Valor_Mobiliario', 'Sigla_Classe_Acao_Preferencial',
    'Classe_Acao_Preferencial', 'Codigo_Negociacao', 'Mercado',
    'Sigla_Entidade_Administradora', 'Segmento', 'Data_Inicio_Negociacao',
    'Data_Fim_Negociacao', 'Data_Inicio_Listagem', 'Data_Fim_Listagem')
FOCUS = ('ABCB10', 'AZUL97', 'AZUL98', 'AZUL99', 'CRTE3', 'BRAX', 'EQMA3B', 'MRSA3B', 'ONCO11')


def _day(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('security_invalid_date')
    return date.fromisoformat(value)


def _norm(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value)
                           if not unicodedata.combining(c)).lower().split())


def _kind(row):
    kind = _norm(row['Valor_Mobiliario'])
    if kind == 'acoes ordinarias':
        return 'ordinary_share', 'ON'
    if kind == 'acoes preferenciais':
        reported = row['Sigla_Classe_Acao_Preferencial'].strip().upper()
        share = {'': 'PN', 'A': 'PNA', 'B': 'PNB', 'C': 'PNC', 'D': 'PND',
                 'PN': 'PN', 'PNA': 'PNA', 'PNB': 'PNB', 'PNC': 'PNC', 'PND': 'PND', 'PNU': 'PNU'}.get(reported)
        return 'preferred_share', share
    return {'units': 'unit', 'bonus de subscricao': 'subscription_warrant',
            'direitos de subscricao': 'subscription_right',
            'direito de subscricao': 'subscription_right'}.get(kind, 'other_reported_security'), None


def parse_fca_registry(content, year):
    """Keep every declared security episode, including contradictory ones."""
    if not isinstance(content, bytes) or len(content) > 20_000_000:
        raise ValueError('security_FCA_size_limit')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        def rows(name, fields):
            if archive.namelist().count(name) != 1 or archive.getinfo(name).file_size > 8_000_000:
                raise ValueError('security_FCA_member_limit')
            reader = csv.DictReader(io.StringIO(archive.read(name).decode('latin1')), delimiter=';')
            if not set(fields).issubset(reader.fieldnames or []):
                raise ValueError('security_FCA_schema_changed')
            result = []
            for number, row in enumerate(reader, 2):
                if None in row or any(row.get(field) is None for field in fields):
                    raise ValueError('security_FCA_row_misaligned')
                result.append({**row, '_csv_record': number})
            return result
        masters = rows(f'fca_cia_aberta_{year}.csv', MASTER_FIELDS)
        securities = rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv', SECURITY_FIELDS)
    by_id = {}
    for row in masters:
        if not re.fullmatch(r'\d+', row['CD_CVM']) or int(row['CD_CVM']) <= 0:
            raise ValueError('security_FCA_invalid_CVM')
        if not re.fullmatch(r'\d+', row['VERSAO']):
            raise ValueError('security_FCA_invalid_version')
        _day(row['DT_REFER'])
        _day(row['DT_RECEB'])
        if row['ID_DOC'] in by_id and by_id[row['ID_DOC']] != row:
            raise ValueError('security_FCA_conflicting_filing_id')
        by_id[row['ID_DOC']] = row
    for row in securities:
        filing = by_id.get(row['ID_Documento'])
        if (filing is None or row['CNPJ_Companhia'] != filing['CNPJ_CIA']
                or row['Data_Referencia'] != filing['DT_REFER'] or row['Versao'] != filing['VERSAO']):
            raise ValueError('security_FCA_child_identity_mismatch')
        for field in ('Data_Inicio_Negociacao', 'Data_Fim_Negociacao',
                      'Data_Inicio_Listagem', 'Data_Fim_Listagem'):
            if row[field]:
                _day(row[field])
    return masters, securities


class SecurityRegistry:
    """Reusable immutable-source reader; all decisions return detached records."""

    def __init__(self, folder, *, inventory=None):
        self.folder = market_root(folder)
        self.inventory = deepcopy(load_inventory(self.folder) if inventory is None else inventory)
        self.sources = {}
        self.fca = []
        for source in self.inventory.get('sources', []):
            url = source.get('url')
            match = (re.fullmatch(re.escape(FCA_URL) + r'fca_cia_aberta_(\d{4})\.zip', url)
                     if isinstance(url, str) else None)
            if not match:
                continue
            if not re.fullmatch(r'[a-f0-9]{64}', source.get('sha256', '')):
                raise ValueError('security_FCA_receipt_hash_missing')
            body, receipt = self.read(source['file'], expected=source['sha256'], url=source['url'],
                                      authority='CVM_FCA', observed_at=source.get('retrieved_at'))
            year = int(match[1])
            masters, securities = parse_fca_registry(body, year)
            self.fca.append((year, masters, securities, receipt))
        if not self.fca:
            raise ValueError('security_official_FCA_receipt_missing')
        self.registration, self.registration_receipt = None, None
        if (self.folder / 'raw/primary-research/registration/current.json').is_file():
            self.registration, self.registration_receipt = load_registration(self.folder)
        self.details = defaultdict(list)
        for path in sorted((self.folder / 'raw/expanded').glob('b3-detail-*.json')):
            relative = path.relative_to(self.folder).as_posix()
            body, receipt = self.read(relative, authority='existing_exchange_registry_cache')
            data = json.loads(body)
            if not isinstance(data, dict):
                continue
            cvm = data.get('codeCVM')
            if not isinstance(cvm, str) or not cvm.isdigit() or int(cvm) <= 0:
                continue
            for code in data.get('otherCodes', []):
                ticker, isin = code.get('code'), code.get('isin')
                if (isinstance(ticker, str) and re.fullmatch(r'[A-Z0-9.-]{1,30}', ticker)
                        and isinstance(isin, str) and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d', isin)):
                    self.details[ticker].append({'cvm': int(cvm), 'isin': isin, 'source': receipt,
                        'locator': 'otherCodes[code=' + ticker + ']',
                        'issuer_status_is_not_security_status': True})

    def read(self, relative, *, expected=None, url=None, authority, observed_at=None):
        if (not isinstance(relative, str) or not relative.startswith('raw/')
                or '..' in relative.replace('\\', '/').split('/')):
            raise ValueError('security_unsafe_cache_path')
        path = self.folder / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.folder) or path.stat().st_size > 20_000_000:
            raise ValueError('security_unsafe_cache_path')
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if expected is not None and digest != expected:
            raise ValueError('security_source_hash_mismatch')
        receipt = {'file': relative, 'sha256': digest, 'bytes': len(body), 'url': url,
                   'authority': authority, 'observed_at': observed_at,
                   'access': 'existing_cache_read_only', 'original_receipt_verified': expected is not None}
        self.sources[relative] = receipt
        return body, receipt

    def _fca_records(self, ticker, when):
        records, related, tables, latest = [], [], [], {}
        for year, masters, securities, source in self.fca:
            eligible = [r for r in masters if _day(r['DT_RECEB']) <= when and _day(r['DT_REFER']) <= when]
            child_ids = {r['ID_Documento'] for r in securities}
            for filing in eligible:
                cvm = int(filing['CD_CVM'])
                key = (filing['DT_REFER'], int(filing['VERSAO']), filing['DT_RECEB'])
                if cvm not in latest or key > latest[cvm][0]:
                    latest[cvm] = (key, filing['ID_DOC'], source['file'], filing['ID_DOC'] in child_ids)
            tables.append((year, {r['ID_DOC']: r for r in eligible}, securities, source))
        for year, by_id, securities, source in tables:
            for row in securities:
                filing = by_id.get(row['ID_Documento'])
                if filing is None:
                    continue
                cvm = int(filing['CD_CVM'])
                latest_id = latest[cvm][1]
                record = {'cvm': cvm, 'cnpj': filing['CNPJ_CIA'], 'filing_id': filing['ID_DOC'],
                    'published_at': filing['DT_RECEB'], 'reference_date': filing['DT_REFER'],
                    'version': int(filing['VERSAO']), 'year': year,
                    'is_latest_received_filing': (filing['ID_DOC'] == latest_id
                                                  and source['file'] == latest[cvm][2]),
                    'latest_child_table_available': latest[cvm][3],
                    'security': {key: row[key] for key in SECURITY_FIELDS},
                    'source': source, 'csv_record': row['_csv_record'],
                    'locator': f'fca_cia_aberta_valor_mobiliario_{year}.csv:'
                               f'ID_Documento={filing["ID_DOC"]};csv_record={row["_csv_record"]}'}
                if row['Codigo_Negociacao'].strip().upper() == ticker:
                    records.append(record)
                related.append(record)
        # A later annual archive can invalidate use of an older child table;
        # absence of the code in that archive still does not prove delisting.
        return records, related

    def _registry_type(self, ticker, when):
        # Reuse, do not replace or extend, the already reviewed UNIT/warrant registry.
        from .unit_composition import UNIT_COMPOSITIONS
        record = UNIT_COMPOSITIONS.get(ticker)
        if record is None or record.effective_from and _day(record.effective_from) > when:
            return None
        evidence = []
        for item in record.evidence:
            if item.published_at and _day(item.published_at) > when:
                continue
            _, receipt = self.read(item.file, expected=item.sha256, url=item.url,
                authority=item.authority)
            evidence.append({**receipt, 'locator': item.locator, 'published_at': item.published_at})
        if not evidence or (record.status == 'not_a_unit'
                and not any(item['authority'] == 'issuer_RI' for item in evidence)):
            return None
        if record.status == 'verified' and when < _day(record.as_of):
            return None
        return {'cvm': record.cvm, 'security_type': record.instrument_type,
                'expires_on': record.instrument_expires_on, 'evidence': evidence,
                'basis': 'existing_reviewed_unit_registry_not_model_admission'}

    def status(self, row, as_of):
        when = _day(as_of)
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9.-]{1,30}', ticker):
            raise ValueError('security_invalid_catalogue_code')
        result = {'policy': POLICY, 'ticker': ticker, 'as_of': when.isoformat(),
            'status': 'unresolved', 'security_type': None, 'type_confidence': 'unresolved',
            'share_class': None, 'cvm': None, 'cvm_basis': 'not_established', 'isin': None,
            'isin_basis': 'not_established', 'listing_status': 'unresolved',
            'market': None, 'episode_dates': [], 'evidence': [], 'issues': [],
            'exchange_trading_verified': False, 'whole_security_delisting_verified': False,
            'blocks_current_purchase': False, 'block_reasons': [],
            'model_admission_granted': False, 'catalogue_member_preserved': True,
            'retrospective_evidence_not_point_in_time_archive': True}
        records, all_records = self._fca_records(ticker, when)
        result['evidence'] = records
        current = [r for r in records if r['is_latest_received_filing']]
        if records and not current:
            result['issues'].append('latest_FCA_security_table_missing_or_exact_code_not_reported')
        cvms = {r['cvm'] for r in records}
        cache_identities = self.details.get(ticker, [])
        cvms.update(r['cvm'] for r in cache_identities)
        declared_cvm = row.get('cvm')
        reviewed = self._registry_type(ticker, when)
        if reviewed:
            cvms.add(reviewed['cvm'])
            result['reviewed_registry'] = reviewed
        if type(declared_cvm) is int and declared_cvm > 0:
            if cvms and declared_cvm not in cvms:
                cvms.add(declared_cvm)
            elif not cvms:
                result['issues'].append('catalogue_CVM_not_independently_verified_for_exact_code')
        if len(cvms) == 1:
            result['cvm'] = next(iter(cvms))
            result['cvm_basis'] = ('primary_exact_code' if records or reviewed
                                   else 'exact_code_existing_exchange_registry_cache')
        elif len(cvms) > 1:
            result['issues'].append('exact_code_CVM_identity_conflict')
        if cache_identities:
            result['cached_exchange_identity'] = cache_identities
            isins = {r['isin'] for r in cache_identities}
            if row.get('isin'):
                isins.add(row['isin'])
            if len(isins) == 1 and len(cvms) == 1:
                result.update(isin=next(iter(isins)),
                    isin_basis='exact_code_in_existing_exchange_cache_not_FCA_reported_ISIN')
            elif len(isins) > 1:
                result['issues'].append('exact_code_ISIN_identity_conflict')
        types = {_kind(r['security']) for r in current}
        if len(types) == 1:
            kind, share = next(iter(types))
            result.update(security_type=kind, share_class=share, type_confidence='confirmed_primary_exact_code',
                          status='primary_security_type_confirmed_not_current_trading')
            if kind == 'preferred_share' and share is None:
                result['issues'].append('reported_preferred_class_not_recognized_no_substitution')
        elif len(types) > 1:
            result['issues'].append('conflicting_exact_code_primary_security_types')
        if reviewed:
            if result['security_type'] not in (None, reviewed['security_type']):
                result['issues'].append('reviewed_registry_and_FCA_type_conflict')
            elif not types:
                result.update(security_type=reviewed['security_type'],
                    type_confidence='confirmed_existing_reviewed_primary_registry',
                    status='primary_security_type_confirmed_not_current_trading')
        if any('conflict' in issue for issue in result['issues']):
            result.update(status='conflicting_primary_or_exact_identity_evidence', security_type=None,
                          share_class=None, type_confidence='unresolved')
        markets = {_norm(r['security']['Mercado']) for r in current}
        market_names = {'bolsa': 'exchange', 'balcao organizado': 'organized_otc',
                        'balcao nao-organizado': 'unorganized_otc'}
        if len(markets) == 1:
            result['market'] = market_names.get(next(iter(markets)), 'other_reported_market')
        open_episodes = []
        for record in current:
            security = record['security']
            episode = {key: security[key] or None for key in ('Segmento', 'Data_Inicio_Negociacao',
                'Data_Fim_Negociacao', 'Data_Inicio_Listagem', 'Data_Fim_Listagem')}
            ended = any(security[k] and _day(security[k]) <= when
                        for k in ('Data_Fim_Negociacao', 'Data_Fim_Listagem'))
            starts = [security[k] for k in ('Data_Inicio_Negociacao', 'Data_Inicio_Listagem')]
            started = all(value and _day(value) <= when for value in starts)
            episode.update(filing_id=record['filing_id'], csv_record=record['csv_record'],
                           ended_by_reference=ended, started_by_reference=started)
            result['episode_dates'].append(episode)
            if started and not ended:
                open_episodes.append(episode)
        if open_episodes:
            result['listing_status'] = 'declared_open_episode_not_current_trading_confirmation'
        elif current and any(e['ended_by_reference'] for e in result['episode_dates']):
            result['listing_status'] = 'declared_closed_episodes_current_status_unresolved'
            result['issues'].append('closed_FCA_episode_alone_is_not_whole_security_delisting')
        elif current:
            result['listing_status'] = 'listing_start_not_established_at_reference'
        if result['security_type'] in ('subscription_warrant', 'subscription_right'):
            result['block_reasons'].append('subscription_instrument_outside_equity_income_scope')
        if self.registration is not None and result['cvm'] is not None:
            receipt = self.registration_receipt
            observed = datetime.fromisoformat(receipt['retrieved_at']).date()
            issuer = registration_status(self.registration, result['cvm'], when, observed_on=observed)
            result['issuer_registration'] = {**issuer, 'source': receipt}
            if issuer['registration_blocks_current_purchase']:
                result['block_reasons'].append(issuer['status'])
        if not records:
            result['issues'].append('exact_code_absent_from_available_FCA_security_rows_not_delisting')
            name = row.get('name')
            # Same name is only a research lead, never a code/alias equivalence.
            if name:
                candidates = [r for r in all_records if _norm(r['security']['Nome_Empresarial']) == _norm(name)]
                result['related_issuer_research_leads_not_code_identity'] = [
                    {'cvm': r['cvm'], 'reported_code': r['security']['Codigo_Negociacao'],
                     'filing_id': r['filing_id'], 'locator': r['locator'], 'source': r['source']}
                    for r in candidates]
        quote_file = f'raw/inventory/quotes/{ticker}.json'
        if (self.folder / quote_file).is_file():
            body, receipt = self.read(quote_file, authority='secondary_quote_provider_not_registration')
            quote = json.loads(body)
            values = quote.get('chart', {}).get('result') or []
            meta = values[0].get('meta', {}) if values and isinstance(values[0], dict) else {}
            if meta.get('symbol') == ticker + '.SA':
                result['secondary_provider_context'] = {
                    'reported_instrument_type': meta.get('instrumentType'), 'source': receipt,
                    'legal_security_type_or_code_alias_verified': False}
        result['blocks_current_purchase'] = bool(result['block_reasons'])
        return deepcopy(result)


def security_status(row, cachefolder, as_of):
    reader = cachefolder if isinstance(cachefolder, SecurityRegistry) else SecurityRegistry(cachefolder)
    return reader.status(row, as_of)


def evidence_report(folder, as_of):
    reader = SecurityRegistry(folder)
    rows = [reader.status(row, as_of) for row in reader.inventory['stocks']]
    daily_missing = {row['ticker'] for row in reader.inventory['stocks']
                     if row.get('model_pending_reason') == 'daily_prices_cache_missing'}
    by_code = {row['ticker']: row for row in rows}
    return {'schema_version': 1, 'policy': POLICY, 'as_of': _day(as_of).isoformat(),
        'created_at': datetime.now(timezone.utc).isoformat(), 'network_requests': 0,
        'active_model_changed': False, 'active_inventory_changed': False,
        'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'test_code_sha256': hashlib.sha256(Path(__file__).with_name('test_security_registration.py').read_bytes()).hexdigest(),
        'inventory_as_of': reader.inventory.get('as_of'),
        'counts': {'catalogue_codes': len(rows), 'preserved_codes': len(by_code),
            'type_confidence': dict(Counter(r['type_confidence'] for r in rows)),
            'security_type': dict(Counter(r['security_type'] or 'unresolved' for r in rows)),
            'market': dict(Counter(r['market'] or 'unresolved' for r in rows)),
            'listing_status': dict(Counter(r['listing_status'] for r in rows)),
            'blocked_current_purchase': sum(r['blocks_current_purchase'] for r in rows),
            'exact_CVM_identity_resolved': sum(r['cvm'] is not None for r in rows),
            'existing_cache_exact_ISIN_mappings': sum(r['isin'] is not None for r in rows),
            'confirmed_whole_security_delistings': 0, 'current_exchange_trading_confirmations': 0},
        'unresolved_primary_type_codes': [r['ticker'] for r in rows if r['security_type'] is None],
        'blocked_codes': [{'ticker': r['ticker'], 'reasons': r['block_reasons']}
                         for r in rows if r['blocks_current_purchase']],
        'documented_closed_episodes': [{'ticker': r['ticker'], 'listing_status': r['listing_status'],
            'episodes': [e for e in r['episode_dates'] if e['ended_by_reference']]}
            for r in rows if any(e['ended_by_reference'] for e in r['episode_dates'])],
        'focus': {ticker: by_code[ticker] for ticker in FOCUS if ticker in by_code},
        'daily_prices_cache_missing_codes': sorted(daily_missing),
        'daily_prices_cache_missing_summary': {
            'codes': len(daily_missing),
            'security_type': dict(Counter(by_code[t]['security_type'] or 'unresolved' for t in daily_missing)),
            'market': dict(Counter(by_code[t]['market'] or 'unresolved' for t in daily_missing)),
            'closed_episode_codes': [t for t in sorted(daily_missing)
                                    if any(e['ended_by_reference'] for e in by_code[t]['episode_dates'])]},
        'records': rows, 'sources': list(reader.sources.values()),
        'limitations': [
            'FCA reports issuer-declared security/listing episodes, not an up-to-date exchange trading feed.',
            'A closed segment episode and an active issuer registration do not establish whole-security status.',
            'Unknown codes, suffixes, similar names, missing prices and provider EQUITY labels do not establish security type.',
            'FCA does not supply ISIN in this table; cached exact-code exchange mappings are labelled separately.',
            'No model admission, financial indicators, payment events, price histories or catalogue exclusions are produced.']}


def write_evidence(folder, report):
    """One content-addressed output; no mutable current pointer is created."""
    root = market_root(folder)
    target = root / EVIDENCE_ROOT
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('security_unsafe_report_path')
    target.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(body).hexdigest()
    path = target / f'security-registration-{report["as_of"]}-{digest}.json'
    try:
        with path.open('xb') as stream:
            stream.write(body)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != body:
            raise ValueError('security_immutable_report_conflict')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--as-of', default=date.today().isoformat())
    parser.add_argument('--write-evidence', action='store_true')
    arguments = parser.parse_args()
    report = evidence_report(arguments.data_dir, arguments.as_of)
    result = {'counts': report['counts'], 'daily_prices_cache_missing_summary': report['daily_prices_cache_missing_summary']}
    if arguments.write_evidence:
        result['report'] = str(write_evidence(arguments.data_dir, report))
    print(json.dumps(result, ensure_ascii=True))
