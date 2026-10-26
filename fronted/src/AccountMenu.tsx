import { ChevronDown, LogOut } from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { User } from "./api";

export default function AccountMenu({
  user,
  logout,
}: {
  user: User;
  logout: () => Promise<void>;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="h-11 max-w-48 gap-2 px-2"
          aria-label="账号菜单"
        >
          <Avatar aria-hidden="true">
            <AvatarFallback>
              {Array.from(user.display_name)[0] || "我"}
            </AvatarFallback>
          </Avatar>
          <span className="hidden truncate sm:inline">{user.display_name}</span>
          <ChevronDown className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        sideOffset={10}
        collisionPadding={12}
        className="w-64 max-w-[calc(100vw-24px)]"
      >
        <DropdownMenuLabel className="flex flex-col gap-1">
          <span className="truncate">{user.display_name}</span>
          <span className="truncate text-xs font-normal text-muted-foreground">
            {user.email}
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem onSelect={() => void logout()}>
            <LogOut />
            退出登录
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
