"""Dated, primary-source unit compositions, with no I/O or builder integration.

get_unit_composition returns a detached JSON-compatible record. The review date
is a conservative observation boundary, not an inferred legal effective date.
map_unit_fundamentals accepts exact-class records shaped like cvm_fundamentals
instruments, plus an explicit eps_basis proof for each class. Composition alone
does not establish a comparable EPS denominator or a usable investment model.
Only basic EPS is additive across the shares in a unit; issuer totals and
valuation ratios are never multiplied or averaged here.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
import math
import re
from types import MappingProxyType
from urllib.parse import urlsplit


RESEARCH_AS_OF = '2026-10-25'
EVIDENCE_ROOT = 'raw/primary-research/units/'
MANIFEST_SHA256 = '1a7d84c94eb122d5a060cdcd435656fd688954b4d9ced0c0ed92ecf9305c149f'
EVIDENCE_MANIFEST = EVIDENCE_ROOT + f'registry-evidence-2026-10-25-{MANIFEST_SHA256}.json'
FCA_SHA256 = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
FRE_SHA256 = '211fd962018edebd8f11393f813aa874df56748b0ae2b77cdeda83e095b93731'
BTG_SHA256 = 'e3cfb4f48b87dd12525bf7d1fe6fa124767139d13b7b1fe348a86b85ed828603'
ONCO_SHA256 = '52367f3d1629184a7c9ee633c5371d0d4cc9bdcaacfe240b8636292c83955f4f'
KLABIN_SHA256 = '8051cd62c241fe868e3d31c0e01d039448c9875450d095674db2bb33b7bc311a'
SHARE_CLASSES = frozenset({'ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'})
PRIMARY_HOSTS = frozenset({
    'dados.cvm.gov.br', 'rad.cvm.gov.br', 'www.rad.cvm.gov.br',
    'api.mziq.com', 'ri.btgpactual.com', 'ri.alupar.com.br',
    'ri.energisa.com.br', 'ri.iguatemi.com.br', 'ri.klabin.com.br',
    'ri.sanepar.com.br', 'ri.taesa.com.br', 'www.santander.com.br',
    'cms.santander.com.br', 'ri.grupooncoclinicas.com',
})


@dataclass(frozen=True)
class Evidence:
    authority: str
    url: str
    file: str
    sha256: str
    locator: str
    published_at: str | None


@dataclass(frozen=True)
class Component:
    share_class: str
    shares: int
    ticker: str | None = None
    isin: str | None = None


@dataclass(frozen=True)
class UnitComposition:
    ticker: str
    cvm: int
    issuer: str
    cnpj: str
    components: tuple[Component, ...]
    evidence: tuple[Evidence, ...]
    status: str = 'verified'
    as_of: str = RESEARCH_AS_OF
    valid_until: str = RESEARCH_AS_OF
    effective_from: str | None = None
    isin: str | None = None
    instrument_type: str = 'unit'
    instrument_expires_on: str | None = None
    notes: tuple[str, ...] = ()


def _fca(filing_id, published_at):
    return Evidence(
        'CVM_FCA',
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip',
        EVIDENCE_ROOT + f'fca_cia_aberta_2026-{FCA_SHA256}.zip', FCA_SHA256,
        f'fca_cia_aberta_2026.csv:ID_DOC={filing_id}; '
        f'fca_cia_aberta_valor_mobiliario_2026.csv:ID_Documento={filing_id}',
        published_at)


_BTG = Evidence(
    'issuer_RI', 'https://ri.btgpactual.com/governanca-corporativa/visao-geral/',
    EVIDENCE_ROOT + f'bpac11-governance-{BTG_SHA256}.html', BTG_SHA256,
    'Praticas diferenciadas de Governanca Corporativa; first paragraph in Astro props', None)
_ONCO = Evidence(
    'issuer_RI', 'https://api.mziq.com/mzfilemanager/v2/d/'
    '2b77cf83-e31f-4408-a759-a80d04f7f950/85ef0582-6875-4b96-53b5-a0299114ed62?origin=1',
    EVIDENCE_ROOT + f'onco11-warrant-notice-{ONCO_SHA256}.pdf', ONCO_SHA256,
    'Aviso aos Acionistas 2025-11-18; p.1 issuer identity; p.3 sections 2.2 and 2.4',
    '2025-11-18')
_ONCO_FRE = Evidence(
    'CVM_FRE', 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS/fre_cia_aberta_2026.zip',
    EVIDENCE_ROOT + f'fre_cia_aberta_2026-{FRE_SHA256}.zip', FRE_SHA256,
    'fre_cia_aberta_2026.csv:ID_DOC=161691; '
    'fre_cia_aberta_outro_valor_mobiliario_2026.csv:ID_Documento=161691, '
    'Valor_Mobiliario=Bonus de Subscricao (original accented); Versao=10; '
    'Data_Emissao=2025-11-19', '2026-10-14')
_KLABIN = Evidence(
    'issuer_RI', 'https://ri.klabin.com.br/para-o-investidor/perguntas-frequentes/',
    EVIDENCE_ROOT + f'klbn11-faq-{KLABIN_SHA256}.html', KLABIN_SHA256,
    'Onde a Klabin negocia suas acoes?; Quais as diferencas entre ON, PN e Units?', None)


# FCA ticker 000000 for BTG is unresolved; issuer RI explicitly supplies BPAC11.
# Component tickers are populated only where the cited FCA explicitly lists them.
UNIT_COMPOSITIONS = MappingProxyType({
    'ALUP11': UnitComposition(
        'ALUP11', 21490, 'ALUPAR INVESTIMENTO S/A', '08.364.948/0001-38',
        (Component('ON', 1), Component('PN', 2)), (_fca('161432', '2026-09-23'),)),
    'BPAC11': UnitComposition(
        'BPAC11', 22616, 'BCO BTG PACTUAL S.A.', '30.306.294/0001-45',
        (Component('ON', 1), Component('PNA', 2)), (_fca('158545', '2026-05-29'), _BTG),
        notes=('FCA trading code is 000000; BPAC11 identity is explicit in issuer RI.',
               'PNA is not interchangeable with generic PN.')),
    'BRBI11': UnitComposition(
        'BRBI11', 25860, 'BRBI BR PARTNERS S.A', '10.739.356/0001-03',
        (Component('ON', 1), Component('PN', 2)), (_fca('157515', '2026-05-15'),)),
    'ENGI11': UnitComposition(
        'ENGI11', 15253, 'ENERGISA S.A.', '00.864.214/0001-06',
        (Component('ON', 1, 'ENGI3'), Component('PN', 4, 'ENGI4')),
        (_fca('158184', '2026-05-29'),)),
    'IGTI11': UnitComposition(
        'IGTI11', 8672, 'IGUATEMI S.A.', '60.543.816/0001-93',
        (Component('ON', 1, 'IGTI3'), Component('PN', 2, 'IGTI4')),
        (_fca('158558', '2026-05-29'),)),
    'KLBN11': UnitComposition(
        'KLBN11', 12653, 'KLABIN S.A.', '89.637.490/0001-45',
        (Component('ON', 1, 'KLBN3'), Component('PN', 4, 'KLBN4')),
        (_fca('155729', '2026-03-26'), _KLABIN),
        notes=('FCA states 1 KLBN3 + 4 KLBN4; issuer FAQ verifies their ON/PN classes.',)),
    'ONCO11': UnitComposition(
        'ONCO11', 26123, 'ONCOCL\u00cdNICAS DO BRASIL SERVI\u00c7OS M\u00c9DICOS S.A.',
        '12.104.241/0004-02', (), (_fca('161322', '2026-09-14'), _ONCO_FRE, _ONCO),
        status='not_a_unit', effective_from='2025-11-19',
        instrument_type='subscription_warrant', instrument_expires_on='2027-11-19',
        notes=('The 2025-11-18 issuer notice explicitly assigns ONCO11 to subscription warrants.',
               'FRE reference 2026-12-31 is a form label; filing was received 2026-10-14.',
               'Warrant exercise rights are not deposited unit components.')),
    'SANB11': UnitComposition(
        'SANB11', 20532, 'BCO SANTANDER (BRASIL) S.A.', '90.400.888/0001-42',
        (Component('ON', 1, 'SANB3'), Component('PN', 1, 'SANB4')),
        (_fca('156191', '2026-04-14'),)),
    'SAPR11': UnitComposition(
        'SAPR11', 18627, 'CIA SANEAMENTO DO PARANA - SANEPAR', '76.484.013/0001-45',
        (Component('ON', 1, 'SAPR3'), Component('PN', 4, 'SAPR4')),
        (_fca('159163', '2026-06-22'),)),
    'TAEE11': UnitComposition(
        'TAEE11', 20257, 'TRANSMISSORA ALIAN\u00c7A DE ENERGIA EL\u00c9TRICA S.A.',
        '07.859.971/0001-30', (Component('ON', 1, 'TAEE3'), Component('PN', 2, 'TAEE4')),
        (_fca('158852', '2026-06-01'),)),
})


def _day(value):
    if isinstance(value, datetime):
        raise ValueError('Use a date without an implicit timezone conversion.')
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Use YYYY-MM-DD dates.')
    return date.fromisoformat(value)


def _positive_int(value):
    return type(value) is int and value > 0


def _evidence_errors(item, as_of, *, registry=False):
    if not isinstance(item, Mapping):
        return ('missing_primary_evidence',)
    errors = []
    try:
        url = urlsplit(item.get('url', ''))
        if (url.scheme != 'https' or url.hostname not in PRIMARY_HOSTS
                or url.username or url.password or url.port not in (None, 443)):
            errors.append('non_primary_evidence_url')
    except (TypeError, ValueError):
        errors.append('non_primary_evidence_url')
    if item.get('authority') not in {'CVM_FCA', 'CVM_FRE', 'CVM_DFP_ITR', 'issuer_RI'}:
        errors.append('unverified_evidence_authority')
    digest, path = item.get('sha256'), item.get('file')
    if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
            or not isinstance(path, str) or not path or '..' in path.split('/')
            or (registry and (not path.startswith(EVIDENCE_ROOT) or digest not in path))):
        errors.append('missing_evidence_hash_or_file')
    if not isinstance(item.get('locator'), str) or not item['locator'].strip():
        errors.append('missing_evidence_locator')
    if item.get('published_at') is not None:
        try:
            if _day(item['published_at']) > as_of:
                errors.append('future_evidence')
        except (TypeError, ValueError):
            errors.append('invalid_evidence_date')
    return tuple(errors)


def validate_composition(record):
    """Return structural/provenance errors; this does not authenticate documents."""
    if not isinstance(record, UnitComposition):
        return ('invalid_composition_record',)
    errors = []
    if not isinstance(record.ticker, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', record.ticker):
        errors.append('invalid_ticker')
    if (not _positive_int(record.cvm) or not isinstance(record.issuer, str)
            or not record.issuer.strip() or not isinstance(record.cnpj, str)
            or not re.fullmatch(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', record.cnpj)):
        errors.append('invalid_issuer_identity')
    try:
        as_of, until = _day(record.as_of), _day(record.valid_until)
        if until < as_of or record.effective_from and _day(record.effective_from) > as_of:
            errors.append('invalid_composition_dates')
        if record.instrument_expires_on and _day(record.instrument_expires_on) < until:
            errors.append('expired_instrument')
    except (TypeError, ValueError):
        return tuple(errors + ['invalid_composition_dates'])
    if record.status not in {'verified', 'not_a_unit'}:
        errors.append('unverified_composition')
    if (record.status == 'verified' and (record.instrument_type != 'unit' or not record.components)
            or record.status == 'not_a_unit' and (record.instrument_type == 'unit' or record.components)):
        errors.append('incompatible_security_type')
    classes = []
    if not isinstance(record.components, tuple):
        errors.append('invalid_components')
    else:
        for component in record.components:
            if not isinstance(component, Component):
                errors.append('invalid_components')
                continue
            classes.append(component.share_class)
            if component.share_class not in SHARE_CLASSES or not _positive_int(component.shares):
                errors.append('invalid_class_or_ratio')
            if component.ticker is not None and (
                    not isinstance(component.ticker, str)
                    or not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', component.ticker)):
                errors.append('invalid_component_ticker')
    if len(classes) != len(set(classes)):
        errors.append('duplicate_component_class')
    if not isinstance(record.evidence, tuple) or not record.evidence:
        errors.append('missing_primary_evidence')
    else:
        for item in record.evidence:
            errors.extend(_evidence_errors(asdict(item) if isinstance(item, Evidence) else item,
                                           as_of, registry=True))
    return tuple(dict.fromkeys(errors))


def validate_registry(registry=UNIT_COMPOSITIONS):
    """Report invalid or miskeyed entries without changing the supplied registry."""
    result = {}
    for ticker, record in registry.items():
        errors = validate_composition(record)
        if isinstance(record, UnitComposition) and ticker != record.ticker:
            errors += ('registry_ticker_mismatch',)
        if errors:
            result[ticker] = errors
    return result


def get_unit_composition(ticker, as_of, *, registry=UNIT_COMPOSITIONS):
    """Look up an explicit ticker; never infer a unit from a suffix or issuer."""
    day = _day(as_of)
    record = registry.get(ticker)
    if record is None:
        return {'ticker': ticker, 'status': 'missing', 'reason': 'unit_not_in_verified_registry',
                'components': [], 'as_of': day.isoformat()}
    errors = validate_composition(record)
    if isinstance(record, UnitComposition) and record.ticker != ticker:
        errors += ('registry_ticker_mismatch',)
    if errors:
        return {'ticker': ticker, 'status': 'invalid', 'reason': 'invalid_composition_evidence',
                'errors': list(errors), 'components': [], 'as_of': day.isoformat()}
    result = asdict(record)
    result['components'] = [asdict(component) for component in record.components]
    result['evidence'] = [asdict(item) for item in record.evidence]
    result.update(reason=None, isin_status='available' if record.isin else 'missing_in_cited_sources',
                  effective_from_status='reported' if record.effective_from else 'not_established',
                  date_basis='review_snapshot_not_legal_effective_interval',
                  listing_basis='issuer_declared_not_intraday_exchange_membership')
    for component in result['components']:
        component['isin_status'] = 'available' if component['isin'] else 'missing_in_cited_sources'
        component['ticker_status'] = 'reported' if component['ticker'] else 'missing_in_cited_sources'
    if day < _day(record.as_of):
        result.update(status='missing', reason='composition_not_verified_for_date')
    elif day > _day(record.valid_until):
        result.update(status='stale', reason='composition_evidence_expired')
    elif record.status == 'not_a_unit':
        result['reason'] = 'subscription_warrant_not_unit'
    return result


def _eps_error(row, component, cvm, day, basis_date):
    if not isinstance(row, Mapping):
        return 'missing', 'component_fundamentals_missing'
    if type(row.get('cvm')) is not int or row['cvm'] != cvm:
        return 'invalid', 'component_issuer_mismatch'
    if row.get('share_class') != component['share_class']:
        return 'invalid', 'component_share_class_mismatch'
    field = row.get('fields', {}).get('basic_eps') if isinstance(row.get('fields'), Mapping) else None
    if not isinstance(field, Mapping) or field.get('status') != 'available':
        return 'missing', 'component_basic_eps_unavailable'
    value = field.get('value')
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        return 'invalid', 'invalid_component_eps_value'
    if field.get('unit') != 'BRL/share' or field.get('annualized') is not False:
        return 'invalid', 'unsupported_component_eps_unit_or_annualization'
    try:
        if not (_day(field['period_start']) <= _day(field['period_end'])
                <= _day(field['published_at']) <= day):
            return 'invalid', 'invalid_or_future_eps_period'
    except (KeyError, TypeError, ValueError):
        return 'invalid', 'invalid_or_future_eps_period'
    if (field.get('period_type') not in {'annual', 'ytd', 'quarter', 'reported_flow'}
            or field.get('scope') not in {'individual', 'consolidated'}
            or field.get('document_type') not in {'DFP', 'ITR'}
            or not _positive_int(field.get('version'))
            or not isinstance(field.get('filing_id'), str) or not field['filing_id']
            or not isinstance(field.get('source_ref'), str) or not field['source_ref']):
        return 'invalid', 'incomplete_component_filing_identity'
    basis = row.get('eps_basis')
    if (not isinstance(basis, Mapping) or basis.get('status') != 'verified'
            or basis.get('denominator') != 'one_class_share'
            or basis.get('share_class') != component['share_class']
            or basis.get('method') not in {'reported_class_per_share', 'explicitly_reconciled_class_per_share'}):
        return 'missing', 'eps_basis_unverified'
    if (basis.get('field_source_ref') != field['source_ref']
            or basis.get('filing_id') != field['filing_id']):
        return 'invalid', 'eps_basis_does_not_identify_field'
    try:
        if not _day(field['period_end']) <= _day(basis['share_basis_date']) == _day(basis_date):
            return 'invalid', 'incompatible_eps_share_basis'
    except (KeyError, TypeError, ValueError):
        return 'missing', 'eps_share_basis_date_missing'
    evidence = basis.get('evidence')
    if _evidence_errors(evidence, day) or not evidence.get('published_at'):
        return 'missing', 'eps_basis_primary_evidence_missing'
    if _day(evidence['published_at']) < _day(field['published_at']):
        return 'invalid', 'eps_basis_evidence_predates_field'
    return None


def map_unit_fundamentals(ticker, class_fundamentals, as_of, *, registry=UNIT_COMPOSITIONS):
    """Sum n_class * reported basic EPS only with explicit, matched basis proofs.

    class_fundamentals is keyed by exact ON/PN/PNA class. Each instrument needs
    cvm, share_class, fields.basic_eps and eps_basis. eps_basis must identify the
    field_source_ref and filing_id, denominator='one_class_share', share_class,
    share_basis_date, method, status='verified', and primary evidence containing
    authority/url/file/sha256/locator/published_at. Proof verification is the
    caller's responsibility; this pure function validates its contract only.
    """
    composition = get_unit_composition(ticker, as_of, registry=registry)
    field = {'value': None, 'unit': 'BRL/unit', 'status': 'missing', 'reason': None,
             'method': 'sum_verified_class_basic_eps_times_deposited_shares', 'annualized': False,
             'period_start': None, 'period_end': None, 'period_type': None,
             'components': [], 'source_refs': []}
    result = {'ticker': ticker, 'cvm': composition.get('cvm'), 'as_of': _day(as_of).isoformat(),
              'composition': composition, 'fields': {'basic_eps': field}, 'status': 'missing'}

    def unavailable(status, reason, share_class=None):
        field.update(status=status, reason=reason)
        if share_class:
            field['missing_or_invalid_class'] = share_class
        result['status'] = status
        return result

    if composition['status'] != 'verified':
        status = ('not_applicable' if composition['status'] == 'not_a_unit' else
                  'invalid' if composition['status'] == 'invalid' else 'missing')
        return unavailable(status, composition['reason'])
    if not isinstance(class_fundamentals, Mapping):
        return unavailable('missing', 'component_fundamentals_missing')
    terms, periods = [], set()
    day = _day(as_of)
    for component in composition['components']:
        share_class = component['share_class']
        row = class_fundamentals.get(share_class)
        error = _eps_error(row, component, composition['cvm'], day, composition['as_of'])
        if error:
            return unavailable(*error, share_class)
        eps = row['fields']['basic_eps']
        periods.add(tuple(eps[key] for key in (
            'period_start', 'period_end', 'period_type', 'scope', 'document_type',
            'version', 'filing_id', 'source_ref')))
        try:
            term = eps['value'] * component['shares']
            if not math.isfinite(term):
                raise OverflowError
        except (OverflowError, ValueError):
            return unavailable('invalid', 'invalid_weighted_eps')
        terms.append(term)
    if len(periods) != 1:
        return unavailable('invalid', 'incompatible_component_filings_or_periods')
    try:
        value = math.fsum(terms)
        if not math.isfinite(value):
            raise OverflowError
    except (OverflowError, ValueError):
        return unavailable('invalid', 'invalid_weighted_eps')
    first = class_fundamentals[composition['components'][0]['share_class']]['fields']['basic_eps']
    field.update(value=value, status='available', reason=None,
                 **{key: first[key] for key in ('period_start', 'period_end', 'period_type', 'scope',
                                               'document_type', 'version', 'filing_id', 'published_at')},
                 share_basis_date=composition['as_of'])
    for component in composition['components']:
        row = class_fundamentals[component['share_class']]
        field['components'].append({'share_class': component['share_class'], 'shares': component['shares'],
                                    'basic_eps': deepcopy(row['fields']['basic_eps']),
                                    'eps_basis': deepcopy(row['eps_basis'])})
    field['source_refs'] = list(dict.fromkeys(
        [item['sha256'] for item in composition['evidence']]
        + [class_fundamentals[c['share_class']]['fields']['basic_eps']['source_ref']
           for c in composition['components']]))
    result['status'] = 'available'
    return result
