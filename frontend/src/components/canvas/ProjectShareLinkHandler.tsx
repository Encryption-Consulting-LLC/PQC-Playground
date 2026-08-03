import { useEffect, useState } from "react"
import { AlertDialog } from "@base-ui/react/alert-dialog"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  acceptProjectShare,
  inspectProjectShare,
  type ProjectShareMetadata,
} from "@/lib/api"
import { deserializeProject } from "@/lib/projectSerialize"
import { useProjectsStore } from "@/store/projects"

function shareIdFromLocation(): string | null {
  return new URL(window.location.href).searchParams.get("share")
}

function clearShareFromLocation() {
  const url = new URL(window.location.href)
  url.searchParams.delete("share")
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`)
}

/** Resolves incoming guest links after login/project hydration. */
export function ProjectShareLinkHandler() {
  const openSharedProject = useProjectsStore((s) => s.openSharedProject)
  const [invitation, setInvitation] = useState<ProjectShareMetadata | null>(
    null,
  )
  const [accepting, setAccepting] = useState(false)

  useEffect(() => {
    const shareId = shareIdFromLocation()
    if (!shareId) return
    const requestedShareId = shareId
    let active = true

    async function inspect() {
      try {
        const metadata = await inspectProjectShare(requestedShareId)
        if (!active) return
        if (metadata.isOwner) {
          const doc = await acceptProjectShare(requestedShareId)
          if (!active) return
          openSharedProject(deserializeProject(doc))
          clearShareFromLocation()
          return
        }
        setInvitation(metadata)
      } catch (error) {
        if (!active) return
        clearShareFromLocation()
        toast.error(
          error instanceof Error
            ? error.message
            : "Could not open shared project.",
        )
      }
    }

    void inspect()
    return () => {
      active = false
    }
  }, [openSharedProject])

  function cancel() {
    clearShareFromLocation()
    setInvitation(null)
  }

  async function accept() {
    if (!invitation) return
    setAccepting(true)
    try {
      const doc = await acceptProjectShare(invitation.projectId)
      // Someone else's project: it stays backed by the share, so this account's
      // project sync leaves it alone rather than forking a private copy.
      openSharedProject(deserializeProject(doc), { collaborative: true })
      clearShareFromLocation()
      setInvitation(null)
      toast.success(`Joined PKI project ${invitation.projectId}.`)
    } catch (error) {
      toast.error(
        error instanceof Error
          ? error.message
          : "Could not join shared project.",
      )
    } finally {
      setAccepting(false)
    }
  }

  return (
    <AlertDialog.Root
      open={invitation !== null}
      onOpenChange={(next) => !next && cancel()}
    >
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[1px] data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <AlertDialog.Popup className="fixed left-1/2 top-1/2 z-50 w-[min(420px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-popover p-5 text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
          <AlertDialog.Title className="text-sm font-semibold">
            Join shared PKI project?
          </AlertDialog.Title>
          <AlertDialog.Description className="mt-2 text-xs text-muted-foreground">
            Do you want to collaborate on PKI project {invitation?.projectId}?
          </AlertDialog.Description>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={accepting}
              onClick={cancel}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              disabled={accepting}
              onClick={() => void accept()}
            >
              {accepting ? "Joining…" : "Continue"}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
