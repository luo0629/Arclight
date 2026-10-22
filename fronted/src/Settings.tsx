import { PagePanel } from "@/components/ui-kit";
import { Notice, FormLabel } from "@/components/ui-kit";
import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useState, type FormEvent } from "react";
import EpisodeEditor from "./EpisodeEditor";
import ProjectProductionSettings from "./ProjectProductionSettings";
import { api, json, message, type Episode, type Project } from "./api";

export default function Settings({
  project,
  episodes,
  pagination,
  reload,
  onEpisodeSaved,
  onProjectSaved,
}: {
  project: Project;
  episodes: Episode[];
  pagination: React.ReactNode;
  onProjectSaved: (project: Project) => void;
  onEpisodeSaved: (episode: Episode) => void;
  reload: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function change(path: string, body: unknown) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(path, json("PATCH", body));
      await reload();
      setNotice("设置已保存");
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void change(`/projects/${project.id}`, {
      name: data.get("name"),
      budget_limit: data.get("budget"),
    });
  }
  return (
    <PagePanel
      eyebrow="收好每一个小宇宙"
      title="项目设置"
      description="归档不会删除历史版本；归档前请先完成或取消所有进行中的任务。预算当前仅保存额度，尚未执行模型费用扣费。"
    >
      {error && (
        <Notice tone="error" className="error" role="alert">
          {error}
        </Notice>
      )}
      {notice && (
        <Notice tone="success" className="success" role="status">
          {notice}
        </Notice>
      )}
      <form
        className="settings-form grid items-end gap-4 md:grid-cols-[1fr_1fr_auto]"
        onSubmit={save}
      >
        <FieldGroup className="contents">
          <FormLabel>
            项目名称
            <Input
              name="name"
              key={project.name}
              defaultValue={project.name}
              maxLength={160}
              required
              disabled={busy || project.archived}
            />
          </FormLabel>
          <FormLabel>
            预算额度
            <Input
              name="budget"
              type="number"
              defaultValue={project.budget_limit}
              min="0"
              max="9999999999.99"
              step="0.01"
              required
              disabled={busy || project.archived}
            />
          </FormLabel>
          <Button
            variant="default"
            size="lg"
            className=""
            disabled={busy || project.archived}
          >
            保存设置
          </Button>
        </FieldGroup>
      </form>
      <ProjectProductionSettings
        project={project}
        disabled={busy}
        onBusyChange={setBusy}
        onSaved={onProjectSaved}
      />
      <section className="settings-section flex min-w-0 flex-col gap-4 rounded-xl border bg-background/60 p-4">
        <h3>{project.archived ? "恢复这本创作手帐" : "归档项目"}</h3>
        <p>归档期间，所有成员只能查看内容。</p>
        <Button
          variant="outline"
          size="lg"
          disabled={busy}
          onClick={() => {
            if (
              window.confirm(
                project.archived
                  ? "恢复项目并允许继续编辑？"
                  : "归档此项目并将全部内容设为只读？",
              )
            )
              void change(`/projects/${project.id}`, {
                archived: !project.archived,
              });
          }}
        >
          {project.archived ? "恢复项目" : "归档项目"}
        </Button>
      </section>
      <section className="settings-section flex min-w-0 flex-col gap-4 rounded-xl border bg-background/60 p-4">
        <h3>剧集管理</h3>
        <ul className="episode-settings flex flex-col gap-3 [&>li]:flex [&>li]:flex-wrap [&>li]:items-center [&>li]:justify-between [&>li]:gap-3 [&>li]:rounded-lg [&>li]:border [&>li]:bg-card [&>li]:p-4 [&_small]:block">
          {episodes.map((episode) => (
            <li key={episode.id}>
              <span>
                第 {episode.episode_no} 集 · {episode.title}
                <small className="muted text-sm text-muted-foreground">
                  {episode.target_duration_ms / 1000} 秒目标时长
                </small>
              </span>
              <div className="episode-settings-actions flex flex-wrap items-center gap-2">
                <EpisodeEditor
                  episode={episode}
                  disabled={
                    busy || project.archived || episode.status === "ARCHIVED"
                  }
                  onSaved={onEpisodeSaved}
                />
                <Button
                  variant="outline"
                  size="lg"
                  disabled={busy || project.archived}
                  onClick={() => {
                    if (
                      window.confirm(
                        episode.status === "ARCHIVED"
                          ? "恢复这集？"
                          : "归档这集并设为只读？",
                      )
                    )
                      void change(`/episodes/${episode.id}`, {
                        archived: episode.status !== "ARCHIVED",
                      });
                  }}
                >
                  {episode.status === "ARCHIVED" ? "恢复剧集" : "归档剧集"}
                </Button>
              </div>
            </li>
          ))}
        </ul>
        {pagination}
      </section>
    </PagePanel>
  );
}
