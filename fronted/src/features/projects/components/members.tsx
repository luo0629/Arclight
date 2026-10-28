"use client";

import { PagePanel } from "@/components/ui-kit";
import { Notice, FormLabel, EmptyState } from "@/components/ui-kit";
import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ChoiceSelect } from "@/components/choice-select";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import { useEffect, useState, type FormEvent } from "react";
import { api, json, message } from "@/lib/api/client";
import type { Member } from "@/types/api";

const roles = [
  { value: "editor", label: "编辑者" },
  { value: "viewer", label: "查看者" },
];

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
  const [inviteRole, setInviteRole] = useState("editor");
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
      setInviteRole("editor");
    });
  }
  return (
    <PagePanel
      eyebrow="一起创作"
      title="给故事找几个好伙伴"
      description="邀请伙伴一起打磨故事。拥有者负责审核与管理，编辑者可以创作，查看者可以阅读。邀请前，请伙伴先注册账号。"
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
              <ChoiceSelect
                name="role"
                aria-label="项目角色"
                value={inviteRole}
                onValueChange={setInviteRole}
                options={roles}
                disabled={busy}
              />
            </FormLabel>
            <Button variant="default" size="lg" className="" disabled={busy}>
              {busy && <Spinner aria-hidden="true" />}
              添加成员
            </Button>
          </FieldGroup>
        </form>
      )}
      {!loaded ? (
        <div role="status" className="flex flex-col gap-3">
          <span className="sr-only">正在寻找伙伴…</span>
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} aria-hidden="true" className="h-20 w-full" />
          ))}
        </div>
      ) : members.length === 0 ? (
        <EmptyState className="empty compact py-8">
          <h3>现在是你的独立创作时间</h3>
          <p>添加伙伴后，他们会出现在这里。项目拥有者不重复列入成员。</p>
        </EmptyState>
      ) : (
        <ul className="member-list flex flex-col gap-3 [&>li]:flex [&>li]:flex-wrap [&>li]:items-center [&>li]:gap-3 [&>li]:rounded-lg [&>li]:border [&>li]:p-4 [&>li>div]:min-w-0 [&>li>div]:flex-1 [&_p]:break-all [&_p]:text-xs [&_p]:text-muted-foreground">
          {members.map((member) => (
            <li key={member.user_id}>
              <Avatar aria-hidden="true">
                <AvatarFallback>
                  {Array.from(member.display_name)[0] || "我"}
                </AvatarFallback>
              </Avatar>
              <div>
                <strong>{member.display_name}</strong>
                <p>{member.email}</p>
              </div>
              {owner && writable ? (
                <>
                  <ChoiceSelect
                    aria-label={`${member.display_name}的角色`}
                    value={member.role}
                    disabled={busy}
                    className="w-36"
                    options={roles}
                    onValueChange={(role) =>
                      void act(() =>
                        api(
                          `${url}/${member.user_id}`,
                          json("PATCH", { role }),
                        ),
                      )
                    }
                  />
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
