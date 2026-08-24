import { useEffect } from "react"
import { ReactFlowProvider } from "@xyflow/react"
import { Canvas } from "./Canvas"
import { Inspector } from "./Inspector"
import { ProjectLanding } from "./ProjectLanding"
import { ProjectTabBar } from "./ProjectTabBar"
import { RemoteDesktopOverlay } from "./RemoteDesktopOverlay"
import { Toolbox } from "./Toolbox"
import { useAgentPromotion } from "@/hooks/useAgentPromotion"
import { attachAgentsSocket } from "@/store/agents"
import { useAuthStore } from "@/store/auth"
import { useProjectsStore } from "@/store/projects"
import { useStagingStore } from "@/store/staging"

/**
 * Full-height authenticated workspace: Toolbox | (tab bar above Canvas | Inspector)
 * The toolbox is shared across all projects, so the project tab bar starts
 * after it rather than spanning the full width.
 * Rendered by App.tsx in the authenticated shell; auth gating is upstream.
 */
export function Workspace() {
  // Live executor-agent presence for the whole workspace — one socket
  // feeding every node's online dot and the Inspector's "Agent: Connected"
  // row. Keyed to the session token so a re-login reattaches with fresh auth.
  const token = useAuthStore((s) => s.token)
  useEffect(() => {
    if (!token) return
    return attachAgentsSocket(token)
  }, [token])

  // Promote nodes from `provisioning` to `deployed` as their agents phone home
  // — the presence-driven confirmation that reveals IPs and solidifies domain
  // circles.
  useAgentPromotion()

  // Ctrl/Cmd+Z pops the last staged op — mirrors the Staged panel's Undo
  // button. Ignored while typing (rename field, config form, ...) so it
  // doesn't fight normal text-input undo.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (
        e.key.toLowerCase() !== "z" ||
        !(e.ctrlKey || e.metaKey) ||
        e.shiftKey
      )
        return
      const target = e.target as HTMLElement | null
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return
      }
      const { ops, deploying, undo } = useStagingStore.getState()
      if (deploying || ops.length === 0) return
      e.preventDefault()
      undo()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [])

  // No active project (the last one was deleted) → the landing page replaces
  // the whole workspace; there's no project to show a toolbox/tabs/canvas for.
  const activeProjectId = useProjectsStore((s) => s.activeProjectId)
  if (!activeProjectId) return <ProjectLanding />

  return (
    <ReactFlowProvider>
      <div className="flex flex-1 overflow-hidden">
        <Toolbox />
        <div className="flex flex-1 flex-col overflow-hidden">
          <ProjectTabBar />
          <div className="flex flex-1 overflow-hidden">
            <Canvas key={activeProjectId} />
            <Inspector />
          </div>
        </div>
      </div>
      {/* Mounted once, driven by store/console — a full-screen session must not
          unmount when the Inspector's selection changes. */}
      <RemoteDesktopOverlay />
    </ReactFlowProvider>
  )
}
