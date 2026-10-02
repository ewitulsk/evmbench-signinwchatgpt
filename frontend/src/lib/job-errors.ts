import type { JobErrorCode } from "@/lib/jobs"

export type JobErrorAction = "manage_usage" | "reconnect" | "new_run"

interface JobErrorInfo {
  message: string
  // The first action is the primary one.
  actions: JobErrorAction[]
}

const jobErrors: Record<JobErrorCode, JobErrorInfo> = {
  usage_limit_exceeded: {
    message:
      "You've reached your ChatGPT plan's usage limit. Check your usage or wait for it to reset, then start a new run.",
    actions: ["manage_usage"],
  },
  usage_unavailable: {
    message:
      "ChatGPT plan usage is temporarily unavailable. Please try again in a little while.",
    actions: ["manage_usage"],
  },
  user_not_eligible: {
    message:
      "Your ChatGPT account isn't eligible to use its plan in EVM Bench. You can run audits with an API key instead.",
    actions: ["new_run"],
  },
  unsupported_capability: {
    message:
      "This run needed a capability your ChatGPT plan doesn't support in EVM Bench. Try a different model or use an API key.",
    actions: ["new_run"],
  },
  invalid_user: {
    message:
      "We couldn't verify your ChatGPT account. Reconnect ChatGPT and start the run again.",
    actions: ["reconnect"],
  },
  reauth_required: {
    message:
      "Your ChatGPT connection has expired. Reconnect ChatGPT and start the run again.",
    actions: ["reconnect"],
  },
  plan_usage_unavailable: {
    message:
      "ChatGPT plan usage isn't enabled for your account. Continue with ChatGPT again to grant access, or use an API key.",
    actions: ["reconnect"],
  },
  model_unavailable: {
    message:
      "The selected model is no longer available on your ChatGPT plan. Start a new run with a different model.",
    actions: ["new_run"],
  },
}

export function getJobErrorInfo(
  code: string | null | undefined,
): JobErrorInfo | null {
  if (!code) return null
  return jobErrors[code as JobErrorCode] ?? null
}

export function jobErrorMessage(job: {
  error: string | null
  error_code?: string | null
}): string | null {
  return getJobErrorInfo(job.error_code)?.message ?? job.error
}
