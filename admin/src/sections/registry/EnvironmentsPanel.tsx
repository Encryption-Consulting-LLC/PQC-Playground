import { useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { ChevronDown, ChevronRight, Loader2, ServerCrash, Trash2 } from "lucide-react"

import { QUERY_KEYS } from "@/constants"
import { listEnvironments, startTeardown, type Environment, type EnvironmentVm } from "@/lib/api"
import { formatDate, showError, statusVariant } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useTeardownStore } from "@/store/teardown"
import { TeardownConfirmDialog } from "./TeardownConfirmDialog"
import { EsxiUnreachableBanner } from "./EsxiUnreachableBanner"

const UNGROUPED = "ungrouped"

function environmentLabel(env: Environment): string {
  if (env.key === UNGROUPED) return "Ungrouped"
  return env.projectName ?? env.projectCode ?? "unassigned"
}

interface Target {
  label: string
  vmNames: string[]
}

/**
 * The playground as the labs it was deployed as, expandable to the VMs in each.
 *
 * A table rather than cards: the admin scans *across* environments comparing VM
 * counts and agent health, and every other section here is a table.
 *
 * There is deliberately no header-level "destroy everything". The Deployments
 * section has a Stop all because stopping is soft and reversible; there is no
 * legitimate one-click nuke for real machines. The Ungrouped bucket likewise
 * gets per-VM destroy only — it is a catch-all of unrelated operator and legacy
 * VMs, not a lab, so "destroy the group" would mean nothing.
 */
export function EnvironmentsPanel() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.environments,
    queryFn: listEnvironments,
    refetchInterval: 10_000,
  })

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState<Target | null>(null)
  const job = useTeardownStore((s) => s.job)
  const startJob = useTeardownStore((s) => s.start)
  // One teardown at a time: concurrent destroys against the shared ESXi host
  // would interleave into one unreadable progress card.
  const busy = job !== null

  const teardown = useMutation({
    mutationFn: ({ vmNames, confirmToken }: { vmNames: string[]; confirmToken: string }) =>
      startTeardown(vmNames, confirmToken),
    onSuccess: (result, variables) => {
      startJob({
        jobId: result.jobId,
        startedAt: Date.now(),
        label: target?.label ?? variables.vmNames[0],
        vmNames: result.vmNames,
      })
      setTarget(null)
    },
    onError: (err) => {
      showError(err)
      setTarget(null)
    },
  })

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  return (
    <div className="space-y-(--gap-row)">
      <p className="text-xs text-muted-foreground">
        Every VM the registry knows about, grouped by the lab it was deployed as. VMs
        cloned directly (outside a deploy plan) are not tracked here at all. Teardown is
        force-only today: machines are destroyed and their addresses released without any
        in-guest cleanup.
      </p>

      {data && !data.esxiReachable && <EsxiUnreachableBanner />}

      {isLoading ? (
        <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading environments…
        </div>
      ) : isError ? (
        <p className="py-(--pad-section) text-xs text-destructive">
          {error instanceof Error ? error.message : "Could not load environments."}
        </p>
      ) : !data || data.environments.length === 0 ? (
        <p className="py-(--pad-section) text-xs text-muted-foreground">
          No environments are deployed right now.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead>Environment</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>VMs</TableHead>
              <TableHead>IPs</TableHead>
              <TableHead>Agents</TableHead>
              <TableHead>Flagged</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.environments.map((env) => {
              const open = expanded.has(env.key)
              return [
                <TableRow key={env.key}>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      onClick={() => toggle(env.key)}
                      title={open ? "Collapse" : "Expand"}
                    >
                      {open ? (
                        <ChevronDown className="size-3.5" />
                      ) : (
                        <ChevronRight className="size-3.5" />
                      )}
                    </Button>
                  </TableCell>
                  <TableCell className="font-medium">{environmentLabel(env)}</TableCell>
                  <TableCell>
                    {env.owner === null ? (
                      <span className="text-xs text-muted-foreground italic">unknown</span>
                    ) : (
                      <span
                        title={
                          env.ownerAmbiguous
                            ? "More than one account matches this name — showing the namespace rather than guessing."
                            : env.ownerResolved
                              ? undefined
                              : "Derived from the VM name; no matching account was found."
                        }
                      >
                        <Badge variant={env.ownerResolved ? "outline" : "warning"}>
                          {env.owner}
                        </Badge>
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {env.vmCount}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {env.ipCount}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {env.agentsTotal === 0
                      ? "—"
                      : `${env.agentsLive}/${env.agentsTotal} live`}
                  </TableCell>
                  <TableCell>
                    {env.flagged > 0 ? (
                      <Badge variant="warning">{env.flagged}</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDate(env.createdAt)}
                  </TableCell>
                  <TableCell className="text-right">
                    {env.key === UNGROUPED ? (
                      <span className="text-xs text-muted-foreground">
                        per-VM only
                      </span>
                    ) : (
                      <span title={busy ? "Another teardown is already running." : undefined}>
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            setTarget({
                              label: environmentLabel(env),
                              vmNames: env.vms.map((vm) => vm.vmName),
                            })
                          }
                        >
                          <Trash2 className="mr-1 size-3.5" />
                          Force destroy
                        </Button>
                      </span>
                    )}
                  </TableCell>
                </TableRow>,
                open ? (
                  // A plain list, not a nested <Table> — ui/table.tsx self-wraps
                  // in a bordered container, so nesting double-borders.
                  <TableRow key={`${env.key}-vms`}>
                    <TableCell colSpan={9} className="whitespace-normal bg-muted/30">
                      <ul className="space-y-(--gap-inline)">
                        {env.vms.map((vm) => (
                          <VmLine
                            key={vm.vmName}
                            vm={vm}
                            busy={busy}
                            onDestroy={() =>
                              setTarget({ label: vm.vmName, vmNames: [vm.vmName] })
                            }
                          />
                        ))}
                      </ul>
                    </TableCell>
                  </TableRow>
                ) : null,
              ]
            })}
          </TableBody>
        </Table>
      )}

      <TeardownConfirmDialog
        open={target !== null}
        label={target?.label ?? ""}
        vmNames={target?.vmNames ?? []}
        busy={teardown.isPending}
        onCancel={() => setTarget(null)}
        onConfirm={(confirmToken) =>
          target && teardown.mutate({ vmNames: target.vmNames, confirmToken })
        }
      />
    </div>
  )
}

function VmLine({
  vm,
  busy,
  onDestroy,
}: {
  vm: EnvironmentVm
  busy: boolean
  onDestroy: () => void
}) {
  return (
    <li className="flex flex-wrap items-center gap-(--gap-inline)">
      <span className="font-mono text-xs">{vm.vmName}</span>
      <span className="font-mono text-xs text-muted-foreground">{vm.ip ?? "—"}</span>
      <Badge variant={statusVariant(vm.status)}>{vm.status}</Badge>
      {vm.presentInEsxi === false && (
        <Badge variant="destructive">
          <ServerCrash className="mr-1" />
          absent on ESXi
        </Badge>
      )}
      {vm.agentState !== "none" && (
        <Badge variant={vm.agentState === "live" ? "success" : "warning"}>
          agent {vm.agentState}
        </Badge>
      )}
      <span
        className="ml-auto"
        title={busy ? "Another teardown is already running." : undefined}
      >
        <Button variant="destructive" size="xs" disabled={busy} onClick={onDestroy}>
          <Trash2 className="mr-1" />
          Destroy
        </Button>
      </span>
    </li>
  )
}
