// 侧边栏导航 - 工程仪表台外壳
import { useEffect, useState, type ReactNode } from "react";
import {
  BookOpen,
  Brain,
  FlaskConical,
  Library,
  MessageSquare,
  ScanSearch,
  Settings2,
  Activity,
  LogOut,
} from "lucide-react";
import { useAuth } from "../store/auth";

export type PageId =
  | "chat"
  | "review"
  | "unittest"
  | "readme"
  | "knowledge"
  | "memory"
  | "prompt"
  | "agent-runs";

const NAV: { id: PageId; label: string; icon: typeof MessageSquare; hint: string }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare, hint: "核心域" },
  { id: "review", label: "Code Review", icon: ScanSearch, hint: "七层 Prompt" },
  { id: "unittest", label: "Unit Test", icon: FlaskConical, hint: "单测生成" },
  { id: "readme", label: "AI ReadMe", icon: BookOpen, hint: "项目文档" },
  { id: "knowledge", label: "Knowledge", icon: Library, hint: "RAG 检索" },
  { id: "memory", label: "Memory", icon: Brain, hint: "长期记忆" },
  { id: "prompt", label: "Prompt", icon: Settings2, hint: "模板管理" },
  { id: "agent-runs", label: "Agent Runs", icon: Activity, hint: "回放评审" },
];

export default function Layout({
  active,
  onNavigate,
  children,
}: {
  active: PageId;
  onNavigate: (p: PageId) => void;
  children: ReactNode;
}) {
  const [up, setUp] = useState<boolean | null>(null);
  const [checks, setChecks] = useState<Record<string, string> | null>(null);
  const user = useAuth((s) => s.user);
  const logout = useAuth((s) => s.logout);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const r = await fetch("/health/ready");
        const b = await r.json();
        if (alive) {
          const ready = b.code === 1 && b.data?.status === "ready";
          setUp(ready);
          setChecks(b.data?.checks ?? null);
        }
      } catch {
        if (alive) setUp(false);
      }
    };
    poll();
    const t = setInterval(poll, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-paper">
      {/* 侧栏 */}
      <aside className="w-60 shrink-0 flex flex-col bg-ink text-paper border-r border-ink">
        {/* 品牌标 */}
        <div className="px-4 py-4 border-b border-paper/10">
          <div className="flex items-center gap-2.5">
            <div className="relative w-6 h-6 flex items-center justify-center">
              <div className="absolute inset-0 bg-oxblood rounded-sm" />
              <div className="relative w-3 h-3 border-2 border-paper rounded-full" />
            </div>
            <div>
              <div className="font-mono text-sm font-semibold tracking-techy leading-none">
                CodeAware
              </div>
              <div className="font-mono text-2xs text-paper/40 tracking-techy mt-0.5">
                ENGINEERING DESK
              </div>
            </div>
          </div>
        </div>

        {/* 导航 */}
        <nav className="flex-1 py-3 overflow-y-auto">
          {NAV.map((item) => {
            const Icon = item.icon;
            const on = active === item.id;
            return (
              <button
                key={item.id}
                onClick={() => onNavigate(item.id)}
                className={`group w-full flex items-center gap-3 px-4 py-2.5 text-left transition-colors border-l-2 ${
                  on
                    ? "bg-paper/5 border-oxblood text-paper"
                    : "border-transparent text-paper/55 hover:text-paper hover:bg-paper/5"
                }`}
              >
                <Icon className={`w-4 h-4 shrink-0 ${on ? "text-oxblood-soft" : ""}`} />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium leading-none">{item.label}</div>
                  <div className="font-mono text-2xs text-paper/30 tracking-techy mt-1">
                    {item.hint}
                  </div>
                </div>
              </button>
            );
          })}
        </nav>

        {/* 健康指示 + 用户 */}
        <div className="px-4 py-3 border-t border-paper/10 flex items-center gap-2">
          <Activity
            className={`w-3.5 h-3.5 ${up ? "text-teal" : up === false ? "text-oxblood-soft" : "text-paper/40"}`}
          />
          <span
            className="font-mono text-2xs tracking-techy text-paper/50 flex-1"
            title={
              checks
                ? Object.entries(checks)
                    .map(([k, v]) => `${k}=${v}`)
                    .join("  ")
                : undefined
            }
          >
            {up
              ? "API · ONLINE"
              : up === false
                ? "API · DEGRADED"
                : "API · …"}
          </span>
          {user && (
            <div className="flex items-center gap-2">
              <span className="font-mono text-2xs text-paper/60 truncate max-w-[80px]">
                {user.display_name || user.username}
              </span>
              <button
                onClick={logout}
                title="退出登录"
                className="text-paper/40 hover:text-oxblood-soft transition-colors"
              >
                <LogOut className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* 内容区 */}
      <main className="flex-1 overflow-hidden flex flex-col">{children}</main>
    </div>
  );
}
