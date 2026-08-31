/**
 * The per-account product catalogue — pure, React-free, mirroring the backend's
 * `authz.allowed_product_templates`.
 *
 * Capabilities are per-role and answer *whether*; a product grant is
 * per-account and answers *which*. Products are what a visitor is usually here
 * to be shown, and which ones differs per cohort — an account evaluating
 * CertSecure has no business standing up CodeSign Secure beside it, and a role
 * could not express that without a role per product.
 *
 * Only the `product` templates are gated. The PKI components (DC, CA, web
 * server) are available to everyone, so the catalogue restricts the products
 * and never the playground.
 */

import { TEMPLATE_BY_ID } from "@/constants/templates"

/**
 * Whether this account may put `typeId` on the canvas.
 *
 * `products` is the resolved list from `GET /auth/me` — an operator holds the
 * whole catalogue there, so callers need no role branch. `undefined` (identity
 * still loading) fails closed: an unavailable product renders greyed out rather
 * than flashing as draggable and then turning inert.
 *
 * COSMETIC only. `POST /api/deploy` refuses a product op the calling account is
 * not entitled to (403) whatever the palette allowed.
 */
export function isTemplateAvailable(
  typeId: string,
  products: string[] | undefined,
): boolean {
  if (TEMPLATE_BY_ID[typeId]?.category !== "product") return true
  return !!products?.includes(typeId)
}
