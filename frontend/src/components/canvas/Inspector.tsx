import { Fragment, useState } from "react"
import {
  AlertTriangle,
  Check,
  Circle,
  Clock,
  Loader2,
  Network,
  Power,
  PowerOff,
  RefreshCw,
  Settings,
  ShieldCheck,
  Tag,
  Trash2,
  X,
} from "lucide-react"
import { toast } from "sonner"
import { TEMPLATE_BY_ID } from "@/constants/templates"
import type { ConfigField } from "@/constants/templates"
import { LIFECYCLE } from "@/constants/topology"
import {
  ISO_DRIFT_FIELD,
  caTier,
  caDepth,
  configurationNameCollision,
  domainMembership,
  driftedFields,
  isDeployed,
  isDrifted,
} from "@/lib/topology"
import {
  PASSWORD_MASK,
  isPasswordValid,
  passwordRules,
} from "@/lib/passwordPolicy"
import { projectNetbiosPrefix } from "@/lib/projectNaming"
import { OP_KIND, OP_STATUS } from "@/lib/staging"
import type { StagedOp } from "@/lib/staging"
import { useTopologyStore } from "@/store/topology"
import { opsReferencingNode, useStagingStore } from "@/store/staging"
import { useAuthStore } from "@/store/auth"
import { useProjectsStore } from "@/store/projects"
import { CAPABILITIES } from "@/constants/auth"
import { useCan } from "@/hooks/useCan"
import { useIsOperator } from "@/hooks/useIsOperator"
import { useAgentConnected } from "@/hooks/useAgentConnected"
import { dispatchExecutorCommand } from "@/lib/api"
import { openJobSocket } from "@/lib/ws"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"
import { IsoAuthoringPanel } from "./IsoAuthoringPanel"
import { StagedRemoveDialog } from "./StagedRemoveDialog"

function PlannedAction({
  icon: Icon,
  label,
  tip,
  disabled = true,
  onClick,
}: {
  icon: React.ElementType
  label: string
  tip: string
  disabled?: boolean
  onClick?: () => void
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      className="w-full justify-start gap-2 text-muted-foreground"
      disabled={disabled}
      title={tip}
      onClick={onClick}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </Button>
  )
}

function isHiddenIn(field: ConfigField, values: Record<string, string>) {
  const cond = field.hideWhen
  if (!cond) return false
  const current = values[cond.key]
  if (cond.equals !== undefined && current === cond.equals) return true
  if (cond.notEquals !== undefined && current !== cond.notEquals) return true
  return false
}

/** The currently-visible field values — the set that gets committed/persisted (hidden fields never leak). */
function visibleValues(fields: ConfigField[], values: Record<string, string>) {
  return Object.fromEntries(
    fields
      .filter((f) => !isHiddenIn(f, values))
      .map((f) => [f.key, values[f.key]]),
  )
}

function withFixedPrefixes(
  fields: ConfigField[],
  values: Record<string, string>,
  fixedPrefixes: Record<string, string>,
) {
  return Object.fromEntries(
    Object.entries(visibleValues(fields, values)).map(([key, value]) => [
      key,
      `${fixedPrefixes[key] ?? ""}${value}`,
    ]),
  )
}

/**
 * Inline config form rendered in the Inspector when a node is unconfigured
 * and its template defines configFields. Keyed by nodeId so state resets when
 * the selection changes — but it re-seeds from the node's persisted `config`
 * (via `initial`), and mirrors edits back through `onChange`, so switching
 * away and back (or reloading) preserves in-progress values instead of
 * resetting them to defaults.
 */
function ConfigForm({
  fields,
  vmName,
  initial,
  fixedPrefixes = {},
  onChange,
  onSubmit,
  disabled = false,
  validateField,
}: {
  fields: ConfigField[]
  vmName?: string
  initial?: Record<string, string>
  fixedPrefixes?: Record<string, string>
  onChange?: (values: Record<string, string>) => void
  onSubmit: (values: Record<string, string>) => void
  disabled?: boolean
  validateField?: (key: string, value: string) => string | null
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map((field) => {
        const prefix = fixedPrefixes[field.key] ?? ""
        const initialValue = initial?.[field.key] ?? field.default
        return [
          field.key,
          prefix && initialValue.startsWith(prefix)
            ? initialValue.slice(prefix.length)
            : initialValue,
        ]
      }),
    ),
  )

  function set(key: string, value: string) {
    const field = fields.find((candidate) => candidate.key === key)
    const optionDefaults =
      field?.type === "select" ? field.defaultsByOption?.[value] : undefined
    const next = { ...values, [key]: value, ...optionDefaults }
    setValues(next)
    // Persist the draft on every edit (visible fields only, matching submit)
    // so it rides the node's `config` into localStorage/server and survives a
    // selection switch. Fires only on user input — never eagerly on mount.
    onChange?.(withFixedPrefixes(fields, next, fixedPrefixes))
  }

  const visibleFields = fields.filter((f) => !isHiddenIn(f, values))
  // Every visible password field must satisfy the AD-complexity policy before
  // the node can be configured — the backend re-checks as the real gate.
  const passwordsOk = visibleFields.every(
    (f) => f.type !== "password" || isPasswordValid(values[f.key], vmName),
  )
  const fieldErrors = Object.fromEntries(
    visibleFields.map((field) => [
      field.key,
      validateField?.(
        field.key,
        `${fixedPrefixes[field.key] ?? ""}${values[field.key]}`,
      ) ?? null,
    ]),
  )
  const fieldsOk = Object.values(fieldErrors).every((error) => error === null)

  function submit() {
    onSubmit(withFixedPrefixes(fields, values, fixedPrefixes))
  }

  return (
    <div className="flex flex-col gap-3">
      {visibleFields.map((field) => (
        <div key={field.key} className="grid gap-1.5">
          <Label className="text-[11px] text-muted-foreground">
            {field.label}
          </Label>
          {field.type === "fixed" ? (
            <div className="flex h-7 items-center rounded-md border bg-muted/40 px-3 text-xs text-muted-foreground">
              {values[field.key]}
            </div>
          ) : field.type === "password" ? (
            <div className="grid gap-1.5">
              <Input
                type="password"
                value={values[field.key]}
                onChange={(e) => set(field.key, e.target.value)}
                placeholder={field.placeholder}
                className="h-7 text-xs"
                autoComplete="new-password"
              />
              <ul className="grid gap-0.5">
                {passwordRules(values[field.key], vmName).map((rule) => (
                  <li
                    key={rule.key}
                    className={`flex items-center gap-1.5 text-[10px] ${
                      rule.ok ? "text-emerald-600" : "text-muted-foreground"
                    }`}
                  >
                    {rule.ok ? (
                      <Check className="h-3 w-3 shrink-0" />
                    ) : (
                      <Circle className="h-3 w-3 shrink-0" />
                    )}
                    {rule.label}
                  </li>
                ))}
              </ul>
            </div>
          ) : field.type === "text" ? (
            <div className="flex min-w-0">
              {fixedPrefixes[field.key] && (
                <span
                  className="flex h-7 shrink-0 items-center rounded-l-md border border-r-0 bg-muted/40 px-2 font-mono text-xs text-muted-foreground"
                  title="Fixed project prefix"
                >
                  {fixedPrefixes[field.key]}
                </span>
              )}
              <Input
                value={values[field.key]}
                onChange={(e) => set(field.key, e.target.value)}
                placeholder={field.placeholder}
                maxLength={
                  field.key === "netbiosName"
                    ? 15 - (fixedPrefixes[field.key]?.length ?? 0)
                    : undefined
                }
                aria-invalid={!!fieldErrors[field.key]}
                className={cn(
                  "h-7 min-w-0 text-xs",
                  fixedPrefixes[field.key] && "rounded-l-none",
                  fieldErrors[field.key] && "border-red-500",
                )}
              />
            </div>
          ) : (
            <Select
              value={values[field.key]}
              onValueChange={(v) => v !== null && set(field.key, v)}
            >
              <SelectTrigger className="h-7 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {field.options.map((opt) => (
                  <SelectItem key={opt} value={opt} className="text-xs">
                    {opt}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {fieldErrors[field.key] && (
            <p className="text-[10px] font-medium text-red-500">
              {fieldErrors[field.key]}
            </p>
          )}
        </div>
      ))}
      <Button
        size="sm"
        className="mt-1 w-full"
        disabled={disabled || !passwordsOk || !fieldsOk}
        onClick={submit}
      >
        <Settings className="mr-2 h-3.5 w-3.5" />
        Configure
      </Button>
    </div>
  )
}

/**
 * Agent correlation + the live orchestrator actions.
 *
 * A real deploy auto-correlates: the clone worker bakes the agent
 * into the ISO and its vm_id rides back on the createVm op result
 * (`orchestratorVmId`, set in `store/staging.ts`), and CA/DC provisioning is
 * dispatched by the backend the moment the agent phones home. The vm_id field
 * here stays editable as a manual override for the dev/register flow. Every
 * action shares the same end-to-end path `cert.verify` first proved
 * (dispatch -> job socket -> result): the guest-eligible reads (`hostname.read`,
 * `ip.read`, `cert.verify`) plus the operator-only `ip.write` form.
 */
function OrchestratorPanel({
  nodeId,
  vmId,
  templateId,
  canRead,
  canWrite,
}: {
  nodeId: string
  vmId: string | undefined
  templateId: string
  canRead: boolean
  canWrite: boolean
}) {
  const store = useTopologyStore()
  const [draftVmId, setDraftVmId] = useState(vmId ?? "")
  const [path, setPath] = useState("")
  const [ipAddress, setIpAddress] = useState("")
  const [ipPrefix, setIpPrefix] = useState("24")
  const [ipGateway, setIpGateway] = useState("")
  // One command in flight at a time (keyed by command name); results keyed
  // the same way so each action row keeps its own last outcome.
  const [busy, setBusy] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, string>>({})
  const connected = useAgentConnected(vmId)

  function saveVmId() {
    const trimmed = draftVmId.trim()
    store.patchNodeData(nodeId, { orchestratorVmId: trimmed || undefined })
  }

  function run(command: string, params: Record<string, string>) {
    if (!vmId || busy) return
    setBusy(command)
    setResults((prev) => {
      const next = { ...prev }
      delete next[command]
      return next
    })
    const finish = (text: string) => {
      setBusy(null)
      setResults((prev) => ({ ...prev, [command]: text }))
    }
    dispatchExecutorCommand(vmId, command, params)
      .then(({ job_id }) => {
        const token = useAuthStore.getState().token
        const close = openJobSocket(job_id, token, {
          onDone: (e) => {
            finish(JSON.stringify(e.result))
            close()
          },
          onError: (e) => {
            finish(`Error: ${e.detail}`)
            close()
          },
        })
      })
      .catch((err) => {
        finish(
          err instanceof Error ? err.message : "Failed to dispatch command.",
        )
      })
  }

  const actionDisabled = !connected || busy !== null

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-1">
        <Input
          value={draftVmId}
          onChange={(e) => setDraftVmId(e.target.value)}
          placeholder="agent vm_id"
          className="h-7 text-xs"
        />
        <Button size="sm" className="h-7 px-2 text-xs" onClick={saveVmId}>
          Set
        </Button>
      </div>
      {vmId && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connected ? "bg-emerald-500" : "bg-muted-foreground/40",
            )}
          />
          Agent: {connected ? "Connected" : "Not connected"}
        </div>
      )}
      {canRead && (
        <div className="flex flex-col gap-1.5">
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start gap-2"
            disabled={actionDisabled}
            onClick={() => run("hostname.read", {})}
          >
            <Tag className="h-3.5 w-3.5" />
            {busy === "hostname.read" ? "Reading…" : "Read Hostname"}
          </Button>
          {results["hostname.read"] && (
            <p className="text-[11px] text-muted-foreground break-all">
              {results["hostname.read"]}
            </p>
          )}
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start gap-2"
            disabled={actionDisabled}
            onClick={() => run("ip.read", {})}
          >
            <Network className="h-3.5 w-3.5" />
            {busy === "ip.read" ? "Reading…" : "Read IP Addresses"}
          </Button>
          {results["ip.read"] && (
            <p className="text-[11px] text-muted-foreground break-all">
              {results["ip.read"]}
            </p>
          )}
        </div>
      )}
      {canWrite && (
        <div className="flex flex-col gap-1.5">
          <div className="flex gap-1">
            <Input
              value={ipAddress}
              onChange={(e) => setIpAddress(e.target.value)}
              placeholder="IPv4, e.g. 192.168.1.10"
              className="h-7 flex-1 text-xs"
              disabled={actionDisabled}
            />
            <Input
              value={ipPrefix}
              onChange={(e) => setIpPrefix(e.target.value)}
              placeholder="/24"
              className="h-7 w-12 text-xs"
              disabled={actionDisabled}
            />
          </div>
          <Input
            value={ipGateway}
            onChange={(e) => setIpGateway(e.target.value)}
            placeholder="gateway (optional)"
            className="h-7 text-xs"
            disabled={actionDisabled}
          />
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start gap-2"
            disabled={actionDisabled || !ipAddress}
            onClick={() =>
              run("ip.write", {
                address: ipAddress.trim(),
                prefixLength: ipPrefix.trim() || "24",
                ...(ipGateway.trim() ? { gateway: ipGateway.trim() } : {}),
              })
            }
          >
            <Network className="h-3.5 w-3.5" />
            {busy === "ip.write" ? "Applying…" : "Set Static IP"}
          </Button>
          {results["ip.write"] && (
            <p className="text-[11px] text-muted-foreground break-all">
              {results["ip.write"]}
            </p>
          )}
        </div>
      )}
      {canRead && (
        <div className="flex flex-col gap-1.5">
          <Input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="cert path, e.g. C:\win11.cer"
            className="h-7 text-xs"
            disabled={actionDisabled}
          />
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start gap-2"
            disabled={actionDisabled || !path}
            onClick={() => run("cert.verify", { path })}
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            {busy === "cert.verify" ? "Verifying…" : "Verify Certificate"}
          </Button>
          {results["cert.verify"] && (
            <p className="text-[11px] text-muted-foreground break-all">
              {results["cert.verify"]}
            </p>
          )}
        </div>
      )}
      {templateId === "client" && (
        <div className="flex flex-col gap-1.5 border-t pt-2">
          <Button
            variant="outline"
            size="sm"
            className="w-full justify-start gap-2"
            disabled={actionDisabled}
            onClick={() =>
              run("cert.enroll", {
                template: "Workstation",
                exportPath: "C:\\win11.cer",
                refreshPolicy: "true",
              })
            }
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            {busy === "cert.enroll" ? "Enrolling…" : "Enroll workstation cert"}
          </Button>
          {results["cert.enroll"] && (
            <p className="text-[11px] text-muted-foreground break-all">
              {results["cert.enroll"]}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export function Inspector() {
  const selectedId = useTopologyStore((s) => s.selectedNodeId)
  const store = useTopologyStore()
  const nodes = useTopologyStore((s) => s.nodes)
  const edges = useTopologyStore((s) => s.edges)
  const activeProjectId = useProjectsStore((s) => s.activeProjectId)

  const [editingName, setEditingName] = useState(false)
  const [draftName, setDraftName] = useState("")
  const [pendingDelete, setPendingDelete] = useState<StagedOp[] | null>(null)

  const canPower = useCan(CAPABILITIES.vmPower)
  const canUpdate = useCan(CAPABILITIES.vmUpdate)
  const canRead = useCan(CAPABILITIES.vmRead)
  const isOperator = useIsOperator()
  const deploying = useStagingStore((s) => s.deploying)
  const ops = useStagingStore((s) => s.ops)
  const retryDeploy = useStagingStore((s) => s.deploy)

  const node = nodes.find((n) => n.id === selectedId) ?? null

  if (!node) {
    return (
      <aside className="flex w-0 shrink-0 flex-col overflow-hidden border-l-0 bg-sidebar transition-[width] duration-200 ease-in-out" />
    )
  }

  const nodeId = node.id
  const { data } = node
  const def = TEMPLATE_BY_ID[data.typeId]
  const Icon = def?.icon ?? Settings
  const isConfigured = isDeployed(data)
  const isConfiguring = data.lifecycle === LIFECYCLE.deploying
  // Clone finished, but the orchestrator agent hasn't phoned home — a real VM
  // exists, so it's past configuring/staging, but not yet a confirmed deploy.
  const isProvisioning = data.lifecycle === LIFECYCLE.provisioning
  const isStaged = data.lifecycle === LIFECYCLE.staged
  const isFailed = data.lifecycle === LIFECYCLE.failed
  const isDestroying = data.lifecycle === LIFECYCLE.destroying
  // Any errored op that deploys or realizes this node — clone, provision, or
  // relationship (webServerCert realizes its `secondary` web host). Blocked
  // nodes have no errored op of their own; `data.errorDetail` carries the
  // "Blocked: …" text applyPlanState wrote.
  const failedOp = ops.find(
    (op) =>
      op.status === OP_STATUS.error &&
      (op.targetNodeId === nodeId ||
        (op.kind === OP_KIND.webServerCert && op.secondaryNodeId === nodeId)),
  )
  const failedDetail = failedOp?.detail ?? data.errorDetail

  const tier =
    data.typeId === "certificateAuthority" ? caTier(nodeId, edges) : null
  const depth =
    tier !== null && tier !== "root" && tier !== "standalone"
      ? caDepth(nodeId, edges)
      : null
  const domain = domainMembership(nodeId, edges, nodes)
  const netbiosPrefix = projectNetbiosPrefix(activeProjectId)

  function startRename() {
    setDraftName(data.name)
    setEditingName(true)
  }

  function commitRename() {
    const trimmed = draftName.trim()
    if (trimmed && trimmed !== data.name) {
      store.renameNode(nodeId, trimmed)
      toast.success(`Renamed to "${trimmed}"`)
    }
    setEditingName(false)
  }

  // Only pre-deploy nodes can be deleted (a real VM is never touched from the
  // canvas). A bare draft with nothing staged deletes with no dialog — that's
  // the low-friction path; anything with staged ops confirms the cascade.
  const canDelete =
    !data.vmName && !isConfigured && !isConfiguring && !isDestroying

  function handleDelete() {
    const affected = opsReferencingNode(useStagingStore.getState().ops, nodeId)
    if (affected.length === 0) {
      store.removeNode(nodeId)
      toast("Node removed.")
      return
    }
    setPendingDelete(affected)
  }

  function confirmDelete() {
    // removeNode itself cascades any ops referencing the node — this dialog
    // is purely the confirmation gate.
    store.removeNode(nodeId)
    toast("Node removed.")
    setPendingDelete(null)
  }

  function handleConfigure(config?: Record<string, string>) {
    store.configureNode(nodeId, config)
    toast.info(`Configuring "${data.name}"…`)
  }

  const hasConfigFields = !!(def?.configFields && def.configFields.length > 0)
  const showConfigForm =
    !isConfigured &&
    !isConfiguring &&
    !isProvisioning &&
    !isStaged &&
    !isDestroying

  return (
    <aside className="flex w-64 shrink-0 flex-col gap-0 overflow-x-hidden overflow-y-auto border-l bg-sidebar transition-[width] duration-200 ease-in-out">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-3 py-3">
        {def?.logo ? (
          <img
            src={def.logo}
            alt=""
            className="h-5 w-5 shrink-0"
            draggable={false}
          />
        ) : (
          <Icon className={cn("h-4 w-4 shrink-0", def?.accent)} />
        )}
        <span className="flex-1 text-sm font-semibold truncate">
          {data.name}
        </span>
        <button
          onClick={() => store.selectNode(null)}
          className="text-muted-foreground hover:text-foreground transition-colors"
          aria-label="Close inspector"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex flex-col gap-4 p-3">
        {/* Identity */}
        <section className="flex flex-col gap-2">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Identity
          </p>

          <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-xs">
            <span className="text-muted-foreground">Role</span>
            <span>{def?.label ?? data.typeId}</span>
            <span className="text-muted-foreground">Platform</span>
            <span>
              {def?.platform === "linux" ? "Linux · Ubuntu 22.04" : "Windows"}
            </span>
            {def?.cloneBase && (
              <>
                <span className="text-muted-foreground">Clone image</span>
                <span className="font-mono">{def.cloneBase}</span>
              </>
            )}
            {/* Offline root: present as air-gapped — the management IP is real
                (it phones home) but hidden here; operators see it in the
                Orchestrator panel. Everyone else just sees the sneakernet
                fiction. */}
            {tier === "root" ? (
              <>
                <span className="text-muted-foreground">Network</span>
                <span className="flex items-center gap-1 text-amber-500">
                  <Network className="h-3 w-3" /> Air-gapped (offline root)
                </span>
                {isOperator && data.ip && (
                  <>
                    <span className="text-muted-foreground">Mgmt IP</span>
                    <span
                      className="font-mono text-muted-foreground"
                      title="operator-only: real management address"
                    >
                      {data.ip}
                    </span>
                  </>
                )}
              </>
            ) : (
              // Held until confirmed deployed (agent online), mirroring the
              // node — a `provisioning` node knows its IP but it isn't
              // reachable-confirmed yet.
              isConfigured &&
              data.ip && (
                <>
                  <span className="text-muted-foreground">IP address</span>
                  <span className="font-mono">{data.ip}</span>
                </>
              )
            )}
            {isOperator && data.vmName && (
              <>
                <span className="text-muted-foreground">VM name</span>
                <span className="truncate font-mono" title={data.vmName}>
                  {data.vmName}
                </span>
              </>
            )}
            {(!isConfigured || isConfiguring || isDrifted(data)) && (
              <>
                <span className="text-muted-foreground">Status</span>
                <span className="flex items-center gap-1">
                  {data.lifecycle === LIFECYCLE.draft && (
                    <>
                      <AlertTriangle className="h-3 w-3 text-amber-500" /> draft
                    </>
                  )}
                  {isStaged && (
                    <>
                      <Clock className="h-3 w-3 text-sky-500" /> staged
                    </>
                  )}
                  {isConfiguring && (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />{" "}
                      deploying…
                    </>
                  )}
                  {isProvisioning && (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin text-emerald-500" />{" "}
                      awaiting orchestrator…
                    </>
                  )}
                  {isFailed && (
                    <>
                      <AlertTriangle className="h-3 w-3 text-red-500" /> failed
                    </>
                  )}
                  {isDestroying && (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin text-red-500" />{" "}
                      removing…
                    </>
                  )}
                  {isDrifted(data) && (
                    <>
                      <RefreshCw className="h-3 w-3 text-orange-500" /> drifted
                    </>
                  )}
                </span>
              </>
            )}
          </div>

          {/* Derived chips */}
          {tier === "root" && (
            <Badge
              variant="outline"
              className="text-[10px] border-amber-500/50 text-amber-500 self-start"
            >
              CA: Root
            </Badge>
          )}
          {tier === "intermediate" && (
            <Badge
              variant="outline"
              className="text-[10px] border-amber-500/40 text-amber-400 self-start"
            >
              CA: Intermediate · T{depth}
            </Badge>
          )}
          {tier === "issuing" && (
            <Badge
              variant="outline"
              className="text-[10px] border-amber-500/30 text-amber-300 self-start"
            >
              CA: Issuing · T{depth}
            </Badge>
          )}
          {domain && (
            <Badge
              variant="outline"
              className="text-[10px] border-blue-500/40 text-blue-400 self-start"
            >
              Domain: {domain}
            </Badge>
          )}
        </section>

        {/* Rename — locked once a real VM exists: the name is baked into the
            deployed VM's inventory name (guest-<user>-<project>-<machine>), so
            it can't change after deployment. */}
        <section className="flex flex-col gap-2">
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Name
          </p>
          {data.vmName ? (
            <p className="text-xs text-muted-foreground">
              {data.name} — locked after deploy
            </p>
          ) : editingName ? (
            <div className="flex gap-1">
              <Input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename()
                  if (e.key === "Escape") setEditingName(false)
                }}
                className="h-7 text-xs"
                autoFocus
              />
              <Button
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={commitRename}
              >
                OK
              </Button>
            </div>
          ) : (
            <button
              onClick={startRename}
              className="text-left text-xs text-muted-foreground hover:text-foreground underline-offset-2 hover:underline transition-colors"
            >
              {data.name} — click to rename
            </button>
          )}
        </section>

        {/* Configuration inputs (shown when draft/failed with fields, or reconfiguring) */}
        {showConfigForm && hasConfigFields && (
          <section className="flex flex-col gap-2">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Configuration
            </p>
            {!isConfiguring && (
              <>
                {!isConfigured && (
                  <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs text-amber-600">
                    <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                    Configure this VM before connecting it or taking actions.
                  </div>
                )}
                <ConfigForm
                  key={nodeId}
                  fields={def!.configFields!}
                  vmName={data.name}
                  initial={data.config}
                  fixedPrefixes={
                    data.typeId === "domainController" && netbiosPrefix
                      ? { netbiosName: netbiosPrefix }
                      : undefined
                  }
                  onChange={(config) => store.setNodeConfig(nodeId, config)}
                  onSubmit={handleConfigure}
                  disabled={deploying}
                  validateField={(key, value) =>
                    configurationNameCollision(nodeId, key, value, nodes)
                  }
                />
              </>
            )}
          </section>
        )}

        {/* Simple configure (no config fields) */}
        {!isConfigured &&
          !isConfiguring &&
          !isProvisioning &&
          !isStaged &&
          !isDestroying &&
          !hasConfigFields && (
            <section className="flex flex-col gap-2">
              <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs text-amber-600">
                <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                Configure this VM before connecting it or taking actions.
              </div>
              <Button
                size="sm"
                className="w-full"
                disabled={deploying}
                onClick={() => handleConfigure()}
              >
                <Settings className="mr-2 h-3.5 w-3.5" />
                Configure
              </Button>
            </section>
          )}

        {/* Operator-only ISO authoring — available while the node is
            still configurable (draft/failed/reconfiguring) or staged (deploy
            reads the panel fresh, so staged edits need no restage). */}
        {isOperator && (showConfigForm || isStaged) && (
          <IsoAuthoringPanel nodeId={nodeId} />
        )}

        {/* Configuring spinner */}
        {isConfiguring && (
          <section className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Creating VM…
          </section>
        )}

        {/* Tearing down — the destroy job is running; nothing else is actionable */}
        {isDestroying && (
          <section className="flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/5 p-2 text-xs text-red-600">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Tearing down VM…
          </section>
        )}

        {/* Provisioning — the clone finished but the orchestrator agent hasn't
            phoned home; the deploy isn't confirmed (IP/domain circle held back)
            until it does. */}
        {isProvisioning && (
          <section className="flex items-start gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-2 text-xs text-emerald-600">
            <Loader2 className="mt-0.5 h-3 w-3 shrink-0 animate-spin" />
            VM created — waiting for the orchestrator to phone home.
          </section>
        )}

        {/* Staged — pending a deploy that will actually create it */}
        {isStaged && (
          <section className="flex items-start gap-2 rounded-md border border-sky-500/30 bg-sky-500/5 p-2 text-xs text-sky-600">
            <Clock className="mt-0.5 h-3 w-3 shrink-0" />
            Staged — will be created when deployed.
          </section>
        )}

        {/* Failed — an op deploying/realizing this node errored (or was blocked
            by an upstream failure); offer the same retry the Staged panel exposes */}
        {isFailed && (
          <section className="flex flex-col gap-2">
            <div className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/5 p-2 text-xs text-red-600">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <div className="flex flex-col gap-1">
                <span>Deploy failed.</span>
                {failedDetail && (
                  <span className="text-[11px] text-muted-foreground">
                    {failedDetail}
                  </span>
                )}
                {failedOp?.trace && (
                  <details>
                    <summary className="cursor-pointer text-[10px] font-medium text-muted-foreground">
                      Technical details
                    </summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-muted/40 p-1.5 text-[9px] leading-snug text-muted-foreground">
                      {failedOp.trace}
                    </pre>
                  </details>
                )}
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="w-full justify-start gap-2"
              disabled={deploying}
              onClick={() => retryDeploy()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              Retry deploy
            </Button>
          </section>
        )}

        {/* Drifted — deployed, but the stored config (or authored ISO) no longer matches what was last deployed */}
        {isDrifted(data) && (
          <section className="flex flex-col gap-2">
            <div className="flex items-start gap-2 rounded-md border border-orange-500/30 bg-orange-500/5 p-2 text-xs text-orange-600">
              <RefreshCw className="mt-0.5 h-3 w-3 shrink-0" />
              <div className="flex flex-col gap-1">
                <span>Configuration changed since last deploy.</span>
                {driftedFields(data).map((key) => {
                  if (key === ISO_DRIFT_FIELD) {
                    return (
                      <span
                        key={key}
                        className="text-[11px] text-muted-foreground"
                      >
                        ISO contents changed
                      </span>
                    )
                  }
                  const field = def?.configFields?.find((f) => f.key === key)
                  const fieldLabel = field?.label ?? key
                  // A password's value is never shown — only that it changed.
                  const mask = (v: string | undefined) =>
                    field?.type === "password"
                      ? v
                        ? PASSWORD_MASK
                        : "—"
                      : (v ?? "—")
                  return (
                    <span
                      key={key}
                      className="text-[11px] text-muted-foreground"
                    >
                      {fieldLabel}: {mask(data.lastDeployedConfig?.[key])} →{" "}
                      {mask(data.config?.[key])}
                    </span>
                  )
                })}
              </div>
            </div>
          </section>
        )}

        {/* Stored config values (post-configure) */}
        {isConfigured && data.config && (
          <section className="flex flex-col gap-2">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Configuration
            </p>
            <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-xs">
              {Object.entries(data.config).map(([key, value]) => {
                const field = def?.configFields?.find((f) => f.key === key)
                const fieldLabel = field?.label ?? key
                // Secrets show only as a mask — the value never reaches the DOM.
                const shown =
                  field?.type === "password"
                    ? value
                      ? PASSWORD_MASK
                      : "—"
                    : value
                return (
                  <Fragment key={key}>
                    <span className="text-muted-foreground">{fieldLabel}</span>
                    <span className="truncate">{shown}</span>
                  </Fragment>
                )
              })}
            </div>
          </section>
        )}

        {/* Actions — operator-only power stubs (roadmap). A deployed node's
            config is fixed: there is no reconfigure, and guests get no actions. */}
        {isConfigured && isOperator && (
          <section className="flex flex-col gap-2">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Actions
            </p>

            <div className="flex flex-col gap-1.5">
              <PlannedAction
                icon={Power}
                label="Power On"
                tip="Power controls coming soon"
                disabled={!canPower || deploying}
              />
              <PlannedAction
                icon={PowerOff}
                label="Power Off"
                tip="Power controls coming soon"
                disabled={!canPower || deploying}
              />
            </div>
          </section>
        )}

        {/* Orchestrator phone-home: manual agent correlation + live hostname/IP/cert
            actions. Operator-only — raw vm_id/token correlation and agent commands
            are infra internals the guest product surface must not expose. */}
        {isOperator && (isConfigured || isProvisioning) && (
          <section className="flex flex-col gap-2 border-t pt-3">
            <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Orchestrator
            </p>
            <OrchestratorPanel
              nodeId={nodeId}
              vmId={data.orchestratorVmId}
              templateId={data.typeId}
              canRead={canRead}
              canWrite={canUpdate}
            />
          </section>
        )}

        {/* Danger zone — only pre-deploy nodes can be deleted. Once a real VM
            exists (deployed/deploying/destroying) there is no destructive
            action on the canvas: the VM stays put. */}
        {canDelete && (
          <section className="flex flex-col gap-2 border-t pt-3">
            <Button
              variant="destructive"
              size="sm"
              className="w-full justify-start gap-2"
              disabled={deploying}
              onClick={handleDelete}
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete node
            </Button>
          </section>
        )}
      </div>

      <StagedRemoveDialog
        ops={pendingDelete}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </aside>
  )
}
