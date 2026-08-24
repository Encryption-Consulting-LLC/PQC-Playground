import { AlertDialog } from "@base-ui/react/alert-dialog"

import { Button } from "@/components/ui/button"

/**
 * Confirmation prompt shown before a project tab is deleted. Same AlertDialog
 * shell as `StagedRemoveDialog`. `projectName` is non-null while open.
 *
 * A joined lab gets different words for a genuinely different act: closing that
 * tab drops this browser's copy of somebody else's lab, which the code reopens.
 * Reusing "permanently removed … cannot be undone" there would describe a
 * consequence that does not happen.
 */
export function ProjectDeleteDialog({
  projectName,
  joinedLab = false,
  onConfirm,
  onCancel,
}: {
  projectName: string | null
  joinedLab?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const open = projectName !== null

  return (
    <AlertDialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel()
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[1px] data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <AlertDialog.Popup className="fixed left-1/2 top-1/2 z-50 w-[min(420px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-popover p-5 text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
          <AlertDialog.Title className="text-sm font-semibold">
            {joinedLab ? `Close “${projectName}”?` : `Delete “${projectName}”?`}
          </AlertDialog.Title>
          <AlertDialog.Description className="mt-2 text-xs text-muted-foreground">
            {joinedLab
              ? "This closes the lab here. The lab and its machines belong to whoever deployed them and are untouched — your joining code reopens it."
              : "The project and its saved topology are permanently removed. Any VMs already deployed to the host are left running — this only deletes the project. This cannot be undone."}
          </AlertDialog.Description>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onCancel}>
              Cancel
            </Button>
            <Button variant="destructive" size="sm" onClick={onConfirm}>
              {joinedLab ? "Close lab" : "Delete project"}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
