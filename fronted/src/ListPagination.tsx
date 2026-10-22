import { Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectGroup,
  SelectItem,
} from "@/components/ui/select";

type Props = {
  label: string;
  count: number;
  loaded: boolean;
  loading: boolean;
  hasMore: boolean;
  error: string;
  batchSize: number;
  setBatchSize: (size: number) => void;
  more: () => Promise<unknown>;
  reload: () => Promise<unknown>;
};
export default function ListPagination({
  label,
  count,
  loaded,
  loading,
  hasMore,
  error,
  batchSize,
  setBatchSize,
  more,
  reload,
}: Props) {
  return (
    <section
      className="list-pagination flex min-w-0 flex-col gap-2 rounded-lg border border-dashed bg-card/80 p-3"
      aria-label={label + "分页"}
      aria-busy={loading}
    >
      <p
        className="hint text-xs leading-relaxed text-muted-foreground"
        role="status"
      >
        {loaded
          ? "已加载 " +
            count +
            " 条" +
            (hasMore ? " · 可继续加载" : " · 已全部加载")
          : "尚未加载"}
        {loading && " · 加载中…"}
      </p>
      {error && (
        <Notice tone="error" className="error" role="alert">
          {error}
        </Notice>
      )}
      <div className="pagination-actions flex min-w-0 flex-wrap items-center gap-2 [&>[data-slot=select-trigger]]:min-w-0 [&>[data-slot=select-trigger]]:max-w-full">
        {hasMore && (
          <>
            <Select
              value={String(batchSize)}
              onValueChange={(value) => setBatchSize(Number(value))}
              disabled={loading}
            >
              <SelectTrigger
                className="w-full"
                aria-label={label + "每次追加条数"}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent
                position="popper"
                sideOffset={6}
                collisionPadding={12}
              >
                <SelectGroup>
                  {[20, 50, 100].map((size) => (
                    <SelectItem key={size} value={String(size)}>
                      <>每次追加 {size} 条</>
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              size="lg"
              disabled={loading}
              onClick={() => void (loaded ? more() : reload()).catch(() => {})}
            >
              {error ? "重试加载" : "加载更多"}
              {label}
            </Button>
          </>
        )}
        <Button
          variant="outline"
          size="lg"
          disabled={loading}
          onClick={() => void reload().catch(() => {})}
        >
          刷新{label}
        </Button>
      </div>
    </section>
  );
}
