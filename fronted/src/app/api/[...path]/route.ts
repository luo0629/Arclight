import { proxyBackend } from "@/lib/server/backend-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export {
  proxyBackend as GET,
  proxyBackend as POST,
  proxyBackend as PUT,
  proxyBackend as PATCH,
  proxyBackend as DELETE,
  proxyBackend as HEAD,
  proxyBackend as OPTIONS,
};
