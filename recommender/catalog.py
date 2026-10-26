"""Separate the public instrument catalogue from the audited modelling sample."""

import csv
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import urllib.parse

from .collect_market_data import ROOT, TREASURY_URL, cached_source, write_json


def collect():
    folder = ROOT / "recommender/data/market"
    raw = folder / "raw/catalog"
    raw.mkdir(parents=True, exist_ok=True)
    sources, stocks, page = [], {}, 1
    while True:
        query = urllib.parse.urlencode({"type": "stock", "limit": 2000, "page": page,
                                       "sortBy": "symbol", "sortOrder": "asc"})
        body = json.loads(cached_source("https://brapi.dev/api/v2/tickers?" + query,
                                       f"tickers-{page}.json", raw, sources, True))
        for row in body["results"]:
            if row.get("isActive") is False:
                continue
            stocks[row["symbol"]] = {"ticker": row["symbol"], "name": row.get("name"),
                                     "type": row.get("assetType"), "subType": row.get("subType")}
        if not body.get("pagination", {}).get("hasNextPage"):
            break
        page += 1
        if page > 20:
            raise ValueError("Paginacao do catalogo excedeu o limite de seguranca.")
    # Daily public prices include historical instruments, not necessarily an
    # assertion that every title can be bought today.
    content = cached_source(TREASURY_URL, "tesouro-precos.csv", folder / "raw", sources, False)
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=";")
    latest, bonds = None, {}
    for row in reader:
        day = datetime.strptime(row["Data Base"], "%d/%m/%Y").date()
        maturity = datetime.strptime(row["Data Vencimento"], "%d/%m/%Y").date()
        if day > date.today() or maturity <= day:
            continue
        if latest is None or day > latest:
            latest, bonds = day, {}
        if day == latest:
            key = row["Tipo Titulo"] + " " + maturity.isoformat()
            bonds[key] = {"name": row["Tipo Titulo"], "maturity": maturity.isoformat(),
                          "asOf": day.isoformat()}
    catalogue = {"collectedAt": datetime.now(timezone.utc).isoformat(),
                 "stocks": sorted(stocks.values(), key=lambda row: row["ticker"]),
                 "treasury": list(bonds.values()), "treasuryReference": latest.isoformat(),
                 "sources": sources,
                 "policy": "Catalogued does not mean eligible, priced, or complete for model training."}
    write_json(folder / "catalog.json", catalogue)
    print(json.dumps({"stocks": len(stocks), "treasury": len(bonds),
                      "treasuryReference": catalogue["treasuryReference"]}))


def summary():
    folder = Path(os.environ.get("RF_DATA_DIR", ROOT / "recommender/data/market"))
    path = folder / "catalog.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {"stocks": len(data["stocks"]), "treasury": len(data["treasury"]),
            "collectedAt": data["collectedAt"], "treasuryReference": data["treasuryReference"],
            "policy": data["policy"]}


if __name__ == "__main__":
    collect()
