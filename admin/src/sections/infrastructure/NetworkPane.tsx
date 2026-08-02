import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useInfraForm } from "./context"

export function NetworkPane() {
  const { form, patch } = useInfraForm()

  return (
    <>
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
    </>
  )
}
