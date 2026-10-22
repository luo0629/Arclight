import { PagePanel } from "@/components/ui-kit";
import { Notice, FormLabel, EmptyState } from "@/components/ui-kit";
import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Button } from "@/components/ui/button";
import { useEffect, useState, type FormEvent } from "react";
import { api, json, message, type Member } from "./api";

export default function Members({
  projectId,
  owner,
  writable,
}: {
  projectId: string;
  owner: boolean;
  writable: boolean;
}) {
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const url = `/projects/${projectId}/members`;
  async function reload() {
    setMembers(await api<Member[]>(url));
    setLoaded(true);
  }
  useEffect(() => {
    let live = true;
    api<Member[]>(url)
      .then((rows) => {
        if (live) {
          setMembers(rows);
          setLoaded(true);
        }
      })
      .catch((error) => {
        if (live) setError(message(error));
      });
    return () => {
      live = false;
    };
  }, [url]);
  async function act(task: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await task();
      await reload();
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    void act(async () => {
      await api(
        url,
        json("POST", { email: data.get("email"), role: data.get("role") }),
      );
      form.reset();
    });
  }
  return (
    <PagePanel
      eyebrow="一起创作"
      title="给故事找几个好伙伴"
      description="Owner 批准关键内容与管理项目；编辑者可编辑和发起任务；查看者只读。添加前，请伙伴先注册账号。"
    >
      {error && (
        <Notice tone="error" className="error" role="alert">
          {error}
        </Notice>
      )}
      {owner && writable && (
        <form
          className="invite-form grid items-end gap-4 md:grid-cols-[1fr_160px_auto]"
          onSubmit={add}
        >
          <FieldGroup className="contents">
            <FormLabel>
              伙伴的邮箱
              <Input
                name="email"
                type="email"
                required
                placeholder="partner@example.com"
              />
            </FormLabel>
            <FormLabel>
              项目角色
              <NativeSelect name="role">
                <NativeSelectOption value="editor">编辑者</NativeSelectOption>
                <NativeSelectOption value="viewer">查看者</NativeSelectOption>
              </NativeSelect>
            </FormLabel>
            <Button variant="default" size="lg" className="" disabled={busy}>
              添加成员
            </Button>
          </FieldGroup>
        </form>
      )}
      {!loaded ? (
        <p role="status">正在寻找伙伴…</p>
      ) : members.length === 0 ? (
        <EmptyState className="empty compact py-8">
          <h3>现在是你的独立创作时间</h3>
          <p>添加伙伴后，他们会出现在这里。项目拥有者不重复列入成员。</p>
        </EmptyState>
      ) : (
        <ul className="member-list flex flex-col gap-3 [&>li]:flex [&>li]:flex-wrap [&>li]:items-center [&>li]:gap-3 [&>li]:rounded-lg [&>li]:border [&>li]:p-4 [&>li>div]:min-w-0 [&>li>div]:flex-1 [&_p]:break-all [&_p]:text-xs [&_p]:text-muted-foreground">
          {members.map((member) => (
            <li key={member.user_id}>
              <span className="avatar flex size-9 shrink-0 items-center justify-center rounded-full bg-secondary text-sm font-semibold text-secondary-foreground">
                {member.display_name.slice(0, 1)}
              </span>
              <div>
                <strong>{member.display_name}</strong>
                <p>{member.email}</p>
              </div>
              {owner && writable ? (
                <>
                  <NativeSelect
                    aria-label={`${member.display_name}的角色`}
                    value={member.role}
                    disabled={busy}
                    onChange={(event) =>
                      void act(() =>
                        api(
                          `${url}/${member.user_id}`,
                          json("PATCH", { role: event.target.value }),
                        ),
                      )
                    }
                  >
                    <NativeSelectOption value="editor">
                      编辑者
                    </NativeSelectOption>
                    <NativeSelectOption value="viewer">
                      查看者
                    </NativeSelectOption>
                  </NativeSelect>
                  <Button
                    variant="outline"
                    size="lg"
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          `移除 ${member.display_name} 的项目访问权限？`,
                        )
                      )
                        void act(() =>
                          api(`${url}/${member.user_id}`, json("DELETE")),
                        );
                    }}
                  >
                    移除
                  </Button>
                </>
              ) : (
                <span>{member.role === "editor" ? "编辑者" : "查看者"}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </PagePanel>
  );
}
