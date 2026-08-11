// Knowledge - 上传(文本/文件) + RAG 检索 + 文档管理列表(ADR-0013)
import { useEffect, useRef, useState } from "react";
import {
  Library,
  Upload,
  Search,
  Trash2,
  FileUp,
  RefreshCw,
  RotateCcw,
  X,
  FileText,
} from "lucide-react";
import { knowledge } from "../api/client";
import type { DocumentDetailVO, DocumentVO, KnowledgeSearchHit } from "../api/types";
import { useAgentOps } from "../store/agentOps";
import {
  Button,
  EmptyState,
  Field,
  Input,
  Meter,
  SuccessTick,
  Textarea,
  ToastBar,
  useToast,
} from "../components/ui";
import PageHeader from "../components/PageHeader";

type Tab = "upload" | "search" | "documents";

export default function KnowledgePage() {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const replaceFileRef = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<Tab>("upload");
  const [okMsg, setOkMsg] = useState<string | null>(null);
  const showOk = (m: string) => {
    setOkMsg(m);
    setTimeout(() => setOkMsg(null), 2000);
  };
  // 上传表单
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [project, setProject] = useState("demo");
  const [uploading, setUploading] = useState(false);
  // 检索
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<KnowledgeSearchHit[]>([]);
  // 文档列表
  const [docStatus, setDocStatus] = useState<"ACTIVE" | "DELETED" | "ALL">("ACTIVE");
  const [docs, setDocs] = useState<DocumentVO[]>([]);
  const [docTotal, setDocTotal] = useState(0);
  const [docPage, setDocPage] = useState(1);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [replacingId, setReplacingId] = useState<number | null>(null);
  // 文档详情抽屉
  const [detail, setDetail] = useState<DocumentDetailVO | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const openDetail = async (docId: number) => {
    setLoadingDetail(true);
    try {
      setDetail(await knowledge.getDetail(docId));
    } catch (e) {
      toast.show(e);
    } finally {
      setLoadingDetail(false);
    }
  };
  const closeDetail = () => setDetail(null);

  // ADR-0017：从 Agent Runs 点 doc 节点跳转进来时自动打开该文档详情
  const focusDocId = useAgentOps((s) => s.knowledgeFocusDocId);
  const clearKnowledgeFocus = useAgentOps((s) => s.clearKnowledgeFocus);
  useEffect(() => {
    if (focusDocId) {
      void openDetail(focusDocId);
      clearKnowledgeFocus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusDocId]);

  // 普通函数（不用 useCallback）：避免 toast 引用不稳定导致 useEffect 无限重跑。
  // useEffect 只依赖 [tab, docStatus] 触发；分页/刷新显式调用 loadDocs。
  const loadDocs = async (status: "ACTIVE" | "DELETED" | "ALL", page: number) => {
    setLoadingDocs(true);
    try {
      const data = await knowledge.listDocuments({ status, page, size: 10 });
      setDocs(data.records);
      setDocTotal(data.total);
      setDocPage(data.page);
    } catch (e) {
      toast.show(e);
    } finally {
      setLoadingDocs(false);
    }
  };

  useEffect(() => {
    if (tab === "documents") void loadDocs(docStatus, 1);
    // 只在 tab 或状态过滤切换时加载一次（不依赖不稳定引用）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, docStatus]);

  const upload = async () => {
    if (!title.trim() || !content.trim()) {
      toast.show(new Error("标题与内容不能为空"));
      return;
    }
    setUploading(true);
    try {
      await knowledge.upload({ title, content, source_type: "MANUAL", project_name: project });
      showOk("已上传");
      setTitle("");
      setContent("");
    } catch (e) {
      toast.show(e);
    } finally {
      setUploading(false);
    }
  };

  const uploadFile = async (f: File) => {
    setUploading(true);
    try {
      await knowledge.uploadFile(f, project);
      showOk(`已上传 ${f.name}`);
    } catch (e) {
      toast.show(e);
    } finally {
      setUploading(false);
    }
  };

  const search = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      setHits(await knowledge.search({ query, top_k: 5 }));
    } catch (e) {
      toast.show(e);
    } finally {
      setSearching(false);
    }
  };

  const remove = async (docId: number) => {
    try {
      await knowledge.remove(docId);
      setHits((h) => h.filter((x) => x.document_id !== docId));
      await loadDocs(docStatus, docPage);
    } catch (e) {
      toast.show(e);
    }
  };

  const replaceDoc = async (docId: number, file: File) => {
    setReplacingId(docId);
    try {
      await knowledge.replace(docId, file, project);
      showOk(`已更新文档 #${docId}`);
      await loadDocs(docStatus, docPage);
    } catch (e) {
      toast.show(e);
    } finally {
      setReplacingId(null);
    }
  };

  const switchStatus = (status: "ACTIVE" | "DELETED" | "ALL") => {
    setDocStatus(status);
    setDocPage(1);
    void loadDocs(status, 1);
  };

  const totalPages = Math.max(1, Math.ceil(docTotal / 10));

  return (
    <div className="flex h-full">
      <ToastBar err={toast.err} onClose={toast.clear} />
      {/* 左侧导航 + 上传面板 */}
      <div className="w-80 shrink-0 border-r border-line bg-panel flex flex-col">
        <div className="p-4 pb-0">
          <PageHeader icon={Library} title="KNOWLEDGE" sub="RAG · BM25 + 向量" />
        </div>
        {/* Tab 切换 */}
        <div className="px-4 py-3 flex gap-1">
          {(
            [
              ["upload", "上传"],
              ["search", "检索"],
              ["documents", "文档列表"],
            ] as [Tab, string][]
          ).map(([id, label]) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`flex-1 px-2 py-1.5 rounded font-mono text-2xs uppercase tracking-techy transition-colors ${
                tab === id ? "bg-oxblood text-paper" : "text-mute hover:bg-graph"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="flex-1 p-4 pt-2 overflow-y-auto">
          {tab === "upload" && (
            <div className="space-y-3">
              <Field label="标题">
                <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Redis 缓存最佳实践" />
              </Field>
              <Field label="项目">
                <Input value={project} onChange={(e) => setProject(e.target.value)} />
              </Field>
              <Field label="文档内容" hint="Markdown 可用">
                <Textarea
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  rows={10}
                  placeholder={"## 缓存击穿\n热点 Key 失效方案…"}
                />
              </Field>
              <Button onClick={upload} loading={uploading} className="w-full justify-center">
                <Upload className="w-4 h-4" /> 上传文本
              </Button>
              {okMsg && (
                <div className="flex justify-center">
                  <SuccessTick>{okMsg}</SuccessTick>
                </div>
              )}
              <div className="my-2 border-t border-line" />
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,.docx,.html,.htm,.md,.markdown,.txt"
                className="hidden"
                onChange={(e) => e.target.files?.[0] && uploadFile(e.target.files[0])}
              />
              <Button variant="ghost" onClick={() => fileRef.current?.click()} className="w-full justify-center">
                <FileUp className="w-4 h-4" /> 上传文件 (PDF/DOCX/HTML/MD/TXT)
              </Button>
            </div>
          )}
          {tab === "documents" && (
            <div className="space-y-2">
              {/* 状态过滤 */}
              <div className="flex gap-1">
                {(["ACTIVE", "ALL", "DELETED"] as const).map((s) => (
                  <button
                    key={s}
                    onClick={() => switchStatus(s)}
                    className={`px-2 py-1 rounded font-mono text-2xs tracking-techy transition-colors ${
                      docStatus === s ? "bg-oxblood text-paper" : "text-mute hover:bg-graph"
                    }`}
                  >
                    {s === "ACTIVE" ? "正常" : s === "DELETED" ? "已删除" : "全部"}
                  </button>
                ))}
                <button
                  onClick={() => void loadDocs(docStatus, docPage)}
                  className="ml-auto px-2 py-1 rounded text-mute hover:text-ink"
                  title="刷新"
                >
                  <RefreshCw className="w-3.5 h-3.5" />
                </button>
              </div>
              <p className="font-mono text-2xs text-mute tracking-techy">共 {docTotal} 篇文档</p>
              {loadingDocs ? (
                <p className="font-mono text-2xs text-mute animate-blink">LOADING…</p>
              ) : docs.length === 0 ? (
                <EmptyState icon={<Library className="w-8 h-8" />} title="暂无文档" hint="上传知识文档后在此查看" />
              ) : (
                <div className="space-y-2">
                  {docs.map((d) => (
                    <div key={d.id} className="bg-paper border border-line rounded p-3">
                      <div className="flex items-center justify-between gap-2">
                        <button
                          onClick={() => void openDetail(d.id)}
                          className="text-sm text-ink truncate flex-1 text-left hover:text-oxblood hover:underline"
                          title="查看详情"
                        >
                          {d.title}
                        </button>
                        <span
                          className={`font-mono text-2xs px-1.5 py-0.5 rounded border shrink-0 ${
                            d.status === "ACTIVE"
                              ? "bg-teal/10 text-teal border-teal/30"
                              : "bg-oxblood/10 text-oxblood border-oxblood/30"
                          }`}
                        >
                          {d.status === "ACTIVE" ? "正常" : "已删除"}
                        </span>
                      </div>
                      <div className="flex items-center gap-3 mt-1.5 font-mono text-2xs text-mute">
                        <span>DOC #{d.id}</span>
                        <span>{d.chunk_count} 分块</span>
                        <span>{d.source_type}</span>
                        <span className="truncate">{d.created_at?.slice(0, 10)}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        {d.status === "ACTIVE" && (
                          <>
                            <input
                              ref={replaceFileRef}
                              type="file"
                              accept=".pdf,.docx,.html,.htm,.md,.markdown,.txt"
                              className="hidden"
                              onChange={(e) => e.target.files?.[0] && replaceDoc(d.id, e.target.files[0])}
                            />
                            <button
                              onClick={() => replaceFileRef.current?.click()}
                              disabled={replacingId === d.id}
                              className="inline-flex items-center gap-1 text-2xs text-mute hover:text-ink disabled:opacity-40"
                              title="用新文件更新该文档（删旧分块 + 重新上传）"
                            >
                              <RotateCcw className="w-3 h-3" />
                              {replacingId === d.id ? "更新中…" : "更新"}
                            </button>
                          </>
                        )}
                        <button
                          onClick={() => {
                            if (window.confirm(`确定删除「${d.title}」？将移除该文档的所有分块。`)) void remove(d.id);
                          }}
                          className="inline-flex items-center gap-1 text-2xs text-mute hover:text-oxblood ml-auto"
                          title="删除（软删，可从已删除列表查看）"
                        >
                          <Trash2 className="w-3 h-3" /> 删除
                        </button>
                      </div>
                    </div>
                  ))}
                  {/* 分页 */}
                  {totalPages > 1 && (
                    <div className="flex items-center justify-center gap-2 pt-2">
                      <button
                        onClick={() => void loadDocs(docStatus, Math.max(1, docPage - 1))}
                        disabled={docPage <= 1}
                        className="px-2 py-1 text-2xs text-mute hover:text-ink disabled:opacity-40"
                      >
                        ‹
                      </button>
                      <span className="font-mono text-2xs text-mute">
                        {docPage} / {totalPages}
                      </span>
                      <button
                        onClick={() => void loadDocs(docStatus, Math.min(totalPages, docPage + 1))}
                        disabled={docPage >= totalPages}
                        className="px-2 py-1 text-2xs text-mute hover:text-ink disabled:opacity-40"
                      >
                        ›
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 检索区（tab=search 时显示） */}
      {tab === "search" && (
        <div className="flex-1 overflow-y-auto p-5">
          <div className="max-w-4xl">
            <div className="flex gap-2 mb-5">
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && search()}
                placeholder="检索知识库，如「缓存击穿如何解决」"
              />
              <Button onClick={search} loading={searching}>
                <Search className="w-4 h-4" /> 检索
              </Button>
            </div>

            {hits.length === 0 && !searching ? (
              <EmptyState
                icon={<Library className="w-10 h-10" />}
                title="知识库检索"
                hint="上传文档后输入查询，混合检索（BM25 词法 + 向量语义）返回相关分块。"
              />
            ) : searching ? (
              <p className="font-mono text-2xs text-mute tracking-techy animate-blink">SEARCHING…</p>
            ) : (
              <div className="space-y-3">
                <p className="font-mono text-2xs uppercase tracking-techy text-mute">
                  {hits.length} 条命中 · RRF 融合
                </p>
                {hits.map((h, i) => (
                  <div key={i} className="bg-panel border border-line rounded p-4">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="tag">{h.match_type}</span>
                        <span className="font-mono text-2xs text-mute tracking-techy">
                          DOC #{h.document_id}
                        </span>
                      </div>
                      <button
                        onClick={() => remove(h.document_id)}
                        className="text-mute hover:text-oxblood"
                        title="删除该文档"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                    <div className="flex items-center gap-3 mb-2">
                      <Meter value={h.score} max={1} />
                      <span className="font-mono text-2xs text-mute w-10 text-right">
                        {h.score.toFixed(3)}
                      </span>
                    </div>
                    <p className="font-mono text-2xs text-ink whitespace-pre-wrap leading-relaxed">
                      {h.chunk_content}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 文档详情抽屉（tab=documents 时显示右侧详情区） */}
      {tab === "documents" && (
        <div className="flex-1 border-l border-line bg-paper overflow-y-auto p-5">
          {loadingDetail ? (
            <p className="font-mono text-2xs text-mute animate-blink">LOADING DETAIL…</p>
          ) : detail ? (
            <div className="max-w-3xl">
              <div className="flex items-start justify-between mb-4">
                <div>
                  <div className="flex items-center gap-2">
                    <FileText className="w-4 h-4 text-oxblood" />
                    <h2 className="text-lg font-semibold text-ink">{detail.title}</h2>
                    <span
                      className={`font-mono text-2xs px-1.5 py-0.5 rounded border ${
                        detail.status === "ACTIVE"
                          ? "bg-teal/10 text-teal border-teal/30"
                          : "bg-oxblood/10 text-oxblood border-oxblood/30"
                      }`}
                    >
                      {detail.status === "ACTIVE" ? "正常" : "已删除"}
                    </span>
                  </div>
                  <div className="flex items-center gap-4 mt-2 font-mono text-2xs text-mute">
                    <span>DOC #{detail.id}</span>
                    <span>{detail.chunk_count} 分块</span>
                    <span>{detail.source_type}</span>
                    <span>创建 {detail.created_at?.slice(0, 10)}</span>
                    {detail.deleted_at && <span>删除 {detail.deleted_at.slice(0, 10)}</span>}
                  </div>
                </div>
                <button
                  onClick={closeDetail}
                  className="text-mute hover:text-ink"
                  title="关闭详情"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              {/* 全文 */}
              <div className="mb-5">
                <p className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">
                  全文内容
                </p>
                <div className="bg-panel border border-line rounded p-4 whitespace-pre-wrap font-mono text-2xs text-ink leading-relaxed max-h-72 overflow-y-auto">
                  {detail.content}
                </div>
              </div>

              {/* 分块列表 */}
              <div>
                <p className="font-mono text-2xs uppercase tracking-techy text-mute mb-2">
                  分块 · {detail.chunks.length}（C5 元素感知分块）
                </p>
                <div className="space-y-2">
                  {detail.chunks.map((c) => (
                    <div key={c.chunk_index} className="bg-panel border border-line rounded p-3">
                      <p className="font-mono text-2xs text-mute mb-1">
                        CHUNK #{c.chunk_index}
                      </p>
                      <p className="font-mono text-2xs text-ink whitespace-pre-wrap leading-relaxed">
                        {c.chunk_content}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <EmptyState
              icon={<FileText className="w-10 h-10" />}
              title="文档详情"
              hint="点击左侧文档标题查看全文与分块"
            />
          )}
        </div>
      )}
    </div>
  );
}
