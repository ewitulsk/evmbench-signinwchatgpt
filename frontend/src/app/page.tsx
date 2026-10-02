"use client"

import { Alert02Icon, ArrowUpRight01Icon } from "@hugeicons/core-free-icons"
import { HugeiconsIcon } from "@hugeicons/react"
import Image from "next/image"
import { useRouter } from "next/navigation"
import { useCallback, useMemo, useState } from "react"
import { AppFooter } from "@/components/app-footer"
import { AppHeader } from "@/components/app-header"
import { FileUploader } from "@/components/file-uploader"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useAuth } from "@/hooks/use-auth"
import { useLocalStorage } from "@/hooks/use-local-storage"
import { useModels } from "@/hooks/use-models"
import { useMounted } from "@/hooks/use-mounted"
import { useSessionStorage } from "@/hooks/use-session-storage"
import { SIGN_IN_URL } from "@/lib/auth"
import { type BillingSource, startJob } from "@/lib/jobs"
import { reasoningLabel } from "@/lib/models"
import { addRecentJob, type RecentJob } from "@/lib/recent-jobs"
import { inferPackageName } from "@/lib/upload-utils"
import { cn } from "@/lib/utils"
import { createZipFromFiles } from "@/lib/zip"
import { useUploadStore } from "@/store/upload-store"
import openaiSmall from "../../public/openai-small.svg"
import paradigmSmall from "../../public/paradigm-small.svg"

const billingOptions: { value: BillingSource; label: string }[] = [
  { value: "chatgpt_plan", label: "Use my ChatGPT plan" },
  { value: "api_key", label: "Use an API key" },
]

export default function Page() {
  const router = useRouter()
  const { files, packageName, setUpload, clearUpload } = useUploadStore()
  const [openaiKey, setOpenaiKey] = useSessionStorage("evmbench.openaiKey", "")
  const [storedModel, setStoredModel] = useLocalStorage("evmbench.model", "")
  const [chosenEffort, setChosenEffort] = useState<string | null>(null)
  const [billingChoice, setBillingChoice] = useState<BillingSource | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [recentJobs, setRecentJobs] = useLocalStorage<RecentJob[]>(
    "evmbench.recentJobs.v1",
    [],
  )
  const [planNoticeSeen, setPlanNoticeSeen] = useLocalStorage(
    "evmbench.planNoticeSeen",
    false,
  )
  const isMounted = useMounted()
  const {
    user,
    isAuthorized,
    isLoading: isAuthLoading,
    isConfigLoading,
    keyPredefined,
    authProvider,
    planUsage,
    planUsageAvailable,
    apiKeyModeAvailable,
    manageUsageUrl,
    learnMoreUrl,
  } = useAuth()

  const isChatGPTUser = user?.provider === "chatgpt"
  const showBillingChoice = planUsageAvailable && isChatGPTUser
  const isPlanConnected = planUsage === "connected"
  // The default only applies until the user picks; we never switch on errors.
  const defaultBillingSource: BillingSource =
    isPlanConnected || !apiKeyModeAvailable ? "chatgpt_plan" : "api_key"
  const billingSource: BillingSource = showBillingChoice
    ? apiKeyModeAvailable
      ? (billingChoice ?? defaultBillingSource)
      : "chatgpt_plan"
    : "api_key"
  const isPlanBilling = billingSource === "chatgpt_plan"

  const { models, isLoading: isModelsLoading } = useModels({
    billingSource,
    openaiKey: isPlanBilling ? "" : openaiKey,
    enabled: !isConfigLoading && !isAuthLoading,
  })
  const availableModels = models.filter(({ available }) => available)
  const selectedModel =
    availableModels.find(({ id }) => id === storedModel) ??
    availableModels[0] ??
    null
  const model = selectedModel?.id ?? ""
  const reasoningEfforts = selectedModel?.reasoning_efforts ?? []
  const defaultEffort =
    selectedModel?.default_reasoning_effort &&
    reasoningEfforts.includes(selectedModel.default_reasoning_effort)
      ? selectedModel.default_reasoning_effort
      : reasoningEfforts.includes("medium")
        ? "medium"
        : (reasoningEfforts[0] ?? "")
  const reasoningEffort =
    chosenEffort && reasoningEfforts.includes(chosenEffort)
      ? chosenEffort
      : defaultEffort
  const hasNoPlanModels =
    isPlanBilling && !isModelsLoading && availableModels.length === 0
  const isPlanBlocked = isPlanBilling && !isPlanConnected
  const showHighEffortWarning =
    isPlanBilling && (reasoningEffort === "xhigh" || reasoningEffort === "max")
  const isPlanNoticeOpen =
    isMounted && showBillingChoice && isPlanConnected && !planNoticeSeen

  const fileCount = files?.length ?? 0
  const selectedLabel = useMemo(() => {
    if (packageName) return packageName
    if (files) return inferPackageName(files)
    return null
  }, [files, packageName])

  const canSubmit =
    !!files &&
    fileCount > 0 &&
    !isSubmitting &&
    !isAuthLoading &&
    isAuthorized &&
    !!model &&
    !isPlanBlocked &&
    !hasNoPlanModels &&
    !(isPlanBilling && isModelsLoading)
  const signInLabel =
    authProvider === "chatgpt" ? "Continue with ChatGPT" : "Authorize"

  const handleFilesSelected = useCallback(
    (selected: File[]) => {
      setUpload(selected, inferPackageName(selected))
    },
    [setUpload],
  )

  const handleKeyChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      setOpenaiKey(event.target.value)
    },
    [setOpenaiKey],
  )

  const handleSubmit = async () => {
    if (!files || fileCount === 0) return
    if (!isAuthorized) {
      setSubmitError(
        authProvider === "chatgpt"
          ? "Continue with ChatGPT to start analysis."
          : "Authorize with GitHub to start analysis.",
      )
      return
    }
    if (!model) return
    const trimmedKey = openaiKey.trim()

    setIsSubmitting(true)
    setSubmitError(null)

    try {
      const name = selectedLabel ?? "files"
      const zipFile = await createZipFromFiles(files, name)
      const response = await startJob({
        file: zipFile,
        model,
        billingSource,
        openaiKey: isPlanBilling ? undefined : trimmedKey,
        reasoningEffort: reasoningEfforts.includes(reasoningEffort)
          ? reasoningEffort
          : undefined,
      })
      // Persist locally so users can navigate back without server-side auth/history.
      const next = addRecentJob({
        job_id: response.job_id,
        label: name,
        created_at_ms: Date.now(),
      })
      setRecentJobs(next)
      router.push(`/results?job_id=${response.job_id}`)
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "Upload failed")
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <main className="flex min-h-screen w-screen flex-col">
      <AppHeader showLogo={false} showBorder={false} />
      <section className="flex flex-1 items-center justify-center px-6 py-12">
        <div className="w-full max-w-4xl">
          <div className="mx-auto grid max-w-sm gap-10 lg:max-w-none lg:grid-cols-5 lg:items-center">
            <div className="space-y-6 lg:col-span-3">
              <div>
                <div className="-ms-2 mb-3 flex items-center gap-2">
                  <a
                    href="https://openai.com"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Image
                      src={openaiSmall}
                      alt="OpenAI"
                      className="size-12 dark:invert"
                    />
                  </a>
                  <div className="h-9 w-px bg-border" />
                  <a
                    href="https://paradigm.xyz"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Image
                      src={paradigmSmall}
                      alt="Paradigm"
                      className="size-12 dark:invert"
                    />
                  </a>
                </div>
                <h1 className="text-5xl leading-[1.1] font-serif text-foreground mb-1.5">
                  evmbench
                </h1>
                <h2 className="text-2xl leading-[1.1] font-serif text-foreground mb-3">
                  Evaluating AI performance on high-severity contract findings
                </h2>
                <div className="space-y-2 text-base text-foreground/80">
                  <p className="leading-tight">
                    evmbench is an open benchmark from OpenAI and Paradigm that
                    evaluates whether AI agents can detect, patch, and exploit
                    high-severity vulnerabilities.
                  </p>
                  <p className="leading-tight">
                    This interface focuses on detection and only reports
                    high-severity findings. Upload a contract folder,{" "}
                    {planUsageAvailable
                      ? "use your ChatGPT plan or an API key"
                      : "provide an API key"}
                    , and start a run.
                  </p>
                  <div className="flex flex-col items-start gap-0.5">
                    <a
                      href="https://www.paradigm.xyz/2026/02/evmbench"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-0.5 font-serif leading-tight underline-offset-4 hover:text-foreground hover:underline"
                    >
                      Read the blog post
                      <HugeiconsIcon
                        icon={ArrowUpRight01Icon}
                        strokeWidth={2}
                        className="size-3.5"
                      />
                    </a>
                    <a
                      href="https://github.com/paradigmxyz/evmbench"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-0.5 font-serif leading-tight underline-offset-4 hover:text-foreground hover:underline"
                    >
                      View the repo
                      <HugeiconsIcon
                        icon={ArrowUpRight01Icon}
                        strokeWidth={2}
                        className="size-3.5"
                      />
                    </a>
                  </div>
                </div>
              </div>
            </div>

            <div className="space-y-6 lg:col-span-2">
              <FileUploader
                onFilesSelected={handleFilesSelected}
                files={files}
                selectedLabel={selectedLabel}
                fileCount={fileCount}
                disabled={isSubmitting}
                onClear={clearUpload}
              />

              <div className="grid gap-3 text-xs text-muted-foreground">
                {showBillingChoice && (
                  <fieldset className="grid gap-1">
                    <legend className="mb-1 text-xs text-foreground">
                      Billing
                    </legend>
                    <div
                      className={cn(
                        "grid gap-0.5 rounded-md border p-0.5",
                        apiKeyModeAvailable ? "grid-cols-2" : "grid-cols-1",
                      )}
                    >
                      {billingOptions
                        .filter(
                          ({ value }) =>
                            value === "chatgpt_plan" || apiKeyModeAvailable,
                        )
                        .map(({ value, label }) => (
                          <label
                            key={value}
                            className="flex cursor-pointer items-center justify-center rounded px-2 py-1 text-xs text-muted-foreground hover:text-foreground has-checked:bg-muted has-checked:text-foreground has-focus-visible:ring-2 has-focus-visible:ring-ring/30"
                          >
                            <input
                              type="radio"
                              name="billing-source"
                              value={value}
                              checked={billingSource === value}
                              onChange={() => setBillingChoice(value)}
                              className="sr-only"
                            />
                            {label}
                          </label>
                        ))}
                    </div>
                    {planUsage === "declined" && (
                      <div className="grid gap-1.5 pt-1">
                        <p>
                          You didn&apos;t allow EVM Bench to use your ChatGPT
                          plan. Continue with ChatGPT again to grant access
                          {apiKeyModeAvailable ? ", or use an API key." : "."}
                        </p>
                        <div className="flex flex-wrap gap-2">
                          <Button asChild size="sm" variant="outline">
                            <a href={SIGN_IN_URL}>Continue with ChatGPT</a>
                          </Button>
                          {apiKeyModeAvailable && isPlanBilling && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => setBillingChoice("api_key")}
                            >
                              Use an API key
                            </Button>
                          )}
                        </div>
                      </div>
                    )}
                    {planUsage === "reauth_required" && (
                      <div className="grid gap-1.5 pt-1">
                        <p>
                          Your ChatGPT connection has expired. Reconnect to keep
                          using your plan.
                        </p>
                        <div>
                          <Button asChild size="sm" variant="outline">
                            <a href={SIGN_IN_URL}>Reconnect ChatGPT</a>
                          </Button>
                        </div>
                      </div>
                    )}
                    {planUsage === "unavailable" && isPlanBilling && (
                      <p className="pt-1">
                        ChatGPT plan usage isn&apos;t available for your account
                        right now.
                        {apiKeyModeAvailable && " Use an API key instead."}
                      </p>
                    )}
                  </fieldset>
                )}
                {!isConfigLoading && !keyPredefined && !isPlanBilling && (
                  <div className="grid gap-1">
                    <Label
                      htmlFor="openai-key"
                      className="text-xs text-foreground"
                    >
                      OpenAI API Key
                    </Label>
                    <Input
                      id="openai-key"
                      type="password"
                      placeholder="sk-&hellip;"
                      value={openaiKey}
                      onChange={handleKeyChange}
                    />
                  </div>
                )}
                <div className="grid gap-1">
                  <Label
                    htmlFor="model-select"
                    className="text-xs text-foreground"
                  >
                    Model
                  </Label>
                  <Select
                    value={model}
                    onValueChange={setStoredModel}
                    disabled={models.length === 0}
                  >
                    <SelectTrigger id="model-select" className="w-full">
                      <SelectValue
                        placeholder={
                          hasNoPlanModels
                            ? "No eligible models"
                            : "Select model"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {models.map(
                        ({
                          id,
                          label,
                          source,
                          available,
                          unavailable_reason,
                        }) => (
                          <SelectItem key={id} value={id} disabled={!available}>
                            {label}
                            {source === "discovered" && (
                              <Tooltip>
                                <TooltipTrigger
                                  render={<Badge variant="outline" />}
                                >
                                  New
                                </TooltipTrigger>
                                <TooltipContent>
                                  Not yet tested with EVM Bench
                                </TooltipContent>
                              </Tooltip>
                            )}
                            {!available && (
                              <span className="text-muted-foreground">
                                {unavailable_reason ?? "Unavailable"}
                              </span>
                            )}
                          </SelectItem>
                        ),
                      )}
                    </SelectContent>
                  </Select>
                  {hasNoPlanModels && (
                    <p className="text-destructive">
                      Your ChatGPT plan has no models eligible for EVM Bench, or
                      your account isn&apos;t eligible.
                      {apiKeyModeAvailable &&
                        " You can use an API key instead."}
                    </p>
                  )}
                </div>
                {reasoningEfforts.length > 0 && (
                  <div className="grid gap-1">
                    <Label
                      htmlFor="reasoning-select"
                      className="text-xs text-foreground"
                    >
                      Reasoning level
                    </Label>
                    <Select
                      value={reasoningEffort}
                      onValueChange={setChosenEffort}
                    >
                      <SelectTrigger id="reasoning-select" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {reasoningEfforts.map((effort) => (
                          <SelectItem key={effort} value={effort}>
                            {reasoningLabel(effort)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p>
                      Higher levels can take longer and use{" "}
                      {isPlanBilling
                        ? "more of your plan's usage"
                        : "more tokens"}
                      .
                    </p>
                    {showHighEffortWarning && (
                      <p className="flex items-start gap-1 text-amber-700 dark:text-amber-400">
                        <HugeiconsIcon
                          icon={Alert02Icon}
                          strokeWidth={2}
                          className="mt-px size-3.5 shrink-0"
                        />
                        Long audits at this level can use a large share of your
                        ChatGPT plan&apos;s usage limit.
                      </p>
                    )}
                  </div>
                )}
                {!isAuthLoading && !isAuthorized && (
                  <span className="text-base font-serif text-muted-foreground">
                    <a
                      href={SIGN_IN_URL}
                      className="text-foreground underline underline-offset-2 hover:text-primary"
                    >
                      {signInLabel}
                    </a>{" "}
                    to start analysis.
                  </span>
                )}
                <Button
                  onClick={handleSubmit}
                  disabled={!canSubmit}
                  className="w-full uppercase"
                >
                  {isSubmitting ? "Uploading…" : "Start analysis"}
                </Button>
                {submitError && (
                  <div className="text-xs text-destructive">{submitError}</div>
                )}

                {recentJobs.length > 0 && (
                  <div className="pt-1">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-xs text-muted-foreground">
                        Recent runs
                      </span>
                      <button
                        type="button"
                        onClick={() => setRecentJobs([])}
                        className="text-xs text-muted-foreground hover:text-foreground"
                      >
                        Clear
                      </button>
                    </div>
                    <div className="mt-2 space-y-1">
                      {recentJobs.slice(0, 6).map((job) => (
                        <button
                          key={job.job_id}
                          type="button"
                          onClick={() =>
                            router.push(`/results?job_id=${job.job_id}`)
                          }
                          className="flex w-full items-center justify-between gap-3 rounded-md px-2 py-1.5 text-left text-xs hover:bg-muted/40"
                          title={job.job_id}
                        >
                          <span className="min-w-0 flex-1 truncate text-foreground">
                            {job.label}
                          </span>
                          <span className="shrink-0 font-mono text-muted-foreground">
                            {job.job_id.slice(0, 8)}
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </section>
      <AppFooter showBorder={false} />
      <Dialog
        open={isPlanNoticeOpen}
        onOpenChange={(open) => {
          if (!open) setPlanNoticeSeen(true)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>You&apos;re using your ChatGPT plan</DialogTitle>
            <DialogDescription>
              Eligible AI requests in EVM Bench use your ChatGPT plan&apos;s
              usage limits.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="sm:justify-between">
            <div className="flex items-center gap-3">
              <a
                href={manageUsageUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 underline-offset-4 hover:underline"
              >
                Manage usage
                <HugeiconsIcon
                  icon={ArrowUpRight01Icon}
                  strokeWidth={2}
                  className="size-3"
                />
              </a>
              <a
                href={learnMoreUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
              >
                Learn more
                <HugeiconsIcon
                  icon={ArrowUpRight01Icon}
                  strokeWidth={2}
                  className="size-3"
                />
              </a>
            </div>
            <Button onClick={() => setPlanNoticeSeen(true)}>Got it</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}
