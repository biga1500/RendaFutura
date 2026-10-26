from datetime import date

import pytest

from . import collect_income_data as collector


class SelectionReached(Exception):
    pass


@pytest.mark.parametrize('refresh', [False, True])
def test_income_refresh_updates_market_sources_but_reuses_annual_cvm(tmp_path, monkeypatch, refresh):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 24)

    calls = {}

    def cached_source(url, name, raw, sources, requested_refresh):
        calls[name] = (url, raw, requested_refresh)
        sources.append({'file': name})
        if name == 'petr-ri.html':
            return (b'<a href="https://example.test/?src=https%3A%2F%2Ffilemanager-cdn.mziq.com%2Fhistory.xlsx">'
                    b'Download do arquivo em Excel</a>')
        if name == 'bcb-ipca.json':
            return b'[{"data":"30/09/2026","valor":"0.5"}]'
        if name == 'tesouro-precos.csv':
            return b'Tipo Titulo;Data Vencimento;Data Base;PU Venda Manha\n'
        if name == 'treasury-coupons.csv':
            return b'Tipo Titulo;Vencimento do Titulo;Data Resgate;PU;Quantidade;Valor\n'
        return b''

    def stop_at_selection(histories, payments, cutoff):
        assert cutoff == date(2026, 9, 30)
        raise SelectionReached

    monkeypatch.setattr(collector, 'date', FixedDate)
    monkeypatch.setattr(collector, 'cached_source', cached_source)
    monkeypatch.setattr(collector, 'petrobras_payments', lambda *args: ([], []))
    monkeypatch.setattr(collector, 'vale_payments', lambda *args: ([], []))
    monkeypatch.setattr(collector, 'stock_history', lambda *args: ({}, {}, []))
    monkeypatch.setattr(collector, 'verified_raw_prices', lambda *args: {})
    monkeypatch.setattr(collector, 'cvm_statements', lambda *args, **kwargs: [])
    monkeypatch.setattr(collector, 'select_bonds', stop_at_selection)

    with pytest.raises(SelectionReached):
        collector.collect(tmp_path, refresh=refresh, activate=False)

    assert calls['tesouro-precos.csv'] == (collector.TREASURY_URL, tmp_path / 'raw', refresh)
    inflation_url, inflation_cache, inflation_refresh = calls['bcb-ipca.json']
    assert inflation_url.startswith('https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?')
    assert inflation_url.endswith('dataFinal=30/09/2026')
    assert (inflation_cache, inflation_refresh) == (tmp_path / 'raw', refresh)
    assert calls['treasury-coupons.csv'] == (collector.COUPON_URL, tmp_path / 'raw/income', refresh)
    for year in range(2010, 2026):
        assert calls[f'cvm-dfp-{year}.zip'] == (
            f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip',
            tmp_path / 'raw', False,
        )
    for ticker in collector.STOCKS:
        assert calls[f'brapi-{ticker}.json'][2] is False
        assert calls[f'brapi-raw-prices-{ticker}.json'][2] is refresh
    assert not (tmp_path / 'current.json').exists()
    assert not (tmp_path / 'snapshots').exists()
