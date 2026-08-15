// Agent Runs - 回放/评审页（ADR-0017 LLMOps 闭环）
// 列表 + 统计条 + 详情回放（时间线 / 流程视图）+ 失败沉淀评审 + 三处跳转
// （doc → Knowledge、对话 → Chat、记忆 → Memory）。
import { useEffect, useMemo, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  MessageSquare,
  RefreshCw,
  Brain,
  X,
} from "lucide-react";
import { agentRuns } from "../api/client";
import type {
  AgentRunDetail,
  AgentRunListItem,
  AgentRunReport,
  AgentRunStats,
  ContextSnapshot,
  TraceEntry,
} from "../api/types";
import { EmptyState, useToast } from "../components/ui";
import PageHeader from "../components/PageHeader";
import FlowTrace from "../components/FlowTrace";
import { buildFlowGraph } from "../components/flowMap";
import { useAgentOps } from "../store/agentOps";
import type { PageId } from "../components/Layout";

type ReviewStatusFilter = "ALL" | "pending" | "accepted" | "rejected";
type ViewMode = "timeline" | "flow";

const TOOL_OPTIONS = ["search_knowledge", "get_document", "list_documents", "calculate", "get_current_time"];

const STATUS_STYLE: Record<string, string> = {
  completed: "text-teal border-teal/30 bg-teal/10",
  empty: "text-amber border-amber/30 bg-amber/10",
  error: "text-oxblood border-oxblood/30 bg-oxblood/10",
  cancelled: "text-mute border-line bg-graph/40",
};

function stepMeta(e: { tokens?: { input: number; output: number }; ms?: number }): string {
  // 元数据扩展：时间线每步追加 token/耗时
  const parts: string[] = [];
  if (e.tokens) parts.push(`⚡${e.tokens.input + e.tokens.output} tok`);
  if (e.ms !== undefined) parts.push(`${e.ms}ms`);
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function Timeline({ trace, onOpenDoc }: { trace: TraceEntry[]; onOpenDoc: (docIds: number[]) => void }) {
  return (
    <div className="space-y-2">
      {trace.map((e, i) => {
        switch (e.type) {
          case "thought":
            return (
              <div key={i} className="rounded border border-line/70 bg-graph/30 px-3 py-2">
                <div className="font-mono text-2xs text-mute">🧠 思考 #{e.step} · {e.chars} 字符{stepMeta(e)}</div>
                {e.reasoning && (
                  <pre className="mt-1 whitespace-pre-wrap text-mute/80 text-2xs max-h-28 overflow-y-auto">{e.reasoning}</pre>
                )}
              </div>
            );
          case "tool_call":
            return (
              <div key={i} className="rounded border border-line/70 bg-panel px-3 py-2">
                <div className="font-mono text-2xs text-ink">
                  🔧 {e.name} <span className="text-mute/70">{JSON.stringify(e.args)}</span>
                </div>
              </div>
            );
          case "tool_result":
            return (
              <div key={i} className="rounded border border-line/70 bg-panel px-3 py-2">
                <div className={`font-mono text-2xs ${e.status === "ok" ? "text-graph" : "text-oxblood"}`}>
                  {e.status === "ok" ? "✓ 观察" : "✗ 观察"}{stepMeta(e)}
                </div>
                <pre className="mt-1 whitespace-pre-wrap text-mute/80 text-2xs max-h-28 overflow-y-auto">{e.result}</pre>
                {e.doc_ids.length > 0 && (
                  <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
                    {e.doc_ids.map((d) => (
                      <button
                        key={d}
                        type="button"
                        onClick={() => onOpenDoc([d])}
                        className="font-mono text-2xs px-1.5 py-0.5 rounded border border-amber/30 text-amber bg-amber/10 hover:bg-amber/20 transition-colors"
                      >
                        📄 doc #{d} →
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          case "answer":
            return (
              <div key={i} className="rounded border border-teal/30 bg-teal/5 px-3 py-2">
                <div className="font-mono text-2xs text-teal">💬 答案</div>
                <pre className="mt-1 whitespace-pre-wrap text-ink text-2xs">{e.content}</pre>
              </div>
            );
          case "convergence_override":
            return (
              <div key={i} className="rounded border border-amber/30 bg-amber/5 px-3 py-2">
                <div className="font-mono text-2xs text-amber">⚠️ 强制终答（忽略 {e.tool_calls?.length ?? 0} 个工具调用）</div>
              </div>
            );
          case "reflection":
            return (
              <div key={i} className="rounded border border-amber/30 bg-amber/5 px-3 py-2">
                <div className="font-mono text-2xs text-amber">
                  🔍 反射判定 #{e.attempt} · {e.accepted ? "✓ 接受" : "✗ 拒绝"}
                  {e.feedback ? ` · ${e.feedback}` : ""}
                  {stepMeta(e)}
                </div>
              </div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}

function ContextSnapshot({ snapshot }: { snapshot: ContextSnapshot | null }) {
  if (!snapshot) return null;
  const hasMemory = (snapshot.memory_refs?.length ?? 0) > 0;
  const hasSummary = Boolean(snapshot.summary);
  const windowCount = snapshot.window?.count ?? 0;
  if (windowCount === 0 && !hasSummary && !hasMemory) {
    return <p className="font-mono text-2xs text-mute/70">本轮无注入记忆/摘要</p>;
  }
  return (
    <div className="space-y-2">
      {windowCount > 0 && (
        <div className="font-mono text-2xs text-mute">短时记忆 · 注入近 {windowCount} 条消息</div>
      )}
      {hasSummary && (
        <details className="rounded border border-line/70 bg-graph/30 px-3 py-2">
          <summary className="font-mono text-2xs text-mute cursor-pointer">增量摘要（短时记忆）</summary>
          <pre className="mt-1 whitespace-pre-wrap text-mute/80 text-2xs max-h-28 overflow-y-auto">{snapshot.summary}</pre>
        </details>
      )}
      {hasMemory && (
        <div className="space-y-1.5">
          {snapshot.memory_refs!.map((m, i) => (
            <div key={i} className="rounded border border-line/70 bg-panel px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="tag">{m.memory_type}</span>
                <span className="font-mono text-2xs text-mute">相似度 {m.similarity.toFixed(3)}</span>
              </div>
              <p className="mt-1 text-xs text-ink">{m.content}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ReportPanel({ report }: { report: AgentRunReport }) {
  const maxCalls = Math.max(1, ...report.tool_usage.map((t) => t.calls));
  const maxDaily = Math.max(1, ...report.daily_trend.map((d) => d.total));
  return (
    <div className="rounded border border-line bg-panel/50 p-4 mb-4 space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="font-mono text-2xs text-mute">
          closure <b className="text-teal">{Math.round(report.closure_rate * 100)}%</b>
        </span>
        <span className="font-mono text-2xs text-mute">
          avg 步 <b className="text-ink">{report.avg_steps}</b>
        </span>
        <span className="font-mono text-2xs text-mute">
          avg 工具 <b className="text-ink">{report.avg_tool_calls}</b>
        </span>
        <span className="font-mono text-2xs text-oxblood">
          异常 run <b className="text-oxblood">{report.error_tool_runs}</b>
        </span>
      </div>

      <div>
        <div className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">工具使用</div>
        {report.tool_usage.length === 0 ? (
          <p className="font-mono text-2xs text-mute/70">暂无工具调用</p>
        ) : (
          <div className="space-y-1.5">
            {report.tool_usage.map((t) => (
              <div key={t.tool} className="flex items-center gap-2">
                <span className="w-36 font-mono text-2xs text-mute truncate">{t.tool}</span>
                <div className="flex-1 h-2 rounded bg-graph/60 overflow-hidden">
                  <div
                    className="h-2 rounded bg-teal"
                    style={{ width: `${(t.calls / maxCalls) * 100}%` }}
                  />
                </div>
                <span className="w-14 text-right font-mono text-2xs text-mute">{t.calls} 次</span>
                {t.errors > 0 && (
                  <span className="w-14 text-right font-mono text-2xs text-oxblood" title="工具结果 error 次数">
                    {t.errors} 错
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">失败沉淀漏斗</div>
        <div className="flex items-center gap-3 flex-wrap">
          {(["pending", "accepted", "rejected", "synced"] as const).map((k) => (
            <span key={k} className="font-mono text-2xs text-mute">
              {k} <b className="text-ink">{report.review_funnel[k] ?? 0}</b>
            </span>
          ))}
        </div>
      </div>

      <div>
        <div className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">近 7 天趋势</div>
        <div className="flex items-end gap-1 h-16">
          {report.daily_trend.map((d) => (
            <div key={d.date} className="flex-1 flex flex-col items-center gap-0.5">
              <div className="flex-1 w-full flex items-end">
                <div
                  className="w-full bg-teal/70 rounded-sm"
                  style={{ height: `${Math.max((d.total / maxDaily) * 100, 3)}%` }}
                  title={`${d.date} · ${d.total} run（${d.completed} 完成 / ${d.error} 错 / ${d.empty} 空）`}
                />
              </div>
              <span className="font-mono text-[9px] text-mute">{d.date.slice(5)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function AgentRunsPage({ onNavigate }: { onNavigate: (p: PageId) => void }) {
  const toast = useToast();
  const focusKnowledgeDoc = useAgentOps((s) => s.focusKnowledgeDoc);
  const focusConversation = useAgentOps((s) => s.focusConversation);

  const [stats, setStats] = useState<AgentRunStats | null>(null);
  const [report, setReport] = useState<AgentRunReport | null>(null);
  const [records, setRecords] = useState<AgentRunListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size] = useState(10);
  const [needsReview, setNeedsReview] = useState<boolean | null>(null);
  const [reviewStatus, setReviewStatus] = useState<ReviewStatusFilter>("ALL");
  const [convFilter, setConvFilter] = useState("");
  const [loading, setLoading] = useState(false);

  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("timeline");
  const [expectedTools, setExpectedTools] = useState<string[]>([]);
  const [category, setCategory] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);

  // 普通函数（不用 useCallback）：useToast 每次渲染返回新对象，放依赖会无限重跑
  //（Memory.tsx 同款坑）。effect 只依赖稳定/离散状态触发；convFilter 由 Enter/刷新显式应用。
  const loadStats = async () => {
    try {
      setStats(await agentRuns.stats());
    } catch (e) {
      toast.show(e);
    }
  };

  const loadReport = async () => {
    try {
      setReport(await agentRuns.report());
    } catch (e) {
      toast.show(e);
    }
  };

  const loadList = async (p: number) => {
    setLoading(true);
    try {
      const data = await agentRuns.list({
        page: p,
        size,
        needs_review: needsReview ?? undefined,
        review_status: reviewStatus === "ALL" ? undefined : reviewStatus,
        conversation_id: convFilter.trim() || undefined,
      });
      setRecords(data.records);
      setTotal(data.total);
      setPage(data.page);
    } catch (e) {
      toast.show(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadStats();
    void loadReport();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 离散筛选（待评审 / 评审状态）变化时自动刷新第一页
  useEffect(() => {
    void loadList(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsReview, reviewStatus]);

  const openDetail = async (turnId: string) => {
    setLoadingDetail(true);
    setViewMode("timeline");
    try {
      setDetail(await agentRuns.detail(turnId));
    } catch (e) {
      toast.show(e);
    } finally {
      setLoadingDetail(false);
    }
  };
  const closeDetail = () => setDetail(null);

  // 打开新详情时重置评审表单
  useEffect(() => {
    setExpectedTools([]);
    setCategory("");
  }, [detail?.turn_id]);

  const reviewRun = async (decision: "accepted" | "rejected") => {
    if (!detail) return;
    if (decision === "accepted" && (expectedTools.length === 0 || !category.trim())) return;
    setReviewBusy(true);
    try {
      await agentRuns.review(detail.turn_id, {
        decision,
        expected_tools: decision === "accepted" ? expectedTools : undefined,
        category: decision === "accepted" ? category.trim() : undefined,
      });
      setDetail((d) =>
        d
          ? {
              ...d,
              review_status: decision,
              expected_tools: decision === "accepted" ? expectedTools : null,
              category: decision === "accepted" ? category.trim() : null,
            }
          : d,
      );
      void loadList(page);
      void loadStats();
    } catch (e) {
      toast.show(e);
    } finally {
      setReviewBusy(false);
    }
  };

  const openDoc = (docIds: number[]) => {
    if (!docIds.length) return;
    focusKnowledgeDoc(docIds[0]);
    onNavigate("knowledge");
  };
  const openConversation = () => {
    if (!detail) return;
    focusConversation(detail.conversation_id);
    onNavigate("chat");
  };

  const flowGraph = useMemo(
    () => (detail ? buildFlowGraph(detail.trace ?? [], detail.query) : { nodes: [], edges: [], stage: new Map<number, string>() }),
    [detail],
  );

  const totalPages = Math.max(1, Math.ceil(total / size));
  const filterBtn = (active: boolean) =>
    `px-2.5 py-1 text-xs font-mono uppercase tracking-techy rounded border transition-colors ${
      active ? "border-oxblood text-oxblood bg-oxblood/5" : "border-line text-mute hover:text-ink"
    }`;

  return (
    <div className="relative h-full overflow-y-auto p-6">
      <PageHeader icon={MessageSquare} title="Agent Runs" sub="回放 · 评审 · 失败沉淀" />

      {stats && (
        <div className="flex items-center gap-3 mt-3 mb-4 flex-wrap">
          <span className="font-mono text-2xs text-mute">总 run <b className="text-ink">{stats.total}</b></span>
          <span className="font-mono text-2xs text-amber">待评审 <b className="text-amber">{stats.needs_review_pending}</b></span>
          {Object.entries(stats.status_counts).map(([k, v]) => (
            <span key={k} className="font-mono text-2xs text-mute">
              {k} <b className="text-ink">{v}</b>
            </span>
          ))}
        </div>
      )}

      {report && <ReportPanel report={report} />}

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        <button type="button" onClick={() => setNeedsReview(null)} className={filterBtn(needsReview === null)}>
          全部
        </button>
        <button type="button" onClick={() => setNeedsReview(true)} className={filterBtn(needsReview === true)}>
          待评审
        </button>
        <select
          value={reviewStatus}
          onChange={(e) => setReviewStatus(e.target.value as ReviewStatusFilter)}
          className="px-2 py-1 text-xs font-mono rounded border border-line text-mute bg-panel"
        >
          <option value="ALL">评审:全部</option>
          <option value="pending">评审:pending</option>
          <option value="accepted">评审:accepted</option>
          <option value="rejected">评审:rejected</option>
        </select>
        <input
          value={convFilter}
          onChange={(e) => setConvFilter(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void loadList(1);
          }}
          placeholder="conversation_id（Enter 应用）"
          className="px-2 py-1 text-xs font-mono rounded border border-line bg-panel text-ink placeholder:text-mute/50"
        />
        <button
          type="button"
          onClick={() => void loadList(1)}
          className="p-1.5 rounded border border-line text-mute hover:text-ink transition-colors"
          title="刷新"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      {loading ? (
        <p className="font-mono text-2xs text-mute tracking-techy animate-blink">LOADING…</p>
      ) : records.length === 0 ? (
        <EmptyState icon={<MessageSquare className="w-10 h-10" />} title="暂无 Agent run" hint="CHAT_MODE=agent 下每轮对话都会记录一条可回放轨迹。" />
      ) : (
        <div className="space-y-3">
          {records.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => void openDetail(r.turn_id)}
              className="w-full text-left bg-panel border border-line rounded p-4 hover:border-teal/50 transition-colors"
            >
              <div className="flex items-center gap-2 mb-1.5 flex-wrap">
                <span className={`px-1.5 py-0.5 rounded border font-mono text-2xs ${STATUS_STYLE[r.status] ?? "text-mute border-line"}`}>
                  {r.status}
                </span>
                <span className="font-mono text-2xs text-mute">stop: {r.stop_reason}</span>
                {r.needs_review && (
                  <span className="px-1.5 py-0.5 rounded border border-amber/30 text-amber bg-amber/10 font-mono text-2xs">
                    待评审
                  </span>
                )}
                <span className="ml-auto font-mono text-2xs text-mute">{r.created_at?.slice(0, 19).replace("T", " ")}</span>
              </div>
              <p className="text-sm text-ink line-clamp-1">{r.query}</p>
              <div className="mt-1.5 flex items-center gap-3 font-mono text-2xs text-mute">
                <span>{r.steps} 步</span>
                <span>{r.tool_calls} 工具</span>
                {r.error_tools > 0 && <span className="text-oxblood">{r.error_tools} 异常</span>}
              </div>
            </button>
          ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 mt-4">
          <button
            type="button"
            onClick={() => void loadList(page - 1)}
            disabled={page <= 1}
            className="p-1.5 rounded border border-line text-mute hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="font-mono text-2xs text-mute">{page} / {totalPages}</span>
          <button
            type="button"
            onClick={() => void loadList(page + 1)}
            disabled={page >= totalPages}
            className="p-1.5 rounded border border-line text-mute hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* 详情回放抽屉 */}
      {detail && (
        <div className="absolute inset-0 z-20 flex justify-end bg-ink/30 backdrop-blur-[1px]" onClick={closeDetail}>
          <div
            className="w-[46rem] max-w-full h-full bg-paper overflow-y-auto p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-3">
              <span className={`px-2 py-0.5 rounded border font-mono text-2xs ${STATUS_STYLE[detail.status] ?? "text-mute border-line"}`}>
                {detail.status}
              </span>
              <span className="font-mono text-2xs text-mute">stop: {detail.stop_reason} · {detail.steps} 步 · {detail.tool_calls} 工具</span>
              {detail.usage && (
                <span className="font-mono text-2xs text-mute">
                  · ⚡{detail.usage.input_tokens + detail.usage.output_tokens} tok · {detail.usage.total_ms}ms
                  · ¥{detail.usage.cost.toFixed(4)} · {detail.usage.model}
                </span>
              )}
              <button
                type="button"
                onClick={openConversation}
                className="ml-auto px-2 py-1 text-xs font-mono rounded border border-teal/40 text-teal hover:bg-teal/10 transition-colors"
              >
                查看对话 →
              </button>
              <button
                type="button"
                onClick={closeDetail}
                className="p-1.5 rounded border border-line text-mute hover:text-ink transition-colors"
                title="关闭"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
            <p className="font-mono text-2xs text-mute mb-1">{detail.turn_id.slice(0, 16)}… · {detail.conversation_id.slice(0, 16)}…</p>
            <p className="text-sm text-ink mb-4">{detail.query}</p>

            {detail.needs_review && detail.review_status === "pending" && (
              <div className="mb-4 rounded border border-amber/30 bg-amber/5 p-3">
                <div className="font-mono text-2xs uppercase tracking-techy text-amber mb-2">失败沉淀评审</div>
                <div className="flex items-center gap-1.5 flex-wrap mb-2">
                  {TOOL_OPTIONS.map((t) => {
                    const on = expectedTools.includes(t);
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() =>
                          setExpectedTools((prev) => (on ? prev.filter((x) => x !== t) : [...prev, t]))
                        }
                        className={`px-2 py-0.5 text-2xs font-mono rounded border transition-colors ${
                          on ? "border-oxblood text-oxblood bg-oxblood/5" : "border-line text-mute hover:text-ink"
                        }`}
                      >
                        {t}
                      </button>
                    );
                  })}
                  <input
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                    placeholder="category（如 need_search）"
                    className="px-2 py-1 text-2xs font-mono rounded border border-line bg-panel text-ink placeholder:text-mute/50"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={reviewBusy || expectedTools.length === 0 || !category.trim()}
                    onClick={() => void reviewRun("accepted")}
                    className="px-3 py-1 text-xs font-mono rounded bg-oxblood text-paper hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    接受为回归样本
                  </button>
                  <button
                    type="button"
                    disabled={reviewBusy}
                    onClick={() => void reviewRun("rejected")}
                    className="px-3 py-1 text-xs font-mono rounded border border-line text-mute hover:text-ink disabled:opacity-30"
                  >
                    拒绝
                  </button>
                </div>
              </div>
            )}

            <div className="mb-4">
              <div className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">本轮上下文快照</div>
              <ContextSnapshot snapshot={detail.context_snapshot} />
              <button
                type="button"
                onClick={() => onNavigate("memory")}
                className="mt-2 px-2 py-1 text-xs font-mono rounded border border-teal/40 text-teal hover:bg-teal/10 transition-colors inline-flex items-center gap-1.5"
              >
                <Brain className="w-3.5 h-3.5" /> 查看记忆 →
              </button>
            </div>

            <div className="mb-3 flex items-center gap-2">
              <div className="font-mono text-2xs uppercase tracking-techy text-mute">回放</div>
              <button
                type="button"
                onClick={() => setViewMode("timeline")}
                className={filterBtn(viewMode === "timeline")}
              >
                时间线
              </button>
              <button
                type="button"
                onClick={() => setViewMode("flow")}
                className={filterBtn(viewMode === "flow")}
              >
                流程
              </button>
            </div>

            {loadingDetail ? (
              <p className="font-mono text-2xs text-mute tracking-techy animate-blink">LOADING…</p>
            ) : viewMode === "flow" ? (
              <FlowTrace graph={flowGraph} onOpenDoc={openDoc} />
            ) : (
              <Timeline trace={detail.trace ?? []} onOpenDoc={openDoc} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
