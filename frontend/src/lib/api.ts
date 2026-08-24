/**
 * Typed client for the FastAPI backend (vmkit / configgen / isokit).
 *
 * Requests go to `/api/*`, which the Vite dev server proxies to the backend
 * (see vite.config.ts). In production, serve the built frontend behind the same
 * origin as the API, or set up an equivalent `/api` reverse-proxy.
 *
 * Token injection: if an active session exists in the auth store, the
 * `X-Session-Token` header is added to every request automatically.
 * When the backend rejects that token the store is cleared so the UI returns
 * to the login form (handles backend restart / expired sessions) — see
 * `clearSessionIfRejected` for why that is narrower than "any 401".
 */

import { API_BASE, URLS, type Capability } from "@/constants"
import type { ProjectDoc, ProjectPayload } from "@/lib/projectSerialize"
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

/**
 * Auto-logout, but only when the 401 is about *our* session.
 *
 * `get_current_user` is the single place the backend rejects a session token,
 * and it marks that 401 with `WWW-Authenticate: Session`. Everything else that
 * can 401 — a rejected sign-in, an SSO code exchange, or (until it was fixed)
 * a failed downstream ESXi login on the deploy preflight — is not about the
 * caller's session, and clearing on those logged operators out mid-deploy.
 */
function clearSessionIfRejected(res: Response) {
  if (res.status !== 401) return
  if (!res.headers.get("www-authenticate")?.startsWith("Session")) return
  if (useAuthStore.getState().token) useAuthStore.getState().clear()
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
  const summary =
    typeof structured.message === "string" ? structured.message : null
  const failedChecks = Array.isArray(structured.preflight?.checks)
    ? structured.preflight.checks
        .filter(
          (check) => check?.ok === false && typeof check.detail === "string",
        )
        .map((check) => check.detail as string)
    : []

  if (summary && failedChecks.length > 0) {
    return `${summary} ${failedChecks.join(" ")}`
  }
  return summary
}

async function request<T>(
  path: string,
  init?: RequestInit,
  asText = false,
): Promise<T> {
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
    clearSessionIfRejected(res)

    // FastAPI/Pydantic errors come back as JSON `{ detail: ... }`.
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

  return (asText ? res.text() : res.json()) as Promise<T>
}

// --- /auth -----------------------------------------------------------------

export interface LoginRequest {
  username: string
  password: string
}

/** Every token-minting route (login, SSO callback) returns this shape. */
export interface SessionResponse {
  token: string
  username: string
  role: string
  capabilities: Capability[]
  host: string | null
}

export const login = (req: LoginRequest) =>
  request<SessionResponse>(URLS.auth.login, {
    method: "POST",
    body: JSON.stringify(req),
  })

export const logout = () =>
  request<{ status: string }>(URLS.auth.logout, { method: "POST" })

export interface AuthConfig {
  oidcEnabled: boolean
}

/** Unauthenticated deploy config — whether the SSO button should show. */
export const getAuthConfig = () => request<AuthConfig>(URLS.auth.config)

export interface Me {
  username: string
  role: string
  auth: "local" | "oidc" | "guest"
  capabilities: Capability[]
}

/** The signed-in identity + capability allowlist (what `useCan` reads). */
export const getMe = () => request<Me>(URLS.auth.me)

/** Start the SSO code flow: redirect the browser to the returned IdP URL. */
export const oidcLoginUrl = () => request<{ url: string }>(URLS.auth.oidcLogin)

/** Finish the SSO code flow with the `?code&state` the IdP sent back. */
export const oidcCallback = (code: string, state: string) =>
  request<SessionResponse>(URLS.auth.oidcCallback, {
    method: "POST",
    body: JSON.stringify({ code, state }),
  })

// --- /health ---------------------------------------------------------------

export interface Health {
  status: string
  libraries: {
    configgen: string[]
    vmkit: string
    isokit: string
  }
}

export const getHealth = () => request<Health>(URLS.health)

// --- /settings -------------------------------------------------------------

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

export type PkiRole =
  | "domainController"
  | "rootCa"
  | "issuingCa"
  | "webServer"
  | "certsecure"
  | "cbom"
  | "codesign"

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

export interface GoldenImagePreflightCheck {
  key: "image" | "datastore" | "guestOs" | "capacity" | "vmNames"
  ok: boolean
  detail: string
}

export interface GoldenImagePreflight {
  ready: boolean
  checkedAt: number
  snapshotId: string
  base: string
  datastore: string
  esxiInstanceUuid: string | null
  baseMoid: string | null
  expectedGuestOs: string
  actualGuestOs: string | null
  cloneCount: number
  capacityBytes: number | null
  freeBytes: number | null
  baseVmdkBytes: number | null
  requiredBytes: number | null
  projectedUsagePct: number | null
  maxUsagePct: number
  checks: GoldenImagePreflightCheck[]
}

export const getSettings = () => request<OperatorSettings>(URLS.settings.root)

export const updateSettings = (update: OperatorSettingsUpdate) =>
  request<OperatorSettings>(URLS.settings.root, {
    method: "PUT",
    body: JSON.stringify(update),
  })

export const validateGoldenImage = (cloneCount = 1) =>
  request<GoldenImagePreflight>(URLS.settings.validateGoldenImage, {
    method: "POST",
    body: JSON.stringify({ cloneCount }),
  })

export interface InfrastructurePreflightCheck {
  key:
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

// --- /generate/hostname ----------------------------------------------------

export type Platform = "linux" | "windows"

export interface HostnameRequest {
  platform: Platform
  hostname: string
}

export const generateHostname = (req: HostnameRequest) =>
  request<string>(
    URLS.generate.hostname,
    { method: "POST", body: JSON.stringify(req) },
    true,
  )

// --- /generate/network -----------------------------------------------------

export interface NetworkRequest {
  platform: Platform
  dhcp?: boolean
  ip?: string | null
  prefix?: number | null
  gateway?: string | null
  dns1?: string | null
  dns2?: string | null
  dns_suffix?: string | null
}

export const generateNetwork = (req: NetworkRequest) =>
  request<string>(
    URLS.generate.network,
    { method: "POST", body: JSON.stringify(req) },
    true,
  )

// --- /generate/password ------------------------------------------------------

export interface PasswordRequest {
  platform: Platform
  username: string
  password: string
}

export const generatePassword = (req: PasswordRequest) =>
  request<string>(
    URLS.generate.password,
    { method: "POST", body: JSON.stringify(req) },
    true,
  )

// --- async jobs --------------------------------------------------------------

/** Every 202-and-stream route (clone, deploy, teardown, executor dispatch)
 * returns this shape; progress streams over the job WS. */
export interface JobAccepted {
  job_id: string
}

// --- /vm/clone -------------------------------------------------------------

export interface CloneRequest {
  name: string
  base: string
  datastore: string
  cpus: number
  mem_mb: number
  mac?: string | null
  iso_path?: string | null
  guest_os?: string | null
  max_usage_pct?: number
  skip_disk_check?: boolean
  power_on?: boolean
}

export const cloneVm = (req: CloneRequest) =>
  request<JobAccepted>(URLS.vm.clone, {
    method: "POST",
    body: JSON.stringify(req),
  })

/** Teardown (destroy + reclaim the guest IP) is async, same 202+job-stream
 * shape as clone/deploy. `name` is the real ESXi inventory name
 * (`MachineData.vmName`), not the display label. */
export const deleteVm = (name: string) =>
  request<JobAccepted>(URLS.vm.one(name), { method: "DELETE" })

// --- /console ----------------------------------------------------------------

/** A one-shot authorization to open a remote-desktop session.
 *
 * Note what is *not* here: the RDP username, the password, and the guest's
 * address. The backend keeps all three and hands them to guacd itself, so the
 * browser only ever holds an opaque ticket id. `credentialLabel` is display text
 * (e.g. `ENCON\Administrator`) so the panel can say who the session signs in
 * as without the client holding the credential. */
export interface ConsoleTicket {
  ticketId: string
  vmName: string
  credentialLabel: string
  expiresInSeconds: number
}

/** Authorize a session on `vmName`. Single-use and short-lived — a reconnect
 * mints a fresh one rather than reusing this, so authorization is re-checked.
 *
 * Meaningful failures (all `ApiError`): 404 the VM is unknown (or, for a guest,
 * outside their namespace), 409 no session is possible and `detail` says why
 * (still cloning / no address / no stored credential → redeploy), 503 guacd is
 * unreachable or every session slot is in use. */
export const mintConsoleTicket = (vmName: string) =>
  request<ConsoleTicket>(URLS.console.ticket(vmName), { method: "POST" })

// --- /deploy -----------------------------------------------------------------

/** One operator-authored firstboot script riding inline in a createVm op. */
export interface IsoFilePayload {
  name: string
  content: string
}

/** Mirrors the backend's `PlanOp` (app/routers/deploy.py) — one node in the deploy DAG. */
export interface PlanOpPayload {
  id: string
  kind: string
  target: string
  /** The DC / parent CA / issuing CA the op wires to. */
  secondary?: string
  params: Record<string, string>
  /** PACK-mode authored scripts (operator-only; validated server-side). */
  files?: IsoFilePayload[]
  dependsOn: string[]
}

export type TopologyRole =
  | "domainController"
  | "rootCa"
  | "issuingCa"
  | "webServer"
  | "client"
  | "standalone"

export interface TopologyPayload {
  version: 1
  nodes: Array<{
    id: string
    name: string
    role: TopologyRole
    state: "planned" | "realized"
    config: Record<string, string>
  }>
  edges: Array<{
    id: string
    kind: "domainMembership" | "caParent" | "caPublication"
    source: string
    target: string
    state: "planned" | "realized"
    ports: Array<
      | "caParent"
      | "caPublication"
      | "domainBoundary"
      | "webHost"
      | "probeCertificate"
    >
  }>
  dnsRecords: Array<{
    id: string
    kind: "A" | "PTR" | "CNAME"
    server: string
    subject: string
    zone: string
    name?: string
  }>
}

export interface TopologyDiagnosticPayload {
  code: string
  message: string
  nodeIds: string[]
  edgeIds: string[]
}

export interface CompiledDeployPlan {
  topologyVersion: number
  operations: PlanOpPayload[]
  criticalPath: string[]
  estimatedDurationSeconds: number
  criticalPathDurationSeconds: number
  groups: CompiledExecutionGroup[]
  resources: {
    nodes: number
    relationships: number
    dnsRecords: TopologyPayload["dnsRecords"]
  }
}

export interface CompiledExecutionStep {
  id: string
  label: string
  command?: string
  kind: "backend" | "clone" | "agent" | "relay" | "verify" | "wait"
  targetNodeId: string
  dependsOn: string[]
}

export interface CompiledExecutionGroup {
  id: string
  kind: string
  label: string
  target: string
  secondary?: string | null
  dependsOn: string[]
  sourceBase?: string | null
  steps: CompiledExecutionStep[]
}

export const compileDeployPlan = (
  ops: PlanOpPayload[],
  topology: TopologyPayload,
  projectId?: string | null,
  init?: RequestInit,
) =>
  request<CompiledDeployPlan>(URLS.deploy.compile, {
    method: "POST",
    body: JSON.stringify({
      ops,
      topology,
      ...(projectId ? { projectId } : {}),
    }),
    ...init,
  })

/**
 * The subset shared by both preflight report shapes (control-plane and
 * infrastructure) that the deploy flow surfaces as a receipt: the 202
 * carries the passed infrastructure preflight, a 409's `detail.preflight`
 * carries whichever one failed.
 */
export interface DeployPreflightReceipt {
  ready: boolean
  checkedAt?: number
  checks: Array<{
    key: string
    ok: boolean
    detail: string
    role?: string | null
    datastore?: string | null
  }>
}

export interface DeployAccepted extends JobAccepted {
  preflight: DeployPreflightReceipt | null
}

export const deployPlan = (
  ops: PlanOpPayload[],
  topology: TopologyPayload,
  projectId?: string | null,
) =>
  request<DeployAccepted>(URLS.deploy.root, {
    method: "POST",
    // The backend derives guest VM names as guest-<user>-<project>-<machine>,
    // so the active project rides along as the <project> segment (required for
    // guest clones; ignored for operators, who keep free-form names).
    body: JSON.stringify({
      ops,
      topology,
      ...(projectId ? { projectId } : {}),
    }),
  })

/**
 * A running plan's presentation facts, for a client that wasn't the one that
 * clicked Deploy (a reload, another tab). The WebSocket carries live op state;
 * these three are only on the run document, and reconstructing them client-side
 * is impossible: `createdAt` predates this page load, `preflight` was a one-time
 * response body, and the manifest can't be recompiled from a topology whose
 * resources are already realized.
 */
export interface DeployRun {
  jobId: string
  createdAt: number | null
  preflight: DeployPreflightReceipt | null
  groups: CompiledExecutionGroup[]
}

export const getDeployRun = (jobId: string) =>
  request<DeployRun>(URLS.deploy.run(jobId))

export interface DownloadedEvidence {
  blob: Blob
  digest: string | null
  filename: string
}

/** Fetch the access-controlled evidence archive without dropping auth headers. */
export async function downloadDeployEvidence(
  jobId: string,
): Promise<DownloadedEvidence> {
  const token = useAuthStore.getState().token
  const res = await fetch(`${API_BASE}${URLS.deploy.evidence(jobId)}`, {
    headers: token ? { "x-session-token": token } : {},
  })
  if (!res.ok) {
    clearSessionIfRejected(res)
    let message = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail)
        message =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail)
    } catch {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new ApiError(res.status, message)
  }
  const disposition = res.headers.get("content-disposition") ?? ""
  const filename =
    disposition.match(/filename="?([^";]+)"?/i)?.[1] ??
    `pki-evidence-${jobId}.zip`
  return {
    blob: await res.blob(),
    digest: res.headers.get("x-evidence-sha256"),
    filename,
  }
}

// --- /iso --------------------------------------------------------------------

export interface UploadedIso {
  isoId: string
  name: string
  size: number
}

/**
 * Upload a pre-built config ISO (UPLOAD-ISO mode). Deliberately bypasses
 * `request()`: multipart bodies must NOT carry a manual `content-type` header —
 * the browser sets it (with the boundary) from the FormData. Token injection
 * and the 401 auto-logout mirror `request()`.
 */
export async function uploadIso(file: File): Promise<UploadedIso> {
  const token = useAuthStore.getState().token
  const body = new FormData()
  body.append("file", file)

  const res = await fetch(`${API_BASE}${URLS.iso.upload}`, {
    method: "POST",
    headers: token ? { "x-session-token": token } : {},
    body,
  })

  if (!res.ok) {
    clearSessionIfRejected(res)
    let message = `${res.status} ${res.statusText}`
    try {
      const errBody = await res.json()
      if (errBody?.detail) {
        message =
          typeof errBody.detail === "string"
            ? errBody.detail
            : JSON.stringify(errBody.detail)
      }
    } catch {
      // non-JSON body — keep the status line.
    }
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<UploadedIso>
}

/** Best-effort — callers are expected to swallow 404s (already consumed/swept). */
export const deleteIso = (isoId: string) =>
  request<string>(URLS.iso.one(isoId), { method: "DELETE" }, true)

export interface TemplateScripts {
  scripts: IsoFilePayload[]
}

/** The template's fixed role scripts, as editable seed content for the PACK panel. */
export const getTemplateScripts = (templateId: string) =>
  request<TemplateScripts>(URLS.iso.templateScripts(templateId))

// --- /projects ---------------------------------------------------------------

export interface ProjectSummary {
  id: string
  name: string
  createdAt: number
  updatedAt: number
}

export const listProjects = () =>
  request<{ projects: ProjectSummary[]; count: number }>(URLS.projects.list)

export const getProject = (id: string) =>
  request<ProjectDoc>(URLS.projects.one(id))

/** `init` passthrough carries `{ keepalive: true }` on beforeunload flushes. */
export const createProject = (doc: ProjectPayload, init?: RequestInit) =>
  request<ProjectDoc>(URLS.projects.list, {
    method: "POST",
    body: JSON.stringify(doc),
    ...init,
  })

export const updateProject = (doc: ProjectPayload, init?: RequestInit) =>
  request<ProjectDoc>(URLS.projects.one(doc.id), {
    method: "PUT",
    body: JSON.stringify(doc),
    ...init,
  })

// 204 No Content — read as text so the empty body doesn't trip res.json().
export const deleteProject = (id: string) =>
  request<string>(URLS.projects.one(id), { method: "DELETE" }, true)

// --- /project-shares --------------------------------------------------------

export interface ProjectShareMetadata {
  projectId: string
  name: string
  isOwner: boolean
  isCollaborator: boolean
  updatedAt: number
}

/** Create a guest share or refresh the snapshot behind an existing link. */
export const publishProjectShare = (doc: ProjectPayload) =>
  request<ProjectShareMetadata>(URLS.projectShares.one(doc.id), {
    method: "PUT",
    body: JSON.stringify(doc),
  })

/** Metadata-only lookup used before showing the collaboration prompt. */
export const inspectProjectShare = (id: string) =>
  request<ProjectShareMetadata>(URLS.projectShares.one(id))

/** Accept the collaboration invitation and receive its current snapshot. */
export const acceptProjectShare = (id: string) =>
  request<ProjectDoc>(URLS.projectShares.accept(id), { method: "POST" })

// --- /executor -------------------------------------------------------------

export interface RegisterAgentResponse {
  vm_id: string
  token: string
}

/** Mints a vm_id/token pair for a not-yet-connected executor agent. */
export const registerAgent = () =>
  request<RegisterAgentResponse>(URLS.executor.register, { method: "POST" })

/** Dispatch is async, same shape as clone/deploy: progress streams over the job WS. */
export const dispatchExecutorCommand = (
  vmId: string,
  command: string,
  params: Record<string, string> = {},
) =>
  request<JobAccepted>(URLS.executor.command(vmId), {
    method: "POST",
    body: JSON.stringify({ command, params }),
  })
