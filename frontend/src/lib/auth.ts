import { API_BASE } from "@/lib/api"

export type AuthUserProvider = "github" | "chatgpt" | "local"
export type PlanUsage =
  | "connected"
  | "declined"
  | "reauth_required"
  | "unavailable"

export interface AuthUser {
  username: string
  avatar_url: string | null
  provider: AuthUserProvider
  plan_usage: PlanUsage
}

export const SIGN_IN_URL = `${API_BASE}/v1/auth/`
export const SWITCH_ACCOUNT_URL = `${API_BASE}/v1/auth/?switch_account=1`
export const LOGOUT_URL = `${API_BASE}/v1/auth/logout`

export async function fetchMe(signal?: AbortSignal): Promise<AuthUser | null> {
  const response = await fetch(`${API_BASE}/v1/auth/me`, {
    signal,
    cache: "no-store",
    credentials: "include",
  })

  if (response.status === 401) {
    return null
  }

  if (!response.ok) {
    throw new Error(`Failed to load session (${response.status})`)
  }

  const data = (await response.json()) as Partial<AuthUser> & {
    username: string
  }
  return {
    username: data.username,
    avatar_url: data.avatar_url ?? null,
    provider: data.provider ?? "github",
    plan_usage: data.plan_usage ?? "unavailable",
  }
}
