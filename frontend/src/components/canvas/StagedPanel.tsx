import { useEffect, useRef, useState } from "react"
import { Popover } from "@base-ui/react/popover"
import { toast } from "sonner"
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  ChevronRight,
  Circle,
  Cog,
  Globe,
  Loader2,
  LogOut,
  MinusCircle,
  Server,
  ShieldCheck,
  Undo2,
  X,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"

import {
  OP_KIND,
  OP_STATUS,
  actionableOps,
  opDestinationNodeId,
  transitiveDependents,
} from "@/lib/staging"
import type { StagedOp } from "@/lib/staging"
import {
  compileDeployPlan,
  type CompiledDeployPlan,
  type CompiledExecutionGroup,
} from "@/lib/api"
import {
  prepareDeployPlan,
  useStagingStore,
  type PlanPhase,
  type PreparedDeployPlan,
} from "@/store/staging"
import { useTopologyStore } from "@/store/topology"
import { Button } from "@/components/ui/button"
import { PreflightReceipt } from "./PreflightReceipt"
import { StagedRemoveDialog } from "./StagedRemoveDialog"
import { DeployCompilerDialog } from "./DeployCompilerDialog"

const KIND_ICON: Record<string, LucideIcon> = {
  [OP_KIND.createVm]: Server,
  [OP_KIND.provision]: Cog,
  [OP_KIND.domainJoin]: Building2,
  [OP_KIND.domainLeave]: LogOut,
  [OP_KIND.caConnect]: ShieldCheck,
  [OP_KIND.webServerCert]: Globe,
}

function StatusGlyph({ op }: { op: StagedOp }) {
  switch (op.status) {
    case OP_STATUS.staged:
    case OP_STATUS.pending:
      return <Circle className="h-3 w-3 shrink-0 text-muted-foreground/40" />
    case OP_STATUS.running:
      return (
        <span className="flex shrink-0 items-center gap-1">
          <Loader2 className="h-3 w-3 animate-spin text-sky-500" />
          {op.progress != null && (
            <span className="text-[10px] text-muted-foreground">
              {Math.round(op.progress)}%
            </span>
          )}
        </span>
      )
    case OP_STATUS.done:
      return <CheckCircle2 className="h-3 w-3 shrink-0 text-emerald-500" />
    case OP_STATUS.error:
      return (
        <Popover.Root>
          <Popover.Trigger
            className="flex shrink-0 items-center"
            aria-label={op.detail ?? "Deploy failed"}
          >
            <AlertTriangle className="h-3 w-3 shrink-0 text-red-500" />
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Positioner side="left" sideOffset={6} className="z-50">
              <Popover.Popup className="w-[min(280px,calc(100vw-2rem))] rounded-lg border bg-popover p-3 text-xs text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
                <p className="font-medium text-red-500">Deploy failed</p>
                <p className="mt-1 text-muted-foreground">
                  {op.detail ?? "No further detail available."}
                </p>
                {op.trace && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-[10px] font-medium text-muted-foreground">
                      Technical details
                    </summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-muted/40 p-1.5 text-[9px] leading-snug text-muted-foreground">
                      {op.trace}
                    </pre>
                  </details>
                )}
              </Popover.Popup>
            </Popover.Positioner>
          </Popover.Portal>
        </Popover.Root>
      )
    case OP_STATUS.cancelled:
      return (
        <MinusCircle className="h-3 w-3 shrink-0 text-muted-foreground/30" />
      )
  }
}

//: Seconds of pre-execution wait before the status detail escalates from the
//: short form to the explain-the-wait form and starts counting elapsed time.
const PRE_EXECUTION_ESCALATE_SEC = 3

const PLAN_PHASES: PlanPhase[] = ["posting", "queued", "preparing", "executing"]

function deploymentProgressCopy(
  phase: PlanPhase,
  detail: string | null,
  elapsedSec: number,
  doneCount: number,
  opCount: number,
): { title: string; detail: string } {
  switch (phase) {
    case "posting":
      return elapsedSec < PRE_EXECUTION_ESCALATE_SEC
        ? {
            title: "Validating deployment",
            detail: "Running infrastructure preflight checks…",
          }
        : {
            title: "Checking ESXi host",
            detail: "Verifying images, capacity, and VM names…",
          }
    case "queued":
      return {
        title: "Deployment queued",
        detail: "Waiting for an available deployment worker…",
      }
    case "preparing":
      return {
        title: "Preparing deployment",
        detail: detail ?? "Setting up the deployment worker…",
      }
    case "executing":
      return {
        title: "Deploying operations",
        detail: `${doneCount} of ${opCount} operation${opCount === 1 ? "" : "s"} complete`,
      }
  }
}

/**
 * Progress belongs beside the deployment controls rather than inside one of
 * them. The four-segment rail gives slow pre-execution phases visible motion,
 * while the copy has enough width to wrap without changing the button layout.
 */
function DeploymentProgress({
  phase,
  detail,
  elapsedSec,
  doneCount,
  opCount,
}: {
  phase: PlanPhase | null
  detail: string | null
  elapsedSec: number
  doneCount: number
  opCount: number
}) {
  const currentIndex = phase == null ? -1 : PLAN_PHASES.indexOf(phase)
  const copy = phase
    ? deploymentProgressCopy(phase, detail, elapsedSec, doneCount, opCount)
    : {
        title: "Deployment in progress",
        detail: "Reconnecting to deployment progress…",
      }

  return (
    <div className="border-t px-2 py-2">
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="rounded-md border border-sky-500/25 bg-sky-500/5 p-2.5"
      >
        <div className="flex min-w-0 items-center gap-2">
          <Loader2
            className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-500"
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 truncate text-[11px] font-medium">
            {copy.title}
          </span>
          {elapsedSec >= PRE_EXECUTION_ESCALATE_SEC && (
            <span
              aria-hidden="true"
              className="shrink-0 text-[10px] tabular-nums text-muted-foreground"
            >
              {elapsedSec}s
            </span>
          )}
        </div>
        <p className="mt-1 break-words text-[10px] leading-snug text-muted-foreground">
          {copy.detail}
        </p>
        <div
          className="mt-2 flex gap-1"
          role="progressbar"
          aria-label="Deployment progress"
          aria-valuemin={1}
          aria-valuemax={PLAN_PHASES.length}
          aria-valuenow={currentIndex >= 0 ? currentIndex + 1 : undefined}
          aria-valuetext={
            currentIndex >= 0 ? copy.title : "Current stage unavailable"
          }
        >
          {PLAN_PHASES.map((item, index) => (
            <span
              key={item}
              aria-hidden="true"
              className={`h-1 flex-1 rounded-full transition-colors ${
                index < currentIndex
                  ? "bg-sky-500"
                  : index === currentIndex
                    ? "animate-pulse bg-sky-500"
                    : "bg-muted"
              }`}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

/**
 * The "Staged" tab of the Toolbox — a linear view over the staging store's
 * op list, backed by the dependency DAG in `lib/staging.ts`. Undo pops the
 * last op (always safe); a row's ✕ cascades it plus everything that
 * transitively depends on it, confirming first when that set is non-empty.
 */
export function StagedPanel() {
  const ops = useStagingStore((s) => s.ops)
  const deploying = useStagingStore((s) => s.deploying)
  const planPhase = useStagingStore((s) => s.planPhase)
  const planPhaseDetail = useStagingStore((s) => s.planPhaseDetail)
  const deployStartedAt = useStagingStore((s) => s.deployStartedAt)
  const undo = useStagingStore((s) => s.undo)
  const removeOpCascade = useStagingStore((s) => s.removeOpCascade)
  const deploy = useStagingStore((s) => s.deploy)
  const cacheExecutionGroups = useStagingStore((s) => s.cacheExecutionGroups)
  const nodes = useTopologyStore((s) => s.nodes)

  const [pendingRemoval, setPendingRemoval] = useState<StagedOp[] | null>(null)
  const [review, setReview] = useState<CompiledDeployPlan | null>(null)
  const [prepared, setPrepared] = useState<PreparedDeployPlan | null>(null)
  const [compiling, setCompiling] = useState(false)
  const [preview, setPreview] = useState<CompiledDeployPlan | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [nowMs, setNowMs] = useState(() => Date.now())
  const previewGeneration = useRef(0)

  // The elapsed clock stays in the dedicated progress card for the full run,
  // including a job resumed after a reload — `resumePlanJob` refetches the run's
  // server-side start time, so the count picks up where the old tab left off
  // rather than restarting or vanishing.
  const ticking = deploying && deployStartedAt != null
  useEffect(() => {
    if (!ticking) return
    const timer = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [ticking])
  const elapsedSec = ticking
    ? Math.max(0, Math.floor((nowMs - (deployStartedAt ?? nowMs)) / 1000))
    : 0

  // The backend owns sequence expansion. Keep its tree warm while the user
  // stages a valid topology, but never surface expected incomplete-topology
  // failures as toasts during construction.
  useEffect(() => {
    if (deploying || ops.length === 0) {
      return
    }
    const generation = ++previewGeneration.current
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      setPreviewing(true)
      const next = prepareDeployPlan(ops)
      try {
        const compiled = await compileDeployPlan(
          next.payload,
          next.topology,
          next.projectId,
          { signal: controller.signal },
        )
        if (generation === previewGeneration.current) setPreview(compiled)
      } catch (error) {
        if (
          generation === previewGeneration.current &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          setPreview(null)
        }
      } finally {
        if (generation === previewGeneration.current) setPreviewing(false)
      }
    }, 400)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [deploying, ops, nodes])

  function nodeName(id: string) {
    return nodes.find((n) => n.id === id)?.data.name ?? "?"
  }

  function requestRemove(op: StagedOp) {
    const dependents = transitiveDependents(op.id, ops)
    if (dependents.length === 0) {
      removeOpCascade(op.id)
      return
    }
    setPendingRemoval([op, ...dependents])
  }

  function confirmRemove() {
    if (!pendingRemoval) return
    removeOpCascade(pendingRemoval[0].id)
    setPendingRemoval(null)
  }

  function handleUndo() {
    if (deploying || ops.length === 0) return
    undo()
  }

  async function handleDeploy() {
    if (deploying || compiling || ops.length === 0) return
    setCompiling(true)
    const next = prepareDeployPlan(ops)
    try {
      const compiled = await compileDeployPlan(
        next.payload,
        next.topology,
        next.projectId,
      )
      setPrepared(next)
      setReview(compiled)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : "Unable to compile deployment.",
      )
    } finally {
      setCompiling(false)
    }
  }

  function confirmDeploy() {
    if (review) cacheExecutionGroups(review.groups)
    setReview(null)
    setPrepared(null)
    deploy()
  }

  const hasErrors = ops.some((op) => op.status === OP_STATUS.error)
  const doneCount = ops.filter((op) => op.status === OP_STATUS.done).length
  // `done` rows survive a failed run for context only — Undo and Deploy act on
  // what is still outstanding, not on what already happened.
  const pendingCount = actionableOps(ops).length
  const groups = new Map(
    (preview?.groups ?? []).map((group) => [group.id, group]),
  )

  function subtitle(op: StagedOp, group?: CompiledExecutionGroup) {
    // The destination is whichever node the title names, which for a
    // webServerCert is the `secondary` web host, not the issuing CA it targets.
    const destinationId = opDestinationNodeId(op)
    const destination = nodeName(destinationId)
    const source =
      group?.sourceBase ??
      (destinationId === op.targetNodeId ? null : nodeName(op.targetNodeId))
    return source ? `${source} → ${destination}` : destination
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto">
        {ops.length === 0 ? (
          <p className="px-2 py-6 text-center text-[11px] text-muted-foreground">
            Nothing staged yet. Configure a node or wire up a connection to
            queue an operation.
          </p>
        ) : (
          <ol className="flex flex-col">
            {ops.map((op, i) => {
              const Icon = KIND_ICON[op.kind]
              const group = groups.get(op.id) ?? op.executionGroup
              const autoExpanded =
                op.status === OP_STATUS.running || op.status === OP_STATUS.error
              const isExpanded = expanded[op.id] ?? autoExpanded
              return (
                <li key={op.id} className="border-b text-xs last:border-b-0">
                  <div className="flex items-start gap-2 px-2 py-2">
                    <button
                      type="button"
                      className="mt-0.5 shrink-0 rounded-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-expanded={isExpanded}
                      aria-label={`${isExpanded ? "Collapse" : "Expand"} ${group?.label ?? op.label}`}
                      onClick={() =>
                        setExpanded((current) => ({
                          ...current,
                          [op.id]: !isExpanded,
                        }))
                      }
                    >
                      <ChevronRight
                        className={`h-3 w-3 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                      />
                    </button>
                    {/* Number every row the panel renders, including the
                        read-only synthesized provision rows. Skipping those
                        while they still consumed a list position printed
                        `1 · 3 · 5 · 7 · 9 10 11 12` — a sequence with holes in
                        it, which reads as steps that went missing rather than
                        as a staged/synthesized distinction. The muted title and
                        the absent ✕ already carry that distinction. */}
                    <span className="w-4 shrink-0 text-right text-[10px] text-muted-foreground">
                      {i + 1}
                    </span>
                    <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1">
                      <span
                        className={`block font-medium leading-snug ${op.synthesized ? "text-muted-foreground" : ""}`}
                      >
                        {group?.label ?? op.label}
                      </span>
                      <span className="block break-words text-[10px] leading-snug text-muted-foreground">
                        {subtitle(op, group)}
                        {op.status === OP_STATUS.running && op.phase
                          ? ` — ${op.phase}`
                          : null}
                      </span>
                      {/* Failure/skip reasons render inline — the glyph popover
                          stays, but the explanation must not hide behind a click. */}
                      {op.status === OP_STATUS.error && op.detail && (
                        <span className="block break-words text-[10px] leading-snug text-red-500">
                          {op.detail}
                        </span>
                      )}
                      {op.status === OP_STATUS.cancelled && (
                        <span className="block break-words text-[10px] leading-snug text-muted-foreground/70">
                          {op.detail ??
                            "Skipped: a dependency failed or was cancelled."}
                        </span>
                      )}
                    </span>
                    <StatusGlyph op={op} />
                    {/* Synthesized rows are read-only — they live and die with
                        their parent createVm, so no direct remove control. A
                        `done` row is history retained after a failed run:
                        removing it would revert canvas state for a VM that
                        really exists. */}
                    {!deploying &&
                      !op.synthesized &&
                      op.status !== OP_STATUS.done && (
                        <button
                          onClick={() => requestRemove(op)}
                          className="mt-0.5 shrink-0 text-muted-foreground transition-colors hover:text-red-500"
                          aria-label={`Remove ${op.label}`}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      )}
                  </div>
                  {isExpanded && (
                    <ol
                      className="border-t bg-muted/20 py-1 pl-9 pr-2"
                      aria-label={`${group?.label ?? op.label} steps`}
                    >
                      {group?.steps.length ? (
                        group.steps.map((step) => {
                          const runtime = op.executionSteps?.[step.id]
                          const status =
                            runtime?.status ??
                            (op.status === OP_STATUS.done
                              ? OP_STATUS.done
                              : OP_STATUS.pending)
                          return (
                            <li
                              key={step.id}
                              className="flex items-start gap-2 border-l py-1.5 pl-3 text-[10px]"
                            >
                              <span
                                className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${status === OP_STATUS.done ? "bg-emerald-500" : status === OP_STATUS.running ? "bg-sky-500" : status === OP_STATUS.error ? "bg-red-500" : "bg-muted-foreground/30"}`}
                              />
                              <span className="min-w-0 flex-1">
                                <span className="block break-words font-medium leading-snug">
                                  {step.label}
                                </span>
                                <span className="block break-words leading-snug text-muted-foreground">
                                  {nodeName(step.targetNodeId)} ·{" "}
                                  {step.command ?? step.kind}
                                  {runtime?.phase ? ` — ${runtime.phase}` : ""}
                                </span>
                              </span>
                              {runtime?.percent != null &&
                                status === OP_STATUS.running && (
                                  <span className="shrink-0 tabular-nums text-muted-foreground">
                                    {Math.round(runtime.percent)}%
                                  </span>
                                )}
                            </li>
                          )
                        })
                      ) : (
                        <li className="border-l py-2 pl-3 text-[10px] text-muted-foreground">
                          {previewing
                            ? "Expanding backend steps…"
                            : "Complete the required relationships to preview backend steps."}
                        </li>
                      )}
                    </ol>
                  )}
                </li>
              )
            })}
          </ol>
        )}
      </div>

      <PreflightReceipt />

      {deploying && (
        <DeploymentProgress
          phase={planPhase}
          detail={planPhaseDetail}
          elapsedSec={elapsedSec}
          doneCount={doneCount}
          opCount={ops.length}
        />
      )}

      <div className="mt-auto flex flex-col gap-1.5 border-t p-2">
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start gap-2"
          disabled={deploying || pendingCount === 0}
          title="Ctrl+Z"
          onClick={handleUndo}
        >
          <Undo2 className="h-3.5 w-3.5" />
          Undo
        </Button>
        <Button
          size="sm"
          className="w-full"
          disabled={pendingCount === 0 || deploying || compiling}
          onClick={handleDeploy}
        >
          {deploying || compiling ? (
            <>
              <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
              {compiling ? "Compiling review…" : "Deploying…"}
            </>
          ) : pendingCount === 0 ? (
            "Nothing staged"
          ) : hasErrors ? (
            "Retry deploy"
          ) : (
            `Deploy (${pendingCount})`
          )}
        </Button>
      </div>

      <StagedRemoveDialog
        ops={pendingRemoval}
        onConfirm={confirmRemove}
        onCancel={() => setPendingRemoval(null)}
      />
      <DeployCompilerDialog
        review={review}
        prepared={prepared}
        onConfirm={confirmDeploy}
        onCancel={() => {
          setReview(null)
          setPrepared(null)
        }}
      />
    </div>
  )
}
