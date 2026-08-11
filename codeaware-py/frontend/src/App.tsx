// CodeAware 工程仪表台 - 7 模块 SPA（状态切换视图，无 router）
import { useEffect, useState } from "react";
import Layout, { type PageId } from "./components/Layout";
import ChatPage from "./pages/Chat";
import CodeReviewPage from "./pages/CodeReview";
import UnitTestPage from "./pages/UnitTest";
import AiReadmePage from "./pages/AiReadme";
import KnowledgePage from "./pages/Knowledge";
import MemoryPage from "./pages/Memory";
import PromptPage from "./pages/Prompt";
import AgentRunsPage from "./pages/AgentRuns";
import LoginPage from "./pages/Login";
import { useAuth } from "./store/auth";

export default function App() {
  const [page, setPage] = useState<PageId>("chat");
  const status = useAuth((s) => s.status);
  const bootstrap = useAuth((s) => s.bootstrap);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (status === "loading") {
    return <div className="flex items-center justify-center h-screen bg-ink text-paper/40 font-mono text-xs">加载中…</div>;
  }

  if (status === "unauthed") {
    return <LoginPage />;
  }

  return (
    <Layout active={page} onNavigate={setPage}>
      {page === "chat" && <ChatPage />}
      {page === "review" && <CodeReviewPage />}
      {page === "unittest" && <UnitTestPage />}
      {page === "readme" && <AiReadmePage />}
      {page === "knowledge" && <KnowledgePage />}
      {page === "memory" && <MemoryPage />}
      {page === "prompt" && <PromptPage />}
      {page === "agent-runs" && <AgentRunsPage onNavigate={setPage} />}
    </Layout>
  );
}
