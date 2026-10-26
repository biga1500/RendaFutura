"""Re-run synthetic personas against real data; do not edit academic documents."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .contracts import RecommendationRequest
from .engine import compute


def audit(fixtures, output):
    output.mkdir(parents=True, exist_ok=True)
    report = {"startedAt": datetime.now(timezone.utc).isoformat(), "cases": [],
              "scope": "Synthetic personas; retrospective real data; not financial validation."}
    for persona in fixtures:
        answers, classification = persona["answers"], persona["result"]
        level = ["Conservador", "Moderado", "Arrojado"].index(classification["profile"])
        request = RecommendationRequest.model_validate({
            "data_mode": "real", "forecast_policy": "validation",
            "investor": {"risk_level": level, "knowledge": classification["knowledge"],
                         "withdrawal_years": [0, 1, 3, 6][["Em menos de 1 ano", "Entre 1 e 3 anos", "Entre 3 e 5 anos", "Depois de 5 anos"].index(answers["withdrawal"])],
                         "high_liquidity": answers["liquidity"] == "Muito provável"},
            "plan": {"income_mode": "distributions_only", "age": persona["age"],
                     "retirement_age": persona["retirementAge"], "end_age": 90,
                     "initial_capital": persona["capital"], "monthly_contribution": persona["contribution"],
                     "target_income": persona["target"], "annual_cost_percent": .5,
                     "seed": 42, "simulations": 500, "block_months": 6}})
        started = time.monotonic()
        result = compute(request)
        record = {"id": persona["id"], "name": persona["name"], "profile": classification["profile"],
                  "request": request.model_dump(), "result": result}
        if result["model"]:
            record["experimental"] = compute(request.model_copy(update={"forecast_policy": "xgboost_experimental"}))
            assert record["experimental"]["model"]["appliedMethod"] == "xgboost"
        for outcome in [result, *([record["experimental"]] if "experimental" in record else [])]:
            assert outcome["status"] == "ready" and outcome["dataMode"] == "real"
            assert outcome["incomeAnalysis"]["usesPrincipal"] is False
            assert abs(sum(a["weight"] for a in outcome["allocation"]) - 1) < 1e-6
            assert all(-1e-7 <= a["weight"] <= .25 + 1e-7 for a in outcome["allocation"] if a["assetClass"] == "acoes")
            assert sum(a["weight"] for a in outcome["allocation"] if a["assetClass"] == "acoes") <= [0, .4, .65][level] + 1e-7
            assert sum(a["weight"] for a in outcome["allocation"] if a["assetClass"] == "liquidez") >= [.25, .15, .1][level] - 1e-7
            assert outcome["portfolio"]["annualVolatility"] <= [.08, .16, .28][level] + 1e-7
            if outcome["model"]:
                assert outcome["model"]["usedForIncomeSimulation"] is True
                assert outcome["model"]["uncertaintyBasis"] == "validation_residuals_only"
                assert all(a["predictedCashPerShare"] is not None for a in outcome["allocation"] if a["assetClass"] == "acoes")
        record["seconds"] = round(time.monotonic() - started, 2)
        report["cases"].append(record)
        (output / "engine-results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        print(json.dumps({"id": persona["id"], "profile": classification["profile"],
                          "income": result["simulation"]["monthlyIncomeCapacity"][1],
                          "experimentalIncome": record.get("experimental", {}).get("simulation", {}).get("monthlyIncomeCapacity"),
                          "seconds": record["seconds"]}), flush=True)
    last = report["cases"][-1]
    assert compute(RecommendationRequest.model_validate(last["request"])) == last["result"]
    report["reproducible"] = True
    report["completed"] = True
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    (output / "engine-results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print("PASS: eight personas, actual algorithms, forecast policies, constraints and reproducibility.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("personas", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    audit(json.loads(arguments.personas.read_text(encoding="utf-8"))["personas"], arguments.output)
