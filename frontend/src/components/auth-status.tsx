"use client"

import {
  DashboardSpeed01Icon,
  Logout01Icon,
  UserSwitchIcon,
  WorkHistoryIcon,
} from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"
import Image from "next/image"
import Link from "next/link"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useAuth } from "@/hooks/use-auth"
import { LOGOUT_URL, SIGN_IN_URL, SWITCH_ACCOUNT_URL } from "@/lib/auth"
import { cn } from "@/lib/utils"
import openaiSmall from "../../public/openai-small.svg"

interface AuthStatusProps {
  className?: string
}

export function AuthStatus({ className }: AuthStatusProps) {
  const { user, isLoading, isAuthEnabled, authProvider, manageUsageUrl } =
    useAuth()

  if (isLoading || !isAuthEnabled) return null

  if (!user) {
    return (
      <div className={cn("flex items-center gap-2 text-xs", className)}>
        <span className="hidden font-serif text-base text-muted-foreground sm:inline">
          Authorization required
        </span>
        <Button asChild size="sm" variant="outline">
          {authProvider === "chatgpt" ? (
            <a href={SIGN_IN_URL}>
              <Image
                src={openaiSmall}
                alt=""
                aria-hidden
                data-icon="inline-start"
                className="size-4 dark:invert"
              />
              Continue with ChatGPT
            </a>
          ) : (
            <a href={SIGN_IN_URL}>Authorize</a>
          )}
        </Button>
      </div>
    )
  }

  const isChatGPTUser = user.provider === "chatgpt"

  const avatar = user.avatar_url ? (
    <Image
      src={user.avatar_url}
      alt={`${user.username} avatar`}
      width={24}
      height={24}
      className="size-6 rounded-full object-cover"
      referrerPolicy="no-referrer"
      unoptimized
    />
  ) : (
    <div className="flex size-6 items-center justify-center rounded-full bg-muted text-xs uppercase">
      {user.username.slice(0, 1)}
    </div>
  )

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex shrink-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-xs text-foreground hover:bg-muted/40",
            className,
          )}
        >
          {avatar}
          <span className="hidden max-w-32 truncate sm:inline">
            {user.username}
          </span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem asChild>
          <Link href="/history" className="flex items-center gap-2">
            <HugeiconsIcon icon={WorkHistoryIcon} strokeWidth={2} />
            History
          </Link>
        </DropdownMenuItem>
        {isChatGPTUser && (
          <>
            <DropdownMenuItem asChild>
              <a
                href={manageUsageUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2"
              >
                <HugeiconsIcon icon={DashboardSpeed01Icon} strokeWidth={2} />
                Manage usage
              </a>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <a href={SWITCH_ACCOUNT_URL} className="flex items-center gap-2">
                <HugeiconsIcon icon={UserSwitchIcon} strokeWidth={2} />
                Switch account
              </a>
            </DropdownMenuItem>
          </>
        )}
        <DropdownMenuItem asChild>
          <a href={LOGOUT_URL} className="flex items-center gap-2">
            <HugeiconsIcon icon={Logout01Icon} strokeWidth={2} />
            Log out
          </a>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
