/**
 * The full-screen remote-desktop overlay.
 *
 * Shell copied from `DeployCompilerDialog` — the app's only large-canvas dialog —
 * and widened to the whole viewport, because a desktop scaled into a 1180px box
 * is unreadable at the resolutions these guests boot at.
 *
 * Mounted once in `Workspace`, driven entirely by `store/console`. Rendering is
 * keyed on `ticketId`, which is what makes reconnect correct: a ticket is
 * single-use, so a retry has to remount the viewer against a *new* ticket rather
 * than ask the old tunnel to redial.
 */

import { AlertDialog } from "@base-ui/react/alert-dialog"
import { Loader2, Monitor, RotateCw, X } from "lucide-react"
import { useCallback, useRef } from "react"

import { GuacDisplay } from "@/components/console/GuacDisplay"
import { Button } from "@/components/ui/button"
import { consoleTunnelData, consoleTunnelUrl } from "@/lib/ws"
import { useAuthStore } from "@/store/auth"
import { useConsoleStore } from "@/store/console"

export function RemoteDesktopOverlay() {
  const session = useConsoleStore((s) => s.session)
  const setStatus = useConsoleStore((s) => s.setStatus)
  const reconnect = useConsoleStore((s) => s.reconnect)
  const close = useConsoleStore((s) => s.close)
  const token = useAuthStore((s) => s.token)
  const actionsRef = useRef<{ sendCtrlAltDel: () => void } | null>(null)

  const onConnected = useCallback(() => setStatus("connected"), [setStatus])
  const onError = useCallback(
    (detail: string) => setStatus("error", detail),
    [setStatus],
  )
  const onDisconnected = useCallback(
    () =>
      setStatus(
        "error",
        "The session ended. This is normal after a sign-out inside the guest, or if the VM was torn down.",
      ),
    [setStatus],
  )

  const open = session !== null
  const connecting =
    session?.status === "authorizing" || session?.status === "connecting"

  return (
    <AlertDialog.Root open={open} onOpenChange={(next) => !next && close()}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="fixed inset-0 z-50 bg-black/70 backdrop-blur-[2px]" />
        <AlertDialog.Popup className="fixed left-1/2 top-1/2 z-50 flex h-[calc(100vh-2rem)] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border bg-popover text-popover-foreground shadow-2xl ring-1 ring-foreground/10">
          <header className="flex shrink-0 items-center justify-between gap-4 border-b px-4 py-2.5">
            <div className="flex min-w-0 items-center gap-2">
              <Monitor className="h-4 w-4 shrink-0 text-muted-foreground" />
              <AlertDialog.Title className="truncate text-sm font-semibold">
                {session?.label ?? "Remote desktop"}
              </AlertDialog.Title>
              <AlertDialog.Description className="truncate text-[11px] text-muted-foreground">
                {session?.credentialLabel
                  ? `signed in as ${session.credentialLabel}`
                  : "connecting"}
              </AlertDialog.Description>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5 text-[11px]"
                disabled={session?.status !== "connected"}
                onClick={() => actionsRef.current?.sendCtrlAltDel()}
                // The one combination a browser will never deliver to a page,
                // and the one a Windows lock screen needs.
                title="Send Ctrl+Alt+Delete to the guest"
              >
                Ctrl+Alt+Del
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5"
                onClick={close}
                aria-label="Close remote desktop"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
          </header>

          <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black">
            {/* Keyed on the ticket: a new ticket is a new session, and a spent
                one must never be redialled. */}
            {session?.ticketId && session.status !== "error" && (
              <GuacDisplay
                key={session.ticketId}
                tunnelUrl={consoleTunnelUrl(session.ticketId)}
                tunnelData={consoleTunnelData(token)}
                actionsRef={actionsRef}
                onConnected={onConnected}
                onError={onError}
                onDisconnected={onDisconnected}
              />
            )}

            {connecting && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
                {session?.status === "authorizing"
                  ? "Authorizing…"
                  : "Opening the desktop…"}
              </div>
            )}

            {session?.status === "error" && (
              <div className="flex max-w-md flex-col items-center gap-4 p-8 text-center">
                <p className="text-sm text-foreground">
                  {session.detail ?? "The remote desktop session ended."}
                </p>
                <div className="flex items-center gap-2">
                  {/* Reconnect, not retry: this mints a fresh ticket. */}
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-1.5"
                    onClick={() => void reconnect()}
                  >
                    <RotateCw className="h-3.5 w-3.5" />
                    Reconnect
                  </Button>
                  <Button variant="ghost" size="sm" onClick={close}>
                    Close
                  </Button>
                </div>
              </div>
            )}
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
