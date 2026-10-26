export type CatalogueQuote = {
  value?: number | null;
  status?: string;
  as_of?: string | null;
  basis?: string;
  provider?: string;
  source_url?: string | null;
  snapshot_at?: string | null;
  retrieved_at?: string | null;
  last_trade_at?: string | null;
  last_trade_as_of?: string | null;
  last_trade_source_url?: string | null;
  previous_reference?: CatalogueQuote;
};

export const isIndicativeQuote = (quote: CatalogueQuote) =>
  quote.status === "indicative_snapshot" || quote.status === "stale_indicative_snapshot" ||
  quote.basis === "provider_snapshot_not_confirmed_trade_or_executable_order";

export function quoteSourceLabel(quote: CatalogueQuote): string {
  try {
    const url = new URL(quote.source_url ?? "");
    if (url.protocol === "https:" && url.hostname === "brapi.dev") return "BRAPI, fonte secundária";
    if (url.protocol === "https:" && ["query1.finance.yahoo.com", "query2.finance.yahoo.com", "finance.yahoo.com"].includes(url.hostname)) return "Yahoo Finance, fonte secundária";
  } catch { /* Unknown sources keep a neutral label. */ }
  return "Fonte da cotação";
}

export function quoteClock(value: string | null | undefined): string {
  if (!value) return "Não informado";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value.split("-").reverse().join("/");
  const stamp = new Date(value);
  if (!Number.isFinite(stamp.getTime())) return "Não informado";
  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "short", timeStyle: "medium", timeZone: "UTC",
  }).format(stamp) + " UTC";
}
