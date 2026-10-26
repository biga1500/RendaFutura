"""Record a frozen engineering run without collecting identities or secrets."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
FILES = ["recommender/engine.py", "recommender/forecast.py", "recommender/forecast_paths.py",
         "recommender/rollover.py", "recommender/planning.py", "recommender/simulation.py",
         "recommender/datasets.py", "recommender/contracts.py", "recommender/data_coverage.py",
         "recommender/collect_income_data.py", "recommender/collect_market_data.py",
         "recommender/api.py", "lib/questionnaire.ts", "lib/recommendation.ts",
         "lib/recommendation-service.ts", "lib/request-body.ts", "app/api/[...path]/route.ts",
         "components/retirement-planner.tsx", "components/rendafutura.tsx", "lib/auth.ts",
         "db/schema.ts", "recommender/requirements.txt", "scripts/run-recommender-local.ps1"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_summary(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    summary = {"cases": len(cases), "failures": sum(case.find("failure") is not None for case in cases),
               "errors": sum(case.find("error") is not None for case in cases),
               "skipped": sum(case.find("skipped") is not None for case in cases),
               "testSuiteNodes": len(list(root.iter("testsuite"))),
               "sha256": digest(path)}
    if not cases or summary["failures"] or summary["errors"]:
        raise ValueError("Nao registrar como aprovada uma suite ausente ou com falhas.")
    return summary


def record(folder):
    engine = json.loads((folder / "engine-results.json").read_text(encoding="utf-8"))
    browser = json.loads((folder / "browser-results.json").read_text(encoding="utf-8"))
    if not engine.get("completed") or not browser.get("completed"):
        raise ValueError("A execucao completa ainda nao terminou.")
    result = engine["cases"][0]["result"]
    data = {"recordedAt": datetime.now(timezone.utc).isoformat(),
            "engineVersion": result["engineVersion"], "dataReference": result["referenceDate"],
            "datasetSnapshot": result["dataCoverage"]["readiness"]["snapshotId"],
            "forecastTarget": result["forecastTarget"]["id"],
            "python": platform.python_version(), "syntheticPersonas": len(engine["cases"]),
            "realHumanParticipants": 0, "financiallyValidated": False,
            "pythonTests": test_summary(ROOT / "work/academic-final-tests.xml"),
            "webBodyTests": test_summary(ROOT / "work/web-body-final-tests.xml"),
            "browserCases": len(browser["cases"]), "browserTestAccounts": browser.get("testAccounts"),
            "allTestAccountsDeleted": all(case.get("testAccountDeleted") for case in browser["cases"]),
            "engineResultsSha256": digest(folder / "engine-results.json"),
            "browserResultsSha256": digest(folder / "browser-results.json"),
            "files": {name: digest(ROOT / name) for name in FILES}}
    (folder / "provenance.json").write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: data[key] for key in ("engineVersion", "datasetSnapshot", "pythonTests", "webBodyTests", "browserCases", "allTestAccountsDeleted")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    record(parser.parse_args().folder)
