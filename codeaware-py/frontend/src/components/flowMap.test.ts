// STAGE 映射表（flowMap）纯逻辑测试（ADR-0017 三层架构第③层）
// 事件源无关：同一映射驱动回放步进与未来实时点亮，正确性在此锁定。
import { describe, expect, it } from "vitest";
import { buildFlowGraph } from "./flowMap";
import type { TraceEntry } from "../api/types";

const trace: TraceEntry[] = [
  { type: "thought", step: 1, chars: 4, reasoning: "abcd" },
  { type: "tool_call", step: 1, name: "search_knowledge", args: { query: "缓存" }, call_id: "c1" },
  { type: "tool_result", step: 1, call_id: "c1", status: "ok", result: "结果", doc_ids: [1, 2] },
  { type: "answer", step: 1, content: "答案" },
];

describe("buildFlowGraph（STAGE 映射表）", () => {
  it("按序生成节点/边/条目→节点映射", () => {
    const g = buildFlowGraph(trace, "缓存击穿怎么解决？");
    expect(g.nodes[0]).toMatchObject({ id: "start", kind: "start", label: "用户问题" });
    expect(g.nodes.map((n) => n.id)).toEqual(["start", "t0", "tc1", "tr2", "ans"]);
    expect(g.edges.map((e) => `${e.from}->${e.to}`)).toEqual(["start->t0", "t0->tc1", "tc1->tr2", "tr2->ans"]);
    // STAGE：每个 trace 条目 index → 应点亮节点 id
    expect(g.stage.get(0)).toBe("t0");
    expect(g.stage.get(1)).toBe("tc1");
    expect(g.stage.get(2)).toBe("tr2");
    expect(g.stage.get(3)).toBe("ans");
  });

  it("tool_result 携带 doc_ids（知识库跳转目标）", () => {
    const g = buildFlowGraph(trace, "q");
    const obs = g.nodes.find((n) => n.kind === "observation");
    expect(obs?.docIds).toEqual([1, 2]);
    expect(obs?.status).toBe("ok");
  });

  it("空 trace：只含 start 节点", () => {
    const g = buildFlowGraph([], "q");
    expect(g.nodes).toHaveLength(1);
    expect(g.stage.size).toBe(0);
  });

  it("answer 去重：多个 answer 条目共用一个节点且无自环边", () => {
    const g = buildFlowGraph(
      [
        { type: "thought", step: 1, chars: 2, reasoning: "ab" },
        { type: "answer", step: 1, content: "A" },
        { type: "answer", step: 2, content: "B" },
      ],
      "q",
    );
    expect(g.nodes.filter((n) => n.kind === "answer")).toHaveLength(1);
    expect(g.stage.get(2)).toBe("ans");
    expect(g.edges.some((e) => e.from === e.to)).toBe(false);
  });

  it("convergence_override 生成 override 节点", () => {
    const g = buildFlowGraph(
      [{ type: "convergence_override", step: 1, tool_calls: [{ name: "search_knowledge" }] }],
      "q",
    );
    expect(g.nodes.some((n) => n.kind === "override")).toBe(true);
  });

  it("reflection 生成反射判定节点（accepted/feedback 展示）", () => {
    const g = buildFlowGraph(
      [
        { type: "thought", step: 1, chars: 4, reasoning: "abcd" },
        { type: "reflection", step: 1, attempt: 1, accepted: false, feedback: "回答不完整" },
        { type: "reflection", step: 2, attempt: 2, accepted: true, feedback: "" },
        { type: "answer", step: 2, content: "答案" },
      ],
      "q",
    );
    const refs = g.nodes.filter((n) => n.kind === "reflection");
    expect(refs).toHaveLength(2);
    expect(refs[0].label).toBe("反射判定 #1");
    expect(refs[0].sub).toContain("✗ 拒绝");
    expect(refs[0].sub).toContain("回答不完整");
    expect(refs[1].sub).toContain("✓ 接受");
    expect(g.stage.get(1)).toBe(refs[0].id);
  });
});
