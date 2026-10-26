import { TooltipProvider } from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { Brand } from "@/components/brand-art";
import { Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

import { lazy, Suspense, useEffect, useState } from "react";
import {
  api,
  configureAuth,
  json,
  message,
  refreshSession,
  type Session,
  type User,
} from "./api";

import Login from "./Login";

import { DraftProvider } from "./Drafts";

const Workbench = lazy(() => import("./Workbench"));
export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionError, setSessionError] = useState("");
  useEffect(() => {
    let live = true;
    configureAuth("", () => {
      setUser(null);
      setSessionError("登录已失效，请重新登录。");
    });
    refreshSession()
      .then((session) => {
        if (live) setUser(session.user);
      })
      .catch((error) => {
        if (live && error.status !== 401) setSessionError(message(error));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, []);
  function login(session: Session) {
    configureAuth(session.access_token);
    setUser(session.user);
    setSessionError("");
  }
  async function logout() {
    try {
      await api("/auth/logout", json("POST"));
      configureAuth("");
      setUser(null);
    } catch (error) {
      setSessionError(message(error));
    }
  }
  if (loading) return <OpeningStory />;
  return (
    <TooltipProvider delayDuration={400}>
      {sessionError && (
        <Notice tone="error" className="global-error mx-4 my-3" role="alert">
          {sessionError}
          <Button
            variant="outline"
            size="lg"
            onClick={() => setSessionError("")}
          >
            关闭提示
          </Button>
        </Notice>
      )}
      {user ? (
        <Suspense fallback={<OpeningStory />}>
          <DraftProvider key={user.id} userId={user.id}>
            <Workbench user={user} logout={logout} />
          </DraftProvider>
        </Suspense>
      ) : (
        <Login onLogin={login} />
      )}
    </TooltipProvider>
  );
}

function OpeningStory() {
  return (
    <main
      className="boot-screen flex min-h-dvh flex-col items-center justify-center gap-4 text-sm text-muted-foreground"
      role="status"
    >
      <Brand />
      <p>正在打开你的故事…</p>
      <div className="flex w-52 flex-col items-center gap-3" aria-hidden="true">
        <Skeleton className="h-2 w-full" />
        <Skeleton className="h-2 w-32" />
      </div>
    </main>
  );
}
