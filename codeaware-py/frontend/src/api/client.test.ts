import { afterEach, describe, expect, it, vi } from "vitest";
import { agentRuns, aiReadme, ApiError, knowledge, memory, prompt, readApiErrorMessage } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("readApiErrorMessage", () => {
  it("保留统一错误 envelope 的稳定业务消息", async () => {
    const message = await readApiErrorMessage({
      status: 409,
      json: async () => ({
        code: 0,
        msg: "CHAT_TURN_IN_PROGRESS",
        data: null,
      }),
    });

    expect(message).toBe("CHAT_TURN_IN_PROGRESS");
  });

  it("非 JSON、空 msg 或异常 envelope 回退 HTTP 状态", async () => {
    await expect(
      readApiErrorMessage({
        status: 502,
        json: async () => {
          throw new SyntaxError("not json");
        },
      }),
    ).resolves.toBe("HTTP 502");

    await expect(
      readApiErrorMessage({
        status: 503,
        json: async () => ({ code: 0, msg: "   ", data: null }),
      }),
    ).resolves.toBe("HTTP 503");

    await expect(
      readApiErrorMessage({
        status: 500,
        json: async () => ({ detail: "not the unified envelope" }),
      }),
    ).resolves.toBe("HTTP 500");
  });
});

describe("knowledge.uploadFile", () => {
  it("使用 FormData 且不手工设置 multipart Content-Type", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { id: 7, title: "README.md" },
      }),
    } as Response);
    const file = new File(["# Upload"], "README.md", { type: "text/markdown" });

    await expect(knowledge.uploadFile(file, "demo-project")).resolves.toEqual({
      id: 7,
      title: "README.md",
    });

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/knowledge/upload-file");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toBeUndefined();
    expect(init?.body).toBeInstanceOf(FormData);
    const form = init?.body as FormData;
    expect(form.get("project_name")).toBe("demo-project");
    const uploaded = form.get("file") as File;
    expect(uploaded.name).toBe("README.md");
    expect(uploaded.type).toBe("text/markdown");
  });

  it("保留后端稳定文件上传错误码", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({
        code: 0,
        msg: "KNOWLEDGE_FILE_TYPE_UNSUPPORTED",
        data: null,
      }),
    } as Response);
    const file = new File(["bad"], "bad.exe", { type: "application/octet-stream" });

    await expect(knowledge.uploadFile(file)).rejects.toEqual(
      new ApiError("KNOWLEDGE_FILE_TYPE_UNSUPPORTED"),
    );
  });
});

describe("aiReadme.capabilities", () => {
  it("读取最小 capability 契约且不发送项目路径", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { enabled: false, reason: "roots_unavailable" },
      }),
    } as Response);

    await expect(aiReadme.capabilities()).resolves.toEqual({
      enabled: false,
      reason: "roots_unavailable",
    });

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/ai-readme/capabilities");
    expect(init?.method).toBeUndefined();
    expect(init?.body).toBeUndefined();
  });
});

describe("prompt client", () => {
  it("使用 canonical sample_code 查询参数", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { rendered: "preview" },
      }),
    } as Response);

    await prompt.preview(7, "public class A {}");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/prompts/7/preview?sample_code=public%20class%20A%20%7B%7D",
    );
  });

  it("创建新版本时发送完整 append-only 输入", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: {
          id: 8,
          type: "CODE_REVIEW",
          version: 2,
          name: "review-v2",
          role_setting: "role",
          template_body: "{{source_code}}",
          review_dimensions: null,
          severity_levels: null,
          is_active: true,
          created_at: "2026-07-30T12:00:00Z",
        },
      }),
    } as Response);
    const input = {
      type: "CODE_REVIEW" as const,
      name: "review-v2",
      role_setting: "role",
      template_body: "{{source_code}}",
    };

    await prompt.create(input);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/prompts");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify(input),
    });
  });
});

describe("memory client", () => {
  it("使用 REFERENCE 与 canonical top_k 契约", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          code: 1,
          msg: "success",
          data: { id: 1, content: "原子事实" },
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          code: 1,
          msg: "success",
          data: [],
        }),
      } as Response);

    await memory.save({ content: "原子事实" });
    await memory.search("事实", 0.4, 7);

    expect(fetchMock.mock.calls[0][1]?.body).toBe(
      JSON.stringify({ memory_type: "REFERENCE", content: "原子事实" }),
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/memory/long-term/search?query=%E4%BA%8B%E5%AE%9E&threshold=0.4&top_k=7",
    );
  });
});

describe("knowledge.listDocuments", () => {
  it("拼接 status/page/size 查询参数", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: {
          total: 2,
          page: 1,
          size: 10,
          records: [
            {
              id: 1,
              title: "文档一",
              source_type: "MANUAL",
              project_name: "demo",
              status: "ACTIVE",
              chunk_count: 3,
              created_at: "2026-08-05T00:00:00",
              deleted_at: null,
            },
          ],
        },
      }),
    } as Response);

    const data = await knowledge.listDocuments({ status: "DELETED", page: 2, size: 10 });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/knowledge/documents?status=DELETED&page=2&size=10",
    );
    expect(data.total).toBe(2);
    expect(data.records[0].status).toBe("ACTIVE");
  });

  it("无参数时不拼接查询字符串", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 1, msg: "success", data: { total: 0, page: 1, size: 20, records: [] } }),
    } as Response);

    await knowledge.listDocuments();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/knowledge/documents");
  });
});

describe("knowledge.replace", () => {
  it("POST FormData 到 /replace 并携带 Authorization", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 1, msg: "success", data: { id: 9, title: "新.md" } }),
    } as Response);

    const file = new File(["# new"], "new.md", { type: "text/markdown" });
    await knowledge.replace(5, file, "demo");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/knowledge/5/replace");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[0][1]?.body).toBeInstanceOf(FormData);
  });
});

describe("knowledge.getDetail", () => {
  it("GET /api/knowledge/:id 返回元数据+全文+分块", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: {
          id: 3,
          title: "详情文档",
          source_type: "MANUAL",
          project_name: null,
          status: "ACTIVE",
          chunk_count: 2,
          created_at: "2026-08-05T00:00:00",
          updated_at: "2026-08-05T00:00:00",
          deleted_at: null,
          content: "# 第一章\n缓存击穿方案",
          chunks: [
            { chunk_index: 0, chunk_content: "缓存击穿方案" },
            { chunk_index: 1, chunk_content: "热点Key失效" },
          ],
        },
      }),
    } as Response);

    const data = await knowledge.getDetail(3);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/knowledge/3");
    expect(data.content).toContain("缓存击穿");
    expect(data.chunks).toHaveLength(2);
    expect(data.chunks[1].chunk_index).toBe(1);
  });
});

describe("agentRuns.list", () => {
  it("拼接 page/size/needs_review 查询参数", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { total: 1, page: 2, size: 10, records: [] },
      }),
    } as Response);
    const data = await agentRuns.list({ page: 2, size: 10, needs_review: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs?page=2&size=10&needs_review=true");
    expect(data.total).toBe(1);
  });

  it("无过滤时不拼接查询字符串", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { total: 0, page: 1, size: 10, records: [] },
      }),
    } as Response);
    await agentRuns.list();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs");
  });
});

describe("agentRuns.detail/review/stats", () => {
  it("detail 请求路径", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 1, msg: "success", data: { turn_id: "t1", trace: [] } }),
    } as Response);
    const data = await agentRuns.detail("t1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs/t1");
    expect(data.turn_id).toBe("t1");
  });

  it("review POST 带 decision body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ code: 1, msg: "success", data: { turn_id: "t1" } }),
    } as Response);
    await agentRuns.review("t1", {
      decision: "accepted",
      expected_tools: ["search_knowledge"],
      category: "need_search",
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs/t1/review");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "accepted",
      expected_tools: ["search_knowledge"],
      category: "need_search",
    });
  });

  it("stats 请求路径", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: { total: 3, needs_review_pending: 1, status_counts: { completed: 2, error: 1 } },
      }),
    } as Response);
    const data = await agentRuns.stats();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs/stats");
    expect(data.needs_review_pending).toBe(1);
  });
});

describe("agentRuns.report", () => {
  it("请求报表路径并解包", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        code: 1,
        msg: "success",
        data: {
          total: 3,
          status_counts: { completed: 2, error: 1 },
          stop_reason_counts: { final: 2, error: 1 },
          closure_rate: 0.667,
          avg_steps: 2,
          avg_tool_calls: 1,
          error_tool_runs: 1,
          review_funnel: { pending: 1, accepted: 0, rejected: 0, synced: 0 },
          tool_usage: [{ tool: "search_knowledge", calls: 2, errors: 0 }],
          daily_trend: [{ date: "2026-08-11", total: 3, completed: 2, error: 1, empty: 0, cancelled: 0 }],
        },
      }),
    } as Response);
    const data = await agentRuns.report();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/agent-runs/report");
    expect(data.closure_rate).toBe(0.667);
    expect(data.tool_usage[0].calls).toBe(2);
  });
});
