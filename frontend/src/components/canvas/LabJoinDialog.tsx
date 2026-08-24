import { useState } from "react"
import { Dialog } from "@base-ui/react/dialog"
import { KeyRound, Loader2 } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { joinLabByCode } from "@/lib/labs"

/** Grouped as the code is issued (`ABCD-2345`), so the field reads like the
 * handout it was copied from. The server normalizes anyway — this is purely
 * legibility while typing, and it never rejects a keystroke, so a pasted code
 * in any form still lands. */
function grouped(raw: string): string {
  const bare = raw
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "")
    .slice(0, 8)
  return bare.length > 4 ? `${bare.slice(0, 4)}-${bare.slice(4)}` : bare
}

/**
 * Enter a join code and open the lab behind it.
 *
 * Two callers, one component: the landing page's "Join a Lab" card and the tab
 * bar's key button, because a lab can be joined both before there is any
 * project and alongside existing ones. `initialCode` is how a `?join=` link
 * prefills it (see `LabJoinLinkHandler`), which is why the link handler shows
 * this dialog rather than joining silently — a code in a URL should still be a
 * deliberate act, and the person clicking it should see what they are joining.
 *
 * `initialCode` seeds the field once, on mount. A caller whose code can change
 * between renders keys this component on it, which resets the field the React
 * way instead of syncing prop→state in an effect.
 */
export function LabJoinDialog({
  open,
  onOpenChange,
  initialCode = "",
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  initialCode?: string
}) {
  const [code, setCode] = useState(grouped(initialCode))
  const [joining, setJoining] = useState(false)

  async function submit() {
    if (!code.trim() || joining) return
    setJoining(true)
    try {
      const lab = await joinLabByCode(code)
      onOpenChange(false)
      toast.success(`Joined ${lab.name}.`)
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Could not join that lab.",
      )
    } finally {
      setJoining(false)
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[1px] data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <Dialog.Popup className="fixed left-1/2 top-1/2 z-50 w-[min(420px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-popover p-5 text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
          <div className="flex items-center gap-2">
            <KeyRound className="size-4 text-emerald-500" />
            <Dialog.Title className="text-sm font-semibold">
              Join a lab
            </Dialog.Title>
          </div>
          <Dialog.Description className="mt-2 text-xs text-muted-foreground">
            Enter the code you were given. It opens the lab that was prepared
            for you — the topology, its machines and their remote desktops.
          </Dialog.Description>

          <Input
            autoFocus
            value={code}
            onChange={(event) => setCode(grouped(event.target.value))}
            onKeyDown={(event) => {
              if (event.key === "Enter") void submit()
            }}
            placeholder="ABCD-2345"
            aria-label="Lab join code"
            className="mt-4 text-center font-mono tracking-[0.2em] uppercase"
          />

          <div className="mt-5 flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={joining}
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={joining || !code}
              onClick={() => void submit()}
            >
              {joining ? (
                <>
                  <Loader2 className="mr-2 size-3.5 animate-spin" />
                  Joining…
                </>
              ) : (
                "Join lab"
              )}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
