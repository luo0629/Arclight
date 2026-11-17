"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormLabel } from "@/components/ui-kit";
import type { GenerationRoute } from "../shot-performance";

export default function StoryboardPerformanceControls({
  schemaVersion,
  routes,
  editing,
  sourceLoaded,
  onRoutes,
  onEnable,
}: {
  schemaVersion?: string;
  routes: GenerationRoute[];
  editing: boolean;
  sourceLoaded: boolean;
  onRoutes: (routes: GenerationRoute[]) => void;
  onEnable: () => void;
}) {
  if (schemaVersion !== "3.0")
    return editing ? (
      <div className="flex min-w-0 flex-col gap-2 text-sm">
        <Button
          type="button"
          variant="outline"
          disabled={!sourceLoaded}
          onClick={onEnable}
        >
          启用表演分镜
        </Button>
        <p>
          显式启用后填写动作起止、接续及精确台词表演，再另存待审核版本。
          {!sourceLoaded && "请先加载精确来源剧本。"}
        </p>
      </div>
    ) : null;
  return (
    <section
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-3 text-sm"
      aria-label="表演分镜制作路线"
    >
      <h4 className="font-medium">表演分镜 · 3.0</h4>
      <p>
        保存为待审核版本。普通批准须通过真实生成能力审核；也可填写理由，仅确认表演意图并保留制作阻塞。必须动态镜头不能改用静图运镜代替。
      </p>
      <p>
        接续关系绑定精确镜头；删除已被接续的镜头前，请先修改后镜的接续来源。
      </p>
      {routes.map((route, index) =>
        editing ? (
          <div
            key={index}
            className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2"
          >
            <FormLabel>
              路线 {index + 1} · Provider
              <Input
                value={route.provider}
                maxLength={100}
                onChange={(event) =>
                  onRoutes(
                    routes.map((row, i) =>
                      i === index
                        ? { ...row, provider: event.target.value }
                        : row,
                    ),
                  )
                }
              />
            </FormLabel>
            <FormLabel>
              路线 {index + 1} · 模型
              <Input
                value={route.model}
                maxLength={200}
                onChange={(event) =>
                  onRoutes(
                    routes.map((row, i) =>
                      i === index ? { ...row, model: event.target.value } : row,
                    ),
                  )
                }
              />
            </FormLabel>
            <Button
              type="button"
              variant="outline"
              onClick={() => onRoutes(routes.filter((_, i) => i !== index))}
            >
              移除路线 {index + 1}
            </Button>
          </div>
        ) : (
          <p key={index} className="break-all">
            生成路线：{route.provider} / {route.model}
          </p>
        ),
      )}
      {editing && (
        <Button
          type="button"
          variant="outline"
          disabled={routes.length >= 10}
          onClick={() => onRoutes([...routes, { provider: "", model: "" }])}
        >
          添加候选生成路线
        </Button>
      )}
      {!routes.length && (
        <p>
          未指定候选路线；服务端仍须查询当前真实能力，不能据此假定动态或口型可用。
        </p>
      )}
    </section>
  );
}
