import { useState } from "react"
import { Dialog } from "@base-ui/react/dialog"
import { Loader2, Wand2 } from "lucide-react"
import { toast } from "sonner"

import { generateHostname, generateNetwork, generatePassword } from "@/lib/api"
import type { Platform } from "@/lib/api"
import { HOSTNAME_BASENAME, defaultHostname } from "@/lib/hostname"
import type { IsoFileEntry } from "@/store/topology"
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

const KINDS = {
  hostname: { label: "Hostname", basename: HOSTNAME_BASENAME },
  network: { label: "Network", basename: "20-network" },
  password: { label: "Password", basename: "25-password" },
} as const

type Kind = keyof typeof KINDS

/**
 * "Generate from template" (sub-plan 4): a small form per configgen generator
 * — hostname / network / password — that calls the existing `/api/generate/*`
 * routes and drops the rendered PowerShell into the PACK panel as an ordinary
 * editable file (`10-`/`20-`/`25-` so the name sort keeps manifest order).
 * Validation lives in configgen — the form just surfaces its 422 messages.
 */
export function GenerateScriptDialog({
  open,
  nodeName,
  platform,
  onInsert,
  onClose,
}: {
  open: boolean
  nodeName: string
  platform: Platform
  /** Inserts (or replaces, keyed by filename) the generated script in the panel. */
  onInsert: (file: IsoFileEntry) => void
  onClose: () => void
}) {
  const [kind, setKind] = useState<Kind>("hostname")
  const [busy, setBusy] = useState(false)

  const [hostname, setHostname] = useState(() =>
    defaultHostname(nodeName, platform),
  )
  const [dhcp, setDhcp] = useState(false)
  const [ip, setIp] = useState("")
  const [prefix, setPrefix] = useState("24")
  const [gateway, setGateway] = useState("")
  const [dns1, setDns1] = useState("")
  const [dns2, setDns2] = useState("")
  const [dnsSuffix, setDnsSuffix] = useState("")
  const [username, setUsername] = useState(
    platform === "linux" ? "ubuntu" : "Administrator",
  )
  const [password, setPassword] = useState("")

  function generate() {
    if (busy) return
    setBusy(true)
    const call =
      kind === "hostname"
        ? generateHostname({ platform, hostname: hostname.trim() })
        : kind === "network"
          ? generateNetwork({
              platform,
              dhcp,
              ...(dhcp
                ? {}
                : {
                    ip: ip.trim(),
                    prefix: Number(prefix) || null,
                    gateway: gateway.trim(),
                    dns1: dns1.trim(),
                  }),
              ...(dns2.trim() ? { dns2: dns2.trim() } : {}),
              ...(dnsSuffix.trim() ? { dns_suffix: dnsSuffix.trim() } : {}),
            })
          : generatePassword({ platform, username: username.trim(), password })

    call
      .then((content) => {
        onInsert({
          name: `${KINDS[kind].basename}.${platform === "linux" ? "sh" : "ps1"}`,
          content,
        })
        onClose()
      })
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "Generation failed.")
      })
      .finally(() => setBusy(false))
  }

  const field = (label: string, input: React.ReactNode) => (
    <div className="grid gap-1.5">
      <Label className="text-[11px] text-muted-foreground">{label}</Label>
      {input}
    </div>
  )

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[1px] data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0" />
        <Dialog.Popup className="fixed left-1/2 top-1/2 z-50 flex w-[min(380px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-3 rounded-xl border bg-popover p-5 text-popover-foreground shadow-lg ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
          <Dialog.Title className="text-sm font-semibold">
            Generate from template
          </Dialog.Title>
          <Dialog.Description className="text-xs text-muted-foreground">
            Renders a firstboot script with configgen and adds it to the panel
            as{" "}
            <span className="font-mono">
              {KINDS[kind].basename}.{platform === "linux" ? "sh" : "ps1"}
            </span>
            , editable like any other file.
          </Dialog.Description>

          {field(
            "Script",
            <Select
              value={kind}
              onValueChange={(v) => v !== null && setKind(v as Kind)}
            >
              <SelectTrigger className="h-7 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(KINDS) as Kind[]).map((k) => (
                  <SelectItem key={k} value={k} className="text-xs">
                    {KINDS[k].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>,
          )}

          {kind === "hostname" &&
            field(
              platform === "windows"
                ? "Hostname (15 chars max)"
                : "Hostname (63 chars max)",
              <Input
                value={hostname}
                onChange={(e) => setHostname(e.target.value)}
                className="h-7 font-mono text-xs"
              />,
            )}

          {kind === "network" && (
            <>
              {field(
                "Mode",
                <Select
                  value={dhcp ? "dhcp" : "static"}
                  onValueChange={(v) => v !== null && setDhcp(v === "dhcp")}
                >
                  <SelectTrigger className="h-7 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="static" className="text-xs">
                      Static
                    </SelectItem>
                    <SelectItem value="dhcp" className="text-xs">
                      DHCP
                    </SelectItem>
                  </SelectContent>
                </Select>,
              )}
              {!dhcp && (
                <>
                  <div className="flex gap-1.5">
                    <div className="flex-1">
                      {field(
                        "IP address",
                        <Input
                          value={ip}
                          onChange={(e) => setIp(e.target.value)}
                          placeholder="192.168.1.50"
                          className="h-7 font-mono text-xs"
                        />,
                      )}
                    </div>
                    <div className="w-16">
                      {field(
                        "Prefix",
                        <Input
                          value={prefix}
                          onChange={(e) => setPrefix(e.target.value)}
                          placeholder="24"
                          className="h-7 font-mono text-xs"
                        />,
                      )}
                    </div>
                  </div>
                  {field(
                    "Gateway",
                    <Input
                      value={gateway}
                      onChange={(e) => setGateway(e.target.value)}
                      placeholder="192.168.1.1"
                      className="h-7 font-mono text-xs"
                    />,
                  )}
                  {field(
                    "DNS 1",
                    <Input
                      value={dns1}
                      onChange={(e) => setDns1(e.target.value)}
                      placeholder="192.168.1.10"
                      className="h-7 font-mono text-xs"
                    />,
                  )}
                </>
              )}
              {field(
                "DNS 2 (optional)",
                <Input
                  value={dns2}
                  onChange={(e) => setDns2(e.target.value)}
                  className="h-7 font-mono text-xs"
                />,
              )}
              {field(
                "DNS suffix (optional)",
                <Input
                  value={dnsSuffix}
                  onChange={(e) => setDnsSuffix(e.target.value)}
                  placeholder="corp.example.com"
                  className="h-7 font-mono text-xs"
                />,
              )}
            </>
          )}

          {kind === "password" && (
            <>
              {field(
                "Username",
                <Input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="h-7 font-mono text-xs"
                />,
              )}
              {field(
                "Password",
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="h-7 text-xs"
                />,
              )}
              <p className="text-[11px] leading-4 text-muted-foreground">
                The password is embedded in the script text and stored with the
                project — treat this as lab-grade only.
              </p>
            </>
          )}

          <div className="mt-1 flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button size="sm" onClick={generate} disabled={busy}>
              {busy ? (
                <>
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" /> Generating…
                </>
              ) : (
                <>
                  <Wand2 className="mr-1 h-3 w-3" /> Generate
                </>
              )}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
