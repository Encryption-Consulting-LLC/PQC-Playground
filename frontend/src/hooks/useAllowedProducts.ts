/**
 * The account's product grant, bound to the signed-in identity.
 *
 * The per-account half of the authorization model, and the counterpart to
 * `useCan`: that reads the role's capability allowlist, this reads the
 * account's own product list. The decision itself lives in `lib/products.ts`
 * (pure, so it is testable without a renderer); this only supplies the
 * identity.
 *
 * Like `useCan`, COSMETIC — see `isTemplateAvailable`.
 */

import { useCallback } from "react"

import { useMe } from "@/hooks/useMe"
import { isTemplateAvailable } from "@/lib/products"

export function useIsTemplateAvailable(): (typeId: string) => boolean {
  const products = useMe()?.products
  return useCallback(
    (typeId: string) => isTemplateAvailable(typeId, products),
    [products],
  )
}
