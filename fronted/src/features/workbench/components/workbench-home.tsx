"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, message } from "@/lib/api/client";
import { projectPath } from "@/lib/routes";
import type { Project } from "@/types/api";
import { useWorkbench } from "@/features/workbench/workbench-context";
import { EmptyState, Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

export default function WorkbenchHome() {
  const { projects, loaded, projectPicker, createProject } = useWorkbench();
  const router = useRouter();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!loaded || !projects.length) return;
    let live = true;
    void (async () => {
      const saved = localStorage.getItem("arclight-project");
      let selected = projects.find((row) => row.id === saved) ?? projects[0];
      if (saved && !projects.some((row) => row.id === saved)) {
        try {
          selected = await api<Project>(
            "/projects/" + encodeURIComponent(saved),
          );
        } catch (error) {
          if (
            ![403, 404].includes((error as { status?: number }).status ?? 0)
          ) {
            if (live) setError(message(error));
            return;
          }
        }
      }
      if (live) router.replace(projectPath(selected.id));
    })();
    return () => {
      live = false;
    };
  }, [loaded, projects, router]);
  return (
    <>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="app-shell grid min-h-[calc(100dvh-81px)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)]">
        <aside className="sidebar flex min-w-0 flex-col gap-4 border-b bg-card/70 p-5 lg:border-r lg:border-b-0">
          {projectPicker}
        </aside>
        <EmptyState className="empty welcome-empty min-h-96">
          <p className="eyebrow text-[11px] font-semibold tracking-widest text-primary">
            故事从这里开始
          </p>
          <h1>
            {loaded && !projects.length
              ? "新的一页，等你落笔"
              : "正在找回你的故事…"}
          </h1>
          <p>为一个小宇宙新建项目，再添加第一集。</p>
          <Button
            variant="default"
            size="lg"
            className=""
            onClick={() => createProject()}
          >
            创建第一个项目
          </Button>
        </EmptyState>
      </div>
    </>
  );
}
