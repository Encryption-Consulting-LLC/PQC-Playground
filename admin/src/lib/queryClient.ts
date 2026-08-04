import { QueryClient } from "@tanstack/react-query"

/**
 * The app's single query client.
 *
 * Lives here rather than in `main.tsx` because the router carries it in its
 * context, so route `loader`s can prefetch without a second client — and a
 * second client would mean two caches disagreeing about the same data.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
})
