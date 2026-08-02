import { useEffect, useState } from "react"
import { Link, Outlet, useMatchRoute } from "@tanstack/react-router"
import { CheckCircle2, CircleAlert, Loader2, ShieldCheck } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
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
import { InfraFormContext, type FormState } from "./infrastructure/context"

const TABS = [
  { value: "esxi", label: "ESXi target", to: "/infrastructure/esxi" },
  { value: "network", label: "Guest network", to: "/infrastructure/network" },
  { value: "image", label: "Golden image", to: "/infrastructure/image" },
  { value: "roles", label: "Role sizing", to: "/infrastructure/roles" },
] as const

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
 *
 * The four panes are child routes, so each has its own address. This is their
 * layout and it owns the form, which is what makes tab switching keep every
 * edit: the component holding `form` never unmounts. It also owns the load
 * and error states, so a pane never renders against a form the server hasn't
 * filled in yet, and Save/Validate, which act on all four panes at once.
 */
export function InfrastructureSection() {
  const matchRoute = useMatchRoute()
  const activeTab = TABS.find((tab) => matchRoute({ to: tab.to }))?.value ?? TABS[0].value

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
          {/* The provider only has to sit above the Outlet — the four panes
              are its only consumers. */}
          <InfraFormContext
            value={{ form, patch, patchProfile, patchQualification, addQualification }}
          >
            <Tabs value={activeTab}>
              <TabsList>
                {TABS.map((tab) => (
                  // Real anchors, so a pane is middle-clickable and linkable.
                  <TabsTab
                    key={tab.value}
                    value={tab.value}
                    nativeButton={false}
                    render={<Link to={tab.to} />}
                  >
                    {tab.label}
                  </TabsTab>
                ))}
              </TabsList>

              {/* One panel, always valued at the active tab, so Base UI still
                  wires role="tabpanel" and aria-labelledby to a real tab. */}
              <TabsPanel value={activeTab} className="mt-(--gap-stack)">
                <Outlet />
              </TabsPanel>
            </Tabs>
          </InfraFormContext>
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
