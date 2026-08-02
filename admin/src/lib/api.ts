/**
 * Typed client for the FastAPI backend — the admin-relevant subset only
 * (accounts, settings, IP pool, VM registry). Same request wrapper and
 * conventions as frontend/src/lib/api.ts: requests go to `/api/*` (proxied in
 * dev by vite.config.ts, served same-origin in production), the session
 * token is auto-injected, and a 401 auto-clears the session.
 */

import { API_BASE, URLS } from "@/constants"
import { useAuthStore } from "@/store/auth"

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

interface StructuredErrorDetail {
  message?: unknown
  preflight?: {
    checks?: Array<{ ok?: unknown; detail?: unknown }>
  }
}

/** Turn nested FastAPI preflight failures into a useful one-line UI error. */
export function formatApiErrorDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail
  if (!detail || typeof detail !== "object") return null

  const structured = detail as StructuredErrorDetail
  const summary = typeof structured.message === "string" ? structured.message : null
  const failedChecks = Array.isArray(structured.preflight?.checks)
    ? structured.preflight.checks
        .filter((check) => check?.ok === false && typeof check.detail === "string")
        .map((check) => check.detail as string)
    : []

  if (summary && failedChecks.length > 0) {
    return `${summary} ${failedChecks.join(" ")}`
  }
  return summary
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = useAuthStore.getState().token

  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "content-type": "application/json",
      ...(token ? { "x-session-token": token } : {}),
      ...init?.headers,
    },
    ...init,
  })

  if (!res.ok) {
    // Auto-logout: if the server rejects our token, clear it so the UI gates
    // back to the login form rather than showing the authenticated shell.
    if (res.status === 401 && useAuthStore.getState().token) {
      useAuthStore.getState().clear()
    }

    let message = `${res.status} ${res.statusText}`
    let detail: unknown
    try {
      const body = await res.json()
      if (body?.detail) {
        detail = body.detail
        message = formatApiErrorDetail(detail) ?? JSON.stringify(detail)
      }
    } catch {
      // non-JSON body — keep the status line.
    }
    throw new ApiError(res.status, message, detail)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

// --- /auth -------------------------------------------------------------

export interface LoginRequest {
  username: string
  password: string
}

/** Every token-minting route (login, SSO callback) returns this shape. */
export interface SessionResponse {
  token: string
  username: string
  role: string
  capabilities: string[]
  host: string | null
}

export const login = (req: LoginRequest) =>
  request<SessionResponse>(URLS.auth.login, {
    method: "POST",
    body: JSON.stringify(req),
  })

export const logout = () => request<{ status: string }>(URLS.auth.logout, { method: "POST" })

export interface AuthConfig {
  oidcEnabled: boolean
}

/** Unauthenticated deploy config — whether the SSO button should show. */
export const getAuthConfig = () => request<AuthConfig>(URLS.auth.config)

export interface Me {
  username: string
  role: string
  auth: "local" | "oidc" | "guest"
  capabilities: string[]
}

export const getMe = () => request<Me>(URLS.auth.me)

/** Start the SSO code flow: redirect the browser to the returned IdP URL. */
export const oidcLoginUrl = () => request<{ url: string }>(URLS.auth.oidcLogin)

/** Finish the SSO code flow with the `?code&state` the IdP sent back. */
export const oidcCallback = (code: string, state: string) =>
  request<SessionResponse>(URLS.auth.oidcCallback, {
    method: "POST",
    body: JSON.stringify({ code, state }),
  })

// --- /health -------------------------------------------------------------

export interface Health {
  status: string
  libraries: {
    configgen: string[]
    vmkit: string
    isokit: string
  }
}

export const getHealth = () => request<Health>(URLS.health)

// --- /admin/users --------------------------------------------------------

export interface AdminUser {
  username: string
  email: string | null
  role: "admin" | "operator" | "guest"
  auth: "local" | "oidc"
  disabled: boolean
  createdAt: number | null
  updatedAt: number | null
}

export const listUsers = () => request<{ users: AdminUser[]; count: number }>(URLS.adminUsers.list)

export interface UserCreateRequest {
  username: string
  password: string
  role: "admin" | "operator" | "guest"
  email?: string | null
}

export const createUser = (body: UserCreateRequest) =>
  request<AdminUser>(URLS.adminUsers.create, {
    method: "POST",
    body: JSON.stringify(body),
  })

export interface UserPatchRequest {
  disabled?: boolean
  password?: string
  role?: "admin" | "operator" | "guest"
}

export const patchUser = (username: string, body: UserPatchRequest) =>
  request<AdminUser>(URLS.adminUsers.patch(username), {
    method: "PATCH",
    body: JSON.stringify(body),
  })

// --- /settings -------------------------------------------------------------

export type PkiRole =
  | "domainController"
  | "rootCa"
  | "issuingCa"
  | "webServer"
  | "certsecure"
  | "cbom"
  | "codesign"

export interface ImageQualification {
  baseChangeVersion: string
  windowsBuild: number
  runnerVersion: string
  agentSha256: string
  validatedAt: number
  mlDsa87Available: boolean
  systemContextValidated: boolean
  timeSynchronized: boolean
  windowsUpdatesCurrent: boolean
  backendCallbackReachable: boolean
  agentCommands: string[]
  publicationManifestVersion: number
  ocspReferenceSha256: string | null
}

export interface InfrastructureProfile {
  role: PkiRole
  base: string
  datastore: string
  expectedGuestOs: string
  network: string
  cpus: number
  memoryMb: number
  systemDiskGb: number
  maxUsagePct: number
  qualification: ImageQualification | null
}

export interface OperatorSettings {
  id: string
  esxiHost: string | null
  esxiUser: string | null
  esxiPort: number
  hasPassword: boolean
  cloneBase: string
  cloneDatastore: string
  cloneGuestOs: string
  cloneNetwork: string
  cloneMaxUsagePct: number
  infrastructureProfiles: InfrastructureProfile[]
  guestIpStart: string | null
  guestIpEnd: string | null
  guestPrefix: number
  guestGateway: string | null
  guestDns1: string | null
  guestDns2: string | null
  guestDnsSuffix: string | null
}

export interface OperatorSettingsUpdate {
  esxiHost?: string
  esxiUser?: string
  esxiPassword?: string
  esxiPort?: number
  cloneBase?: string
  cloneDatastore?: string
  cloneGuestOs?: string
  cloneNetwork?: string
  cloneMaxUsagePct?: number
  infrastructureProfiles?: InfrastructureProfile[]
  guestIpStart?: string
  guestIpEnd?: string
  guestPrefix?: number
  guestGateway?: string
  guestDns1?: string
  guestDns2?: string
  guestDnsSuffix?: string
}

export const getSettings = () => request<OperatorSettings>(URLS.settings.root)

export const updateSettings = (update: OperatorSettingsUpdate) =>
  request<OperatorSettings>(URLS.settings.root, {
    method: "PUT",
    body: JSON.stringify(update),
  })

export interface InfrastructurePreflightCheck {
  key:
    | "connection"
    | "vmNames"
    | "image"
    | "guestOs"
    | "network"
    | "datastore"
    | "capacity"
    | "qualification"
  ok: boolean
  detail: string
  role: PkiRole | null
  datastore: string | null
}

export interface InfrastructurePreflight {
  ready: boolean
  checkedAt: number
  snapshotId: string
  esxiInstanceUuid: string | null
  machines: Array<
    InfrastructureProfile & {
      name: string
      baseMoid: string | null
      baseChangeVersion: string | null
      actualGuestOs: string | null
      reservedBytes: number | null
    }
  >
  datastores: Array<{
    datastore: string
    capacityBytes: number | null
    freeBytes: number | null
    reservedBytes: number | null
    projectedUsagePct: number | null
    maxUsagePct: number
  }>
  checks: InfrastructurePreflightCheck[]
}

export const validateInfrastructure = () =>
  request<InfrastructurePreflight>(URLS.settings.validateInfrastructure, {
    method: "POST",
    body: JSON.stringify({}),
  })

export interface EnvironmentPreflight {
  ready: boolean
  checkedAt: number
  agentSha256: string | null
  checks: Array<{ key: string; ok: boolean; detail: string }>
}

export const validateEnvironment = () =>
  request<EnvironmentPreflight>(URLS.settings.validateEnvironment, {
    method: "POST",
    body: JSON.stringify({}),
  })

// --- /ip-pool --------------------------------------------------------------

export interface IpPoolEntry {
  ip: string
  status: "free" | "allocated"
  vmName: string | null
  allocatedAt: number | null
}

export interface IpPoolState {
  entries: IpPoolEntry[]
  free: number
  allocated: number
}

export const getIpPool = () => request<IpPoolState>(URLS.ipPool)

// --- /vm-registry ------------------------------------------------------------

export interface VmRegistryEntry {
  vmName: string
  appName: string
  projectId: string | null
  nodeId: string | null
  moid: string | null
  status: "cloning" | "ready" | "error" | "deleted"
  powerState: string | null
  ip: string | null
  jobId: string | null
  createdAt: number | null
  updatedAt: number | null
}

export const listVmRegistry = () =>
  request<{ entries: VmRegistryEntry[]; count: number }>(URLS.vmRegistry)

// --- /admin/deployments ------------------------------------------------------

export interface ActiveDeployment {
  jobId: string
  owner: string | null
  ownerRole: string | null
  startedAt: number | null
  updatedAt: number | null
  opTotal: number
  opActive: number
  opDone: number
  opFailed: number
}

export const listDeployments = () =>
  request<{ deployments: ActiveDeployment[]; count: number }>(URLS.deployments.list)

/**
 * Cooperatively stop deployments. Omit `owner` to stop every user's active
 * deployments; pass one to stop just that user's. `mode` defaults to "step"
 * (the most immediate cooperative boundary) on the backend.
 */
export const stopDeployments = (body: { owner?: string; mode?: "step" | "operation" }) =>
  request<{ stopped: string[]; count: number }>(URLS.deployments.stop, {
    method: "POST",
    body: JSON.stringify(body),
  })

// --- /admin/teardown ---------------------------------------------------------

/** Why a registry entry looks abandoned. A row can fire several at once. */
export type OrphanSignal = "absentFromEsxi" | "agentDead" | "statusError" | "stuckCloning"

export interface EnvironmentVm {
  vmName: string
  appName: string | null
  status: "cloning" | "ready" | "error" | "deleted"
  ip: string | null
  /** `null` when ESXi could not be read — never assume absence from it. */
  presentInEsxi: boolean | null
  /** "none" means the VM never had an agent, which is normal for several templates. */
  agentState: "live" | "dead" | "none"
  lastConnectedAt: number | null
  signals: OrphanSignal[]
  /** Whether deleting the registry row alone is provably safe. */
  purgeable: boolean
  jobId: string | null
  createdAt: number | null
  updatedAt: number | null
}

export interface Environment {
  key: string
  owner: string | null
  ownerResolved: boolean
  ownerAmbiguous: boolean
  projectCode: string | null
  projectName: string | null
  vms: EnvironmentVm[]
  vmCount: number
  ipCount: number
  agentsLive: number
  agentsTotal: number
  flagged: number
  createdAt: number | null
  updatedAt: number | null
}

export interface EnvironmentList {
  environments: Environment[]
  count: number
  /** False when the host could not be read; absence signals are then omitted. */
  esxiReachable: boolean
}

export const listEnvironments = () => request<EnvironmentList>(URLS.teardown.environments)

export type OrphanEntry = EnvironmentVm & {
  owner: string | null
  ownerResolved: boolean
  projectCode: string | null
  environmentKey: string
}

export interface OrphanList {
  orphans: OrphanEntry[]
  count: number
  esxiReachable: boolean
}

export const listOrphans = () => request<OrphanList>(URLS.teardown.orphans)

export interface TeardownPreview {
  vms: Array<EnvironmentVm & { releasesIp: string | null }>
  vmNames: string[]
  toDestroy: number
  alreadyAbsent: number
  releasesIp: number
  /**
   * Digest of exactly what this preview showed. Handing it back with the
   * confirmation is what makes the destroy act on data the admin actually saw
   * — the server 409s if anything drifted in between.
   */
  confirmToken: string
}

/** Dry run: 503s rather than guessing when the ESXi host is unreachable. */
export const previewTeardown = (vmNames: string[]) =>
  request<TeardownPreview>(URLS.teardown.preview, {
    method: "POST",
    body: JSON.stringify({ vmNames }),
  })

export interface TeardownStarted {
  jobId: string
  vmNames: string[]
  count: number
}

export const startTeardown = (vmNames: string[], confirmToken: string) =>
  request<TeardownStarted>(URLS.teardown.start, {
    method: "POST",
    body: JSON.stringify({ vmNames, confirmToken }),
  })

/** Delete registry rows only — destroys no VM. 409s on anything still on the host. */
export const purgeOrphans = (vmNames: string[]) =>
  request<{ purged: string[]; count: number }>(URLS.teardown.purge, {
    method: "POST",
    body: JSON.stringify({ vmNames }),
  })
