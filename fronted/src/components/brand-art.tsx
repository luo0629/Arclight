import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Brand({ className, ...props }: ComponentProps<"span">) {
  return (
    <span className={cn("brand-wordmark", className)} {...props}>
      arclight<span className="brand-period">.</span>
    </span>
  );
}

// 原创线描插画仅作装饰，不承载操作或业务状态。
export function StoryDrawing({
  variant = "writer",
  className,
}: {
  variant?: "writer" | "landscape" | "book" | "plane";
  className?: string;
}) {
  return (
    <svg
      className={cn("story-drawing", className)}
      viewBox="0 0 240 190"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      focusable="false"
    >
      <g
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {variant === "writer" ? (
          <>
            <path d="M30 159c31-4 130-4 177 0M54 164l-7 21m137-21 8 21" />
            <path
              d="M64 149c-4-24 5-54 26-61l35 2c17 7 30 23 31 43l-21 15"
              fill="var(--secondary)"
            />
            <path d="M85 91c-3 16 17 27 32 17m-36 3-10 30 24 10m37-38 14 22-25 10" />
            <path d="m92 145 28-3 12 9-39 3m-11-35 8 18" />
            <path
              d="M86 73c-5-21 4-46 26-47 21-1 29 14 26 34l-11 21-15 10-21-4z"
              fill="var(--background)"
            />
            <path
              d="M86 76c-13-13-15-33-3-47 15-18 44-20 54 2 4 8 4 15 1 22-11-3-17-9-22-19-2 16-16 23-30 25z"
              fill="currentColor"
            />
            <path d="m124 52 3 9-6 2m-7 8c3 3 7 2 9-1m-13-21h2" />
            <path
              d="m106 127 9 16m-11-19 3-2 11 20-3 2z"
              fill="var(--accent)"
            />
            <path d="m127 151 44-4-5-28-44 7z" fill="var(--background)" />
            <path d="m129 132 25-4m-24 11 20-3" />
            <path d="M181 147v-26h17v26m-18-18h-4c-8 0-7 13 4 13m4-29c-5-6 4-8 0-14" />
            <path
              d="M43 106c-13-3-18-15-10-18 7-2 10 8 10 8s-2-20 6-19c9 0 3 22 3 22s9-14 14-8c6 8-10 15-10 15"
              fill="var(--accent)"
            />
            <path d="m49 104-2 37m-9-24 9 8 10-11" />
            <path
              d="M22 50c9 2 12 6 13 13 1-8 4-11 12-13-8-1-11-5-12-13-1 8-4 12-13 13z"
              fill="var(--accent)"
            />
            <path d="m166 40 10-7m-8 18 15 1m-21-20 3-10" />
          </>
        ) : variant === "landscape" ? (
          <>
            <path d="M8 175V15h224v160z" fill="var(--secondary)" />
            <circle
              cx="172"
              cy="52"
              r="21"
              fill="var(--accent)"
              stroke="none"
            />
            <path d="m9 137 54-53 41 33 43-43 84 64M9 151c43-24 72 10 115-4s65-23 107 6" />
            <path d="M93 175c36-44 83-18 65-56m-44 56c28-23 67-27 48-50" />
            <path
              d="m38 113 15-37 15 37H38zm15 0v23m131-10 11-30 12 30h-23zm11 0v14"
              fill="var(--background)"
            />
            <path d="M21 34h35m-18-7h32M95 53h26m-15-7h28" />
            <path d="m10 10 5 4m211 159 6 5" />
          </>
        ) : variant === "book" ? (
          <>
            <path
              d="M34 52c25-9 55-2 85 16 30-18 59-25 86-16l-8 108c-25-7-52-2-78 14-27-16-53-21-79-14z"
              fill="var(--secondary)"
            />
            <path d="M119 68v106M46 67c23-4 42 1 58 11m-57 4c22-3 40 2 56 12m-55 4c20-1 38 4 55 12m-54 5c20 0 36 5 54 13m34-46c15-9 30-12 51-11" />
            <path d="m150 99 10 12 22-28" strokeWidth="5" />
            <path d="m172 28 5-17m9 22 12-11m-42 5-5-12" />
            <path
              d="M54 32c14-2 19-7 21-22 2 15 7 20 21 22-14 2-19 7-21 22-2-15-7-20-21-22z"
              fill="var(--accent)"
            />
          </>
        ) : (
          <>
            <path
              d="M22 157c42 17 100-7 88-38-8-20-38-3-22 14 15 17 43 0 61-26"
              strokeDasharray="4 7"
            />
            <path d="m113 67 102-42-39 87-19-36-44-9z" fill="var(--accent)" />
            <path d="m157 76 58-51-72 43m14 8-10 28 29 8m-29-8 17-16" />
            <path d="m70 48 5-13m5 20 13-3" />
          </>
        )}
      </g>
    </svg>
  );
}
