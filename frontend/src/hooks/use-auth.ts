import { useEffect, useState } from "react"
import { type AuthUser, fetchMe } from "@/lib/auth"
import {
  type FrontendConfig,
  fetchFrontendConfig,
  normalizeFrontendConfig,
} from "@/lib/integration"

const FRONTEND_CONFIG_TTL_MS = 10000
// OSS-friendly default: if the backend config can't be fetched, don't gate usage on auth.
const DEFAULT_FRONTEND_CONFIG: FrontendConfig = normalizeFrontendConfig({
  auth_enabled: false,
  key_predefined: false,
})
let frontendConfigCache: { value: FrontendConfig; timestamp: number } | null =
  null
let frontendConfigInFlight: Promise<FrontendConfig> | null = null
let authUserCache: { value: AuthUser | null; timestamp: number } | null = null
let authUserInFlight: Promise<AuthUser | null> | null = null

async function getFrontendConfig(): Promise<FrontendConfig> {
  const now = Date.now()
  if (
    frontendConfigCache &&
    now - frontendConfigCache.timestamp < FRONTEND_CONFIG_TTL_MS
  ) {
    return frontendConfigCache.value
  }
  if (frontendConfigInFlight) {
    return frontendConfigInFlight
  }

  frontendConfigInFlight = fetchFrontendConfig()
    .then((config) => {
      frontendConfigCache = { value: config, timestamp: Date.now() }
      return config
    })
    .catch(() => {
      return frontendConfigCache
        ? frontendConfigCache.value
        : DEFAULT_FRONTEND_CONFIG
    })
    .finally(() => {
      frontendConfigInFlight = null
    })

  return frontendConfigInFlight
}

async function getAuthUser(): Promise<AuthUser | null> {
  const now = Date.now()
  if (authUserCache && now - authUserCache.timestamp < FRONTEND_CONFIG_TTL_MS) {
    return authUserCache.value
  }
  if (authUserInFlight) {
    return authUserInFlight
  }

  authUserInFlight = fetchMe()
    .then((user) => {
      authUserCache = { value: user, timestamp: Date.now() }
      return user
    })
    .catch(() => {
      return authUserCache ? authUserCache.value : null
    })
    .finally(() => {
      authUserInFlight = null
    })

  return authUserInFlight
}

export function useAuth() {
  const [user, setUser] = useState<AuthUser | null>(
    authUserCache ? authUserCache.value : null,
  )
  const [isLoading, setIsLoading] = useState(
    !(authUserCache || frontendConfigCache),
  )
  const [isConfigLoading, setIsConfigLoading] = useState(!frontendConfigCache)
  const [config, setConfig] = useState<FrontendConfig>(
    frontendConfigCache ? frontendConfigCache.value : DEFAULT_FRONTEND_CONFIG,
  )

  useEffect(() => {
    let isMounted = true

    const loadUser = async () => {
      try {
        const nextConfig = await getFrontendConfig()
        if (isMounted) {
          setConfig(nextConfig)
          setIsConfigLoading(false)
        }

        if (!nextConfig.auth_enabled) {
          if (isMounted) {
            authUserCache = null
            setUser(null)
            setIsLoading(false)
          }
          return
        }

        const result = await getAuthUser()
        if (isMounted) {
          setUser(result)
        }
      } catch {
        if (isMounted) {
          setUser(null)
          setIsConfigLoading(false)
        }
      } finally {
        if (isMounted) {
          setIsLoading(false)
        }
      }
    }

    void loadUser()

    return () => {
      isMounted = false
    }
  }, [])

  const isAuthEnabled = config.auth_enabled

  return {
    user,
    isLoading,
    isConfigLoading,
    isAuthEnabled,
    keyPredefined: config.key_predefined,
    isAuthorized: !isAuthEnabled || Boolean(user),
    authProvider: config.auth_provider,
    provider: user?.provider ?? null,
    planUsage: user?.plan_usage ?? "unavailable",
    planUsageAvailable: config.plan_usage_available,
    apiKeyModeAvailable: config.api_key_mode_available,
    modelDiscovery: config.model_discovery,
    manageUsageUrl: config.manage_usage_url,
    learnMoreUrl: config.plan_usage_learn_more_url,
  }
}
