"""Small sequential access audit; never log credentials or response bodies."""

import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .collect_market_data import ROOT, write_json
from .credentials import brapi_token


def probe():
    token = brapi_token()
    checks = [
        ("basic_quote", "/api/quote/BBAS3"),
        ("short_history", "/api/v2/stocks/historical?symbols=BBAS3&range=1mo&interval=1d"),
        ("long_history", "/api/v2/stocks/historical?symbols=BBAS3&range=10y&interval=1mo"),
        ("dividends", "/api/v2/stocks/dividends?symbols=BBAS3"),
        ("sandbox_raw", "/api/v2/stocks/historical?symbols=PETR4&range=10y&interval=1mo&includeRaw=true"),
    ]
    report = {"credentialConfigured": bool(token), "checks": []}
    for name, path in checks:
        headers = {"User-Agent": "RendaFutura academic access audit"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with urllib.request.urlopen(urllib.request.Request("https://brapi.dev" + path, headers=headers), timeout=45) as response:
                body = json.load(response)
                result = body.get("results", [])
                report["checks"].append({"name": name, "status": response.status,
                                          "results": len(result) if isinstance(result, list) else None,
                                          "firstResultKeys": sorted(result[0]) if isinstance(result, list) and result else []})
                if name == "sandbox_raw":
                    folder = ROOT / "recommender/data/market/raw/access"
                    folder.mkdir(parents=True, exist_ok=True)
                    write_json(folder / "sandbox-raw-PETR4.json", body)
        except urllib.error.HTTPError as error:
            report["checks"].append({"name": name, "status": error.code})
        time.sleep(3.2)
    write_json(ROOT / ".sites-runtime/brapi-access.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    probe()
