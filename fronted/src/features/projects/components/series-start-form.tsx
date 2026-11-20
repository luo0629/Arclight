"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormLabel, Notice } from "@/components/ui-kit";
import { useAuth } from "@/features/auth/auth-context";
import useProducerIntent from "@/features/tasks/hooks/use-producer-intent";
import type { SeriesStartPayload } from "@/features/tasks/producer-types";
import { episodePath, isResourceId } from "@/lib/routes";
import type { Episode } from "@/types/api";
import type { SeriesWorkspace } from "../series-types";

type Selected = { episode: Episode; requires: string[] };

export default function SeriesStartForm({
  workspace,
  episodes,
  writable,
  fresh,
  onStarted,
}: {
  workspace: SeriesWorkspace;
  episodes: Episode[];
  writable: boolean;
  fresh: boolean;
  onStarted: () => void;
}) {
  const { user } = useAuth();
  const intent = useProducerIntent({
    userId: user.id,
    projectId: workspace.project_id,
    episodeId: "",
    seriesStart: true,
    writable,
  });
  const [selected, setSelected] = useState<Selected[]>([]);
  const [goal, setGoal] = useState("");
  const [budget, setBudget] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [opened, setOpened] = useState<string | null>(null);
  const plan = workspace.plan;
  const count = plan?.content.target_episode_count ?? 0;
  const eligible = episodes.filter(
    (row) =>
      row.project_id === workspace.project_id &&
      row.status !== "ARCHIVED" &&
      row.episode_no <= count,
  );
  const locked =
    !writable || !intent.ready || intent.mutating || !!intent.pending;
  const validBudget = /^\d{1,12}(\.\d{1,6})?$/.test(budget);

  function toggle(episode: Episode) {
    setConfirmed(false);
    setSelected((rows) =>
      rows.some((row) => row.episode.id === episode.id)
        ? rows
            .filter((row) => row.episode.id !== episode.id)
            .map((row) => ({
              ...row,
              requires: row.requires.filter((id) => id !== episode.id),
            }))
        : [...rows, { episode, requires: [] }],
    );
  }

  function setRequirement(
    targetId: string,
    sourceId: string,
    checked: boolean,
  ) {
    setConfirmed(false);
    setSelected((rows) =>
      rows.map((row) =>
        row.episode.id !== targetId
          ? row
          : {
              ...row,
              requires: checked
                ? [...row.requires, sourceId]
                : row.requires.filter((id) => id !== sourceId),
            },
      ),
    );
  }

  async function send(payload: SeriesStartPayload | null) {
    const previous = intent.pending
      ? (JSON.parse(intent.pending.body) as SeriesStartPayload)
      : null;
    const anchor = (payload ?? previous)?.scope?.episodes?.[0]?.episode_id;
    if (!anchor || !isResourceId(anchor)) return;
    const saved = payload
      ? await intent.submit({ kind: "SERIES_START", payload })
      : await intent.replay();
    if (!saved) return;
    setOpened(
      `${episodePath(workspace.project_id, anchor, "tasks")}&run=${saved.run_id}`,
    );
    setSelected([]);
    setConfirmed(false);
    setGoal("");
    setBudget("");
    onStarted();
  }

  if (!writable)
    return <Notice>当前只能查看。需由可编辑项目的拥有者发起多集制作。</Notice>;
  return (
    <section aria-label="按批准范围发起多集制作" className="grid min-w-0 gap-3">
      {intent.error && <Notice tone="error">{intent.error}</Notice>}
      {intent.pending && (
        <Notice>
          发起结果尚未确认。核对原请求，不会再创建一个新请求。
          <Button
            type="button"
            variant="outline"
            disabled={intent.mutating}
            onClick={() => void send(null)}
          >
            重发原多集发起
          </Button>
        </Notice>
      )}
      {opened && (
        <Notice>
          多集制作已提交。
          <Link className="underline" href={opened}>
            打开本次多集运行
          </Link>
        </Notice>
      )}
      {plan && (
        <form
          className="grid min-w-0 gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (
              locked ||
              !fresh ||
              !confirmed ||
              !selected.length ||
              !goal.trim() ||
              !validBudget
            )
              return;
            void send({
              goal: goal.trim(),
              limits: { maximum_cost: budget, max_duration_seconds: 3600 },
              scope: {
                series_plan_version_id: plan.id,
                ledger_version_id: workspace.ledger_version_id,
                expected_ledger_head_revision: workspace.ledger_head_revision,
                confirmation: `确认按规划第 ${plan.version_no} 版制作所列剧集，预算上限 ${budget} USD；批次成果不等于全剧交付。`,
                episodes: selected.map((row) => ({
                  episode_id: row.episode.id,
                  requires_episode_ids: row.requires,
                })),
              },
            });
          }}
        >
          <div className="space-y-2">
            <h4 className="text-sm font-medium">选择制作剧集</h4>
            <p className="text-sm text-muted-foreground">
              只列出当前总览页中符合规划的未归档剧集。可在下方剧集目录翻页；已选剧集仍保留。
            </p>
            {!!eligible.length ? (
              <div className="flex flex-wrap gap-2">
                {eligible.map((row) => (
                  <Button
                    type="button"
                    variant="outline"
                    key={row.id}
                    aria-pressed={selected.some(
                      ({ episode }) => episode.id === row.id,
                    )}
                    disabled={locked}
                    onClick={() => toggle(row)}
                  >
                    第 {row.episode_no} 集 · {row.title}
                  </Button>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                本页没有可加入制作范围的剧集。
              </p>
            )}
          </div>
          {!!selected.length && (
            <ol
              className="grid list-decimal gap-2 pl-5 text-sm"
              aria-label="已选制作顺序"
            >
              {selected.map(({ episode, requires }, index) => (
                <li key={episode.id} className="space-y-1">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      第 {episode.episode_no} 集 · {episode.title} ·{" "}
                      {episode.target_duration_ms / 1000} 秒
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={locked}
                      onClick={() => toggle(episode)}
                    >
                      移出范围
                    </Button>
                  </div>
                  {index > 0 && (
                    <fieldset
                      className="flex flex-wrap gap-2"
                      disabled={locked}
                    >
                      <legend className="text-xs text-muted-foreground">
                        仅勾选实际制作前置
                      </legend>
                      {selected.slice(0, index).map(({ episode: source }) => (
                        <label
                          key={source.id}
                          className="flex items-center gap-1.5"
                        >
                          <input
                            type="checkbox"
                            checked={requires.includes(source.id)}
                            onChange={(event) =>
                              setRequirement(
                                episode.id,
                                source.id,
                                event.target.checked,
                              )
                            }
                          />
                          等待第 {source.episode_no} 集确认
                        </label>
                      ))}
                    </fieldset>
                  )}
                </li>
              ))}
            </ol>
          )}
          <FormLabel>
            制作目标
            <Textarea
              disabled={locked}
              value={goal}
              required
              maxLength={4000}
              onChange={(event) => {
                setGoal(event.target.value);
                setConfirmed(false);
              }}
            />
          </FormLabel>
          <FormLabel>
            跨集预算上限（USD）
            <Input
              disabled={locked}
              value={budget}
              inputMode="decimal"
              required
              maxLength={19}
              onChange={(event) => {
                setBudget(event.target.value);
                setConfirmed(false);
              }}
            />
          </FormLabel>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              disabled={locked}
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            确认上述范围和预算；所有集共用同一上限及截止时间。
          </label>
          <Button
            className="justify-self-start"
            type="submit"
            disabled={
              locked ||
              !fresh ||
              !confirmed ||
              !selected.length ||
              !validBudget ||
              !goal.trim()
            }
          >
            发起多集制作
          </Button>
        </form>
      )}
    </section>
  );
}
