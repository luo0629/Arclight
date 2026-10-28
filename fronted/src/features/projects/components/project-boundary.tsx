"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import OpeningStory from "@/components/feedback/opening-story";
import { useWorkbench } from "@/features/workbench/workbench-context";
import ProjectWorkspace from "@/features/projects/components/project-workspace";

export default function ProjectBoundary({ children }: { children: ReactNode }) {
  const context = useWorkbench();
  if (!context.project) {
    if (!context.error) return <OpeningStory />;
    return (
      <div className="app-shell grid min-h-[calc(100dvh-81px)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)]">
        <aside className="sidebar flex min-w-0 flex-col gap-4 border-b bg-card/70 p-5 lg:border-r lg:border-b-0">
          {context.projectPicker}
        </aside>
        <main className="flex min-h-96 flex-col items-center justify-center gap-4 p-6">
          <h1>无法打开此项目</h1>
          <p>项目不存在、已被删除，或你没有访问权限。</p>
          <Link href="/" className="text-primary underline">
            返回我的项目
          </Link>
        </main>
      </div>
    );
  }
  return (
    <ProjectWorkspace {...context} project={context.project}>
      {children}
    </ProjectWorkspace>
  );
}
