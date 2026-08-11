// archMap 事件→模块映射纯函数测试（Agent 模式架构图高亮）
// 覆盖：入口/guardrail 点亮、记忆召回条件点亮、search 展开检索栈、calculate 不展开、
// 完成点亮输出、错误路径标 error。
import { describe, expect, it } from "vitest";
import {
  archOnCompleted,
  archOnModel,
  archOnReferences,
  archOnStarted,
  archOnToolCall,
  ARCH_ERROR_IDS,
  RETRIEVAL_STACK,
} from "./archMap";
import type { ContextReferences } from "../api/chatEvents";

function refs(memory = false): ContextReferences {
  return {
    protocol_version: 1,
    conversation_id: "c",
    turn_id: "t",
    sequence: 1,
    knowledge_refs: [],
    memory_refs: memory ? [{ content: "m", memory_type: "FACT", similarity: 0.9 }] : [],
  } as ContextReferences;
}

describe("archMap 事件→模块映射", () => {
  it("started 点亮 输入/guardrail/coordinator", () => {
    expect(archOnStarted()).toEqual(["input", "guardrail", "coordinator"]);
  });

  it("references 点亮 context；有记忆召回追加 memory", () => {
    expect(archOnReferences(refs(false))).toEqual(["context"]);
    expect(archOnReferences(refs(true))).toEqual(["context", "memory"]);
  });

  it("模型事件点亮 llm", () => {
    expect(archOnModel()).toEqual(["llm"]);
  });

  it("tool.call search_knowledge 展开整个检索栈", () => {
    const lit = archOnToolCall("search_knowledge");
    expect(lit).toContain("toolkit");
    expect(lit).toContain("tool:search_knowledge");
    for (const id of RETRIEVAL_STACK) expect(lit).toContain(id);
  });

  it("tool.call calculate 不展开检索栈", () => {
    const lit = archOnToolCall("calculate");
    expect(lit).toEqual(["toolkit", "tool:calculate"]);
    expect(lit.some((x) => RETRIEVAL_STACK.includes(x as never))).toBe(false);
  });

  it("completed 点亮 sse + agent_runs", () => {
    expect(archOnCompleted()).toEqual(["sse", "agent_runs"]);
  });

  it("错误路径覆盖 coordinator + llm", () => {
    expect(ARCH_ERROR_IDS).toContain("coordinator");
    expect(ARCH_ERROR_IDS).toContain("llm");
  });
});
