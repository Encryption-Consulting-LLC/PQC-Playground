import { useEffect, useState } from "react"

import { LabJoinDialog } from "./LabJoinDialog"

function codeFromLocation(): string | null {
  return new URL(window.location.href).searchParams.get("join")
}

function clearJoinFromLocation() {
  const url = new URL(window.location.href)
  url.searchParams.delete("join")
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`)
}

/**
 * Resolves `?join=CODE` links after sign-in.
 *
 * The code is prefilled into the join dialog rather than redeemed on arrival:
 * a link is a convenience for not typing eight characters, not a reason to skip
 * the confirmation, and the person clicking it should see which lab they are
 * about to open. The parameter is stripped immediately either way, so a reload
 * (or a link kept in history) doesn't re-prompt.
 */
export function LabJoinLinkHandler() {
  // Read once, during the first render, rather than synced out of an effect:
  // the parameter is already in the URL by the time this mounts, and there is
  // no second value to reconcile with.
  const [code, setCode] = useState<string | null>(codeFromLocation)

  useEffect(() => {
    if (code !== null) clearJoinFromLocation()
    // Strip the parameter once, on mount: the dialog owns the code from here,
    // and a reload (or a back button) must not re-prompt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <LabJoinDialog
      key={code ?? ""}
      open={code !== null}
      onOpenChange={(next) => !next && setCode(null)}
      initialCode={code ?? ""}
    />
  )
}
