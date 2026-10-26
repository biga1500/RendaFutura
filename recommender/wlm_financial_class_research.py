"""Bounded WLM PN/PNA financial-label audit, not an authorized crosswalk.

The native annual note was obtained by the authorized second HTTPS attempt.
Its published class numerators/denominators do not reconcile with reported EPS.
No production module imports this research helper; it admits no instrument.
"""

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from . import inventory_financials as native
from .collect_market_data import cvm_statements, normalize


ROOT = Path(__file__).parent / 'data/market'
OUTPUT = 'raw/primary-research/wlm-financial-class'
CUTOFF = date(2026, 9, 30)
IDENTITY = {'ticker': 'WLMM4', 'cvm': 11070, 'cnpj': '33.228.024/0001-51',
            'share_class': 'PNA', 'isin': 'BRWLMMACNPR3', 'sector_code': 5}
EXPECTED = ('2025-12-31', '1', '155797', '2026-03-27', IDENTITY['cnpj'])
FCA_FILES = {
    2025: ('raw/primary-research/units/fca_cia_aberta_2025-'
           'd3bf01b0f751aa001fba318adad7b8fb8f2d7cc84a4c5b03741b9e8ef9cadf37.zip',
           'd3bf01b0f751aa001fba318adad7b8fb8f2d7cc84a4c5b03741b9e8ef9cadf37'),
    2026: ('raw/inventory/fca_cia_aberta_2026.zip',
           'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'),
}
ANNUAL_FILE = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_HASH = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
DRE_HASH = '14fca8b74d7242eaf7e6c6ae9f791a30defec3caa48ddca2ccfbca9dd2847c72'
CONTAINER_HASH = 'b6bbd172e861c3fd35704648f9aefefe3eb3bc1b858758968113d6393b62db6e'
PDF_HASH = '56c7ae71e2b71a004f895bd8c69320d6fa3ace3a83a4128ae1ffb79d8f04003d'
XML_HASH = '54d2bc73901f74469a2a02234f78062e05d2e80276cf21e533489e5e861c433d'


def _immutable_json(path, value):
    encoded = (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == encoded, 'immutable_proof_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(encoded)


def fetch_native_once(folder=ROOT, *, https_transport_phase=False):
    """One authorized master URL, bounded bytes, same-host redirects, no retry."""
    folder = Path(folder)
    root = folder / OUTPUT
    root.mkdir(parents=True, exist_ok=True)
    stem = 'native-https' if https_transport_phase else 'native'
    _require(not (root / ('request-' + stem + '.json')).exists(), 'native_request_already_attempted')
    content, _ = _receipt(folder, ANNUAL_FILE, url=None, expected=ANNUAL_HASH)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        master = latest_filing(list(native._rows(archive, 'dfp_cia_aberta_2025.csv')))
    master_url = master['LINK_DOC']
    url = master_url
    prior = None
    if https_transport_phase:
        _require(len(list(root.glob('request-native*.json'))) == 1,
                 'cumulative_native_budget_exhausted_or_prior_missing')
        prior_request = (root / 'request-native.json').read_bytes()
        prior_response = (root / 'response-native.json').read_bytes()
        first_request, first_response = json.loads(prior_request), json.loads(prior_response)
        _require(first_request['url'] == first_response['url'] == master_url
                 and first_response['status'] is None and first_response['bytes'] == 0
                 and 'ConnectionResetError' in first_response['failure']
                 and first_response['redirects'] == [], 'HTTPS_transport_phase_not_authorized')
        original = urllib.parse.urlsplit(master_url)
        _require(original.scheme == 'http', 'transport_upgrade_must_be_HTTP_to_HTTPS_only')
        url = urllib.parse.urlunsplit(original._replace(scheme='https'))
        prior = {'logical_attempt': 1, 'request': first_request, 'response': first_response,
                 'request_file': 'request-native.json', 'response_file': 'response-native.json',
                 'request_sha256': hashlib.sha256(prior_request).hexdigest(),
                 'response_sha256': hashlib.sha256(prior_response).hexdigest()}
    else:
        _require(not list(root.glob('request-native*.json')), 'native_request_already_attempted')
    parsed = urllib.parse.urlsplit(url)
    _require(parsed.hostname == 'www.rad.cvm.gov.br' and parsed.scheme in ('http', 'https')
             and parsed.username is None, 'exact_public_master_URL_required')
    request = {'url': url, 'master_filing_id': '155797', 'master_version': '1',
               'master_url_raw': master_url, 'scheme_upgrade_only': https_transport_phase,
               'logical_attempt': 2 if https_transport_phase else 1,
               'cumulative_authorized_request_budget': 2 if https_transport_phase else 1,
               'published_at': master['DT_RECEB'], 'started_at': datetime.now(timezone.utc).isoformat(),
               'maximum_response_bytes': 25_000_000, 'retries': 0,
               'policy': 'one exact observed master URL; same host only; stop denial/auth/oversize'}
    _immutable_json(root / ('request-' + stem + '.json'), request)
    redirects = []

    class SameHost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            destination = urllib.parse.urlsplit(newurl)
            _require(len(redirects) < 3 and destination.hostname == parsed.hostname
                     and destination.scheme in ('http', 'https') and destination.username is None,
                     'redirect_outside_same_host_or_budget')
            redirects.append({'from': req.full_url, 'status': code, 'location': newurl})
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    response, body, complete, failure = None, b'', False, None
    try:
        opener = urllib.request.build_opener(SameHost())
        try:
            response = opener.open(urllib.request.Request(url, headers={
                'User-Agent': 'RendaFutura-bounded-native-financial-review/1.0'}), timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        status = response.status
        headers = dict(response.headers.items())
        length = response.headers.get('Content-Length')
        _require(length is None or int(length) <= 25_000_000, 'native_response_exceeds_25MB')
        parts, count = [], 0
        while True:
            part = response.read(min(65536, 25_000_001 - count))
            if not part:
                break
            count += len(part)
            _require(count <= 25_000_000, 'native_response_exceeds_25MB')
            parts.append(part)
        body = b''.join(parts)
        complete = length is None or len(body) == int(length)
        if status in (401, 403, 429):
            failure = 'access_denied_no_retry'
        elif status != 200:
            failure = 'non_success_response_no_retry'
        elif not complete:
            failure = 'incomplete_response_no_retry'
        elif not body.startswith(b'PK'):
            failure = 'not_native_ZIP_no_auth_or_challenge_handling'
    except Exception as error:
        failure = type(error).__name__ + ':' + str(error)[:250]
    finally:
        if response is not None:
            response.close()
    digest = hashlib.sha256(body).hexdigest()
    filename = 'original-' + digest + '.bin'
    if body:
        with (root / filename).open('xb') as stream:
            stream.write(body)
    receipt = {**request, 'completed_at': datetime.now(timezone.utc).isoformat(),
               'status': getattr(response, 'status', None),
               'headers': dict(response.headers.items()) if response else {},
               'final_url': response.geturl() if response else None,
               'redirects': redirects, 'file': filename if body else None,
               'sha256': digest, 'bytes': len(body), 'complete_response': complete,
               'failure': failure, 'extracted_members': []}
    if failure is None:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                _require(archive.testzip() is None, 'native_ZIP_CRC_failed')
                xml_name = '011070DFP31-12-2025v1.xml'
                xml_body = archive.read(xml_name)
                xml = ET.fromstring(xml_body)
                _require(tuple(xml.findtext(field) for field in (
                    'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
                    'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) ==
                    ('011070', '33228024000151', '1', '31/12/2025'), 'native_XML_identity_mismatch')
                pdf_names = [name for name in archive.namelist()
                             if re.fullmatch(r'155797_011070_\d+\.pdf', name)]
                _require(len(pdf_names) == 1, 'exact_native_PDF_member_missing_or_ambiguous')
                for name in (xml_name, pdf_names[0]):
                    member = archive.read(name)
                    member_hash = hashlib.sha256(member).hexdigest()
                    target = 'native-' + member_hash + Path(name).suffix
                    with (root / target).open('xb') as stream:
                        stream.write(member)
                    receipt['extracted_members'].append({'member': name, 'file': target,
                        'sha256': member_hash, 'bytes': len(member), 'original_container_sha256': digest})
        except (ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as error:
            receipt['failure'] = 'native_identity_or_container_validation_failed:' + str(error)[:250]
    _immutable_json(root / ('response-' + stem + '.json'), receipt)
    if https_transport_phase:
        _immutable_json(root / 'transport-ledger-v2.json', {'authorized_request_budget': 2,
            'attempts_used': 2, 'first_request_and_response_preserved': True,
            'retry_or_fallback_planned': False,
            'attempts': [prior, {'logical_attempt': 2, 'request': request, 'response': receipt,
                                'request_file': 'request-native-https.json',
                                'response_file': 'response-native-https.json'}]})
    return receipt


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _receipt(folder, relative, url, expected=None):
    root = Path(folder).resolve()
    path = root / relative
    _require(path.resolve().is_relative_to(root) and not path.is_symlink(), 'unsafe_source_path')
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    _require(expected is None or digest == expected, 'original_source_hash_mismatch')
    return body, {'id': 'wlm-financial-class:' + digest, 'file': relative,
                  'sha256': digest, 'bytes': len(body), 'url': url, 'authority': 'primary',
                  'retrieved_at': None,
                  'file_mtime_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                  'mtime_is_retrieval_or_publication': False}


def latest_filing(rows, cutoff=CUTOFF):
    _require(cutoff == CUTOFF, 'unreviewed_cutoff')
    eligible = [row for row in rows if int(row['CD_CVM']) == 11070
                and native._day(row['DT_REFER']) <= cutoff
                and native._day(row['DT_RECEB']) <= cutoff]
    _require(bool(eligible), 'latest_annual_missing')
    newest = max((row['DT_REFER'], int(row['VERSAO'])) for row in eligible)
    matches = [row for row in eligible if (row['DT_REFER'], int(row['VERSAO'])) == newest]
    identities = {tuple(row[field][:10] if field == 'DT_RECEB' else row[field]
                        for field in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
                  for row in matches}
    _require(len(identities) == 1, 'conflicting_latest_annual_mirrors')
    _require(next(iter(identities)) == EXPECTED, 'review_not_latest_exact_annual')
    return matches[0]


def listed_fca_universe(masters, securities, year, cutoff=CUTOFF):
    """Listed instruments only, deliberately NOT a full legal-capital proof."""
    _require(year in (2025, 2026) and cutoff == CUTOFF, 'unreviewed_fca_period')
    issuer = [row for row in masters if int(row['CD_CVM']) == 11070
              and native._day(row['DT_REFER']) <= cutoff
              and native._day(row['DT_RECEB']) <= cutoff]
    _require(bool(issuer), 'published_FCA_missing')
    latest = max((row['DT_REFER'], int(row['VERSAO'])) for row in issuer)
    matches = [row for row in issuer if (row['DT_REFER'], int(row['VERSAO'])) == latest]
    _require(len(matches) == 1, 'ambiguous_latest_FCA')
    master = matches[0]
    _require(master['CNPJ_CIA'] == IDENTITY['cnpj']
             and master['DT_REFER'] == f'{year}-01-01'
             and (master['VERSAO'], master['ID_DOC'], master['DT_RECEB']) ==
                 (('1', '148173', '2025-05-22') if year == 2025 else
                  ('1', '158140', '2026-05-28')), 'unreviewed_exact_FCA_identity')
    children = [row for row in securities if row['ID_Documento'] == master['ID_DOC']]
    _require(bool(children), 'FCA_security_rows_missing')
    _require(all(row['CNPJ_Companhia'] == IDENTITY['cnpj']
                 and row['Data_Referencia'] == master['DT_REFER']
                 and row['Versao'] == master['VERSAO'] for row in children),
             'FCA_child_period_or_identity_mismatch')
    shares = [row for row in children if normalize(row['Valor_Mobiliario']) in
              ('acoes ordinarias', 'acoes preferenciais')]
    preferred = [row for row in shares if normalize(row['Valor_Mobiliario']) == 'acoes preferenciais']
    _require(len(preferred) == 1
             and preferred[0]['Sigla_Classe_Acao_Preferencial'] == 'PNA'
             and preferred[0]['Classe_Acao_Preferencial'] == 'Preferencial Classe A'
             and preferred[0]['Codigo_Negociacao'] == 'WLMM4',
             'preferred_class_universe_not_unique_exact_PNA')
    ordinary = [row for row in shares if normalize(row['Valor_Mobiliario']) == 'acoes ordinarias']
    _require(len(ordinary) == 1 and ordinary[0]['Codigo_Negociacao'] == 'WLMM3',
             'ordinary_instrument_identity_mismatch')
    return {'master': master, 'all_reported_security_rows': children,
            'listed_preferred_classes': ['PNA'], 'legal_class_retained': 'PNA',
            'full_legal_capital_universe_proved': False,
            'within_year_capital_changes_proved_absent': False}


def _br_decimal(value):
    negative = value.startswith('(') and value.endswith(')')
    number = Decimal(value.strip('()').replace('.', '').replace(',', '.'))
    _require(number.is_finite(), 'nonfinite_note_value')
    return -number if negative else number


def parse_basic_note(text):
    """Read actual published cells; never manufacture EPS from preference rights."""
    clean = normalize(text)
    _require('controladora e consolidado' in clean and '2025' in clean and '2024' in clean
             and 'ordinarias' in clean and 'preferenciais' in clean
             and 'nao houve alteracao no total em circulacao' in clean
             and 'media ponderada' in clean, 'native_note_scope_period_or_denominator_absent')

    def row(label, pattern):
        found = re.findall(r'(?m)^\s*' + re.escape(label) + r'\s+' + pattern, clean)
        _require(len(found) == 1, 'native_note_row_missing_or_ambiguous')
        return list(map(_br_decimal, found[0]))

    counts = row('acoes em circulacao - unidades', r'([\d.]+)\s+([\d.]+)\s+([\d.]+)')
    numeric = r'(\(?[\d.]+,\d+\)?)\s+(\(?[\d.]+,\d+\)?)\s+(\(?[\d.]+,\d+\)?)'
    continued = row('lucro liquido atribuivel a cada classe de acoes (r$)', numeric)
    discontinued = row('prejuizo liquido atribuivel a cada classe de acoes (r$)', numeric)
    _require(counts == [Decimal('16571220'), Decimal('19843450'), Decimal('36414670')]
             and sum(counts[:2]) == counts[2], 'reported_weighted_denominator_changed')
    _require(continued == list(map(Decimal, ('40559687.37', '48568791.45', '89128478.82')))
             and discontinued == list(map(Decimal, ('-1204364.13', '-1442183.47', '-2646547.60'))),
             'native_note_class_numerators_changed')
    return {'weighted_ON_shares': str(counts[0]), 'weighted_preferred_shares': str(counts[1]),
            'continued_ON_numerator_brl': str(continued[0]),
            'continued_PN_numerator_brl': str(continued[1]),
            'discontinued_ON_numerator_brl': str(discontinued[0]),
            'discontinued_PN_numerator_brl': str(discontinued[1]),
            'net_ON_numerator_brl': str(continued[0] + discontinued[0]),
            'net_PN_numerator_brl': str(continued[1] + discontinued[1]),
            'total_net_allocated_parent_profit_brl': str(continued[2] + discontinued[2]),
            'no_change_in_outstanding_in_last_two_years_explicitly_reported': True,
            'denominator_kind': 'annual_weighted_equals_unchanged_outstanding_explicit_note_32',
            'accounts_scope': 'controladora_and_consolidado_explicit_note'}


def eps_reconciles(reported_eps, numerator, denominator):
    eps, net, shares = map(Decimal, (reported_eps, numerator, denominator))
    _require(all(value.is_finite() for value in (eps, net, shares)) and shares > 0,
             'invalid_crosscheck_value')
    # Numerator is printed to cents, EPS to five decimals, shares as exact units.
    low, high = (net - Decimal('.005')) / shares, (net + Decimal('.005')) / shares
    return high >= eps - Decimal('.000005') and low <= eps + Decimal('.000005')


def verified_native_note(folder, master):
    from pypdf import PdfReader

    root = folder / OUTPUT
    transport = json.loads((root / 'response-native-https.json').read_bytes())
    ledger = json.loads((root / 'transport-ledger-v2.json').read_bytes())
    prior = ledger['attempts'][0]
    for kind in ('request', 'response'):
        content = (root / prior[kind + '_file']).read_bytes()
        _require(hashlib.sha256(content).hexdigest() == prior[kind + '_sha256'],
                 'immutable_first_transport_proof_changed')
    parsed = urllib.parse.urlsplit(master['LINK_DOC'])
    expected_url = urllib.parse.urlunsplit(parsed._replace(scheme='https'))
    _require(transport['status'] == 200 and transport['failure'] is None
             and transport['url'] == transport['final_url'] == expected_url
             and transport['master_url_raw'] == master['LINK_DOC']
             and transport['scheme_upgrade_only'] is True and transport['published_at'] == '2026-03-27'
             and transport['sha256'] == CONTAINER_HASH and transport['bytes'] == 6644043
             and transport['complete_response'] is True and transport['redirects'] == []
             and ledger['attempts_used'] == 2 and ledger['authorized_request_budget'] == 2,
             'native_transport_binding_mismatch')
    sources = []
    originals = [('original-' + CONTAINER_HASH + '.bin', CONTAINER_HASH),
                 ('native-' + PDF_HASH + '.pdf', PDF_HASH), ('native-' + XML_HASH + '.xml', XML_HASH)]
    bodies = []
    for filename, digest in originals:
        body, source = _receipt(folder, OUTPUT + '/' + filename, expected_url, digest)
        source.update(retrieved_at=transport['completed_at'],
                      retrieval_started_at=transport['started_at'],
                      http_date=transport['headers'].get('Date'),
                      publication_date='2026-03-27',
                      clock_use='retrieval_provenance_not_publication',
                      original_container_sha256=CONTAINER_HASH)
        bodies.append(body)
        sources.append(source)
    with zipfile.ZipFile(io.BytesIO(bodies[0])) as archive:
        _require(archive.read('011070DFP31-12-2025v1.xml') == bodies[2]
                 and archive.read('155797_011070_25102026091937.pdf') == bodies[1],
                 'native_original_member_binding_failed')
    xml = ET.fromstring(bodies[2])
    _require(tuple(xml.findtext(field) for field in ('DadosEmpresa/CodigoCvm',
        'DadosEmpresa/CnpjEmpresa', 'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) ==
        ('011070', '33228024000151', '1', '31/12/2025'), 'native_XML_identity_mismatch')
    pdf = PdfReader(io.BytesIO(bodies[1]))
    _require(len(pdf.pages) == 105, 'unreviewed_native_PDF_page_count')
    text = pdf.pages[93].extract_text()
    note = parse_basic_note(text)
    note.update(physical_page=94, printed_page=93, source_ref=sources[1]['id'])
    for filename in ('request-native.json', 'response-native.json', 'request-native-https.json',
                     'response-native-https.json', 'transport-ledger-v2.json'):
        _, source = _receipt(folder, OUTPUT + '/' + filename, expected_url)
        source.update(authority='local_transport_metadata_not_primary_financial_statement',
                      publication_date=None)
        sources.append(source)
    return note, sources


def audit(folder=ROOT, cutoff=CUTOFF):
    folder = Path(folder)
    _require(cutoff == CUTOFF, 'unreviewed_cutoff')
    sources, masters = [], []
    for directory in (folder / 'raw/official-financials', folder / 'raw'):
        for path in sorted(directory.glob('*.zip')):
            match = re.fullmatch(r'(?:cvm-dfp-?(\d{4})|dfp_cia_aberta_(\d{4}))\.zip', path.name)
            if not match:
                continue
            year = int(match[1] or match[2])
            if year > cutoff.year:
                continue
            _, receipt = _receipt(folder, path.relative_to(folder).as_posix(),
                'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/'
                f'dfp_cia_aberta_{year}.zip')
            sources.append(receipt)
            with zipfile.ZipFile(path) as archive:
                masters.extend(row for row in native._rows(archive, f'dfp_cia_aberta_{year}.csv')
                               if int(row['CD_CVM']) == 11070)
    master = latest_filing(masters, cutoff)
    note, native_sources = verified_native_note(folder, master)
    sources.extend(native_sources)
    fca = {}
    for year, (relative, digest) in FCA_FILES.items():
        body, receipt = _receipt(folder, relative,
            f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip', digest)
        sources.append(receipt)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            def rows(name):
                return list(csv.DictReader(io.StringIO(archive.read(name).decode('latin1')), delimiter=';'))
            fca[str(year)] = listed_fca_universe(rows(f'fca_cia_aberta_{year}.csv'),
                rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv'), year, cutoff)
    body, _ = _receipt(folder, ANNUAL_FILE,
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip', ANNUAL_HASH)
    key = (11070, '2025-12-31', '1')
    tables = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for suffix in native.TABLES:
            tables[suffix] = [row for row in native._rows(archive, f'dfp_cia_aberta{suffix}_2025.csv')
                              if native._key(row) == key and row['ORDEM_EXERC'] == '\u00daLTIMO']
        capital = [row for row in native._rows(archive, 'dfp_cia_aberta_composicao_capital_2025.csv')
                   if row['CNPJ_CIA'] == IDENTITY['cnpj'] and row['DT_REFER'] == '2025-12-31'
                   and row['VERSAO'] == '1']
    dre = tables['_DRE_con']
    _require(native._dre_fingerprint(dre, full=True) == DRE_HASH, 'current_consolidated_DRE_changed')
    pn = [row for row in dre if row['CD_CONTA'].startswith('3.99.01.') and row['DS_CONTA'] == 'PN']
    _require(len(pn) == 1 and Decimal(pn[0]['VL_CONTA']) == Decimal('2.47741'),
             'unique_reported_generic_PN_basic_EPS_missing')
    roots = {code: [native._brl(row) for row in dre if row['CD_CONTA'] == code]
             for code in ('3.11', '3.11.01', '3.11.02')}
    _require(roots == {'3.11': [Decimal('86892000')], '3.11.01': [Decimal('86482000')],
                       '3.11.02': [Decimal('410000')]}
             and roots['3.11'][0] == roots['3.11.01'][0] + roots['3.11.02'][0],
             'consolidated_parent_NCI_reconciliation_changed')
    specs = {'WLMM3': (11070, 'ON', 5), 'WLMM4': (11070, 'PNA', 5)}
    diagnostics = []
    filtered, contexts = native._annual_archive(body, 2025, specs, cutoff, diagnostics)
    parsed = cvm_statements(filtered, 2025, specs=specs, diagnostics=diagnostics)
    current = [row for row in parsed if row['filing_id'] == '155797']
    on = next(row for row in current if row['share_class'] == 'ON')
    pna = [row for row in current if row['share_class'] == 'PNA']
    pna_diagnostics = [row for row in diagnostics if row.get('cvm') == 11070
                      and row.get('share_class') == 'PNA' and row.get('filing_id') == '155797']
    _require(contexts[key]['accounts_scope'] == 'consolidated'
             and on['profit_per_share'] == 2.25219
             and not pna and len(pna_diagnostics) == 1
             and pna_diagnostics[0]['reasons'] == ['basic_eps_absent_ambiguous_or_zero'],
             'default_parser_state_changed_reinspect')
    return {
        'schema_version': 1, 'identity': IDENTITY, 'cutoff': cutoff.isoformat(),
        'network_requests': 2, 'offline_audit_network_requests': 0,
        'production_writes': False, 'admissions': [],
        'status': 'blocked_reported_class_EPS_vs_native_note_numerator_denominator_conflict',
        'diagnosis': 'Actual generic PN BASIC EPS is present; literal PN != legal PNA '
                     'causes the current parser rejection, but the exact native note '
                     'shows a separate EPS arithmetic conflict; a label-only fix is insufficient.',
        'latest_annual': master, 'exact_native_download_URL_from_master': master['LINK_DOC'],
        'native_annual_PDF_XML_found_or_verified_in_this_task': True,
        'native_note_32': note,
        'actual_reported_PN_EPS_reconciles_with_note': eps_reconciles(
            '2.47741', note['net_PN_numerator_brl'], note['weighted_preferred_shares']),
        'actual_reported_ON_EPS_reconciles_with_note': eps_reconciles(
            '2.25219', note['net_ON_numerator_brl'], note['weighted_ON_shares']),
        'arithmetic_diagnostic_only_not_replacement_EPS': {
            'PN_net_note_numerator_divided_by_reported_weighted_shares': str(
                Decimal(note['net_PN_numerator_brl']) / Decimal(note['weighted_preferred_shares'])),
            'ON_net_note_numerator_divided_by_reported_weighted_shares': str(
                Decimal(note['net_ON_numerator_brl']) / Decimal(note['weighted_ON_shares'])),
            'class_allocations_parent_reconciliation_difference_brl': str(
                Decimal('86482000') - Decimal(note['total_net_allocated_parent_profit_brl'])),
            'allocations_reconcile_to_parent_within_reported_thousand_rounding': True,
            'NCI_imported': False, 'engine_replacement_authorized': False},
        'FCA_periods': fca, 'consolidated_capital_at_2025_year_end': capital,
        'capital_qualification': 'Year-end share counts are not annual weighted denominators '
            'and cannot prove absence of intra-year conversions, capital changes or unlisted classes.',
        'reported_source_basic_EPS': {'account': pn[0]['CD_CONTA'], 'label': 'PN',
            'value': '2.4774100000', 'unit': 'BRL_per_share', 'legal_class_requested': 'PNA',
            'weighted_preferred_denominator_reported': note['weighted_preferred_shares'],
            'class_allocated_parent_numerator_reported': note['net_PN_numerator_brl'],
            'derived_from_dividend_preference_price_or_cash': False},
        'consolidated_profit_brl': {'total': '86892000', 'parent': '86482000', 'NCI': '410000'},
        'consolidated_cash_brl': {'operating': '280163000', 'PPE_acquisition': '-180199000',
                                 'intangibles_acquisition': '-7450000',
                                 'PNA_FCF_per_share_authorized': None},
        'unchanged_parser_latest_records': current,
        'unchanged_parser_PNA_diagnostic': pna_diagnostics[0],
        'required_before_crosswalk': [
            'Primary issuer clarification/correction reconciling reported PN 2.47741 with '
            'note-32 class numerator and weighted denominator. Do not derive replacement EPS.',
            'Primary full preferred legal-capital class universe throughout FY2025, not just listed symbols.',
            'Explicit weighted class denominator and class-allocated parent numerator excluding NCI.',
            'Primary explanation of capital/treasury/class changes or explicit absence during the period.',
            'Printed basic PN EPS binds uniquely to legal PNA; diluted EPS stays separate.'
        ],
        'source_table_fingerprints': {suffix: native._dre_fingerprint(rows, full=True)
                                      for suffix, rows in tables.items()},
        'sources': sources,
    }


def get_wlm_financial_reading(folder=ROOT, cutoff=CUTOFF):
    """Fail closed: this frozen audit contains no authorized financial reading."""
    report = audit(folder, cutoff)
    _require(report['status'] == 'authorized',
             'reported_class_EPS_does_not_reconcile_with_native_note_numerator_and_denominator')
    raise ValueError('no_WLM_financial_crosswalk_authorized')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT)
    parser.add_argument('--fetch-native-once', action='store_true')
    parser.add_argument('--fetch-native-https-phase', action='store_true')
    args = parser.parse_args()
    if args.fetch_native_once or args.fetch_native_https_phase:
        _require(not (args.fetch_native_once and args.fetch_native_https_phase), 'choose_one_transport_phase')
        receipt = fetch_native_once(args.folder, https_transport_phase=args.fetch_native_https_phase)
        print(json.dumps({key: receipt[key] for key in ('status', 'bytes', 'failure', 'extracted_members')}, indent=2))
        return
    report = audit(args.folder)
    encoded = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    root = args.folder / OUTPUT
    root.mkdir(parents=True, exist_ok=True)
    path = root / ('audit-' + hashlib.sha256(encoded).hexdigest() + '.json')
    if path.exists():
        _require(path.read_bytes() == encoded, 'immutable_audit_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(encoded)
    print(json.dumps({'report': str(path.resolve()), 'status': report['status'],
                      'native_document_master_URL': report['exact_native_download_URL_from_master'],
                      'reported_PN_basic_EPS': '2.47741', 'legal_class_kept': 'PNA',
                      'admissions': []}, indent=2))


if __name__ == '__main__':
    main()
