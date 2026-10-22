import type { ComponentProps, ReactNode } from "react";
import { Alert, AlertDescription } from "./ui/alert";
import { Badge } from "./ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "./ui/card";
import { Empty, EmptyHeader } from "./ui/empty";
import { Field, FieldLabel } from "./ui/field";
import { Skeleton } from "./ui/skeleton";
import { cn } from "@/lib/utils";
import { statusText } from "@/api";

export function Notice({
  tone = "info",
  className,
  children,
  role,
  ...props
}: ComponentProps<"div"> & { tone?: "info" | "error" | "success" }) {
  return (
    <Alert
      variant={tone === "error" ? "destructive" : "default"}
      role={role ?? (tone === "error" ? "alert" : "status")}
      className={cn(
        tone === "success" && "border-success/30 bg-success/10 text-success",
        className,
      )}
      {...props}
    >
      <AlertDescription className="flex flex-col gap-2 text-inherit">
        {children}
      </AlertDescription>
    </Alert>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge
      variant="outline"
      className={cn(
        "status w-fit shrink-0",
        ["APPROVED", "COMPLETED"].includes(status) &&
          "border-success/20 bg-success/10 text-success",
        ["READY_FOR_REVIEW", "WAITING_REVIEW"].includes(status) &&
          "border-primary/20 bg-accent text-accent-foreground",
        ["FAILED", "REJECTED"].includes(status) &&
          "border-destructive/20 bg-destructive/10 text-destructive",
      )}
    >
      {statusText[status] ?? status}
    </Badge>
  );
}

// 同时兼容包裹控件的标签与 htmlFor 标签，保持既有表单的可访问名称。
export function FormLabel({
  className,
  ...props
}: ComponentProps<typeof FieldLabel>) {
  return (
    <Field>
      <FieldLabel
        className={cn(
          "flex-col items-stretch gap-2 leading-relaxed",
          className,
        )}
        {...props}
      />
    </Field>
  );
}

export function PagePanel({
  eyebrow,
  title,
  description,
  children,
  className,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("page-panel gap-6 shadow-none", className)}>
      <CardHeader>
        {eyebrow && (
          <p className="text-xs font-semibold tracking-widest text-primary">
            {eyebrow}
          </p>
        )}
        <CardTitle role="heading" aria-level={2} className="text-xl">
          {title}
        </CardTitle>
        {description && (
          <CardDescription className="leading-relaxed">
            {description}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-5">
        {children}
      </CardContent>
    </Card>
  );
}

export function EmptyState({
  children,
  className,
  ...props
}: ComponentProps<"div">) {
  return (
    <Empty className={cn("empty min-w-0 py-12", className)} {...props}>
      <EmptyHeader className="max-w-lg [&_h1]:text-2xl [&_h2]:text-xl [&_h3]:text-lg [&_p]:text-sm [&_p]:leading-relaxed [&_p]:text-muted-foreground">
        {children}
      </EmptyHeader>
    </Empty>
  );
}

export function LoadingState({ children }: { children: ReactNode }) {
  return (
    <div
      role="status"
      className="flex flex-col gap-3 py-6 text-sm text-muted-foreground"
    >
      <Skeleton className="h-4 w-2/3" />
      <Skeleton className="h-4 w-1/2" />
      {children}
    </div>
  );
}
