// AgentOps 跨页跳转目标（ADR-0017）- zustand
// Agent Runs 详情里点 doc 节点 → Knowledge 页打开该文档；点"查看对话" → Chat 页打开该会话。
// 目标页面 mount 时读 store 消费 focus 值（消费后不自动清，避免切页丢失；由目标页自行处理）。
import { create } from "zustand";

interface AgentOpsState {
  knowledgeFocusDocId: number | null;
  conversationFocusId: string | null;
  focusKnowledgeDoc: (docId: number) => void;
  focusConversation: (conversationId: string) => void;
  clearKnowledgeFocus: () => void;
  clearConversationFocus: () => void;
}

export const useAgentOps = create<AgentOpsState>((set) => ({
  knowledgeFocusDocId: null,
  conversationFocusId: null,
  focusKnowledgeDoc: (docId) => set({ knowledgeFocusDocId: docId }),
  focusConversation: (conversationId) => set({ conversationFocusId: conversationId }),
  clearKnowledgeFocus: () => set({ knowledgeFocusDocId: null }),
  clearConversationFocus: () => set({ conversationFocusId: null }),
}));
