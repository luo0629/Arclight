import { Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

import { useEffect, useState } from "react";
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

import Workbench from "./Workbench";
import { DraftProvider } from "./Drafts";
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
  if (loading)
    return (
      <main
        className="boot-screen flex min-h-dvh flex-col items-center justify-center gap-4 text-sm text-muted-foreground"
        role="status"
      >
        <span className="brand-mark flex size-10 shrink-0 items-center justify-center rounded-2xl bg-primary font-serif text-3xl italic text-primary-foreground shadow-sm">
          a
        </span>
        <p>正在打开你的创作手帐…</p>
      </main>
    );
  return (
    <>
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
        <DraftProvider key={user.id} userId={user.id}>
          <Workbench user={user} logout={logout} />
        </DraftProvider>
      ) : (
        <Login onLogin={login} />
      )}
    </>
  );
}
