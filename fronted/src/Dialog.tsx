import { useRef, type ReactNode } from "react";
import {
  Dialog as DialogRoot,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";

export default function Dialog({
  labelledBy,
  busy,
  onDismiss,
  children,
}: {
  labelledBy: string;
  busy: boolean;
  onDismiss: () => void;
  children: ReactNode;
}) {
  const previous = useRef(
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );
  return (
    <DialogRoot
      open
      onOpenChange={(open) => {
        if (!open && !busy) onDismiss();
      }}
    >
      <DialogContent
        showCloseButton={false}
        aria-labelledby={labelledBy}
        aria-describedby={undefined}
        className="max-h-[90dvh] overflow-y-auto"
        onInteractOutside={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => {
          if (busy) event.preventDefault();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          previous.current?.focus();
        }}
      >
        <DialogTitle className="sr-only">创作内容</DialogTitle>
        {children}
      </DialogContent>
    </DialogRoot>
  );
}
