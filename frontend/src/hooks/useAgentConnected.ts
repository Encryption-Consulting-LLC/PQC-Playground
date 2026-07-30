/**
 * True if the given executor vm_id currently has a connected agent.
 *
 * Reads the live presence store (`store/agents.ts`), which is pushed fresh
 * snapshots over `ws /api/executor/agents/watch` the moment any agent
 * connects or disconnects — the socket is attached for the whole authenticated
 * workspace (see `Workspace.tsx`), so this hook is a plain subscription with
 * no per-consumer polling.
 */

import { useAgentsStore } from "@/store/agents"

export function useAgentConnected(vmId: string | undefined): boolean {
  return useAgentsStore((s) => !!vmId && s.onlineVmIds.includes(vmId))
}
