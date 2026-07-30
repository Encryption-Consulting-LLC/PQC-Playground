import { useEffect, useState } from "react"
import { CheckCircle2, CircleAlert, Loader2, ShieldCheck } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsList, TabsPanel, TabsTab } from "@/components/ui/tabs"
import {
  getSettings,
  updateSettings,
  validateInfrastructure,
  validateEnvironment,
  type EnvironmentPreflight,
  type InfrastructurePreflight,
  type InfrastructureProfile,
  type ImageQualification,
  type OperatorSettingsUpdate,
} from "@/lib/api"

interface FormState {
  esxiHost: string
  esxiUser: string
  esxiPassword: string
  esxiPort: string
  cloneBase: string
  cloneDatastore: string
  cloneGuestOs: string
  cloneNetwork: string
  cloneMaxUsagePct: string
  guestIpStart: string
  guestIpEnd: string
  guestPrefix: string
  guestGateway: string
  guestDns1: string
  guestDns2: string
  guestDnsSuffix: string
  profiles: InfrastructureProfile[]
  hasPassword: boolean
}

const ROLE_LABELS = {
  domainController: "Domain controller",
  rootCa: "Offline root CA",
  issuingCa: "Issuing CA",
  webServer: "Web and OCSP server",
  certsecure: "CertSecure Manager",
  cbom: "CBOM Secure",
  codesign: "CodeSign Secure",
} as const

const DEFAULT_PROFILES: InfrastructureProfile[] = [
  ["domainController", 8, 8192, 60],
  ["rootCa", 8, 8192, 60],
  ["issuingCa", 8, 8192, 80],
  ["webServer", 8, 8192, 80],
].map(([role, cpus, memoryMb, systemDiskGb]) => ({
  role: role as InfrastructureProfile["role"],
  base: "ws-2025-base",
  datastore: "datastore1",
  expectedGuestOs: "windows2022srvNext-64",
  network: "VM Network",
  cpus: cpus as number,
  memoryMb: memoryMb as number,
  systemDiskGb: systemDiskGb as number,
  maxUsagePct: 80,
  qualification: null,
}))

const EMPTY_FORM: FormState = {
  esxiHost: "",
  esxiUser: "",
  esxiPassword: "",
  esxiPort: "443",
  cloneBase: "ws-2025-base",
  cloneDatastore: "datastore1",
  cloneGuestOs: "windows2022srvNext-64",
  cloneNetwork: "VM Network",
  cloneMaxUsagePct: "80",
  guestIpStart: "",
  guestIpEnd: "",
  guestPrefix: "24",
  guestGateway: "",
  guestDns1: "",
  guestDns2: "",
  guestDnsSuffix: "",
  profiles: DEFAULT_PROFILES,
  hasPassword: false,
}

function formatBytes(value: number | null): string {
  if (value == null) return "unknown"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

/**
 * Shared ESXi target, guest addressing, and per-role placement/sizing — a
 * tabified port of the operator app's SettingsDialog (frontend/src/
 * components/SettingsDialog.tsx), split across sub-tabs instead of one long
 * scroll. Same form state, same payload/validation logic; only the layout
 * and token usage changed.
 */
export function InfrastructureSection() {
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [preflight, setPreflight] = useState<InfrastructurePreflight | null>(null)
  const [environment, setEnvironment] = useState<EnvironmentPreflight | null>(null)

  useEffect(() => {
    let active = true
    getSettings()
      .then((settings) => {
        if (!active) return
        setForm({
          esxiHost: settings.esxiHost ?? "",
          esxiUser: settings.esxiUser ?? "",
          esxiPassword: "",
          esxiPort: String(settings.esxiPort ?? 443),
          cloneBase: settings.cloneBase ?? "ws-2025-base",
          cloneDatastore: settings.cloneDatastore ?? "datastore1",
          cloneGuestOs: settings.cloneGuestOs ?? "windows2022srvNext-64",
          cloneNetwork: settings.cloneNetwork ?? "VM Network",
          cloneMaxUsagePct: String(settings.cloneMaxUsagePct ?? 80),
          guestIpStart: settings.guestIpStart ?? "",
          guestIpEnd: settings.guestIpEnd ?? "",
          guestPrefix: String(settings.guestPrefix ?? 24),
          guestGateway: settings.guestGateway ?? "",
          guestDns1: settings.guestDns1 ?? "",
          guestDns2: settings.guestDns2 ?? "",
          guestDnsSuffix: settings.guestDnsSuffix ?? "",
          profiles: settings.infrastructureProfiles?.length === 4
            ? settings.infrastructureProfiles
            : DEFAULT_PROFILES.map((profile) => ({
                ...profile,
                base: settings.cloneBase ?? profile.base,
                datastore: settings.cloneDatastore ?? profile.datastore,
                expectedGuestOs: settings.cloneGuestOs ?? profile.expectedGuestOs,
                network: settings.cloneNetwork ?? profile.network,
                maxUsagePct: settings.cloneMaxUsagePct ?? profile.maxUsagePct,
              })),
          hasPassword: settings.hasPassword,
        })
      })
      .catch((error: Error) => {
        if (active) setLoadError(error.message)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  function patch(field: keyof FormState, value: string) {
    setForm((current) => ({ ...current, [field]: value }))
    setPreflight(null)
    setEnvironment(null)
  }

  function patchProfile(
    role: InfrastructureProfile["role"],
    field: keyof Omit<InfrastructureProfile, "role">,
    value: string,
  ) {
    setForm((current) => ({
      ...current,
      profiles: current.profiles.map((profile) =>
        profile.role === role
          ? {
              ...profile,
              [field]: ["cpus", "memoryMb", "systemDiskGb", "maxUsagePct"].includes(field)
                ? Number(value)
                : value,
            }
          : profile,
      ),
    }))
    setPreflight(null)
    setEnvironment(null)
  }

  function patchQualification(
    role: InfrastructureProfile["role"],
    field: keyof ImageQualification,
    value: string | number | boolean | string[] | null,
  ) {
    setForm((current) => ({
      ...current,
      profiles: current.profiles.map((profile) => {
        if (profile.role !== role || !profile.qualification) return profile
        return {
          ...profile,
          qualification: {
            ...profile.qualification,
            [field]: ["windowsBuild", "publicationManifestVersion"].includes(field)
              ? Number(value)
              : value,
            validatedAt: Date.now(),
          },
        }
      }),
    }))
    setPreflight(null)
    setEnvironment(null)
  }

  function addQualification(role: InfrastructureProfile["role"]) {
    setForm((current) => ({
      ...current,
      profiles: current.profiles.map((profile) =>
        profile.role === role
          ? {
              ...profile,
              qualification: {
                baseChangeVersion: "",
                windowsBuild: 26100,
                runnerVersion: "",
                agentSha256: environment?.agentSha256 ?? "",
                validatedAt: Date.now(),
                mlDsa87Available: false,
                systemContextValidated: false,
                timeSynchronized: false,
                windowsUpdatesCurrent: false,
                backendCallbackReachable: false,
                agentCommands: [],
                publicationManifestVersion: 0,
                ocspReferenceSha256: null,
              },
            }
          : profile,
      ),
    }))
    setPreflight(null)
    setEnvironment(null)
  }

  function useBundledAgentDigest() {
    const digest = environment?.agentSha256
    if (!digest) return
    setForm((current) => ({
      ...current,
      profiles: current.profiles.map((profile) =>
        profile.qualification
          ? {
              ...profile,
              qualification: {
                ...profile.qualification,
                agentSha256: digest,
                validatedAt: Date.now(),
              },
            }
          : profile,
      ),
    }))
    setPreflight(null)
    setEnvironment(null)
    toast.success("Bundled agent digest applied. Save and validate again.")
  }

  function payload(): OperatorSettingsUpdate {
    return {
      esxiHost: form.esxiHost.trim(),
      esxiUser: form.esxiUser.trim(),
      ...(form.esxiPassword ? { esxiPassword: form.esxiPassword } : {}),
      esxiPort: Number(form.esxiPort),
      cloneBase: form.cloneBase.trim(),
      cloneDatastore: form.cloneDatastore.trim(),
      cloneGuestOs: form.cloneGuestOs.trim(),
      cloneNetwork: form.cloneNetwork.trim(),
      cloneMaxUsagePct: Number(form.cloneMaxUsagePct),
      guestIpStart: form.guestIpStart.trim(),
      guestIpEnd: form.guestIpEnd.trim(),
      guestPrefix: Number(form.guestPrefix),
      guestGateway: form.guestGateway.trim(),
      guestDns1: form.guestDns1.trim(),
      guestDns2: form.guestDns2.trim(),
      guestDnsSuffix: form.guestDnsSuffix.trim(),
      infrastructureProfiles: form.profiles,
    }
  }

  const formValid =
    !!form.esxiHost.trim() &&
    !!form.esxiUser.trim() &&
    (form.hasPassword || !!form.esxiPassword) &&
    Number(form.esxiPort) > 0 &&
    !!form.cloneBase.trim() &&
    !!form.cloneDatastore.trim() &&
    !!form.cloneGuestOs.trim() &&
    !!form.cloneNetwork.trim() &&
    Number(form.cloneMaxUsagePct) > 0 &&
    Number(form.cloneMaxUsagePct) <= 100 &&
    !!form.guestIpStart.trim() &&
    !!form.guestIpEnd.trim() &&
    Number(form.guestPrefix) >= 1 &&
    Number(form.guestPrefix) <= 32 &&
    !!form.guestGateway.trim() &&
    !!form.guestDns1.trim() &&
    form.profiles.every((profile) =>
      !!profile.base.trim() && !!profile.datastore.trim() &&
      !!profile.expectedGuestOs.trim() && !!profile.network.trim() &&
      profile.cpus > 0 && profile.memoryMb >= 1024 &&
      profile.systemDiskGb >= 32 && profile.maxUsagePct > 0 &&
      profile.maxUsagePct <= 100,
    )

  async function save(showToast = true) {
    setSaving(true)
    try {
      const saved = await updateSettings(payload())
      setForm((current) => ({
        ...current,
        esxiPassword: "",
        hasPassword: saved.hasPassword,
      }))
      if (showToast) toast.success("Infrastructure settings saved.")
      return true
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not save settings.")
      return false
    } finally {
      setSaving(false)
    }
  }

  async function runValidation() {
    setPreflight(null)
    setEnvironment(null)
    const saved = await save(false)
    if (!saved) return
    setValidating(true)
    try {
      const [result, controlPlane] = await Promise.all([
        validateInfrastructure(),
        validateEnvironment(),
      ])
      setPreflight(result)
      setEnvironment(controlPlane)
      if (result.ready && controlPlane.ready) toast.success("Infrastructure is deploy-ready.")
      else toast.error("Infrastructure validation found blocking issues.")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Validation failed.")
    } finally {
      setValidating(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-(--gap-inline) py-(--pad-section) text-xs text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading settings…
      </div>
    )
  }

  if (loadError) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-(--pad-card) text-xs text-destructive">
        {loadError}
      </div>
    )
  }

  return (
    <div className="space-y-(--gap-stack)">
      <Card>
        <CardHeader>
          <CardTitle>Infrastructure</CardTitle>
          <CardDescription>
            Configure ESXi, guest addressing, and per-role placement, then prove the complete
            four-machine reservation before cloning.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="esxi">
            <TabsList>
              <TabsTab value="esxi">ESXi target</TabsTab>
              <TabsTab value="network">Guest network</TabsTab>
              <TabsTab value="image">Golden image</TabsTab>
              <TabsTab value="roles">Role sizing</TabsTab>
            </TabsList>

            <TabsPanel value="esxi" className="mt-(--gap-stack)">
              <div className="grid gap-(--gap-row) sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="esxi-host">Host</Label>
                  <Input id="esxi-host" value={form.esxiHost} onChange={(e) => patch("esxiHost", e.target.value)} placeholder="192.168.100.10" />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="esxi-port">Port</Label>
                  <Input id="esxi-port" type="number" min="1" max="65535" value={form.esxiPort} onChange={(e) => patch("esxiPort", e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="esxi-user">Username</Label>
                  <Input id="esxi-user" value={form.esxiUser} onChange={(e) => patch("esxiUser", e.target.value)} placeholder="root" />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="esxi-password">Password</Label>
                  <Input id="esxi-password" type="password" value={form.esxiPassword} onChange={(e) => patch("esxiPassword", e.target.value)} placeholder={form.hasPassword ? "Saved — enter to replace" : "Required"} />
                </div>
              </div>
            </TabsPanel>

            <TabsPanel value="network" className="mt-(--gap-stack)">
              <div className="grid gap-(--gap-row) sm:grid-cols-3">
                <div className="space-y-1.5"><Label htmlFor="guest-start">IP range start</Label><Input id="guest-start" value={form.guestIpStart} onChange={(e) => patch("guestIpStart", e.target.value)} /></div>
                <div className="space-y-1.5"><Label htmlFor="guest-end">IP range end</Label><Input id="guest-end" value={form.guestIpEnd} onChange={(e) => patch("guestIpEnd", e.target.value)} /></div>
                <div className="space-y-1.5"><Label htmlFor="guest-prefix">Prefix</Label><Input id="guest-prefix" type="number" min="1" max="32" value={form.guestPrefix} onChange={(e) => patch("guestPrefix", e.target.value)} /></div>
                <div className="space-y-1.5"><Label htmlFor="guest-gateway">Gateway</Label><Input id="guest-gateway" value={form.guestGateway} onChange={(e) => patch("guestGateway", e.target.value)} /></div>
                <div className="space-y-1.5"><Label htmlFor="guest-dns1">Primary DNS</Label><Input id="guest-dns1" value={form.guestDns1} onChange={(e) => patch("guestDns1", e.target.value)} /></div>
                <div className="space-y-1.5"><Label htmlFor="guest-dns2">Secondary DNS</Label><Input id="guest-dns2" value={form.guestDns2} onChange={(e) => patch("guestDns2", e.target.value)} /></div>
                <div className="space-y-1.5 sm:col-span-3"><Label htmlFor="guest-suffix">DNS suffix</Label><Input id="guest-suffix" value={form.guestDnsSuffix} onChange={(e) => patch("guestDnsSuffix", e.target.value)} placeholder="encon.pki" /></div>
              </div>
              <p className="mt-(--gap-row) text-xs text-muted-foreground">
                The range is inclusive. Network and broadcast addresses of each /{form.guestPrefix || "24"} subnet
                it spans (e.g. .0 and .255 on a /24) are skipped and never handed to a VM.
              </p>
            </TabsPanel>

            <TabsPanel value="image" className="mt-(--gap-stack)">
              <div className="grid gap-(--gap-row) sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="clone-base">Datastore image name</Label>
                  <Input id="clone-base" value={form.cloneBase} onChange={(e) => patch("cloneBase", e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="clone-datastore">Datastore</Label>
                  <Input id="clone-datastore" value={form.cloneDatastore} onChange={(e) => patch("cloneDatastore", e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="clone-guest-os">Expected VMware guest OS</Label>
                  <Input id="clone-guest-os" value={form.cloneGuestOs} onChange={(e) => patch("cloneGuestOs", e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="clone-usage">Maximum datastore usage (%)</Label>
                  <Input id="clone-usage" type="number" min="1" max="100" step="0.1" value={form.cloneMaxUsagePct} onChange={(e) => patch("cloneMaxUsagePct", e.target.value)} />
                </div>
              </div>
            </TabsPanel>

            <TabsPanel value="roles" className="mt-(--gap-stack)">
              <div className="space-y-(--gap-row)">
                {form.profiles.map((profile) => (
                  <div key={profile.role} className="rounded-lg border p-(--pad-card)">
                    <h4 className="text-xs font-medium">{ROLE_LABELS[profile.role]}</h4>
                    <div className="mt-(--gap-row) grid gap-(--gap-row) sm:grid-cols-4">
                      <div className="space-y-1.5 sm:col-span-2"><Label>Image</Label><Input value={profile.base} onChange={(e) => patchProfile(profile.role, "base", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>Datastore</Label><Input value={profile.datastore} onChange={(e) => patchProfile(profile.role, "datastore", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>Port group</Label><Input value={profile.network} onChange={(e) => patchProfile(profile.role, "network", e.target.value)} /></div>
                      <div className="space-y-1.5 sm:col-span-2"><Label>VMware guest OS</Label><Input value={profile.expectedGuestOs} onChange={(e) => patchProfile(profile.role, "expectedGuestOs", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>vCPU</Label><Input type="number" min="1" value={profile.cpus} onChange={(e) => patchProfile(profile.role, "cpus", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>Memory (MiB)</Label><Input type="number" min="1024" step="1024" value={profile.memoryMb} onChange={(e) => patchProfile(profile.role, "memoryMb", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>Disk reservation (GiB)</Label><Input type="number" min="32" value={profile.systemDiskGb} onChange={(e) => patchProfile(profile.role, "systemDiskGb", e.target.value)} /></div>
                      <div className="space-y-1.5"><Label>Usage limit (%)</Label><Input type="number" min="1" max="100" value={profile.maxUsagePct} onChange={(e) => patchProfile(profile.role, "maxUsagePct", e.target.value)} /></div>
                    </div>
                    {profile.qualification ? (
                      <div className="mt-(--gap-row) border-t pt-(--gap-row)">
                        <div className="grid gap-(--gap-row) sm:grid-cols-3">
                          <div className="space-y-1.5"><Label>Qualified image revision</Label><Input value={profile.qualification.baseChangeVersion} onChange={(e) => patchQualification(profile.role, "baseChangeVersion", e.target.value)} /></div>
                          <div className="space-y-1.5"><Label>Windows build</Label><Input type="number" min="26100" value={profile.qualification.windowsBuild} onChange={(e) => patchQualification(profile.role, "windowsBuild", e.target.value)} /></div>
                          <div className="space-y-1.5"><Label>Runner version</Label><Input value={profile.qualification.runnerVersion} onChange={(e) => patchQualification(profile.role, "runnerVersion", e.target.value)} /></div>
                          <div className="space-y-1.5 sm:col-span-3"><Label>Agent SHA-256</Label><Input value={profile.qualification.agentSha256} onChange={(e) => patchQualification(profile.role, "agentSha256", e.target.value)} /></div>
                          <div className="space-y-1.5 sm:col-span-2"><Label>Qualified agent commands</Label><Input value={profile.qualification.agentCommands.join(", ")} onChange={(e) => patchQualification(profile.role, "agentCommands", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))} /></div>
                          <div className="space-y-1.5"><Label>Publication manifest version</Label><Input type="number" min="1" value={profile.qualification.publicationManifestVersion} onChange={(e) => patchQualification(profile.role, "publicationManifestVersion", Number(e.target.value))} /></div>
                          {profile.role === "webServer" && <div className="space-y-1.5 sm:col-span-3"><Label>OCSP reference dump SHA-256</Label><Input value={profile.qualification.ocspReferenceSha256 ?? ""} onChange={(e) => patchQualification(profile.role, "ocspReferenceSha256", e.target.value || null)} /></div>}
                        </div>
                        <div className="mt-(--gap-row) flex flex-wrap gap-(--gap-stack) text-xs text-muted-foreground">
                          <label className="flex items-center gap-(--gap-inline)"><input type="checkbox" checked={profile.qualification.systemContextValidated} onChange={(e) => patchQualification(profile.role, "systemContextValidated", e.target.checked)} /> SYSTEM operations validated</label>
                          <label className="flex items-center gap-(--gap-inline)"><input type="checkbox" checked={profile.qualification.timeSynchronized} onChange={(e) => patchQualification(profile.role, "timeSynchronized", e.target.checked)} /> Time synchronized</label>
                          <label className="flex items-center gap-(--gap-inline)"><input type="checkbox" checked={profile.qualification.windowsUpdatesCurrent} onChange={(e) => patchQualification(profile.role, "windowsUpdatesCurrent", e.target.checked)} /> Windows updates current</label>
                          <label className="flex items-center gap-(--gap-inline)"><input type="checkbox" checked={profile.qualification.backendCallbackReachable} onChange={(e) => patchQualification(profile.role, "backendCallbackReachable", e.target.checked)} /> Backend callback reached</label>
                          {(profile.role === "rootCa" || profile.role === "issuingCa") && <label className="flex items-center gap-(--gap-inline)"><input type="checkbox" checked={profile.qualification.mlDsa87Available} onChange={(e) => patchQualification(profile.role, "mlDsa87Available", e.target.checked)} /> ML-DSA-87 provider validated</label>}
                        </div>
                      </div>
                    ) : (
                      <Button className="mt-(--gap-row)" variant="outline" size="sm" onClick={() => addQualification(profile.role)}>
                        Add canary qualification
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            </TabsPanel>
          </Tabs>
        </CardContent>
      </Card>

      {preflight && (
        <Card>
          <CardContent>
            <div className="flex items-center justify-between gap-(--gap-row)">
              <div className="flex items-center gap-(--gap-inline) text-sm font-medium">
                {preflight.ready ? <ShieldCheck className="size-4 text-success" /> : <CircleAlert className="size-4 text-destructive" />}
                {preflight.ready ? "Image ready" : "Action required"}
              </div>
              <span className="text-[10px] text-muted-foreground">
                reserve {formatBytes(preflight.datastores.reduce((sum, item) => sum + (item.reservedBytes ?? 0), 0))}
              </span>
            </div>
            <ul className="mt-(--gap-row) space-y-(--gap-inline)">
              {preflight.checks.map((check) => (
                <li key={check.key} className="flex items-start gap-(--gap-inline) text-xs">
                  {check.ok ? <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success" /> : <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-destructive" />}
                  <span className={check.ok ? "text-muted-foreground" : "text-destructive"}>{check.detail}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {environment && (
        <Card>
          <CardContent>
            <div className="flex items-center gap-(--gap-inline) text-sm font-medium">
              {environment.ready ? <ShieldCheck className="size-4 text-success" /> : <CircleAlert className="size-4 text-destructive" />}
              {environment.ready ? "Control plane ready" : "Control plane action required"}
            </div>
            <ul className="mt-(--gap-row) space-y-(--gap-inline)">
              {environment.checks.map((check) => (
                <li key={check.key} className="flex items-start gap-(--gap-inline) text-xs">
                  {check.ok ? <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success" /> : <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-destructive" />}
                  <span className={check.ok ? "text-muted-foreground" : "text-destructive"}>{check.detail}</span>
                </li>
              ))}
            </ul>
            {environment.agentSha256 && environment.checks.some((check) => check.key === "agentBinary" && !check.ok) && (
              <div className="mt-(--gap-row) border-t pt-(--gap-row)">
                <p className="text-xs text-muted-foreground">
                  The agent is injected into each clone&apos;s first-boot ISO; it does not belong in the base VM. If this binary has already passed your image canary, apply its digest to the existing role qualifications.
                </p>
                <Button className="mt-(--gap-inline)" variant="outline" size="sm" onClick={useBundledAgentDigest}>
                  Use bundled agent digest
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap justify-end gap-(--gap-inline)">
        <Button variant="outline" size="sm" disabled={saving || validating || !formValid} onClick={() => void save()}>
          {saving && !validating ? <Loader2 className="animate-spin" /> : null}
          Save
        </Button>
        <Button size="sm" disabled={saving || validating || !formValid} onClick={() => void runValidation()}>
          {validating ? <Loader2 className="animate-spin" /> : <ShieldCheck />}
          Validate infrastructure
        </Button>
      </div>
    </div>
  )
}
