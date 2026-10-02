import staticModels from "@/data/models.json"
import { API_BASE } from "@/lib/api"
import type { BillingSource } from "@/lib/jobs"

export type ModelSource = "curated" | "discovered"

export interface CatalogModel {
  id: string
  label: string
  reasoning_efforts: string[]
  default_reasoning_effort: string | null
  source: ModelSource
  available: boolean
  unavailable_reason: string | null
}

export interface ModelCatalog {
  billing_source: BillingSource
  models: CatalogModel[]
}

export const FALLBACK_MODELS: CatalogModel[] = staticModels.map(
  ({ id, label, reasoningEfforts }) => ({
    id,
    label,
    reasoning_efforts: reasoningEfforts,
    default_reasoning_effort: null,
    source: "curated",
    available: true,
    unavailable_reason: null,
  }),
)

function normalizeModel(model: Partial<CatalogModel> & { id: string }) {
  return {
    id: model.id,
    label: model.label || model.id,
    reasoning_efforts: model.reasoning_efforts ?? [],
    default_reasoning_effort: model.default_reasoning_effort ?? null,
    source: model.source ?? "curated",
    available: model.available ?? true,
    unavailable_reason: model.unavailable_reason ?? null,
  } satisfies CatalogModel
}

async function readCatalog(
  response: Response,
  billingSource: BillingSource,
): Promise<ModelCatalog> {
  if (!response.ok) {
    throw new Error(`Failed to load models (${response.status})`)
  }
  const data = (await response.json()) as Partial<ModelCatalog> | null
  const models = Array.isArray(data?.models)
    ? data.models.filter((model) => model && typeof model.id === "string")
    : []
  return {
    billing_source: data?.billing_source ?? billingSource,
    models: models.map(normalizeModel),
  }
}

export async function fetchModels(
  billingSource: BillingSource,
  signal?: AbortSignal,
): Promise<ModelCatalog> {
  const params = new URLSearchParams({ billing_source: billingSource })
  const response = await fetch(`${API_BASE}/v1/models?${params}`, {
    signal,
    cache: "no-store",
    credentials: "include",
  })
  return readCatalog(response, billingSource)
}

// The key goes in the body, never in the URL.
export async function fetchModelsWithKey(
  openaiKey: string,
  signal?: AbortSignal,
): Promise<ModelCatalog> {
  const response = await fetch(`${API_BASE}/v1/models`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ openai_key: openaiKey }),
    signal,
    cache: "no-store",
    credentials: "include",
  })
  return readCatalog(response, "api_key")
}
