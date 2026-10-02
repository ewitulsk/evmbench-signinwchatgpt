"use client"

import { ArrowUpRight01Icon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"
import Link from "next/link"
import { Button } from "@/components/ui/button"
import { useAuth } from "@/hooks/use-auth"
import { SIGN_IN_URL } from "@/lib/auth"
import { getJobErrorInfo, type JobErrorAction } from "@/lib/job-errors"
import { cn } from "@/lib/utils"

interface JobErrorNoticeProps {
  errorCode: string | null | undefined
  fallback?: string | null
  className?: string
}

export function JobErrorNotice({
  errorCode,
  fallback,
  className,
}: JobErrorNoticeProps) {
  const { manageUsageUrl } = useAuth()
  const info = getJobErrorInfo(errorCode)
  const message = info?.message ?? fallback

  if (!message) return null

  const renderAction = (action: JobErrorAction, isPrimary: boolean) => {
    const variant = isPrimary ? "default" : "outline"
    switch (action) {
      case "manage_usage":
        return (
          <Button key={action} asChild size="sm" variant={variant}>
            <a href={manageUsageUrl} target="_blank" rel="noopener noreferrer">
              Manage usage
              <HugeiconsIcon
                icon={ArrowUpRight01Icon}
                strokeWidth={2}
                data-icon="inline-end"
              />
            </a>
          </Button>
        )
      case "reconnect":
        return (
          <Button key={action} asChild size="sm" variant={variant}>
            <a href={SIGN_IN_URL}>Reconnect ChatGPT</a>
          </Button>
        )
      case "new_run":
        return (
          <Button key={action} asChild size="sm" variant={variant}>
            <Link href="/">Start a new run</Link>
          </Button>
        )
    }
  }

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-destructive",
        className,
      )}
    >
      <span className="min-w-0 flex-1">{message}</span>
      {info && info.actions.length > 0 && (
        <div className="flex shrink-0 items-center gap-2">
          {info.actions.map((action, index) =>
            renderAction(action, index === 0),
          )}
        </div>
      )}
    </div>
  )
}
