import { proxyBackend } from "@/lib/server/backend-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export { proxyBackend as GET, proxyBackend as HEAD };
