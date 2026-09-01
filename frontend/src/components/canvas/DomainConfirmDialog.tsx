import { AlertDialog } from "@base-ui/react/alert-dialog"

import { Button } from "@/components/ui/button"
import type { DomainSyncChange } from "@/store/topology"

/**
 * Confirmation prompt shown after a node is dragged into / out of a domain
 * region, before the membership change is committed. Cancelling leaves the
 * topology untouched (the caller reverts the drag), so geometry and membership
 * never disagree.
 */
export function DomainConfirmDialog({
  changes,
  operations = [],
  onConfirm,
  onCancel,
}: {
  changes: DomainSyncChange[] | null
  operations?: string[]
  onConfirm: () => void
  onCancel: () => void
}) {
  const open = !!changes && changes.length > 0
  const { title, body, confirmLabel } = describe(changes ?? [])

  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(next) => {
        // Any dismissal (Esc, backdrop, Cancel) is treated as a decline.
        if (!next) onCancel()
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[1px] data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <AlertDialog.Popup className="fixed left-1/2 top-1/2 z-50 w-[min(420px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-popover p-5 text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
          <AlertDialog.Title className="text-sm font-semibold">
            {title}
          </AlertDialog.Title>
          <AlertDialog.Description className="mt-2 text-xs text-muted-foreground">
            {body}
          </AlertDialog.Description>
          {operations.length > 0 && (
            <div className="mt-3 rounded-lg border bg-muted/40 p-2.5">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Generated operations
              </p>
              <ol className="space-y-1 font-mono text-[10px] text-foreground">
                {operations.map((operation, index) => (
                  <li key={operation}>
                    {index + 1}. {operation}
                  </li>
                ))}
              </ol>
            </div>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button size="sm" onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}

function describe(changes: DomainSyncChange[]): {
  title: string
  body: string
  confirmLabel: string
} {
  const joins = changes.filter((c) => c.dcId)
  const leaves = changes.filter((c) => !c.dcId)

  // Single-node drag — the common case — gets a precise message.
  if (changes.length === 1) {
    const c = changes[0]
    // A product appliance is never domain-joined, so saying so would describe
    // something that does not happen — the same reason a joined lab's tab says
    // "Close lab" rather than "Delete project".
    if (c.kind === "integration") {
      if (c.dcId)
        return {
          title: "Integrate with domain?",
          body: `"${c.nodeName}" will register its service names in ${c.domainName} and every machine in that domain will trust its certificate. It is not domain-joined.`,
          confirmLabel: "Integrate",
        }
      return {
        title: "Remove domain integration?",
        body: `"${c.nodeName}" will stop registering DNS records in its domain. Records and trust already deployed are left in place.`,
        confirmLabel: "Remove integration",
      }
    }
    if (c.dcId)
      return {
        title: "Join domain?",
        body: `"${c.nodeName}" will be domain-joined to ${c.domainName}.`,
        confirmLabel: "Join domain",
      }
    return {
      title: "Leave domain?",
      body: `"${c.nodeName}" will be removed from its domain.`,
      confirmLabel: "Leave domain",
    }
  }

  // Multiple nodes (e.g. a domain controller was moved).
  const parts: string[] = []
  if (joins.length)
    parts.push(
      `${joins.length} ${joins.length === 1 ? "node joins" : "nodes join"}`,
    )
  if (leaves.length)
    parts.push(
      `${leaves.length} ${leaves.length === 1 ? "node leaves" : "nodes leave"}`,
    )

  return {
    title: "Update domain membership?",
    body: `${parts.join(" and ")} as a result of this move.`,
    confirmLabel: "Apply",
  }
}
