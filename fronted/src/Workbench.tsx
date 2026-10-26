import { Brand } from "@/components/brand-art";
import { SlidersHorizontal } from "lucide-react";
import AccountMenu from "./AccountMenu";
import { ChoiceSelect } from "@/components/choice-select";
import { Spinner } from "@/components/ui/spinner";
import ServiceSettings from "./ServiceSettings";
import { Notice, FormLabel, EmptyState } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { api, json, message, type Project, type User } from "./api";

import Dialog from "./Dialog";
import usePagedList from "./usePagedList";
import ListPagination from "./ListPagination";

import ProjectTrash from "./ProjectTrash";
import ProjectWorkspace from "./ProjectWorkspace";
export default function Workbench({
  user,
  logout,
}: {
  user: User;
  logout: () => Promise<void>;
}) {
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
  const [projectId, setProjectId] = useState(
    () => localStorage.getItem("arclight-project") ?? "",
  );
  const projectPickerRef = useRef<HTMLButtonElement>(null);
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
    const id = currentProjectId.current;
    if (id && !rows.some((row) => row.id === id)) {
      try {
        const selected = await api<Project>("/projects/" + id);
        if (currentProjectId.current === id) setPinnedProject(selected);
      } catch (error) {
        if (![403, 404].includes((error as { status?: number }).status ?? 0))
          throw error;
        if (currentProjectId.current === id) {
          setPinnedProject(rows[0] ?? null);
          setProjectId(rows[0]?.id ?? "");
        }
      }
    } else {
      setPinnedProject(rows.find((row) => row.id === id) ?? rows[0] ?? null);
      if (!id) setProjectId(rows[0]?.id ?? "");
    }
    setLoaded(true);
    setError("");
  }, [loadProjects]);
  useEffect(() => {
    void reload().catch((error) => setError(message(error)));
  }, [reload]);
  const project = excludedIds.has(projectId)
    ? null
    : (projects.find((row) => row.id === projectId) ??
      (pinnedProject?.id === projectId ? pinnedProject : null));
  function updateProject(updated: Project) {
    projectList.update(updated);
    setPinnedProject(updated);
  }
  function select(id: string) {
    setPinnedProject(projects.find((row) => row.id === id) ?? null);
    currentProjectId.current = id;
    setProjectId(id);
    localStorage.setItem("arclight-project", id);
  }
  function deleted() {
    const id = currentProjectId.current;
    excluded.current.add(id);
    setExcludedIds((previous) => new Set(previous).add(id));
    currentProjectId.current = "";
    setProjectId("");
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
    <div
      className="application min-h-dvh"
      aria-busy={!loaded || projectList.loading}
    >
      <header className="app-header flex min-h-16 flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-3 lg:px-6">
        <a
          className="brand flex items-center gap-3 font-semibold tracking-tight no-underline [&_small]:block [&_small]:text-[10px] [&_small]:font-normal [&_small]:tracking-[.2em] [&_small]:text-muted-foreground"
          href="/"
          aria-label="Arclight 首页"
        >
          <Brand />
        </a>
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
      {project ? (
        <ProjectWorkspace
          key={project.id}
          project={project}
          directoryOpen={directoryOpen}
          setDirectoryOpen={setDirectoryOpen}
          onDeleted={deleted}
          projectPicker={projectPicker}
          reloadProjects={reload}
          onProjectSaved={updateProject}
        />
      ) : (
        <div className="app-shell grid min-h-[calc(100dvh-81px)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)]">
          <aside className="sidebar flex min-w-0 flex-col gap-4 border-b bg-card/70 p-5 lg:border-r lg:border-b-0">
            {projectPicker}
          </aside>
          <EmptyState className="empty welcome-empty min-h-96">
            <p className="eyebrow text-[11px] font-semibold tracking-widest text-primary">
              故事从这里开始
            </p>
            <h1>{loaded ? "新的一页，等你落笔" : "正在找回你的故事…"}</h1>
            <p>为一个小宇宙新建项目，再添加第一集。</p>
            <Button
              variant="default"
              size="lg"
              className=""
              onClick={() => setCreating(true)}
            >
              创建第一个项目
            </Button>
          </EmptyState>
        </div>
      )}
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
  );
}
