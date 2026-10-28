import type { ReactNode } from "react";
import SessionBoundary from "@/features/auth/components/session-boundary";
import Workbench from "@/features/workbench/components/workbench";

export default function WorkbenchLayout({ children }: { children: ReactNode }) {
  return (
    <SessionBoundary>
      <Workbench>{children}</Workbench>
    </SessionBoundary>
  );
}
