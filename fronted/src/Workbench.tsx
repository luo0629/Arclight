import { Notice, FormLabel, EmptyState } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
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

import ProjectWorkspace from "./ProjectWorkspace";
export default function Workbench({
  user,
  logout,
}: {
  user: User;
  logout: () => Promise<void>;
}) {
  const projectList = usePagedList<Project>("/projects");
  const { rows: projects, reload: loadProjects } = projectList;
  const [pinnedProject, setPinnedProject] = useState<Project | null>(null);
  const [projectId, setProjectId] = useState(
    () => localStorage.getItem("arclight-project") ?? "",
  );
  const currentProjectId = useRef(projectId);
  currentProjectId.current = projectId;
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    const rows = await loadProjects();
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
  const project =
    projects.find((row) => row.id === projectId) ??
    (pinnedProject?.id === projectId ? pinnedProject : null);
  function updateProject(updated: Project) {
    projectList.update(updated);
    setPinnedProject(updated);
  }
  function select(id: string) {
    setPinnedProject(projects.find((row) => row.id === id) ?? null);
    setProjectId(id);
    localStorage.setItem("arclight-project", id);
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
      <FormLabel className="project-picker text-xs text-muted-foreground">
        我的创作本
        <NativeSelect
          value={projectId}
          onChange={(event) => select(event.target.value)}
          aria-label="选择项目"
        >
          <NativeSelectOption value="" disabled>
            选择一个项目
          </NativeSelectOption>
          {(project && !projects.some((row) => row.id === project.id)
            ? [project, ...projects]
            : projects
          ).map((row) => (
            <NativeSelectOption key={row.id} value={row.id}>
              {row.name}
              {row.archived ? "（归档）" : ""}
            </NativeSelectOption>
          ))}
        </NativeSelect>
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
    <div className="application min-h-dvh">
      <header className="app-header flex min-h-20 flex-wrap items-center justify-between gap-4 border-b bg-card px-5 py-4 lg:px-8">
        <a
          className="brand flex items-center gap-3 font-semibold tracking-tight no-underline [&_small]:block [&_small]:text-[10px] [&_small]:font-normal [&_small]:tracking-[.2em] [&_small]:text-muted-foreground"
          href="/"
          aria-label="Arclight 创作手帐首页"
        >
          <span
            className="brand-mark flex size-10 shrink-0 items-center justify-center rounded-2xl bg-primary font-serif text-3xl italic text-primary-foreground shadow-sm"
            aria-hidden="true"
          >
            a
          </span>
          <span>
            Arclight<small>创作手帐</small>
          </span>
        </a>
        <p className="header-note hidden text-sm text-muted-foreground lg:block">
          让好故事，一格一格发生。
        </p>
        <div className="account flex items-center gap-2 text-sm [&>span:nth-child(2)]:hidden sm:[&>span:nth-child(2)]:inline">
          <span className="avatar flex size-9 shrink-0 items-center justify-center rounded-full bg-secondary text-sm font-semibold text-secondary-foreground">
            {user.display_name.slice(0, 1)}
          </span>
          <span>{user.display_name}</span>
          <Button variant="outline" size="lg" onClick={() => void logout()}>
            退出登录
          </Button>
        </div>
      </header>
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
            <span
              className="big-flower text-5xl text-primary/50"
              aria-hidden="true"
            >
              ✿
            </span>
            <p className="eyebrow text-[11px] font-semibold tracking-widest text-primary">
              故事从这里开始
            </p>
            <h1>{loaded ? "新的一页，等你落笔" : "正在载入你的创作本…"}</h1>
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
