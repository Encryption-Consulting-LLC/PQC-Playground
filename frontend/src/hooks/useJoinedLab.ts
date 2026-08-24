/**
 * The active project's joined-lab identity, or null when it is the user's own.
 *
 * A joined lab is a pre-deployed environment somebody else built (see
 * `lib/labs.ts`), so the canvas is view + remote desktop only: no drops, no
 * connections, no staging, no deploy. This hook is what every one of those
 * locks reads, which is why the flag lives on the project rather than in a
 * separate store — switching tabs must switch the lock with it.
 *
 * Like `useCan`, this is COSMETIC. The backend already refuses to build in, or
 * tear down, a lab the caller merely joined; hiding the controls is so the
 * refusals never have to happen.
 */

import { useProjectsStore } from "@/store/projects"

export function useJoinedLab(): { code: string; owner: string | null } | null {
  return useProjectsStore(
    (s) =>
      s.projects.find((p) => p.id === s.activeProjectId)?.joinedLab ?? null,
  )
}

/** Convenience for the many places that only need "is this read-only". */
export function useIsJoinedLab(): boolean {
  return useJoinedLab() !== null
}
