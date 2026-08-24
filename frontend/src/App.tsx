import { useEffect, useRef } from "react"
import { ShieldAlert } from "lucide-react"
import { CAPABILITIES, ROLES } from "@/constants"
import { useAuthStore } from "@/store/auth"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { HealthBadge } from "@/components/HealthBadge"
import { LoginForm } from "@/components/LoginForm"
import { LogoutButton } from "@/components/LogoutButton"
import { Splash } from "@/components/Splash"
import { ThemeToggle } from "@/components/ThemeToggle"
import { Workspace } from "@/components/canvas/Workspace"
import { ProjectShareLinkHandler } from "@/components/canvas/ProjectShareLinkHandler"
import { LabJoinLinkHandler } from "@/components/canvas/LabJoinLinkHandler"
import { useApplyTheme } from "@/hooks/useTheme"
import { useBeforeUnloadWarning } from "@/hooks/useBeforeUnloadWarning"
import { useMe } from "@/hooks/useMe"
import { initProjectAutosave } from "@/lib/projectAutosave"
import { restoreJoinedLabs } from "@/lib/labs"
import {
  initLocalProjects,
  initServerProjects,
  releaseAccountProjects,
  retryInitServerProjects,
  useProjectSyncStore,
} from "@/lib/projectSync"

function App() {
  // Apply the resolved theme to <html> on every render. Must be called before
  // any early returns so theme applies to the splash / login screens too.
  useApplyTheme()

  useBeforeUnloadWarning()

  // Login is always required — no anonymous mode. Without a session token the
  // first (and only) screen is the login form; both operators and guests sign
  // in with an account (guests via username/password only).
  const token = useAuthStore((s) => s.token)
  const sessionReady = !!token

  // Once a session exists, re-open the previously active project (if any) and
  // start the autosave bridge. Runs once per session. With no saved projects
  // the workspace shows <ProjectLanding> instead of auto-opening a blank one.
  //
  // Operator roles carry the project:* capabilities → projects live on the
  // server (lib/projectSync.ts). Guests keep localStorage persistence.
  // Capabilities are per-user (GET /auth/me), so wait for that query before
  // choosing a persistence mode — `me` is undefined until it lands.
  const me = useMe()
  const canProjects =
    !!me?.capabilities.includes(CAPABILITIES.projectRead) &&
    !!me?.capabilities.includes(CAPABILITIES.projectWrite)
  const syncStatus = useProjectSyncStore((s) => s.status)
  const syncError = useProjectSyncStore((s) => s.loadError)
  const initializedSession = useRef<string | null>(null)
  useEffect(() => {
    if (!sessionReady) {
      // Sign-out: detach browser storage from the account and clear the store,
      // so the next person to sign in on this browser starts from nothing
      // rather than inheriting the previous session's tabs.
      if (initializedSession.current) releaseAccountProjects()
      initializedSession.current = null
      return
    }
    if (!me) return
    // The username is part of the key: switching accounts on one token-less
    // reload must re-init, because storage is scoped per account.
    const sessionMode = `${token}:${me.username}:${canProjects ? "server" : "local"}`
    if (initializedSession.current === sessionMode) return
    initializedSession.current = sessionMode
    initProjectAutosave()
    // Joined labs are restored after the account's own projects, and only
    // once: they are nobody's own project, so neither the server project list
    // nor browser storage carries them, and the membership record is all there
    // is to rebuild the tabs from.
    const init = canProjects
      ? initServerProjects(me.username)
      : initLocalProjects(me.username)
    void init.then(() => restoreJoinedLabs())
  }, [sessionReady, token, me, canProjects])

  if (!token) return <LoginForm />

  // Capabilities (GET /auth/me) decide the persistence mode below; rendering
  // the workspace before they land would flash the wrong mode for operators.
  if (!me) return <Splash label="Loading session…" />

  // Admin accounts manage the platform (accounts, ESXi target, base images)
  // from the separate /admin console — they never build on this canvas. The
  // real gate is ROLE_CAPABILITIES (admin has none of vm:*/deploy/project:*);
  // this is the cosmetic mirror, same convention as admin/App.tsx's denial
  // screen for non-admin roles.
  if (me.role === ROLES.admin) {
    return (
      <div className="flex h-svh flex-col items-center justify-center gap-3 px-4 text-center">
        <ShieldAlert className="size-8 text-warning" />
        <p className="text-sm font-medium">
          This workspace is for operators and guests only.
        </p>
        <p className="max-w-sm text-xs text-muted-foreground">
          Your account ({me.username}) is an admin account — use the{" "}
          <a href="/admin" className="underline">
            admin console
          </a>{" "}
          instead.
        </p>
        <LogoutButton />
      </div>
    )
  }

  // Server-persistence gate (operator only): the canvas can't render until the
  // project list is hydrated from the backend. No silent localStorage fallback
  // on error — serving stale local data while the server is the record invites
  // divergence.
  if (canProjects && syncStatus !== "ready") {
    if (syncStatus === "error") {
      return (
        <div className="flex h-svh flex-col items-center justify-center gap-3">
          <p className="text-sm text-muted-foreground">
            Couldn&apos;t load projects from the server
            {syncError ? `: ${syncError}` : "."}
          </p>
          <Button
            variant="outline"
            onClick={() => retryInitServerProjects(me.username)}
          >
            Retry
          </Button>
        </div>
      )
    }
    return <Splash label="Loading projects…" />
  }

  const isGuest = me.role === ROLES.guest

  return (
    <div className="flex h-svh flex-col overflow-hidden">
      {/* Top bar */}
      <header className="flex shrink-0 items-center justify-between gap-4 border-b px-4 py-2">
        <div>
          <img
            src="/ec-logo.png"
            alt="PQC Playground"
            className="ec-logo h-6"
          />
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <HealthBadge />
          {isGuest && <Badge variant="secondary">Guest</Badge>}
          <LogoutButton />
          <ThemeToggle />
        </div>
      </header>

      {/* Canvas workspace — takes the remaining viewport height */}
      {isGuest && <ProjectShareLinkHandler />}
      <LabJoinLinkHandler />
      <Workspace />
    </div>
  )
}

export default App
