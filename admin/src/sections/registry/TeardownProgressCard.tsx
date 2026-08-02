import { AlertTriangle, CheckCircle2, Loader2, X, XCircle } from "lucide-react"

import { plural } from "@/lib/display"
import type { OpRunState } from "@/lib/ws"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ProgressBar } from "@/components/ui/progress-bar"
import { useTeardownJob } from "@/hooks/useTeardownJob"

function statusIcon(status: OpRunState["status"]) {
  switch (status) {
    case "done":
      return <CheckCircle2 className="size-3.5 text-success" />
    case "error":
      return <XCircle className="size-3.5 text-destructive" />
    case "running":
      return <Loader2 className="size-3.5 animate-spin text-primary" />
    default:
      return <div className="size-3.5 rounded-full border border-muted-foreground/40" />
  }
}

/**
 * Live progress for the running teardown.
 *
 * Sits above the tabs rather than inside a panel, for two reasons: a teardown
 * started from Environments keeps running while the admin reads Orphans, and
 * rows for destroyed VMs disappear on the next refetch — inline row progress
 * would unmount itself mid-job.
 */
export function TeardownProgressCard() {
  const { job, state, dismiss } = useTeardownJob()
  if (!job) return null

  const entries = Object.entries(state.ops).sort(([a], [b]) => a.localeCompare(b))
  const done = entries.filter(([, op]) => op.status === "done").length
  const failed = entries.filter(([, op]) => op.status === "error").length

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-(--gap-row)">
          <div>
            <CardTitle className="flex items-center gap-(--gap-inline)">
              {state.phase === "done" ? (
                <CheckCircle2 className="size-4 text-success" />
              ) : state.phase === "error" ? (
                <XCircle className="size-4 text-destructive" />
              ) : (
                <Loader2 className="size-4 animate-spin text-primary" />
              )}
              Tearing down {job.label}
            </CardTitle>
            <CardDescription>
              {state.phase === "done"
                ? `Finished — ${plural(done, "VM")} handled${failed ? `, ${failed} failed` : ""}.`
                : state.phase === "error"
                  ? (state.error ?? "The teardown failed.")
                  : `Force destroy in progress — ${done} of ${job.vmNames.length} done.`}
            </CardDescription>
          </div>
          <div className="flex items-center gap-(--gap-inline)">
            {failed > 0 && <Badge variant="destructive">{failed} failed</Badge>}
            <Button variant="ghost" size="icon-sm" onClick={dismiss} title="Dismiss">
              <X className="size-3.5" />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-(--gap-row)">
        {state.warning && (
          <p className="rounded-lg bg-warning/10 px-(--pad-control) py-(--gap-inline) text-xs text-warning">
            {state.warning}
          </p>
        )}
        {entries.length === 0 ? (
          <p className="text-xs text-muted-foreground">Queued — waiting for a worker.</p>
        ) : (
          <ul className="space-y-(--gap-inline)">
            {entries.map(([vmName, op]) => (
              <li key={vmName} className="space-y-1">
                <div className="flex items-center gap-(--gap-inline)">
                  {statusIcon(op.status)}
                  <span className="font-mono text-xs">{vmName}</span>
                  <span className="text-xs text-muted-foreground">
                    {op.phase ?? op.status}
                  </span>
                </div>
                {op.status === "running" && <ProgressBar pct={op.percent ?? 0} />}
                {op.status === "error" && (
                  <div className="space-y-1 pl-(--gap-stack)">
                    <p className="text-xs text-destructive">{op.detail}</p>
                    {op.trace && (
                      <details className="text-xs text-muted-foreground">
                        <summary className="cursor-pointer">Technical detail</summary>
                        <pre className="mt-1 overflow-x-auto text-xs">{op.trace}</pre>
                      </details>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
        {failed > 0 && state.phase === "done" && (
          <p className="flex items-center gap-(--gap-inline) text-xs text-warning">
            <AlertTriangle className="size-3.5" />
            Failed VMs keep their address and registry entry — nothing was half-reclaimed.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
