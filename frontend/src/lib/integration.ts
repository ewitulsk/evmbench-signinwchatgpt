import { API_BASE } from "@/lib/api"

export type AuthProvider = "github" | "chatgpt"
export type ModelDiscovery = "off" | "plan_only" | "all"

export const DEFAULT_MANAGE_USAGE_URL = "https://chatgpt.com/#settings"
export const DEFAULT_PLAN_USAGE_LEARN_MORE_URL =
  "https://help.openai.com/en/articles/20001410-sign-in-with-chatgpt"

export interface FrontendConfig {
  auth_enabled: boolean
  key_predefined: boolean
  auth_provider: AuthProvider | null
  plan_usage_available: boolean
  api_key_mode_available: boolean
  model_discovery: ModelDiscovery
  manage_usage_url: string
  plan_usage_learn_more_url: string
}

export function normalizeFrontendConfig(
  data: Partial<FrontendConfig> | null | undefined,
): FrontendConfig {
  return {
    auth_enabled: data?.auth_enabled ?? false,
    key_predefined: data?.key_predefined ?? false,
    auth_provider: data?.auth_provider ?? null,
    plan_usage_available: data?.plan_usage_available ?? false,
    api_key_mode_available: data?.api_key_mode_available ?? true,
    model_discovery: data?.model_discovery ?? "off",
    manage_usage_url: data?.manage_usage_url || DEFAULT_MANAGE_USAGE_URL,
    plan_usage_learn_more_url:
      data?.plan_usage_learn_more_url || DEFAULT_PLAN_USAGE_LEARN_MORE_URL,
  }
}

export async function fetchFrontendConfig(
  signal?: AbortSignal,
): Promise<FrontendConfig> {
  const response = await fetch(`${API_BASE}/v1/integration/frontend`, {
    signal,
    cache: "no-store",
    credentials: "omit",
  })

  if (!response.ok) {
    throw new Error(`Failed to load frontend config (${response.status})`)
  }

  return normalizeFrontendConfig(await response.json())
}
