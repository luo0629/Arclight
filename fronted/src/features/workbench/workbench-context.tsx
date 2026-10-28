"use client";

import { createContext, useContext, type ReactNode } from "react";
import type { Project } from "@/types/api";

type WorkbenchContextValue = {
  project: Project | null;
  projects: Project[];
  loaded: boolean;
  error: string;
  projectPicker: ReactNode;
  directoryOpen: boolean;
  setDirectoryOpen: (open: boolean) => void;
  onDirectoryOpenAutoFocus: (event: Event) => void;
  createProject: () => void;
  reloadProjects: () => Promise<void>;
  onProjectSaved: (project: Project) => void;
  onDeleted: () => void;
};

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(
  null,
);

export function useWorkbench() {
  const context = useContext(WorkbenchContext);
  if (!context) throw new Error("Workbench layout is required");
  return context;
}
