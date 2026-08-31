import { useState } from "react"

import { TEMPLATE_CATALOG } from "@/constants/templates"
import { actionableOps } from "@/lib/staging"
import { cn } from "@/lib/utils"
import { useStagingStore } from "@/store/staging"
import { useIsTemplateAvailable } from "@/hooks/useAllowedProducts"
import { useIsJoinedLab } from "@/hooks/useJoinedLab"
import { StagedPanel } from "./StagedPanel"

const DRAG_TYPE = "application/reactflow"

type Tab = "templates" | "staged"

const COMPONENT_TEMPLATES = TEMPLATE_CATALOG.filter(
  (def) => def.category !== "product",
)
const PRODUCT_TEMPLATES = TEMPLATE_CATALOG.filter(
  (def) => def.category === "product",
)

export function Toolbox() {
  const [tab, setTab] = useState<Tab>("templates")
  // Outstanding work only: a failed deploy leaves its succeeded rows listed
  // for context, and those aren't staged for anything any more.
  const opsCount = useStagingStore((s) => actionableOps(s.ops).length)
  const deploying = useStagingStore((s) => s.deploying)
  // A joined lab is finished: there is nothing to add to it, so the palette is
  // inert for the same reason it is mid-deploy. Kept visible rather than hidden
  // — the templates are what the lab is made of, and reading them is useful.
  const joinedLab = useIsJoinedLab()
  const locked = deploying || joinedLab
  // Per-account, unlike `locked`: which products an account may deploy is a
  // property of the account, not of what the canvas is doing. An unavailable
  // one stays listed — the catalogue is worth reading either way, and a card
  // that silently disappears reads as a missing feature rather than as one
  // this account was not given.
  const isAvailable = useIsTemplateAvailable()

  return (
    <aside
      className={cn(
        "flex shrink-0 flex-col overflow-hidden border-r bg-sidebar transition-[width] duration-200 ease-in-out",
        tab === "staged" ? "w-72" : "w-48",
      )}
    >
      <div className="flex h-9 shrink-0 border-b text-[11px] font-semibold uppercase tracking-wider">
        <button
          onClick={() => setTab("templates")}
          className={cn(
            "flex flex-1 items-center justify-center border-b-2 px-2 transition-colors",
            tab === "templates"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          Templates
        </button>
        <button
          onClick={() => setTab("staged")}
          className={cn(
            "flex flex-1 items-center justify-center border-b-2 px-2 transition-colors",
            tab === "staged"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          Staged{opsCount > 0 ? ` (${opsCount})` : ""}
        </button>
      </div>

      {tab === "templates" ? (
        <div className="flex flex-1 flex-col overflow-y-auto p-3">
          <h2 className="mb-2 px-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            Components
          </h2>
          <div className="grid grid-cols-2 gap-2">
            {COMPONENT_TEMPLATES.map((def) => {
              const Icon = def.icon
              return (
                <div
                  key={def.id}
                  draggable={!locked}
                  onDragStart={(e) => {
                    e.dataTransfer.setData(DRAG_TYPE, def.id)
                    e.dataTransfer.effectAllowed = "copy"
                  }}
                  className={cn(
                    "flex flex-col items-center justify-center gap-2",
                    "rounded-lg border bg-card px-2 py-3",
                    "shadow-sm transition-colors select-none",
                    locked
                      ? "cursor-not-allowed opacity-50"
                      : "cursor-grab hover:bg-accent hover:text-accent-foreground active:cursor-grabbing",
                  )}
                >
                  <Icon className={cn("h-6 w-6 shrink-0", def.accent)} />
                  <span className="text-center text-[11px] font-semibold leading-tight">
                    {def.label}
                  </span>
                </div>
              )
            })}
          </div>

          <h2 className="mb-2 mt-5 px-1 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            Product Catalog
          </h2>
          <div className="grid grid-cols-2 gap-2">
            {PRODUCT_TEMPLATES.map((product) => {
              const Icon = product.icon
              const available = isAvailable(product.id)
              const draggable = !locked && available
              return (
                <div
                  key={product.id}
                  draggable={draggable}
                  onDragStart={(e) => {
                    // `draggable={false}` already stops the gesture; refusing
                    // to write the payload too means a drag started some other
                    // way carries nothing the canvas would accept.
                    if (!draggable) {
                      e.preventDefault()
                      return
                    }
                    e.dataTransfer.setData(DRAG_TYPE, product.id)
                    e.dataTransfer.effectAllowed = "copy"
                  }}
                  title={
                    available
                      ? `${product.description} · clones ${product.cloneBase}`
                      : `${product.label} is not available to your account.`
                  }
                  className={cn(
                    "flex flex-col items-center justify-center gap-2 rounded-lg border bg-card px-2 py-3 shadow-sm select-none transition-colors",
                    !available
                      ? "cursor-not-allowed opacity-40 grayscale"
                      : locked
                        ? "cursor-not-allowed opacity-50"
                        : "cursor-grab hover:bg-accent hover:text-accent-foreground active:cursor-grabbing",
                  )}
                >
                  {product.logo ? (
                    <img
                      src={product.logo}
                      alt=""
                      className="h-8 w-8 shrink-0"
                      draggable={false}
                    />
                  ) : (
                    <Icon className={cn("h-8 w-8 shrink-0", product.accent)} />
                  )}
                  <span className="text-center text-[11px] font-semibold leading-tight">
                    {product.label}
                  </span>
                </div>
              )
            })}
          </div>

          <p className="mt-3 px-1 text-[10px] text-muted-foreground leading-snug">
            {joinedLab
              ? "This lab was deployed for you — open a machine's desktop from the inspector. Create your own project to build a topology."
              : "Drag a component onto the canvas to add it."}
          </p>
        </div>
      ) : (
        <StagedPanel />
      )}
    </aside>
  )
}
