import { useEffect, useState } from "react"
import { useAuth } from "@/hooks/use-auth"
import type { BillingSource } from "@/lib/jobs"
import {
  type CatalogModel,
  FALLBACK_MODELS,
  fetchModels,
  fetchModelsWithKey,
} from "@/lib/models-api"

const KEY_DEBOUNCE_MS = 600

interface UseModelsOptions {
  billingSource: BillingSource
  openaiKey?: string
  enabled?: boolean
}

export function useModels({
  billingSource,
  openaiKey = "",
  enabled = true,
}: UseModelsOptions) {
  const { user, modelDiscovery } = useAuth()
  const [models, setModels] = useState<CatalogModel[]>(FALLBACK_MODELS)
  const [isLoading, setIsLoading] = useState(enabled)
  const [error, setError] = useState<string | null>(null)

  const trimmedKey = openaiKey.trim()
  // Only API-key users with discovery "all" get a key-specific catalog.
  const discoveryKey =
    billingSource === "api_key" && modelDiscovery === "all" ? trimmedKey : ""
  const signedInAs = user ? `${user.provider}:${user.username}` : null

  // biome-ignore lint/correctness/useExhaustiveDependencies: signedInAs refetches the list after sign-in or an account switch.
  useEffect(() => {
    if (!enabled) {
      setIsLoading(false)
      return
    }

    const controller = new AbortController()
    setIsLoading(true)

    const load = async () => {
      try {
        const catalog = discoveryKey
          ? await fetchModelsWithKey(discoveryKey, controller.signal)
          : await fetchModels(billingSource, controller.signal)
        if (controller.signal.aborted) return
        // An empty catalog for plan users is meaningful (not eligible);
        // for API keys fall back to the static list.
        setModels(
          catalog.models.length > 0 || billingSource === "chatgpt_plan"
            ? catalog.models
            : FALLBACK_MODELS,
        )
        setError(null)
      } catch (err) {
        if (controller.signal.aborted) return
        setModels(FALLBACK_MODELS)
        setError(err instanceof Error ? err.message : "Failed to load models")
      } finally {
        if (!controller.signal.aborted) {
          setIsLoading(false)
        }
      }
    }

    const timeout = window.setTimeout(
      () => void load(),
      discoveryKey ? KEY_DEBOUNCE_MS : 0,
    )

    return () => {
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [enabled, billingSource, discoveryKey, signedInAs])

  return { models, isLoading, error }
}
