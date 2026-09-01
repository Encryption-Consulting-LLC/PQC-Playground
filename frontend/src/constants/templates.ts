import { Building2, Globe, Server, ShieldCheck } from "lucide-react"
import type { LucideIcon } from "lucide-react"

/**
 * Hide this field based on another field's current value: while it `equals`
 * the given value, or (the inverse) while it does not equal `notEquals`.
 */
export interface HideWhen {
  key: string
  equals?: string
  notEquals?: string
}

export type ConfigField =
  | {
      key: string
      label: string
      type: "text"
      default: string
      placeholder?: string
      hideWhen?: HideWhen
    }
  | {
      key: string
      label: string
      type: "select"
      options: string[]
      default: string
      /** Values to reset when this option changes another configuration mode. */
      defaultsByOption?: Record<string, Record<string, string>>
      hideWhen?: HideWhen
    }
  | {
      // Read-only value the user can't change (e.g. a fixed key size).
      key: string
      label: string
      type: "fixed"
      default: string
      hideWhen?: HideWhen
    }
  | {
      // Masked secret with an inline AD-complexity checklist
      // (see lib/passwordPolicy.ts). Stored/diffed masked, never in op labels.
      key: string
      label: string
      type: "password"
      default: string
      placeholder?: string
      hideWhen?: HideWhen
    }

export interface TemplateDef {
  id: string
  label: string
  icon: LucideIcon
  logo?: string
  accent: string
  description: string
  platform: "linux" | "windows"
  category?: "component" | "product"
  cloneBase?: string
  configFields?: ConfigField[]
}

export const TEMPLATE_CATALOG: TemplateDef[] = [
  {
    id: "domainController",
    label: "Domain Controller",
    icon: Building2,
    accent: "text-blue-500",
    description: "AD DS · DNS",
    platform: "windows",
    configFields: [
      {
        key: "domainName",
        label: "Domain Name",
        type: "text",
        default: "encon.pki",
        placeholder: "encon.pki",
      },
      {
        key: "netbiosName",
        label: "NetBIOS Name",
        type: "text",
        default: "ENCON",
        placeholder: "ENCON",
      },
      {
        key: "forestLevel",
        label: "Forest Level",
        type: "select",
        options: [
          "Windows Server 2016",
          "Windows Server 2019",
          "Windows Server 2022",
          "Windows Server 2025",
        ],
        default: "Windows Server 2016",
      },
      {
        key: "reverseZone",
        label: "Reverse DNS Zone (optional)",
        type: "text",
        default: "",
        placeholder: "100.168.192.in-addr.arpa",
      },
      {
        key: "domainAdminPassword",
        label: "Domain Admin Password",
        type: "password",
        default: "",
        placeholder: "used to join members + install the issuing CA",
      },
    ],
  },
  {
    id: "certificateAuthority",
    label: "Certificate Authority",
    icon: ShieldCheck,
    accent: "text-amber-500",
    description: "AD CS",
    platform: "windows",
    configFields: [
      {
        key: "caType",
        label: "Type",
        type: "select",
        options: ["Root", "Issuing"],
        default: "Root",
        defaultsByOption: {
          Root: {
            commonName: "EC-Root-CA",
            validityYears: "20",
          },
          Issuing: {
            commonName: "EC-Issuing-CA",
            validityYears: "10",
          },
        },
      },
      {
        key: "commonName",
        label: "Common Name",
        type: "text",
        default: "EC-Root-CA",
        placeholder: "EC-Root-CA",
      },
      {
        key: "certEnrollPath",
        label: "CA Publication Directory",
        type: "text",
        default: "C:\\Windows\\System32\\CertSrv\\CertEnroll",
        placeholder: "C:\\Windows\\System32\\CertSrv\\CertEnroll",
      },
      {
        key: "keyAlgorithm",
        label: "Key Algorithm",
        type: "select",
        options: ["RSA", "ECDSA", "ML-DSA-87"],
        default: "ML-DSA-87",
      },
      {
        key: "keyLength",
        label: "Key Length",
        type: "select",
        options: ["2048", "4096"],
        default: "2048",
        hideWhen: { key: "keyAlgorithm", equals: "ML-DSA-87" },
      },
      {
        // ML-DSA-87 has a fixed 2,592-byte public key — not user-selectable.
        key: "keyLengthFixed",
        label: "Key Length",
        type: "fixed",
        default: "2,592 bytes",
        hideWhen: { key: "keyAlgorithm", notEquals: "ML-DSA-87" },
      },
      {
        key: "hashAlgorithm",
        label: "Hash Algorithm",
        type: "select",
        options: ["SHA256", "SHA384", "SHA512"],
        default: "SHA256",
        // ML-DSA is a standalone signature scheme with no separate hash choice.
        hideWhen: { key: "keyAlgorithm", equals: "ML-DSA-87" },
      },
      {
        key: "validityYears",
        label: "Validity (years)",
        type: "text",
        default: "20",
        placeholder: "20",
      },
      {
        // CPS statement URL — issuing CAs only (a root has no CPS pointer).
        key: "cpsUrl",
        label: "CPS URL",
        type: "text",
        default: "http://pki.encon.pki/cps.txt",
        placeholder: "http://pki.…/cps.txt",
        hideWhen: { key: "caType", equals: "Root" },
      },
    ],
  },
  {
    id: "webServer",
    label: "PKI Web Services",
    icon: Globe,
    accent: "text-emerald-500",
    description: "IIS publication · CDP/AIA · OCSP",
    platform: "windows",
    configFields: [
      {
        key: "certEnrollPath",
        label: "CertEnroll Directory",
        type: "text",
        default: "C:\\CertEnroll",
        placeholder: "C:\\CertEnroll",
      },
      {
        key: "enableOcsp",
        label: "Online Responder",
        type: "select",
        options: ["Enabled", "Disabled"],
        default: "Enabled",
      },
      {
        key: "ocspRefreshMinutes",
        label: "OCSP Refresh (min)",
        type: "text",
        default: "15",
        placeholder: "15",
        hideWhen: { key: "enableOcsp", equals: "Disabled" },
      },
    ],
  },
  // {
  //   id: "client",
  //   label: "Client",
  //   icon: Monitor,
  //   accent: "text-violet-400",
  //   description: "Windows 11 workstation",
  //   platform: "windows",
  // },
  {
    id: "standalone",
    label: "Standalone",
    icon: Server,
    accent: "text-slate-400",
    description: "Generic Windows Server",
    platform: "windows",
  },
  {
    id: "certsecure",
    label: "CertSecure Manager",
    icon: Server,
    logo: "/certsecure-manager.svg",
    accent: "text-cyan-500",
    description: "Linux server · Ubuntu 24.04",
    platform: "linux",
    category: "product",
    cloneBase: "ub-24.04-base",
    // Exactly the four names the vendor installer prompts for. Everything else
    // it writes is the same on every deployment, and the two certificate paths
    // it also asks for are backend-controlled — the certificate is generated
    // into the product tree during provisioning rather than supplied.
    //
    // The defaults describe the lab's own domain, so wiring the node to a
    // domain controller and pressing Deploy yields working names with nothing
    // typed. Only names that fall inside the domain controller's zone get DNS
    // records; pointing these somewhere else still installs, it just leaves the
    // lab's DNS out of it.
    configFields: [
      {
        key: "frontendHost",
        label: "Dashboard host name",
        type: "text",
        default: "certsecure.encon.pki",
        placeholder: "certsecure.encon.pki",
      },
      {
        key: "backendHost",
        label: "API host name",
        type: "text",
        default: "certsecure-api.encon.pki",
        placeholder: "certsecure-api.encon.pki",
      },
      {
        key: "keycloakHost",
        label: "Keycloak host name",
        type: "text",
        default: "kc.encon.pki",
        placeholder: "kc.encon.pki",
      },
      {
        key: "keycloakRealm",
        label: "Keycloak realm",
        type: "text",
        default: "encon.pki",
        placeholder: "encon.pki",
      },
    ],
  },
  {
    id: "cbom",
    label: "CBOM Secure",
    icon: Server,
    logo: "/cbom-secure.svg",
    accent: "text-teal-500",
    description: "Linux server · Ubuntu 24.04",
    platform: "linux",
    category: "product",
    cloneBase: "ub-24.04-base",
  },
  {
    id: "codesign",
    label: "CodeSign Secure",
    icon: Server,
    logo: "/codesign-secure.svg",
    accent: "text-indigo-500",
    description: "Linux server · Ubuntu 24.04",
    platform: "linux",
    category: "product",
    cloneBase: "ub-24.04-base",
  },
]

export const TEMPLATE_BY_ID = Object.fromEntries(
  TEMPLATE_CATALOG.map((t) => [t.id, t]),
) as Record<string, TemplateDef>

export const AUTO_NAME_PREFIX: Record<string, string> = {
  domainController: "dc",
  certificateAuthority: "ca",
  webServer: "srv",
  client: "client",
  standalone: "misc",
  certsecure: "certsecure",
  cbom: "cbom",
  codesign: "codesign",
}

export function templatePlatform(typeId: string): "linux" | "windows" {
  return TEMPLATE_BY_ID[typeId]?.platform ?? "windows"
}

/**
 * Whether dropping this template should pre-fill the Inspector's Config ISO
 * panel (see `IsoAuthoringPanel`) instead of leaving it off and empty.
 *
 * True for the PKI *component* roles — a DC/CA/web server has a firstboot disc
 * worth disclosing, so an operator can see and edit the hostname script rather
 * than guess at what boots. Excluded: `standalone` (a bare Windows Server with
 * nothing role-specific to show) and the Linux `product` templates (which
 * cannot carry the Windows agent at all).
 */
export function seedsFirstbootScripts(typeId: string): boolean {
  const def = TEMPLATE_BY_ID[typeId]
  return !!def && def.category !== "product" && typeId !== "standalone"
}
