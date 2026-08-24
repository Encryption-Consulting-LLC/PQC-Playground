/**
 * Joining a pre-deployed lab, and getting it back after a reload.
 *
 * A lab is somebody else's already-deployed project, handed over by an
 * admin-issued join code. It is therefore *not* one of this account's projects:
 * `lib/projectSync.ts` skips it (the id belongs to whoever deployed it) and
 * browser storage never holds it either. The membership record on the server is
 * the only place it lives, which is why `restoreJoinedLabs` exists — without it
 * a joined lab would vanish on refresh and the visitor would need their code
 * again.
 *
 * Both entry points (the code dialog and a `?join=` link) go through
 * `joinLabByCode` so a code typed by hand and a code clicked in a link cannot
 * diverge on what "joined" means.
 */

import { joinLab, listJoinedLabs, type JoinedLab } from "@/lib/api"
import { deserializeProject } from "@/lib/projectSerialize"
import { useProjectsStore } from "@/store/projects"

function open(lab: JoinedLab, opts?: { activate?: boolean }) {
  useProjectsStore
    .getState()
    .openJoinedLab(
      deserializeProject(lab.project),
      { code: lab.code, owner: lab.owner },
      opts,
    )
}

/** Redeem a code and open the lab. Throws the API error for the caller to show. */
export async function joinLabByCode(code: string): Promise<JoinedLab> {
  const lab = await joinLab(code)
  open(lab)
  return lab
}

/**
 * Reattach every joined lab as a tab after sign-in.
 *
 * Deliberately quiet: a failure here means the tabs are missing, not that
 * anything the user did went wrong, and the code can always be re-entered.
 * The active project is left alone unless there is none — restoring a lab must
 * not yank the user off the project they were last working in, but landing on
 * the "How do you wish to start?" screen while holding a joined lab would be
 * worse.
 */
export async function restoreJoinedLabs(): Promise<void> {
  let labs: JoinedLab[]
  try {
    labs = (await listJoinedLabs()).labs
  } catch {
    return
  }
  for (const lab of labs) open(lab, { activate: false })
  if (!useProjectsStore.getState().activeProjectId && labs.length > 0) {
    useProjectsStore.getState().switchProject(labs[0].project.id)
  }
}
