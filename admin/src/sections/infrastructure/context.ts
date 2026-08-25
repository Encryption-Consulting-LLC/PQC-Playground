import { createContext, useContext } from "react"

import type { ImageQualification, InfrastructureProfile } from "@/lib/api"

export interface FormState {
  esxiHost: string
  esxiUser: string
  esxiPassword: string
  esxiPort: string
  cloneBase: string
  cloneDatastore: string
  cloneGuestOs: string
  cloneNetwork: string
  cloneMaxUsagePct: string
  cloneAdminPassword: string
  guestIpStart: string
  guestIpEnd: string
  guestPrefix: string
  guestGateway: string
  guestDns1: string
  guestDns2: string
  guestDnsSuffix: string
  profiles: InfrastructureProfile[]
  hasPassword: boolean
  hasCloneAdminPassword: boolean
}

/**
 * What the four panes need from the layout that owns the form.
 *
 * The panes are route components, so they take no props — and this is the
 * whole surface between them and `InfrastructureSection`: the current values
 * and the four setters. Everything else the section holds (`loading`,
 * `saving`, `preflight`, `environment`, Save/Validate) stays above the
 * `Outlet` and out of here.
 *
 * The setters must come from the layout rather than be rebuilt per pane: each
 * one also clears the preflight and environment results, because an edit
 * invalidates them, and `addQualification` seeds itself from the last
 * environment probe's agent digest.
 */
export interface InfraFormContextValue {
  form: FormState
  patch: (field: keyof FormState, value: string) => void
  patchProfile: (
    role: InfrastructureProfile["role"],
    field: keyof Omit<InfrastructureProfile, "role">,
    value: string,
  ) => void
  patchQualification: (
    role: InfrastructureProfile["role"],
    field: keyof ImageQualification,
    value: string | number | boolean | string[] | null,
  ) => void
  addQualification: (role: InfrastructureProfile["role"]) => void
}

export const InfraFormContext = createContext<InfraFormContextValue | null>(null)

export function useInfraForm(): InfraFormContextValue {
  const value = useContext(InfraFormContext)
  if (!value) {
    throw new Error("useInfraForm must be used inside the /infrastructure layout route")
  }
  return value
}
