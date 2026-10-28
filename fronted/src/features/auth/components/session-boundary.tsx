"use client";

import OpeningStory from "@/components/feedback/opening-story";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { AuthContext } from "@/features/auth/auth-context";
import {
  api,
  configureAuth,
  json,
  message,
  refreshSession,
} from "@/lib/api/client";
import type { Session, User } from "@/types/api";

import Login from "@/features/auth/components/login";

import { DraftProvider } from "@/providers/draft-provider";

export default function SessionBoundary({ children }: { children: ReactNode }) {
  const router = useRouter();
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
      router.replace("/");
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
        <AuthContext.Provider value={{ user, logout }}>
          <DraftProvider key={user.id} userId={user.id}>
            {children}
          </DraftProvider>
        </AuthContext.Provider>
      ) : (
        <Login onLogin={login} />
      )}
    </TooltipProvider>
  );
}
