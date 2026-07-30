/**
 * Returns true when the signed-in user is an operator.
 *
 * The presentation-level counterpart to useCan: capabilities gate *actions*
 * the backend enforces, while this gates *infra internals* (executor
 * panel, planned-action stubs, library names) that guests should never see —
 * a split the backend has no capability for. Fail-closed: while /auth/me is
 * loading (or absent) this is false, so the clean guest surface renders and
 * operator internals never flash.
 *
 * Like useCan, this is COSMETIC — hiding UI, not enforcing anything.
 */

import { ROLES } from "@/constants"
import { useMe } from "@/hooks/useMe"

export function useIsOperator(): boolean {
  return useMe()?.role === ROLES.operator
}
