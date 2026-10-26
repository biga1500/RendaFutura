"use client";

import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Info, RefreshCw, Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { isIndicativeQuote, quoteClock, quoteSourceLabel, type CatalogueQuote } from "@/lib/quote-provenance";

type Field = {value?:number|null;status?:string;unit?:string;period_start?:string|null;period_end?:string|null;source_url?:string;method?:string};
type Stock = {ticker:string;name:string;share_class:string|null;model_ready:boolean;model_reference_date:string|null;model_pending_reason:string|null;issuer_registration?:{registration_status:string|null;cancelled_on:string|null;cancellation_reason:string|null;source_url:string;registration_blocks_current_purchase:boolean};quote:Field&CatalogueQuote;history:{observed_months?:number};payments:{status:string;event_count?:number;window_start?:string;window_end?:string;source_url?:string|null;authority?:string|null;primary_documents?:{url:string;published_at:string}[]};fundamentals:{status?:string;fields?:Record<string,Field>;fiscal_end?:string;published_at?:string}};
type Bond = {name:string;kind:string;maturity:string;quote:Field&{as_of?:string;rate_text?:string|null;market_status?:string};history:{observed_months?:number};payments:{status:string};model_ready:boolean};
type Inventory = {as_of:string;collected_at:string;counts:Record<string,number>;stocks:Stock[];treasury:Bond[]};
const money=(value:number|null|undefined)=>value==null?"Não informado":value.toLocaleString("pt-BR",{style:"currency",currency:"BRL"});
const day=(value:string|null|undefined)=>value?value.slice(0,10).split("-").reverse().join("/"):"Não informado";
const labels:Record<string,string>={available:"Disponível",partial:"Parcial",not_found:"Não encontrado",invalid:"Fonte não validada",invalid_format:"Formato não reconhecido",source_unavailable:"Fonte indisponível",source_http_error:"Erro de acesso à fonte",source_rate_limited:"Limite da fonte",stale:"Cotação antiga",not_fetched:"Não consultado",not_applicable:"Não se aplica",zero_window_confirmed:"Fonte informa zero no período",reported_zero_window:"Fonte informa zero no período",unresolved:"Não confirmado",missing_identity:"Identidade pendente",not_configured:"Não configurado",observed:"Pagamentos históricos",unavailable:"Não disponível",no_periodic_coupon:"Sem cupom periódico",observed_partial_coverage:"Histórico parcial de cupons",unknown_unvalidated_principal_interest_split:"Separação de juros e principal pendente"};
const snapshotLabels:Record<string,string>={indicative_snapshot:"Snapshot indicativo",stale_indicative_snapshot:"Snapshot indicativo antigo"};
const status=(value:string|undefined)=>value?snapshotLabels[value]??labels[value]??"Verificar fonte":"Não informado";
const fieldLabels:Record<string,string>={earnings_per_share:"Lucro por ação",profit_per_share:"Lucro por ação",free_cash_per_share:"FCL por ação",free_cash_flow:"FCL agregado",assets:"Ativo total",total_passivo:"Passivo total, incluindo patrimônio líquido",liabilities:"Passivo exigível",equity:"Patrimônio líquido",profit:"Lucro atribuível aos controladores",net_income:"Lucro líquido",revenue:"Receita",operating_cash_flow:"Caixa operacional",capex:"Aquisições de imobilizado e intangível",liabilities_to_assets:"Passivo / ativo"};

export function MarketInventory({currentContracts=[]}:{currentContracts?:string[]}) {
  const [open,setOpen]=useState(false);
  const [data,setData]=useState<Inventory|null>(null);
  const [error,setError]=useState("");
  const [loading,setLoading]=useState(false);
  const [retry,setRetry]=useState(0);
  const [tab,setTab]=useState<"stocks"|"treasury">("stocks");
  const [search,setSearch]=useState("");
  const [page,setPage]=useState(0);
  const [selected,setSelected]=useState<Stock|null>(null);
  useEffect(()=>{
    if(!open)return;
    const controller=new AbortController();
    fetch("/api/recommendation/market",{signal:controller.signal}).then(async response=>{
      const result=await response.json() as Inventory&{error?:string};
      if(!response.ok)throw new Error(result.error??"Não foi possível consultar as fontes.");
      if(!Array.isArray(result.stocks)||!Array.isArray(result.treasury))throw new Error("Resposta do inventário inválida.");
      setData(result);
    }).catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Falha na consulta.");})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[open,retry]);
  const query=search.trim().toLocaleLowerCase("pt-BR");
  const stocks=data?.stocks.filter(row=>(row.ticker+" "+row.name).toLocaleLowerCase("pt-BR").includes(query))??[];
  const bonds=data?.treasury.filter(row=>(row.name+" "+row.kind+" "+row.maturity).toLocaleLowerCase("pt-BR").includes(query))??[];
  const rows=tab==="stocks"?stocks:bonds;
  const lastPage=Math.max(0,Math.ceil(rows.length/25)-1);
  const current=Math.min(page,lastPage);
  return <details className="planner-details market-inventory" open={open} onToggle={event=>{const nextOpen=event.currentTarget.open;if(nextOpen){setLoading(true);setError("");}setOpen(nextOpen);}}>
    <summary>Ativos e fontes de dados</summary>
    {loading&&<p role="status">Consultando o catálogo consolidado...</p>}
    {error&&<div className="error" role="alert"><p>{error}</p><button type="button" className="quiet-button" onClick={()=>{setLoading(true);setError("");setRetry(n=>n+1);}}><RefreshCw size={16}/>Tentar novamente</button></div>}
    {data&&<>
      <p className="help">Consulta de referência: {day(data.as_of)}. {data.stocks.length} códigos de ações e outras estruturas, sem duplicar o mercado fracionário; {data.treasury.length} títulos na oferta atual. Cada fonte tem sua própria data. Dados ausentes não significam zero.</p>
      <p className="help">Ações: {data.counts.quotes_available??0} cotações de negócios informadas; {data.counts.quotes_indicative_snapshots??0} snapshots indicativos recentes; {data.counts.quotes_stale_indicative_snapshots??0} snapshots antigos. Snapshot não confirma último negócio nem disponibilidade de compra.</p>
      <div className="market-toolbar">
        <div className="market-tabs" role="group" aria-label="Classe de ativos">{([['stocks','Ações'],['treasury','Tesouro']] as const).map(([value,label])=><button type="button" key={value} aria-pressed={tab===value} onClick={()=>{setTab(value);setPage(0);setSelected(null);}}>{label}</button>)}</div>
        <label className="market-search"><Search size={16}/><Input aria-label="Buscar ativo no catálogo" value={search} onChange={event=>{setSearch(event.target.value);setPage(0);setSelected(null);}} placeholder="Buscar por código ou nome"/></label>
      </div>
      <div className="planner-table-wrap"><table className="planner-table"><thead>{tab==="stocks"?<tr><th>Ativo</th><th>Preço de referência</th><th>Data da referência</th><th>Lucro por ação</th><th>Período do lucro</th><th>Pagamentos</th><th>Modelo histórico</th><th>Fontes</th></tr>:<tr><th>Título</th><th>Vencimento</th><th>Preço de compra observado</th><th>Taxa informada</th><th>Data da cotação</th><th>Cupons históricos</th><th>Modelo histórico</th><th>Motor contratual</th></tr>}</thead>
        <tbody>{tab==="stocks"?stocks.slice(current*25,(current+1)*25).map(row=>{
          const earnings=row.fundamentals.fields?.earnings_per_share??row.fundamentals.fields?.profit_per_share;
          return <tr key={row.ticker}><td><strong>{row.ticker}</strong><span>{row.name}</span></td><td>{money(row.quote.value)}<span>{status(row.quote.status)}</span></td><td>{day(row.quote.as_of)}</td><td>{money(earnings?.value)}</td><td>{earnings?.period_start?`${day(earnings.period_start)} a ${day(earnings.period_end)}`:day(earnings?.period_end??row.fundamentals.fiscal_end)}</td><td>{status(row.payments.status)}{row.payments.event_count!=null&&<span>{row.payments.event_count} registros lidos</span>}</td><td>{row.model_ready?`Preparado em ${day(row.model_reference_date)}`:"Dados parciais"}</td><td><button type="button" className="quiet-button" aria-label={`Ver fontes de ${row.ticker}`} title="Ver fontes e períodos" onClick={()=>setSelected(row)}><Info size={18}/></button></td></tr>;
        }):bonds.slice(current*25,(current+1)*25).map(row=><tr key={`${row.kind}-${row.maturity}`}><td>{row.name}</td><td>{day(row.maturity)}</td><td>{money(row.quote.value)}</td><td>{row.quote.rate_text??"Não informada"}</td><td>{day(row.quote.as_of)}</td><td>{status(row.payments.status)}</td><td>{row.model_ready?"Histórico preparado":"Sem histórico preparado"}</td><td>{currentContracts.includes(`${row.kind}|${row.maturity}`)?"Fluxos contratuais e cenários preparados":"Não verificado nesta sessão"}</td></tr>)}</tbody></table></div>
      {!rows.length&&<p>Nenhum ativo encontrado.</p>}
      <div className="market-pagination"><button type="button" className="quiet-button" aria-label="Página anterior de ativos" title="Página anterior" disabled={current===0} onClick={()=>setPage(current-1)}><ArrowLeft size={18}/></button><span>{rows.length?current*25+1:0} a {Math.min((current+1)*25,rows.length)} de {rows.length}</span><button type="button" className="quiet-button" aria-label="Próxima página de ativos" title="Próxima página" disabled={current===lastPage} onClick={()=>setPage(current+1)}><ArrowRight size={18}/></button></div>
      {selected&&<section className="market-fields" aria-label={`Fontes e campos de ${selected.ticker}`}><h3>{selected.ticker}: fontes e períodos</h3>
        {selected.issuer_registration&&<p className="help">Registro do emissor na <a href={selected.issuer_registration.source_url} target="_blank" rel="noreferrer">CVM: cadastro de companhias</a>: {selected.issuer_registration.registration_status??"não localizado"}{selected.issuer_registration.cancelled_on&&`, cancelado em ${day(selected.issuer_registration.cancelled_on)}`}{selected.issuer_registration.cancellation_reason&&` (${selected.issuer_registration.cancellation_reason})`}. {selected.issuer_registration.registration_blocks_current_purchase?"Compra bloqueada pelo registro do emissor.":"Registro ativo não comprova negociação do código neste instante."}</p>}
        <p className="help">Cotação: {selected.quote.source_url?<a href={selected.quote.source_url} target="_blank" rel="noreferrer">{quoteSourceLabel(selected.quote)}</a>:"fonte não informada"}. Pagamentos: {status(selected.payments.status)}{selected.payments.window_start&&` na consulta de ${day(selected.payments.window_start)} a ${day(selected.payments.window_end)}`}. Histórico de preços lido: {selected.history.observed_months==null?"não informado":`${selected.history.observed_months} meses`}; isso não comprova conciliação para simulação.</p>
        {isIndicativeQuote(selected.quote)&&<>
          <p className="help"><strong>Snapshot indicativo: não confirma último negócio nem oferta executável.</strong> A data do provedor e a coleta não atualizam o último negócio conhecido.</p>
          <p className="help">Snapshot do provedor: <time dateTime={selected.quote.snapshot_at??undefined}>{quoteClock(selected.quote.snapshot_at)}</time>.<br/>Coleta do recibo: <time dateTime={selected.quote.retrieved_at??undefined}>{quoteClock(selected.quote.retrieved_at)}</time>.<br/>Último negócio informado pela fonte: {quoteClock(selected.quote.last_trade_at??selected.quote.last_trade_as_of)}{selected.quote.last_trade_source_url&&<> (<a href={selected.quote.last_trade_source_url} target="_blank" rel="noreferrer">{quoteSourceLabel({source_url:selected.quote.last_trade_source_url})}</a>)</>}.</p>
          {selected.quote.previous_reference&&<p className="help">Referência anterior preservada: {money(selected.quote.previous_reference.value)}, {day(selected.quote.previous_reference.as_of)} ({status(selected.quote.previous_reference.status)}){selected.quote.previous_reference.source_url&&<>. <a href={selected.quote.previous_reference.source_url} target="_blank" rel="noreferrer">{quoteSourceLabel(selected.quote.previous_reference)}</a></>}.</p>}
        </>}
        {selected.payments.source_url&&<p className="help"><a href={selected.payments.source_url} target="_blank" rel="noreferrer">Fonte dos registros de proventos ({selected.payments.authority==="primary"?"primária":"secundária"})</a>. Datas previstas e pagamentos declarados não comprovam liquidação financeira.</p>}
        {!!selected.payments.primary_documents?.length&&<div className="help"><p>Documentos de proventos localizados na CVM, com conteúdo ainda não validado para cálculo de renda:</p><ul>{selected.payments.primary_documents.map(item=><li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">Documento publicado em {day(item.published_at)}</a></li>)}</ul></div>}
        <div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Campo</th><th>Valor</th><th>Unidade</th><th>Período</th><th>Fonte</th></tr></thead><tbody>{Object.entries(selected.fundamentals.fields??{}).map(([name,field])=><tr key={name}><td>{fieldLabels[name]??name}</td><td>{field.value==null?status(field.status):field.value.toLocaleString("pt-BR",{maximumFractionDigits:6})}</td><td>{field.unit??"-"}</td><td>{field.period_start?`${day(field.period_start)} a `:""}{day(field.period_end)}</td><td>{field.source_url?<a href={field.source_url} target="_blank" rel="noreferrer">CVM</a>:"Não informada"}</td></tr>)}</tbody></table></div>
        <p className="help">Valores trimestrais ou acumulados no ano não são lucro anual previsto. Uma pendência nos pagamentos não apaga a cotação ou o balanço disponível.</p>
      </section>}
      <p className="help">O catálogo não garante elegibilidade, histórico completo nem disponibilidade de negociação neste instante. Os cenários continuam vinculados à data-base indicada no resultado, sem inventar pagamentos ou históricos ausentes.</p>
    </>}
  </details>;
}
