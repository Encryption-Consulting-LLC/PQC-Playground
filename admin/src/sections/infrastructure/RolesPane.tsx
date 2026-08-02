import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useInfraForm } from "./context"

const ROLE_LABELS = {
  domainController: "Domain controller",
  rootCa: "Offline root CA",
  issuingCa: "Issuing CA",
  webServer: "Web and OCSP server",
  certsecure: "CertSecure Manager",
  cbom: "CBOM Secure",
  codesign: "CodeSign Secure",
} as const

export function RolesPane() {
  const { form, patchProfile, patchQualification, addQualification } = useInfraForm()

  return (
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
  )
}
