"""Current purchase offers, never historical sale quotes or model-ready histories.

collect_current(folder, refresh=False) caches public HTTPS evidence in raw/current
and writes current-offers.json there. It never activates a prepared snapshot.
Rates are percentage points per year; Selic/IPCA spreads are not total returns.
Naive official timestamps are Brasilia time. Quotes older than five calendar
days, beyond five minutes in the future, or already matured are rejected.

import_current_csv(path, delimiter=',', decimal_format='dot') is an offline
alternative. Required headers are CSV_COLUMNS; empty quotes/availability remain
unknown. Optional columns: minimum_investment, minimum_fraction, sell_rate,
market_status, catalogue_group. Manual imports do not establish catalogue
completeness. Structure support always requires a subsequent history/payment join.
"""

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import tempfile
import unicodedata
import urllib.error
from urllib.parse import parse_qsl, urlsplit
import urllib.request

from lxml import html


CURRENT_URL = "https://www.tesourodireto.com.br/o/rentabilidade/investir"
LEGACY_URL = "https://www.tesourodireto.com.br/json/br/com/b3/tesourodireto/service/api/treasurybondsinfo.json"
PAGE_URL = "https://www.tesourodireto.com.br/produtos/dados-sobre-titulos/rendimento-dos-titulos"
SANDBOX_URL = ("https://brapi.dev/api/v2/treasury/indicators?symbols="
               "tesouro-selic-01032031,tesouro-prefixado-com-juros-semestrais-01012037,"
               "tesouro-ipca-com-juros-semestrais-15082060")
BRT = timezone(timedelta(hours=-3))
MAX_BYTES = 4 * 1024 * 1024
MAX_INSTRUMENTS = 1000
MAX_QUOTE_AGE = timedelta(days=5)
CACHE_AGE = timedelta(minutes=15)
FUTURE_TOLERANCE = timedelta(minutes=5)
CSV_COLUMNS = ("kind", "maturity", "name", "buy_price", "sell_price", "buy_rate", "as_of", "tradable", "source_url")
SUPPORTED_KINDS = {"Tesouro Selic", "Tesouro Prefixado", "Tesouro IPCA+",
                   "Tesouro Prefixado com Juros Semestrais", "Tesouro IPCA+ com Juros Semestrais"}


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Reference time must include a timezone.")
    return value.astimezone(timezone.utc)


def _normalize(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value)
                            if not unicodedata.combining(c)).lower().split())


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError("Invalid " + field)
    return value.strip()


def _url(value):
    value = _text(value, "source_url")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or any(key.lower() in {"token", "key", "api_key", "access_token"}
                                      for key, _ in parse_qsl(parsed.query))):
        raise ValueError("Source URL must be public HTTPS without credentials.")
    return value


def _timestamp(value, now):
    if not isinstance(value, str):
        raise ValueError("Missing quote timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid quote timestamp") from error
    result = result.replace(tzinfo=BRT) if result.tzinfo is None else result
    if result > now + FUTURE_TOLERANCE:
        raise ValueError("Future quote timestamp")
    if now - result > MAX_QUOTE_AGE:
        raise ValueError("Stale quote timestamp")
    return result.isoformat()


def _number(value, *, decimal_format="dot", price=False):
    if value is None or isinstance(value, str) and value.strip().lower() in {"", "-", "--", "n/a", "null"}:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Invalid numeric quote")
    if isinstance(value, str):
        value = value.strip().removeprefix("R$").strip()
        if decimal_format == "comma":
            if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?", value):
                raise ValueError("Invalid comma-decimal quote")
            value = value.replace(".", "").replace(",", ".")
        elif not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
            raise ValueError("Invalid dot-decimal quote")
    number = float(value)
    if not math.isfinite(number) or price and number < 0:
        raise ValueError("Nonfinite or negative price")
    return None if price and number == 0 else number


def _rate(value, *, decimal_format="dot"):
    if value is None or isinstance(value, str) and value.strip().lower() in {"", "-", "--", "n/a", "null"}:
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if text.upper() in {"SELIC", "IPCA", "IGP-M"}:
            return None, text
        match = re.fullmatch(r"(?:(SELIC|IPCA|IGP-M)\s*\+\s*)?([+-]?\d+(?:[.,]\d+)?)\s*%", text, re.I)
        if match:
            result = float(match[2].replace(",", "."))
        else:
            result = _number(value, decimal_format=decimal_format)
    else:
        text, result = None, _number(value)
    if result is not None and not -100 < result <= 1000:
        raise ValueError("Implausible annual rate")
    return result, text


def _boolean(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false", "unknown"}:
        return {"true": True, "false": False, "unknown": None}[value.lower()]
    raise ValueError("Availability must be true, false or unknown")


def _kind(name):
    normal = _normalize(name)
    if normal.startswith("tesouro renda+"):
        return "Tesouro Renda+"
    if normal.startswith("tesouro educa+"):
        return "Tesouro Educa+"
    if normal.startswith("tesouro reserva"):
        return "Tesouro Reserva"
    return re.sub(r"\s+\d{4}$", "", name).strip()


def _instrument(row, source_url, now, *, decimal_format="dot"):
    name = _text(row.get("name"), "name")
    kind = _text(row.get("kind") or _kind(name), "kind")
    try:
        maturity = date.fromisoformat(str(row.get("maturity", "")))
    except ValueError as error:
        raise ValueError("Invalid maturity") from error
    if maturity <= now.astimezone(BRT).date():
        raise ValueError("Matured instrument is not a current purchase offer")
    as_of = _timestamp(row.get("as_of"), now)
    buy = _number(row.get("buy_price"), decimal_format=decimal_format, price=True)
    sell = _number(row.get("sell_price"), decimal_format=decimal_format, price=True)
    rate, rate_text = _rate(row.get("buy_rate"), decimal_format=decimal_format)
    sell_rate, sell_text = _rate(row.get("sell_rate"), decimal_format=decimal_format)
    minimum = _number(row.get("minimum_investment"), decimal_format=decimal_format, price=True)
    fraction = _number(row.get("minimum_fraction"), decimal_format=decimal_format, price=True)
    if fraction is not None and fraction > 1:
        raise ValueError("Invalid minimum fraction")
    available = _boolean(row.get("tradable"))
    if buy is None and available is True:
        available = None
    state = row.get("market_status") or "unknown"
    if state not in {"open", "closed", "suspended", "unknown"}:
        raise ValueError("Invalid market status")
    if state in {"closed", "suspended"}:
        available = False
    known_kind = _kind(name)
    if kind != known_kind:
        raise ValueError("Kind disagrees with instrument name")
    isin, code, selic = row.get("isin"), row.get("treasury_bond_code"), row.get("selic_code")
    if isin is not None and (not isinstance(isin, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin)):
        raise ValueError("Invalid ISIN identity")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int) or not 0 < code < 1_000_000_000):
        raise ValueError("Invalid Treasury bond code")
    if selic is not None and (not isinstance(selic, str) or not re.fullmatch(r"\d{1,10}", selic)):
        raise ValueError("Invalid Selic identity")
    basis = ("spread_over_selic" if kind in {"Tesouro Selic", "Tesouro Reserva"} else
             "real_above_ipca" if "IPCA+" in kind or kind in {"Tesouro Renda+", "Tesouro Educa+"} else
             "nominal_annual" if "Prefixado" in kind else "unknown")
    return {"kind": kind, "maturity": maturity.isoformat(), "name": name,
            "buy_price": buy, "sell_price": sell, "buy_rate": rate, "sell_rate": sell_rate,
            "buyPrice": buy, "minimumInvestment": minimum, "minimumFraction": fraction,
            "minimum_investment": minimum, "minimum_fraction": fraction,
            "buy_rate_text": rate_text, "sell_rate_text": sell_text,
            "rate_basis": basis, "rate_unit": "percentage_points_per_year",
            "as_of": as_of, "tradable": available, "source_url": _url(source_url),
            "market_status": state, "offered_for_purchase": row.get("offered_for_purchase"),
            "availability_basis": row.get("availability_basis", "not_provided"),
            "catalogue_group": row.get("catalogue_group"),
            "isin": isin, "treasury_bond_code": code,
            "selic_code": selic, "interest_payment_type": row.get("interest_payment_type"),
            "support_status": "supported_structure_pending_history" if kind in SUPPORTED_KINDS else
                              "unsupported_amortizing_structure" if kind in {"Tesouro Renda+", "Tesouro Educa+"} else
                              "unsupported_reserve_structure" if kind == "Tesouro Reserva" else "unsupported_instrument_type",
            "recommendation_supported": False,
            "support_reason": "Current offer only; prepared history, risk and paid-cash evidence require a separate join."}


def _parse_rows(rows, source_url, now):
    if not 0 < len(rows) <= MAX_INSTRUMENTS:
        raise ValueError("Current catalogue is empty or exceeds the instrument limit")
    instruments, failures, identities = [], [], set()
    for number, row in enumerate(rows, 1):
        try:
            item = _instrument(row, source_url, now)
        except (ValueError, TypeError) as error:
            failures.append({"row": number, "name": row.get("name"), "reason": str(error)})
            continue
        identity = (item["kind"], item["maturity"])
        if identity in identities:
            raise ValueError("Duplicate current instrument identity")
        identities.add(identity)
        instruments.append(item)
    instruments.sort(key=lambda item: (item["kind"], item["maturity"], item["name"]))
    return {"instruments": instruments, "failures": failures, "catalogue_complete": not failures,
            "observed_rows": len(rows)}


def parse_official_json(content, *, source_url=CURRENT_URL, now=None):
    """Parse the current Liferay feed or the legacy B3 response, without network."""
    now = _now(now)
    body = json.loads(content)
    if not isinstance(body, dict):
        raise ValueError("Invalid current Treasury JSON root")
    rows = []
    if "TesouroLegado" in body or "Tesouro24x7" in body:
        for group, entries in body.items():
            if not isinstance(entries, list):
                raise ValueError("Unexpected current catalogue group")
            for item in entries:
                if not isinstance(item, dict):
                    raise ValueError("Invalid current catalogue entry")
                rows.append({"name": item.get("treasuryBondName"), "maturity": str(item.get("maturityDate", ""))[:10],
                             "buy_price": item.get("unitaryInvestmentValue"), "sell_price": item.get("unitaryRedemptionValue"),
                             "buy_rate": item.get("investmentProfitabilityIndexerName"),
                             "sell_rate": item.get("redemptionProfitabilityFeeIndexerName"),
                             "minimum_investment": item.get("investmentBondMinimumValue"),
                             "minimum_fraction": item.get("minimumInvestmentFraction"),
                             "as_of": item.get("lastMarketPricingDate"), "catalogue_group": group,
                             "offered_for_purchase": True, "availability_basis": "official_current_purchase_catalogue",
                             "isin": item.get("isinCode"), "treasury_bond_code": item.get("treasuryBondCode"),
                             "selic_code": item.get("selicCode"), "interest_payment_type": item.get("typeReceiptInterest")})
    else:
        response = body.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("TrsrBdTradgList"), list):
            raise ValueError("Unrecognized official current-offer schema")
        market = response.get("TrsrBondMkt", {})
        state = {"aberto": "open", "fechado": "closed", "suspenso": "suspended"}.get(_normalize(str(market.get("sts", ""))), "unknown")
        for entry in response["TrsrBdTradgList"]:
            item = entry.get("TrsrBd") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                raise ValueError("Invalid legacy current-offer entry")
            rows.append({"name": item.get("nm"), "maturity": str(item.get("mtrtyDt", ""))[:10],
                         "buy_price": item.get("untrInvstmtVal"), "sell_price": item.get("untrRedVal"),
                         "buy_rate": item.get("anulInvstmtRate"), "sell_rate": item.get("anulRedRate"),
                         "as_of": market.get("qtnDtTm"), "market_status": state,
                         "tradable": False if state in {"closed", "suspended"} else None,
                         "catalogue_group": "legacy_b3", "treasury_bond_code": item.get("cd")})
    return _parse_rows(rows, source_url, now)


def parse_public_html(content, *, source_url=PAGE_URL, now=None):
    """Read explicit market state and quote tables; never execute page scripts."""
    now = _now(now)
    document = html.fromstring(content, parser=html.HTMLParser(encoding="utf-8"))
    for node in document.xpath("//script|//style"):
        node.drop_tree()
    labels = {_normalize(text) for text in document.xpath("//h1/text()|//h2/text()|//h3/text()")}
    states = {state for label, state in [("mercado aberto", "open"), ("mercado fechado", "closed"),
                                         ("mercado suspenso", "suspended")] if label in labels}
    state = next(iter(states)) if len(states) == 1 else "unknown"
    text = _normalize(document.text_content())
    match = re.search(r"ultima atualizacao:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2})h(\d{2})", text)
    updated = None
    if match:
        updated = _timestamp(datetime.strptime(match[1] + " " + match[2] + ":" + match[3], "%d/%m/%Y %H:%M").isoformat(), now)
    if not updated:
        state = "unknown"
    rows = []
    headers = {"titulo": "name", "vencimento": "maturity", "preco de compra": "buy_price",
               "preco de venda": "sell_price", "taxa de compra": "buy_rate",
               "taxa de venda": "sell_rate", "atualizacao": "as_of"}
    for table in document.xpath("//table"):
        columns = [headers.get(_normalize(cell.text_content())) for cell in table.xpath(".//tr[th][1]/th")]
        if not {"name", "maturity", "buy_price"} <= set(columns):
            continue
        for record in table.xpath(".//tr[td]"):
            cells = record.xpath("./td")
            if len(cells) != len(columns):
                raise ValueError("Misaligned current quote table")
            row = {key: cell.text_content().strip() for key, cell in zip(columns, cells) if key}
            row["as_of"] = row.get("as_of") or updated
            row["market_status"] = state
            row["offered_for_purchase"] = True
            row["availability_basis"] = "official_public_purchase_table"
            for key in ("buy_price", "sell_price"):
                row[key] = _number(row.get(key), decimal_format="comma", price=True)
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", row["maturity"]):
                row["maturity"] = datetime.strptime(row["maturity"], "%d/%m/%Y").date().isoformat()
            rows.append(row)
    result = _parse_rows(rows, source_url, now) if rows else {"instruments": [], "failures": [], "catalogue_complete": False, "observed_rows": 0}
    result["catalogue_complete"] = False
    result["market_status"] = {"state": state, "as_of": updated, "source_url": source_url,
                               "scope": "TesouroLegado"}
    return result


def parse_brapi_sandbox(content, *, now=None):
    now = _now(now)
    body = json.loads(content)
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("Unrecognized brapi sandbox schema")
    rows = []
    for item in body["results"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid brapi sandbox entry")
        kind = item.get("bondType")
        maturity = item.get("maturityDate")
        rows.append({"kind": kind, "name": f"{kind} {str(maturity)[:4]}", "maturity": maturity,
                     "buy_price": item.get("buyPrice"), "sell_price": item.get("sellPrice"),
                     "buy_rate": item.get("buyRate"), "sell_rate": item.get("sellRate"),
                     "as_of": item.get("baseDate"), "availability_basis": "indicative_daily_quote_not_purchase_availability",
                     "catalogue_group": "brapi_sandbox"})
    result = _parse_rows(rows, SANDBOX_URL, now)
    result["catalogue_complete"] = False
    return result


def import_current_csv(path, *, now=None, delimiter=",", decimal_format="dot"):
    """Offline current-quote import; missing numeric fields and booleans stay None."""
    path, now = Path(path), _now(now)
    if decimal_format not in {"dot", "comma"} or delimiter not in {",", ";", "\t"}:
        raise ValueError("Choose an explicit supported delimiter and decimal format")
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Missing or oversized current-quote CSV")
    content = path.read_bytes()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=delimiter)
    fields = reader.fieldnames or []
    if len(fields) != len(set(fields)) or not set(CSV_COLUMNS) <= set(fields):
        raise ValueError("Missing or duplicated current-quote CSV columns")
    rows = list(reader)
    normalized, failures, identities = [], [], set()
    if not 0 < len(rows) <= MAX_INSTRUMENTS:
        raise ValueError("Invalid current-quote CSV row count")
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Misaligned current-quote CSV")
        item = _instrument(row, row["source_url"], now, decimal_format=decimal_format)
        identity = (item["kind"], item["maturity"])
        if identity in identities:
            raise ValueError("Duplicate current instrument identity")
        identities.add(identity)
        normalized.append(item)
    return {"instruments": normalized, "failures": failures, "catalogue_complete": False,
            "sources": [{"file": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest(),
                         "bytes": len(content), "method": "manual_csv", "availability_independently_verified": False}]}


def _contained(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Current quote cache path escapes its data folder or is a symlink")
    return path


def _write(path, content, root):
    _contained(path, root)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".current-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _contained(path, root)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        original, target = urlsplit(request.full_url), urlsplit(newurl)
        if (target.scheme != "https" or target.hostname != original.hostname or target.username or target.password
                or (target.port or 443) != (original.port or 443)):
            raise ValueError("Public quote redirect changed HTTPS origin")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "RendaFutura academic current-offer collector",
                                                   "Accept": "application/json,text/html"})
    opener = urllib.request.build_opener(_PublicRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=25) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Public current-quote response exceeds size limit")
        return body, response.status, response.headers.get("Content-Type", "")


def _fetch(url, name, cache, root, manifest, refresh, now):
    path, meta_path = _contained(cache / name, root), _contained(cache / (name + ".meta.json"), root)
    if path.is_file() and meta_path.is_file() and not refresh and path.stat().st_size <= MAX_BYTES:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(meta["retrieved_at"])
            body = path.read_bytes()
            if (meta["url"] == url and timedelta(0) <= now - retrieved <= CACHE_AGE
                    and meta["sha256"] == hashlib.sha256(body).hexdigest()):
                manifest.append({**meta, "cache_hit": True})
                return body
        except (ValueError, KeyError, TypeError):
            pass
    body, status, content_type = _download(url)
    record = {"url": url, "file": path.relative_to(root).as_posix(), "http_status": status,
              "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
              "retrieved_at": now.isoformat(), "content_type": content_type}
    _write(path, body, root)
    _write(meta_path, json.dumps(record, ensure_ascii=True).encode(), root)
    manifest.append({**record, "cache_hit": False})
    return body


def collect_current(folder, refresh=False):
    root, now = Path(folder).resolve(), _now()
    cache = _contained(root / "raw" / "current", root)
    cache.mkdir(parents=True, exist_ok=True)
    manifest, failures, instruments, complete, market = [], [], [], False, None

    def attempt(url, name, parser):
        try:
            body = _fetch(url, name, cache, root, manifest, refresh, now)
            result = parser(body, now=now)
            failures.extend({"source_url": url, **row} for row in result["failures"])
            return result
        except (OSError, ValueError, urllib.error.URLError) as error:
            status = error.code if isinstance(error, urllib.error.HTTPError) else None
            code = ("http_error" if status else "tls_verification_failed" if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError)
                    else "network_error" if isinstance(error, (OSError, urllib.error.URLError)) else "invalid_source_data")
            record = {"source_url": url, "code": code, "http_status": status}
            # Do not persist response bodies, request headers or exception text that could contain secrets.
            failures.append(record)
            if status:
                manifest.append({"url": url, "http_status": status, "retrieved_at": now.isoformat(), "status": "failed"})
            return None

    for url, name in [(CURRENT_URL, "official-investir.json"), (LEGACY_URL, "official-legacy.json")]:
        result = attempt(url, name, lambda content, now: parse_official_json(content, source_url=url, now=now))
        if result and result["instruments"]:
            instruments, complete = result["instruments"], result["catalogue_complete"]
            break
    page = attempt(PAGE_URL, "official-offers.html", parse_public_html)
    if page:
        market = page["market_status"]
        if not instruments and page["instruments"]:
            instruments, complete = page["instruments"], page["catalogue_complete"]
        for item in instruments:
            if item["catalogue_group"] == "TesouroLegado" and market["state"] != "unknown":
                item["market_status"] = market["state"]
                item["market_status_source_url"] = PAGE_URL
                if market["state"] in {"closed", "suspended"}:
                    item["tradable"] = False
                elif item["buy_price"] is not None:
                    item["tradable"] = True
                    item["availability_basis"] = "official_purchase_catalogue_and_open_market"
    if not instruments:
        sandbox = attempt(SANDBOX_URL, "brapi-sandbox.json", parse_brapi_sandbox)
        if sandbox:
            instruments = sandbox["instruments"]
        complete = False
    counts = {"instruments": len(instruments), "with_buy_quote": sum(i["buy_price"] is not None for i in instruments),
              "tradable": sum(i["tradable"] is True for i in instruments),
              "not_tradable": sum(i["tradable"] is False for i in instruments),
              "availability_unknown": sum(i["tradable"] is None for i in instruments),
              "supported_structure": sum(i["kind"] in SUPPORTED_KINDS for i in instruments),
              "unsupported_structure": sum(i["kind"] not in SUPPORTED_KINDS for i in instruments)}
    counts["by_kind"] = {kind: sum(i["kind"] == kind for i in instruments) for kind in sorted({i["kind"] for i in instruments})}
    report_path = cache / "current-offers.json"
    report = {"schema_version": 1, "status": "ready" if instruments and complete else "partial" if instruments else "unavailable",
              "instruments": instruments, "sources": manifest, "failures": failures,
              "catalogue_complete": complete, "market_status": market, "collected_at": now.isoformat(),
              "counts": counts, "cache_directory": str(cache), "report_path": str(report_path),
              "historical_prices_establish_purchase_availability": False,
              "recommendation_ready": False}
    _write(report_path, json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False).encode(), root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    report = collect_current(args.folder, refresh=args.refresh)
    print(json.dumps({key: report[key] for key in ("status", "counts", "catalogue_complete", "failures", "report_path")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
