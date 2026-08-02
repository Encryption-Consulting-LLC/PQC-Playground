import { useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Eraser, Loader2, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { QUERY_KEYS } from "@/constants"
import { listOrphans, purgeOrphans, startTeardown, type OrphanEntry } from "@/lib/api"
import { formatDate, plural, showError, statusVariant } from "@/lib/display"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useTeardownStore } from "@/store/teardown"
import { EsxiUnreachableBanner } from "./EsxiUnreachableBanner"
import { PurgeConfirmDialog } from "./PurgeConfirmDialog"
import { SIGNAL_META } from "./signals"
import { TeardownConfirmDialog } from "./TeardownConfirmDialog"

/**
 * Registry entries that look abandoned, with the reasons they were flagged.
 *
 * Checkboxes live here and nowhere else. Environments get per-group and per-row
 * buttons, because a multi-select on the most destructive action in the product
 * buys nothing and weakens the safety rail: "Force destroy acme-pki?" listing
 * four named VMs is legible, "Force destroy 3 selected items?" listing eleven
 * across three owners is not. Orphans invert that — low consequence per row,
 * high volume, recoverable — so bulk selection is right.
 *
 * Purging a row and destroying a VM are kept apart on purpose: purge is a
 * header bulk action, outline-toned, worded entirely as "entries"; destroy is a
 * per-row destructive button worded entirely as "VM". Each row can only take
 * the one that is actually correct for it, enforced server-side and mirrored
 * here by the disabled states.
 */
export function OrphansPanel() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error } = useQuery({
    queryKey: QUERY_KEYS.orphans,
    queryFn: listOrphans,
    refetchInterval: 10_000,
  })

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [purgeOpen, setPurgeOpen] = useState(false)
  const [destroyTarget, setDestroyTarget] = useState<string | null>(null)

  const job = useTeardownStore((s) => s.job)
  const startJob = useTeardownStore((s) => s.start)
  const busy = job !== null

  const orphans = useMemo(() => data?.orphans ?? [], [data])
  const purgeable = useMemo(
    () => orphans.filter((o) => o.purgeable).map((o) => o.vmName),
    [orphans],
  )
  const chosen = useMemo(
    () => purgeable.filter((name) => selected.has(name)),
    [purgeable, selected],
  )

  const purge = useMutation({
    mutationFn: (vmNames: string[]) => purgeOrphans(vmNames),
    onSuccess: (result) => {
      setSelected(new Set())
      setPurgeOpen(false)
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.orphans })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.environments })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.registry })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.ipPool })
      toast.success(`Removed ${plural(result.count, "registry entry", "registry entries")}.`)
    },
    onError: (err) => {
      showError(err)
      setPurgeOpen(false)
    },
  })

  const teardown = useMutation({
    mutationFn: ({ vmNames, confirmToken }: { vmNames: string[]; confirmToken: string }) =>
      startTeardown(vmNames, confirmToken),
    onSuccess: (result) => {
      startJob({
        jobId: result.jobId,
        startedAt: Date.now(),
        label: result.vmNames[0],
        vmNames: result.vmNames,
      })
      setDestroyTarget(null)
    },
    onError: (err) => {
      showError(err)
      setDestroyTarget(null)
    },
  })

  const toggle = (vmName: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(vmName)) next.delete(vmName)
      else next.add(vmName)
      return next
    })

  const allChosen = purgeable.length > 0 && chosen.length === purgeable.length

  return (
    <div className="space-y-(--gap-row)">
      <div className="flex flex-wrap items-start justify-between gap-(--gap-row)">
        <p className="max-w-prose text-xs text-muted-foreground">
          Entries whose VM is missing, whose agent has gone quiet, or whose clone never
          finished. Removing an entry is bookkeeping only — it never touches a machine.
        </p>
        <span
          title={
            chosen.length === 0
              ? "Select entries whose VM is provably gone."
              : undefined
          }
        >
          <Button
            variant="outline"
            size="sm"
            disabled={chosen.length === 0 || purge.isPending}
            onClick={() => setPurgeOpen(true)}
          >
            <Eraser className="mr-1 size-3.5" />
            Remove {chosen.length > 0 ? chosen.length : ""} registry{" "}
            {chosen.length === 1 ? "entry" : "entries"}
          </Button>
        </span>
      </div>

      {data && !data.esxiReachable && <EsxiUnreachableBanner />}

      {isLoading ? (
        <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading orphaned entries…
        </div>
      ) : isError ? (
        <p className="py-(--pad-section) text-xs text-destructive">
          {error instanceof Error ? error.message : "Could not load orphaned entries."}
        </p>
      ) : orphans.length === 0 ? (
        <p className="py-(--pad-section) text-xs text-muted-foreground">
          Nothing is orphaned — every registry entry matches a live VM.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8">
                <Checkbox
                  checked={allChosen}
                  indeterminate={chosen.length > 0 && !allChosen}
                  disabled={purgeable.length === 0}
                  onCheckedChange={(next) =>
                    setSelected(next ? new Set(purgeable) : new Set())
                  }
                />
              </TableHead>
              <TableHead>VM name</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>IP</TableHead>
              <TableHead>Signals</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {orphans.map((orphan) => (
              <OrphanRow
                key={orphan.vmName}
                orphan={orphan}
                checked={selected.has(orphan.vmName)}
                busy={busy}
                onToggle={() => toggle(orphan.vmName)}
                onDestroy={() => setDestroyTarget(orphan.vmName)}
              />
            ))}
          </TableBody>
        </Table>
      )}

      <PurgeConfirmDialog
        open={purgeOpen}
        vmNames={chosen}
        busy={purge.isPending}
        onCancel={() => setPurgeOpen(false)}
        onConfirm={() => purge.mutate(chosen)}
      />

      <TeardownConfirmDialog
        open={destroyTarget !== null}
        label={destroyTarget ?? ""}
        vmNames={destroyTarget ? [destroyTarget] : []}
        busy={teardown.isPending}
        onCancel={() => setDestroyTarget(null)}
        onConfirm={(confirmToken) =>
          destroyTarget && teardown.mutate({ vmNames: [destroyTarget], confirmToken })
        }
      />
    </div>
  )
}

function OrphanRow({
  orphan,
  checked,
  busy,
  onToggle,
  onDestroy,
}: {
  orphan: OrphanEntry
  checked: boolean
  busy: boolean
  onToggle: () => void
  onDestroy: () => void
}) {
  // Each row can only take the action that is actually correct for it: a row
  // still backed by a VM can be destroyed but not purged, and vice versa.
  const canDestroy = orphan.presentInEsxi === true
  return (
    <TableRow>
      <TableCell>
        <span
          title={
            orphan.purgeable
              ? undefined
              : "This entry still has a VM on the host — destroy the VM instead."
          }
        >
          <Checkbox
            checked={checked}
            disabled={!orphan.purgeable}
            onCheckedChange={onToggle}
          />
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs">{orphan.vmName}</TableCell>
      <TableCell className="text-xs">
        {orphan.owner ?? <span className="text-muted-foreground italic">unknown</span>}
      </TableCell>
      <TableCell>
        <Badge variant={statusVariant(orphan.status)}>{orphan.status}</Badge>
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {orphan.ip ?? "—"}
      </TableCell>
      <TableCell className="whitespace-normal">
        <div className="flex flex-wrap gap-(--gap-inline)">
          {orphan.signals.map((signal) => {
            const meta = SIGNAL_META[signal]
            if (!meta) return null
            const Icon = meta.icon
            return (
              <span key={signal} title={meta.help}>
                <Badge variant={meta.variant}>
                  <Icon className="mr-1" />
                  {meta.label}
                </Badge>
              </span>
            )
          })}
        </div>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {formatDate(orphan.updatedAt)}
      </TableCell>
      <TableCell className="text-right">
        <span
          title={
            canDestroy
              ? busy
                ? "Another teardown is already running."
                : undefined
              : "There is no VM on the host to destroy — remove the registry entry instead."
          }
        >
          <Button
            variant="destructive"
            size="xs"
            disabled={!canDestroy || busy}
            onClick={onDestroy}
          >
            <Trash2 className="mr-1" />
            Destroy VM
          </Button>
        </span>
      </TableCell>
    </TableRow>
  )
}
