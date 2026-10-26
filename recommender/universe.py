"""Profile screening of the complete, frozen stock preparation universe."""

import re


DATA_REASONS = {
    'outside_user_positive_dividend_yield_scope': 'Fora do recorte solicitado: somente papeis com dividend yield maior que zero na tabela enviada.',
    'native_financial_eps_source_conflict': 'O lucro por acao publicado diverge dos numeradores e denominadores da nota explicativa nativa; indicador nao usado ate conciliacao.',
    'conflicting_primary_security_identity': 'As fontes documentais divergem sobre a identidade ou o tipo exato do instrumento; compra bloqueada ate conciliacao.',
    'subscription_instrument_outside_equity_income_scope': 'Direito ou bonus de subscricao, nao uma acao ou unit de renda; fora do universo de carteiras de dividendos.',
    'subscription_warrant_outside_equity_income_scope': 'Bonus de subscricao, nao uma acao ou unit de renda; fora do universo de carteiras de dividendos.',
    'unit_in_period_weighted_share_denominator_unverified': 'A base de acoes ponderadas do lucro anual da unit ainda precisa de comprovacao.',
    'unit_historical_FCA_2021_exact_security_missing': 'A composicao exata da unit nao esta comprovada no inicio do periodo.',
    'unit_listing_does_not_cover_return_window': 'A listagem da unit nao cobre todo o periodo comum dos cenarios.',
    'cancelled_issuer_registration': 'Registro do emissor cancelado na CVM; nao e uma oferta atual para compra.',
    'suspended_issuer_registration': 'Registro do emissor suspenso na CVM; compra bloqueada ate verificacao especifica.',
    'daily_prices_cache_missing': 'Historico diario de precos ainda nao disponivel para conciliacao.',
    'independent_cash_cache_missing': 'Falta uma segunda evidencia para conciliar os valores dos proventos.',
    'independent_corporate_evidence_unavailable': 'Falta evidencia independente dos eventos societarios necessarios.',
    'independent_corporate_evidence_cache_missing': 'Falta evidencia independente dos eventos societarios necessarios.',
    'payments_cache_missing': 'Historico de pagamentos ainda nao disponivel para conciliacao.',
    'in_window_payment_date_or_amount_unresolved': 'Existe pagamento sem data ou valor resolvido no periodo.',
    'independent_identity_evidence_unavailable': 'Falta evidencia historica independente da classe de acao.',
    'identity_detail_cache_missing': 'Falta evidencia historica da classe de acao para conciliacao.',
    'past_scheduled_payment_not_confirmed_paid': 'A fonte informa data prevista, sem confirmacao explicita de pagamento.',
    'in_window_payment_source_issue': 'Existem inconsistencias na tabela de pagamentos do periodo.',
    'share_actions_cache_missing': 'Faltam eventos societarios para normalizar precos e proventos.',
    'independent_cash_evidence_does_not_cover_window_start': 'A evidencia independente dos proventos nao cobre o inicio do periodo.',
    'unverified_share_price_regime_requires_raw_history': 'A escala original dos precos ainda nao foi conciliada.',
    'exact_cached_B3_ISIN_unavailable': 'Falta identificacao historica exata da classe na evidencia de conciliacao.',
    'unsupported_ticker_identity': 'Codigo ou estrutura de negociacao ainda sem tratamento especifico.',
    'conflicting_B3_reference_quotes': 'As cotacoes de referencia para conciliacao sao conflitantes.',
    'in_window_payment_calendar_unavailable': 'Nao ha calendario utilizavel de pagamentos no periodo dos cenarios.',
    'non_cash_corporate_action_requires_reconciliation': 'Evento societario nao monetario precisa de conciliacao especifica.',
    'unsupported_exact_share_class': 'Classe de acao sem correspondencia exata nas fontes.',
    'payment_date_unresolved_may_overlap_window': 'Existe provento sem data de pagamento resolvida que pode afetar o periodo.',
    'payment_publication_availability_unresolved': 'A data de publicacao do pagamento nao foi documentada.',
    'exact_issuer_identity_unresolved': 'Identidade do emissor ainda nao resolvida nas fontes.',
    'exact_CVM_code_unavailable': 'Codigo CVM exato ainda nao disponivel para conciliacao.',
    'unexplained_extreme_price_change': 'Variacao extrema de preco sem evento societario conciliado.',
    'payment_table_absent': 'Tabela de pagamentos ainda não localizada.',
    'payment_history_too_short': 'Cobertura histórica de pagamentos ainda não comprovada.',
    'payments_do_not_cover_common_return_window': 'Pagamentos não cobrem o período comum dos cenários.',
    'less_than_61_consecutive_prices': 'Preços não cobrem os meses comuns dos cenários.',
    'current_CVM_accounts_or_share_scale_unavailable': 'Fundamentos ou escala por ação ainda não conciliados.',
    'unit_or_other_share_structure_not_reconciled': 'Composição por cota ou classe de ação ainda não conciliada.',
    'cash_amount_or_share_scale_disagrees_with_B3': 'Valores dos pagamentos divergem da evidência independente.',
    'historical_quote_or_share_scale_disagrees_with_B3': 'Preços ou escala por ação divergem da evidência independente.',
    'historical_quote_or_share_scale_disagrees_with_independent_source': 'Precos ou escala por acao divergem entre o historico e a fonte independente de cotacoes.',
    'cash_amount_or_share_scale_disagrees_with_independent_source': 'Valores dos proventos divergem do historico primario da companhia.',
    'independent_cash_event_missing_from_payment_table': 'Existem proventos no historico primario sem pagamento conciliado na outra fonte.',
    'issuer_primary_share_action_document_missing': 'Falta o comunicado primario que comprova a data ou proporcao de um evento societario.',
    'issuer_primary_share_actions_disagree': 'Eventos societarios divergem dos comunicados primarios da companhia.',
    'issuer_independent_quote_cache_missing': 'Falta o historico de cotacoes do provedor independente da companhia.',
    'B3_cash_event_missing_from_payment_table': 'Existem eventos de proventos sem pagamento conciliado.',
    'missing_payment_date_or_amount': 'Há pagamentos sem data ou valor utilizável.',
    'insufficient_independent_cash_matches': 'Conciliação independente dos pagamentos ainda insuficiente.',
    'insufficient_independent_price_matches': 'Conciliação independente dos preços ainda insuficiente.',
    'missing_cached_source': 'Falta um arquivo de origem para conciliar o histórico.',
    'identity_unresolved': 'Identidade do emissor ou da classe de ação ainda não resolvida.',
    'financial_features_unavailable': 'Indicadores por ação ainda não estão completos para o modelo.',
}


def profile_reasons(asset, investor):
    reasons = []
    if asset.risk_level > investor.risk_level:
        reasons.append('Risco acima do perfil operacional.')
    if asset.minimum_years > investor.withdrawal_years:
        reasons.append('Prazo de resgate incompatível.')
    if investor.high_liquidity and asset.liquidity_days > 2:
        reasons.append('Liquidez insuficiente para o resgate declarado.')
    if asset.complex and investor.knowledge != 'Avançado':
        reasons.append('Complexidade incompatível com o conhecimento declarado.')
    return reasons


def screen_universe(dataset, investor, eligible_indices, forecasts=None):
    from types import SimpleNamespace

    preparation = dataset.metadata.get('stock_universe_preparation')
    if preparation is None:
        return None
    selection = dataset.metadata.get('stock_selection')
    rows = preparation.get('stocks')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Universo de preparacao invalido.')
    prepared = {asset.ticker: asset for asset in dataset.assets if asset.asset_class == 'acoes'}
    eligible = {dataset.assets[index].ticker for index in eligible_indices}
    forecasts = forecasts or {}
    if not set(forecasts).issubset(set(prepared) & eligible):
        raise ValueError('Previsao atribuida a acao nao preparada ou inelegivel.')
    seen, output = set(), []
    for row in rows:
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,29}', ticker) or ticker in seen:
            raise ValueError('Codigo ausente ou duplicado no universo de preparacao.')
        seen.add(ticker)
        asset = prepared.get(ticker)
        if row.get('model_ready') is not (asset is not None):
            raise ValueError('Universo e colunas de simulacao divergem.')
        criteria = asset or SimpleNamespace(risk_level=2, minimum_years=5, liquidity_days=2, complex=False)
        in_scope = row.get('in_user_scope', True) if selection is not None else True
        reasons = profile_reasons(criteria, investor) if in_scope else []
        codes = row.get('reason_codes', [])
        data_reasons = [DATA_REASONS.get(code, 'Histórico de preços, pagamentos ou fundamentos ainda não conciliado.') for code in codes]
        output.append({
            'ticker': ticker, 'name': row['name'], 'dataReady': asset is not None,
            'profileCompatible': not reasons if in_scope else None, 'profileReasons': reasons,
            'dataReasons': list(dict.fromkeys(data_reasons)), 'dataReasonCodes': codes,
            'eligible': ticker in eligible, 'riskLevel': criteria.risk_level,
            'minimumYears': criteria.minimum_years, 'liquidityDays': criteria.liquidity_days,
            'observedQuote': row.get('quote'), 'financialPeriod': row.get('financial_period'),
            'annualAccounts': row.get('annual_accounts'), 'preparationBasis': row.get('preparation_basis'),
            'sourceReviewReasons': [DATA_REASONS.get(code, 'A revisao da fonte ainda possui pendencias de comprovacao.') for code in row.get('source_review_reason_codes', [])],
            'predictedCashPerShare': forecasts.get(ticker),
        })
        if selection is not None:
            output[-1].update(selectionCompatible=in_scope,
                              selectionReasons=[] if in_scope else data_reasons, profileEvaluated=in_scope)
    if not set(prepared).issubset(seen):
        raise ValueError('Uma acao preparada nao foi avaliada no universo.')
    in_scope_count = sum(row.get('selectionCompatible', True) for row in output)
    result = {
        'asOf': preparation['as_of'], 'modelReference': dataset.dates[-1],
        'preparedAt': preparation['prepared_at'], 'inventorySha256': preparation['inventory_sha256'],
        'counts': {'stocks': len(output), 'readyStocks': len(prepared),
                   'annualAccountsStocks': sum(bool((row['annualAccounts'] or {}).get('available')) for row in output),
                   'pendingDataStocks': in_scope_count - len(prepared),
                   'profileCompatibleStocks': sum(row['profileCompatible'] is True for row in output),
                   'excludedByProfileStocks': sum(row['profileCompatible'] is False for row in output),
                   'eligibleStocks': sum(row['eligible'] for row in output),
                   'forecastedStocks': sum(row['predictedCashPerShare'] is not None for row in output)},
        'stocks': output, 'missingDataAreZero': False, 'unpreparedAssetsAllocated': False,
    }
    if selection is not None:
        result['dividendYieldSelection'] = {
            **selection, 'matchedCatalogTickersCount': in_scope_count,
            'unmatchedTickers': preparation.get('unmatched_selected_tickers', []),
            'outsideScopeStocks': len(output) - in_scope_count,
            'selectedReadyStocks': len(prepared), 'selectedPendingStocks': in_scope_count - len(prepared),
        }
        result['counts'].update(selectedScopeStocks=in_scope_count, excludedBySelectionStocks=len(output) - in_scope_count)
    return result


def preparation_summary(dataset):
    preparation = getattr(dataset, 'metadata', {}).get('stock_universe_preparation')
    if not preparation:
        return None
    rows = preparation['stocks']
    ready = sum(row['model_ready'] for row in rows)
    selection = dataset.metadata.get('stock_selection')
    selected = sum(row.get('in_user_scope', True) for row in rows) if selection is not None else len(rows)
    result = {'evaluatedStocks': len(rows), 'readyStocks': ready, 'pendingDataStocks': selected - ready,
              'asOf': preparation['as_of'], 'preparedAt': preparation['prepared_at']}
    if selection is not None:
        result.update(selectedScopeStocks=selected, excludedBySelectionStocks=len(rows) - selected,
                      dividendYieldSelection={**selection, 'matchedCatalogTickersCount': selected,
                          'unmatchedTickers': preparation.get('unmatched_selected_tickers', []),
                          'outsideScopeStocks': len(rows) - selected,
                          'selectedReadyStocks': ready, 'selectedPendingStocks': selected - ready})
    return result
