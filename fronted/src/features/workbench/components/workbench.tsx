"use client";

import { Brand } from "@/components/brand-art";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { projectPath } from "@/lib/routes";
import { WorkbenchContext } from "@/features/workbench/workbench-context";
import { useAuth } from "@/features/auth/auth-context";
import { SlidersHorizontal } from "lucide-react";
import AccountMenu from "@/features/auth/components/account-menu";
import { ChoiceSelect } from "@/components/choice-select";
import { Spinner } from "@/components/ui/spinner";
import ServiceSettings from "@/features/settings/components/service-settings";
import { Notice, FormLabel } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { api, json, message } from "@/lib/api/client";
import type { Project } from "@/types/api";

import Dialog from "@/components/shared/app-dialog";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";

import ProjectTrash from "@/features/projects/components/project-trash";
export default function Workbench({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const router = useRouter();
  const params = useParams<{ projectId?: string }>();
  const projectId = params.projectId ?? "";
  const projectList = usePagedList<Project>("/projects");
  const [trashOpen, setTrashOpen] = useState(false);
  const [excludedIds, setExcludedIds] = useState<Set<string>>(new Set());
  const excluded = useRef(new Set<string>());
  const { rows: allProjects, reload: loadProjects } = projectList;
  const projects = allProjects.filter((row) => !excludedIds.has(row.id));
  const [projectNotice, setProjectNotice] = useState("");
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [servicesOpen, setServicesOpen] = useState(false);
  const [pinnedProject, setPinnedProject] = useState<Project | null>(null);
  useEffect(() => {
    if (projectId) localStorage.setItem("arclight-project", projectId);
  }, [projectId]);
  const projectPickerRef = useRef<HTMLButtonElement>(null);
  const restorePickerFocus = useRef(false);
  const currentProjectId = useRef(projectId);
  currentProjectId.current = projectId;
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    const rows = (await loadProjects()).filter(
      (row) => !excluded.current.has(row.id),
    );
    setLoaded(true);
    const id = currentProjectId.current;
    if (id && !rows.some((row) => row.id === id)) {
      try {
        const selected = await api<Project>("/projects/" + id);
        if (currentProjectId.current === id) setPinnedProject(selected);
      } catch (error) {
        if (![403, 404].includes((error as { status?: number }).status ?? 0))
          throw error;
        if (currentProjectId.current === id) {
          setPinnedProject(null);
          throw error;
        }
      }
    } else {
      setPinnedProject(rows.find((row) => row.id === id) ?? rows[0] ?? null);
    }
    setError("");
  }, [loadProjects]);
  useEffect(() => {
    void reload().catch((error) => setError(message(error)));
  }, [reload, projectId]);
  const project = excludedIds.has(projectId)
    ? null
    : (projects.find((row) => row.id === projectId) ??
      (pinnedProject?.id === projectId ? pinnedProject : null) ??
      null);
  function updateProject(updated: Project) {
    projectList.update(updated);
    setPinnedProject(updated);
  }
  function select(id: string) {
    restorePickerFocus.current = directoryOpen && id !== projectId;
    setPinnedProject(projects.find((row) => row.id === id) ?? null);
    currentProjectId.current = id;
    router.push(projectPath(id), { scroll: false });
    localStorage.setItem("arclight-project", id);
  }
  function deleted() {
    const id = currentProjectId.current;
    excluded.current.add(id);
    setExcludedIds((previous) => new Set(previous).add(id));
    currentProjectId.current = "";
    router.replace("/");
    setPinnedProject(null);
    localStorage.removeItem("arclight-project");
    setProjectNotice("项目已移入回收站，30 天内可恢复。");
    void reload().catch((error) =>
      setError("删除已成功；项目目录刷新失败：" + message(error)),
    );
  }
  function restored(row: Project) {
    excluded.current.delete(row.id);
    setExcludedIds((previous) => {
      const next = new Set(previous);
      next.delete(row.id);
      return next;
    });
    currentProjectId.current = row.id;
    select(row.id);
    setPinnedProject(row);
    setTrashOpen(false);
    setProjectNotice("项目已恢复，仍为归档状态。需要编辑时请先解除归档。");
    void reload().catch((error) =>
      setError("恢复已成功；项目目录刷新失败：" + message(error)),
    );
  }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const row = await api<Project>(
        "/projects",
        json("POST", { name: data.get("name") }),
      );
      await reload();
      select(row.id);
      setPinnedProject(row);
      setCreating(false);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  const projectPicker = (
    <>
      <Button
        variant="outline"
        onClick={() => {
          setDirectoryOpen(false);
          setTrashOpen(true);
        }}
      >
        项目回收站
      </Button>
      <FormLabel className="project-picker text-xs text-muted-foreground">
        我的故事
        <ChoiceSelect
          value={project ? projectId : ""}
          triggerRef={projectPickerRef}
          onValueChange={select}
          onCloseAutoFocus={(event) => {
            // 等弹层释放焦点锁后，再聚焦切换项目后新挂载的选择器。
            event.preventDefault();
            projectPickerRef.current?.focus({ preventScroll: true });
          }}
          aria-label="选择项目"
          placeholder="选择一个项目"
          disabled={!projects.length && !project}
          options={(project && !projects.some((row) => row.id === project.id)
            ? [project, ...projects]
            : projects
          ).map((row) => ({
            value: row.id,
            label: row.name + (row.archived ? "（归档）" : ""),
          }))}
        />
      </FormLabel>
      <ListPagination
        {...projectList}
        reload={reload}
        label="项目"
        count={projects.length}
      />
      <Button
        variant="outline"
        size="lg"
        className="new-project w-full"
        onClick={() => setCreating(true)}
      >
        ＋ 新建项目
      </Button>
    </>
  );
  return (
    <WorkbenchContext.Provider
      value={{
        project,
        projects,
        loaded,
        error,
        projectPicker,
        directoryOpen,
        setDirectoryOpen,
        onDirectoryOpenAutoFocus: (event) => {
          if (restorePickerFocus.current) {
            event.preventDefault();
            projectPickerRef.current?.focus({ preventScroll: true });
            restorePickerFocus.current = false;
          }
        },
        createProject: () => setCreating(true),
        reloadProjects: reload,
        onProjectSaved: updateProject,
        onDeleted: deleted,
      }}
    >
      <div
        className="application min-h-dvh"
        aria-busy={
          !loaded || projectList.loading || (!projectId && projects.length > 0)
        }
      >
        <header className="app-header flex min-h-16 flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-3 lg:px-6">
          <Link
            className="brand flex items-center gap-3 font-semibold tracking-tight no-underline [&_small]:block [&_small]:text-[10px] [&_small]:font-normal [&_small]:tracking-[.2em] [&_small]:text-muted-foreground"
            href="/"
            aria-label="Arclight 首页"
          >
            <Brand />
          </Link>
          <p className="header-note hidden text-sm text-muted-foreground lg:block">
            给灵感一片自由生长的地方。
          </p>
          <div className="account flex items-center gap-2 text-sm">
            <Button variant="ghost" onClick={() => setServicesOpen(true)}>
              <SlidersHorizontal />
              模型服务配置
            </Button>
            <AccountMenu user={user} logout={logout} />
          </div>
        </header>
        {projectNotice && (
          <Notice className="mx-4 my-3" role="status">
            {projectNotice}
          </Notice>
        )}
        {servicesOpen && (
          <ServiceSettings onDismiss={() => setServicesOpen(false)} />
        )}
        {error && (
          <Notice tone="error" className="global-error mx-4 my-3" role="alert">
            {error}
            <Button
              variant="outline"
              size="lg"
              onClick={() =>
                void reload().catch((error) => setError(message(error)))
              }
            >
              重试载入项目
            </Button>
          </Notice>
        )}
        {children}
        {trashOpen && (
          <ProjectTrash
            onDismiss={() => setTrashOpen(false)}
            onRestored={restored}
          />
        )}
        {creating && (
          <Dialog
            labelledBy="project-title"
            busy={busy}
            onDismiss={() => setCreating(false)}
          >
            <h2 id="project-title">给新故事起个名字</h2>
            <form className="flex min-w-0 flex-col gap-4" onSubmit={create}>
              <FieldGroup className="contents">
                <FormLabel>
                  项目名称
                  <Input
                    name="name"
                    required
                    maxLength={160}
                    autoFocus
                    placeholder="例如：雨巷里的小小奇遇"
                  />
                </FormLabel>
                {error && (
                  <Notice tone="error" className="error" role="alert">
                    {error}
                  </Notice>
                )}
                <div className="button-row flex flex-wrap items-center gap-2">
                  <Button
                    variant="outline"
                    size="lg"
                    type="button"
                    disabled={busy}
                    onClick={() => setCreating(false)}
                  >
                    先不创建
                  </Button>
                  <Button
                    variant="default"
                    size="lg"
                    className=""
                    disabled={busy}
                  >
                    {busy && <Spinner aria-hidden="true" />}
                    {busy ? "创建中…" : "创建项目"}
                  </Button>
                </div>
              </FieldGroup>
            </form>
          </Dialog>
        )}
      </div>
    </WorkbenchContext.Provider>
  );
}
