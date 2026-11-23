import type { ReactNode } from "react";
import Link from "next/link";
import { projectPath } from "@/lib/routes";
import type { ExactAssetReference } from "@/types/api";

export default function ExactAssetLink({
  projectId,
  reference,
  children,
}: {
  projectId: string;
  reference: ExactAssetReference;
  children: ReactNode;
}) {
  const params = new URLSearchParams({
    asset: reference.asset_id,
    asset_version: reference.asset_version_id,
  });
  return (
    <Link
      className="underline underline-offset-4"
      href={`${projectPath(projectId, "assets")}&${params}`}
    >
      {children}
    </Link>
  );
}
