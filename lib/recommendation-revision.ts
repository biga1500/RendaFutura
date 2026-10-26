import type { EngineStatus, SavedRecommendation } from "./recommendation";

export function recommendationIsOutdated(saved: SavedRecommendation, status: EngineStatus | null) {
  if (!saved) return false;
  const result = saved.result;
  if (status && (result.engineVersion !== status.engineVersion
      || status.realDataRevision != null && result.dataRevision !== status.realDataRevision)) return true;
  if (result.dataMode !== "real" || saved.settings.forecastPolicy !== "xgboost_experimental") return true;
  return result.status !== "income_data_unavailable" && (!result.portfolios?.length
    || result.plan.income_mode !== "distributions_only" || !!result.model && result.model.appliedMethod !== "xgboost");
}
