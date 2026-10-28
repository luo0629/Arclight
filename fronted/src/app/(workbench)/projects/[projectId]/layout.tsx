import type { ReactNode } from "react";
import { notFound } from "next/navigation";
import { isResourceId } from "@/lib/routes";
import ProjectBoundary from "@/features/projects/components/project-boundary";

export default async function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;
  if (!isResourceId(projectId)) notFound();
  return <ProjectBoundary key={projectId}>{children}</ProjectBoundary>;
}
