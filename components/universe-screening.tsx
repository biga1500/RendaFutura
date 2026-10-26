"use client";

import { useState } from "react";
import { ArrowLeft, ArrowRight, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import type { UniverseScreening } from "@/lib/recommendation";

const date = (value: string | null | undefined) => value ? value.slice(0, 10).split("-").reverse().join("/") : "Não informada";
const money = (value: number | null | undefined) => value == null ? "Não estimado" : value.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });

export function UniverseScreeningTable({ screening }: { screening?: UniverseScreening | null }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  if (!screening) return null;
  const search = query.trim().toLocaleLowerCase("pt-BR");
  const rows = screening.stocks.filter(row => (row.ticker + " " + row.name).toLocaleLowerCase("pt-BR").includes(search)
    && (filter === "all" && row.selectionCompatible !== false || filter === "catalog" || filter === "eligible" && row.eligible
      || filter === "pending" && !row.dataReady && row.selectionCompatible !== false
      || filter === "scope" && row.selectionCompatible === false || filter === "profile" && row.profileCompatible === false));
  const last = Math.max(0, Math.ceil(rows.length / 25) - 1);
  const current = Math.min(page, last);
  const counts = screening.counts;
  const selection = screening.dividendYieldSelection;
  const uniform = screening.stocks.every(row => !row.dataReady || row.preparationBasis === "rebuilt_from_reconciled_sources");
  return <section className="planner-section universe-screening" aria-labelledby="universe-screening-title">
    <h2 id="universe-screening-title">Universo analisado pelo motor</h2>
    <p>{counts.stocks} códigos no catálogo. {counts.readyStocks} com históricos preparados; {counts.pendingDataStocks} com dados pendentes{selection ? " dentro do recorte de dividend yield positivo" : ""}. Neste perfil, {counts.eligibleStocks} ações estão elegíveis e {counts.forecastedStocks} recebem previsão de proventos.</p>
    {selection && <p data-testid="dividend-yield-selection">Dividend yield maior que zero: {selection.selectedTickersCount} papéis na tabela enviada, {selection.matchedCatalogTickersCount} identificados no catálogo e {selection.outsideScopeStocks} códigos fora do recorte. {selection.unmatchedTickers.length > 0 && <>Identificação pendente: {selection.unmatchedTickers.join(", ")}. </>}Títulos do Tesouro mantidos. Data da tabela não informada; os indicadores não substituem o histórico de pagamentos.</p>}
    <p className="help">Contas anuais utilizáveis na data-base: {counts.annualAccountsStocks} códigos. {uniform ? "Todas as ações usam os mesmos critérios de conciliação de preços, valores e escala por ação." : "Esta amostra contém históricos da preparação anterior, identificados na tabela."} Calendários reportados não comprovam liquidação bancária nem constituem certificação financeira.</p>
    <p className="help">Referência do inventário: {date(screening.asOf)}. Históricos dos cenários: {date(screening.modelReference)}. Pendência de dados e incompatibilidade com o perfil são verificadas separadamente; valores ausentes não são zero.</p>
    <div className="market-toolbar">
      <label className="field"><span>Situação das ações</span><select aria-label="Filtrar situação das ações" value={filter} onChange={event => { setFilter(event.target.value); setPage(0); }}>
        <option value="all">{selection ? "Recorte de DY positivo" : "Todos os códigos"}</option><option value="eligible">Elegíveis neste perfil</option>
        <option value="pending">Com dados pendentes</option><option value="profile">Fora do perfil</option>
        {selection && <option value="scope">Fora do recorte de DY positivo</option>}
        {selection && <option value="catalog">Catálogo completo</option>}
      </select></label>
      <label className="market-search"><Search size={16} /><Input aria-label="Buscar ação na análise do motor" value={query} placeholder="Buscar por código ou nome" onChange={event => { setQuery(event.target.value); setPage(0); }} /></label>
    </div>
    <div className="planner-table-wrap"><table className="planner-table"><colgroup>{[14,10,10,15,14,12,25].map((width,index)=><col key={index} style={{width:`${width}%`}}/>)}</colgroup><thead><tr>
      <th>Ativo</th><th>Cotação observada</th><th>Lucro anual por ação</th><th>Preparação dos dados</th><th>Perfil</th><th>Proventos estimados em 12 meses por ação</th><th>Motivos</th>
    </tr></thead><tbody>{rows.slice(current * 25, (current + 1) * 25).map(row => <tr key={row.ticker}>
      <td><strong>{row.ticker}</strong><span>{row.name}</span></td>
      <td>{row.observedQuote?.value == null ? "Não disponível" : money(row.observedQuote.value)}<span>{date(row.observedQuote?.as_of)}</span></td>
      <td>{row.annualAccounts?.available ? money(row.annualAccounts.earnings_per_share) : "Não disponível"}<span>{date(row.annualAccounts?.fiscal_end)}</span>{row.annualAccounts?.source_url&&<a href={row.annualAccounts.source_url} target="_blank" rel="noreferrer">CVM</a>}</td>
      <td>{row.selectionCompatible === false ? "Fora do recorte de DY positivo" : row.dataReady ? row.preparationBasis === "retained_prior_snapshot" ? "Histórico anterior preparado" : "Histórico preparado" : "Dados pendentes"}</td>
      <td>{row.profileCompatible == null ? "Não avaliado" : row.profileCompatible ? "Compatível nos critérios de perfil" : "Incompatível"}</td>
      <td>{money(row.predictedCashPerShare)}</td>
      <td>{Array.from(new Set([...(row.selectionReasons ?? []), ...row.profileReasons, ...row.dataReasons])).join(" ") || "Elegível para a análise de carteira."}{row.dataReady&&!!row.sourceReviewReasons?.length&&<span>A nova conferência da fonte ainda tem pendências.</span>}</td>
    </tr>)}</tbody></table></div>
    {!rows.length && <p>Nenhum ativo encontrado nesta seleção.</p>}
    <div className="market-pagination">
      <button type="button" className="quiet-button" aria-label="Página anterior da análise" title="Página anterior" disabled={!current} onClick={() => setPage(current - 1)}><ArrowLeft size={18} /></button>
      <span>{rows.length ? current * 25 + 1 : 0} a {Math.min((current + 1) * 25, rows.length)} de {rows.length}</span>
      <button type="button" className="quiet-button" aria-label="Próxima página da análise" title="Próxima página" disabled={current === last} onClick={() => setPage(current + 1)}><ArrowRight size={18} /></button>
    </div>
  </section>;
}
