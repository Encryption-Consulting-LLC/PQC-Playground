import { Eraser, Info } from "lucide-react"

import { plural } from "@/lib/display"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBackdrop,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogPopup,
  DialogPortal,
  DialogTitle,
} from "@/components/ui/dialog"

/**
 * Confirmation for removing registry rows.
 *
 * Every word here says "entry" and never "VM" — this action destroys nothing,
 * and the whole risk of putting it next to force destroy is an admin reading
 * one as the other. Tone is informational rather than destructive for the same
 * reason: the server has already refused any row that still backs a VM, so
 * there is nothing here to warn about.
 */
export function PurgeConfirmDialog({
  open,
  vmNames,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  vmNames: string[]
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogPortal>
        <DialogBackdrop />
        <DialogPopup className="w-[min(560px,calc(100vw-2rem))]">
          <DialogTitle className="flex items-center gap-(--gap-inline)">
            <Eraser className="size-4 text-muted-foreground" />
            Remove {plural(vmNames.length, "registry entry", "registry entries")}?
          </DialogTitle>
          <DialogDescription>
            These bookkeeping entries describe machines that no longer exist. Removing
            them clears the entry and returns any address it still held to the pool.
          </DialogDescription>

          <DialogBody>
            <ul className="max-h-56 space-y-(--gap-inline) overflow-y-auto rounded-lg border p-(--pad-control)">
              {vmNames.map((name) => (
                <li key={name} className="font-mono text-xs">
                  {name}
                </li>
              ))}
            </ul>
            <div className="flex gap-(--gap-inline) rounded-lg bg-info/10 p-(--pad-control) text-xs text-info">
              <Info className="mt-0.5 size-3.5 shrink-0" />
              <span>No virtual machine is destroyed by this action.</span>
            </div>
          </DialogBody>

          <DialogFooter>
            <Button variant="outline" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button variant="outline" size="sm" disabled={busy} onClick={onConfirm}>
              <Eraser className="mr-1 size-3.5" />
              Remove {vmNames.length === 1 ? "entry" : "entries"}
            </Button>
          </DialogFooter>
        </DialogPopup>
      </DialogPortal>
    </Dialog>
  )
}
