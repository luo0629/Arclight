"use client";

import { createContext, useContext } from "react";
import type { User } from "@/types/api";

export const AuthContext = createContext<{
  user: User;
  logout: () => Promise<void>;
} | null>(null);

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("Authenticated layout is required");
  return context;
}
