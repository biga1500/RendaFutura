"""Authenticated inventory endpoints using isolated, explicitly synthetic files."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from . import api, market_inventory


TOKEN = 'inventory-api-test-only-not-production-123456789'
HEADERS = {'Authorization': 'Bearer ' + TOKEN}
UNAVAILABLE = {'detail': 'Inventario de fontes ainda nao disponivel.'}
PRIVATE_MARKER = 'private-inventory-diagnostic-token-do-not-expose'
CVM_DOCUMENT = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?'
                'Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo=1477025&'
                'numSequencia=1001731&numVersao=1')


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Follow the existing service tests: set only a test token, use the real
    # authorization dependency and enter TestClient so lifespan validation runs.
    monkeypatch.setenv('RECOMMENDER_TOKEN', TOKEN)
    monkeypatch.setenv('RF_DATA_DIR', str(tmp_path))

    def no_model(mode):
        assert mode == 'real'
        raise ValueError(PRIVATE_MARKER)

    monkeypatch.setattr(api, 'get_dataset', no_model)
    monkeypatch.setattr(api, 'catalog_summary', lambda: None)
    monkeypatch.setattr(api, 'coverage_summary', lambda dataset=None: {'fixture': 'isolated'})
    with TestClient(api.app) as client:
        yield client, tmp_path


@pytest.fixture
def inventory():
    # Documentary provenance is real; all values/identities below are synthetic
    # API fixtures. No documentary reference is represented as a payment event.
    return {
        'schema_version': 1, 'as_of': '2026-10-24', 'collected_at': '2026-10-24T12:00:00+00:00',
        'stocks': [
            {'ticker': 'TEST3', 'name': 'Synthetic API fixture one', 'model_ready': False,
             'quote': {'status': 'available', 'value': 12.5, 'unit': 'BRL', 'authority': 'secondary',
                       'as_of': '2026-10-23', 'provider': 'Yahoo Finance'},
             'payments': {'status': 'partial', 'event_count': None, 'events': [],
                          'documents': [{'url': CVM_DOCUMENT, 'reference_date': '2026-02-11',
                                         'published_at': '2026-02-12', 'protocol': None, 'version': 1,
                                         'status': 'document_reference_only', 'cash_model_eligible': False}],
                          'history_complete': False, 'cash_model_eligible': False},
             'fundamentals': {'status': 'partial', 'fields': {
                 'earnings_per_share': {'value': None, 'status': 'not_found'},
                 'assets': {'value': 100, 'status': 'available', 'authority': 'primary'}}},
             'history': {'status': 'partial', 'observed_months': 2,
                         'paid_cash_inferred_from_ex_dates': False}},
            {'ticker': 'TEST4', 'name': 'Synthetic API fixture two', 'model_ready': False,
             'quote': {'status': 'not_found', 'value': None},
             'payments': {'status': 'not_found', 'event_count': None, 'events': [],
                          'history_complete': False, 'cash_model_eligible': False},
             'fundamentals': {'status': 'partial', 'fields': {
                 'earnings_per_share': {'value': 2.5, 'status': 'available', 'authority': 'primary'}}},
             'history': {'status': 'not_found', 'observed_months': 0,
                         'paid_cash_inferred_from_ex_dates': False}},
        ],
        'treasury': [
            {'name': 'Synthetic Treasury API fixture', 'kind': 'Tesouro Selic',
             'quote': {'status': 'available', 'value': 1000, 'unit': 'BRL'},
             'payments': {'status': 'no_periodic_coupon', 'event_count': 0, 'events': [],
                          'principal_counted_as_income': False},
             'history': {'status': 'partial', 'observed_months': 2}, 'model_ready': False},
        ],
        'counts': {'stocks': 2, 'treasury': 1, 'quotes_available': 1, 'fundamentals_available': 2,
                   'payments_read': 0, 'model_ready_stocks': 0},
        'sources': [{'authority': 'primary', 'provider': 'CVM IPE', 'url': CVM_DOCUMENT,
                     'scope': 'document_reference_only'}],
        'model_reference_date': '2026-09-30', 'model_sample_unchanged': True,
        'catalogue_minimum_payments': 0, 'catalogue_minimum_history_months': 0,
    }


@pytest.mark.parametrize('route', ['/market', '/status'])
@pytest.mark.parametrize('authorization', [None, 'Bearer wrong-test-token', 'Basic abc', TOKEN])
def test_inventory_endpoints_require_auth_before_reading(service, monkeypatch, route, authorization):
    client, _ = service
    reads = []

    def forbidden_read(*args, **kwargs):
        reads.append(True)
        raise AssertionError('Unauthenticated requests must not inspect inventory files.')

    monkeypatch.setattr(market_inventory, 'load_inventory', forbidden_read)
    monkeypatch.setattr(market_inventory, 'inventory_summary', forbidden_read)
    headers = {} if authorization is None else {'Authorization': authorization}
    response = client.get(route, headers=headers)
    assert response.status_code == 401
    assert response.json() == {'detail': 'Acesso restrito ao servidor RendaFutura.'}
    assert response.headers['cache-control'] == 'no-store'
    assert not reads
    assert TOKEN not in response.text


@pytest.mark.parametrize('route', ['/market', '/status'])
def test_inventory_endpoints_reject_cross_origin_even_with_token(service, route):
    client, _ = service
    response = client.get(route, headers={**HEADERS, 'Origin': 'https://untrusted.example'})
    assert response.status_code == 403
    assert 'access-control-allow-origin' not in response.headers


def test_missing_inventory_returns_generic_503_and_status_stays_available(service):
    client, root = service
    response = client.get('/market', headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert response.headers['cache-control'] == 'no-store'
    assert str(root) not in response.text
    assert PRIVATE_MARKER not in response.text
    status = client.get('/status', headers=HEADERS)
    assert status.status_code == 200
    assert status.json()['marketInventory'] is None
    assert status.json()['realDataAvailable'] is False
    assert status.json()['cashIncomeDataAvailable'] is False
    assert PRIVATE_MARKER not in status.text


@pytest.mark.parametrize('fault', ['pointer_json', 'pointer_schema', 'traversal', 'missing_snapshot',
                                  'hash', 'snapshot_json', 'snapshot_schema', 'duplicate_ticker',
                                  'missing_component', 'nonfinite'])
def test_invalid_inventory_returns_generic_503_without_diagnostics(service, inventory, fault):
    client, root = service
    snapshot = market_inventory.publish_inventory(root, inventory)
    pointer_path = root / 'inventory/current.json'
    pointer = json.loads(pointer_path.read_text(encoding='utf8'))
    if fault == 'pointer_json':
        pointer_path.write_text('{"' + PRIVATE_MARKER, encoding='utf8')
    elif fault in ('pointer_schema', 'traversal', 'missing_snapshot', 'hash'):
        if fault == 'pointer_schema':
            pointer['schema_version'] = PRIVATE_MARKER
        elif fault == 'traversal':
            pointer['snapshot'] = '../' + PRIVATE_MARKER
        elif fault == 'missing_snapshot':
            pointer['snapshot'] = 'snapshots/' + PRIVATE_MARKER
        else:
            pointer['sha256'] = '0' * 64
        pointer_path.write_text(json.dumps(pointer), encoding='utf8')
    else:
        changed = deepcopy(inventory)
        if fault == 'snapshot_json':
            content = ('{"' + PRIVATE_MARKER).encode('utf8')
        else:
            if fault == 'snapshot_schema':
                changed['schema_version'] = PRIVATE_MARKER
            elif fault == 'duplicate_ticker':
                changed['stocks'][1]['ticker'] = changed['stocks'][0]['ticker']
            elif fault == 'missing_component':
                del changed['stocks'][0]['payments']
            elif fault == 'nonfinite':
                changed['stocks'][0]['quote']['value'] = float('nan')
            content = json.dumps(changed).encode('utf8')
        (snapshot / 'market.json').write_bytes(content)
        # Preserve a correct file hash here so schema/JSON validation is tested,
        # rather than every invalid payload stopping at the integrity check.
        pointer['sha256'] = hashlib.sha256(content).hexdigest()
        pointer_path.write_text(json.dumps(pointer), encoding='utf8')
    response = client.get('/market', headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert response.headers['cache-control'] == 'no-store'
    assert PRIVATE_MARKER not in response.text
    assert str(root) not in response.text
    assert TOKEN not in response.text


@pytest.mark.parametrize('error_type', [OSError, ValueError, KeyError])
def test_loader_errors_do_not_leak_details(service, monkeypatch, error_type):
    client, _ = service

    def fail():
        raise error_type(PRIVATE_MARKER)

    monkeypatch.setattr(market_inventory, 'load_inventory', fail)
    response = client.get('/market', headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert PRIVATE_MARKER not in response.text


def test_authenticated_market_preserves_independent_fields_and_document_references(service, inventory):
    client, root = service
    # Publishing an inventory must not activate or rewrite the modelling sample.
    model_pointer = root / 'current.json'
    model_pointer.write_text('{"fixture_model_pointer":"unchanged"}', encoding='utf8')
    before = model_pointer.read_bytes()
    market_inventory.publish_inventory(root, inventory)
    response = client.get('/market', headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == inventory
    assert response.headers['cache-control'] == 'no-store'
    first, second = response.json()['stocks']
    assert first['quote']['value'] == 12.5
    assert first['fundamentals']['fields']['earnings_per_share']['value'] is None
    assert second['quote']['value'] is None
    assert second['fundamentals']['fields']['earnings_per_share']['value'] == 2.5
    assert first['history']['observed_months'] == 2
    assert first['payments']['documents'][0]['status'] == 'document_reference_only'
    assert first['payments']['documents'][0]['url'] == CVM_DOCUMENT
    assert first['payments']['events'] == second['payments']['events'] == []
    assert first['payments']['event_count'] is second['payments']['event_count'] is None
    assert not first['payments']['cash_model_eligible']
    assert response.json()['counts']['payments_read'] == 0
    assert model_pointer.read_bytes() == before


def test_status_summary_reports_inventory_counts_without_claiming_cash_or_model_readiness(service, inventory, monkeypatch):
    client, root = service
    market_inventory.publish_inventory(root, inventory)
    model = SimpleNamespace(source='Synthetic model fixture, not production data',
                            dates=['2026-09-30'], assets=[SimpleNamespace(ticker='MODEL3')], cash_returns=None)
    monkeypatch.setattr(api, 'get_dataset', lambda mode: model)
    response = client.get('/status', headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    result = response.json()
    assert result['marketInventory'] == {'asOf': inventory['as_of'], 'collectedAt': inventory['collected_at'],
                                         'counts': inventory['counts']}
    assert result['marketInventory']['counts']['stocks'] == 2
    assert result['marketInventory']['counts']['quotes_available'] == 1
    assert result['marketInventory']['counts']['fundamentals_available'] == 2
    assert result['marketInventory']['counts']['payments_read'] == 0
    assert result['marketInventory']['counts']['model_ready_stocks'] == 0
    assert result['realDataAvailable'] is True
    assert result['realDataReference'] == '2026-09-30'
    assert result['realDataAssets'] == ['MODEL3']
    assert result['cashIncomeDataAvailable'] is False
    assert result['financiallyValidated'] is False
    assert 'stocks' not in result['marketInventory']


@pytest.mark.parametrize('pointer', ['{', '{"schema_version":2,"counts":{}}',
                                    '{"schema_version":1,"counts":[]}'])
def test_status_unreadable_inventory_summary_is_none_not_a_global_failure(service, pointer):
    client, root = service
    (root / 'inventory').mkdir()
    (root / 'inventory/current.json').write_text(pointer, encoding='utf8')
    response = client.get('/status', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['marketInventory'] is None
    assert response.json()['cashIncomeDataAvailable'] is False
