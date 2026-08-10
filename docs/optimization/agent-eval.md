# Agent 模式评估：工具决策 + ReAct 闭环（ADR-0016）

> 结论：**门禁全过**——工具 recall 1.0、闭环率 1.0、direct 不误调工具 1.0。
> eval 驱动一轮 prompt/防打转迭代：首轮暴露"过度工具调用"，修复后闭环率 0.833→1.0。

## 1. 评估目标与方法

衡量 Agent 模式（ReAct 工具循环）的**模型决策质量**，非检索质量（检索由 60 条 golden 另评）。

- **case 集**（6 类各 1 核心）：need_search / need_doc / need_calc / need_time / multi_step / direct，每 case 标注期望工具序列
- **指标**：
  - `recall`：期望工具 ⊆ 实际调用（该调的都调了）——主指标
  - `exact`：实际调用集 == 期望集（不多调）
  - `closure`：步数上限内收敛到终答（非死循环/达上限无答案）
  - `direct no_tool`：常识问题不调工具
- **运行**：真实 DeepSeek v4-flash + Ollama bge-m3 + BM25，复用生产代码（AgentToolkit/react_loop/_build_agent_system_prompt），live_eval
- **产物**：`tests/eval/artifacts/agent_eval.json`

## 2. 首轮结果：recall 达标，但暴露"过度工具调用"

| 指标 | 首轮 |
|---|---|
| 工具 recall | **1.0**（该调的都调了 ✅）|
| 闭环率 | **0.833**（multi_step 未闭环 ❌）|
| exact | 0.5 |
| direct 不误调 | 1.0 |

**暴露的问题**：模型对信息不满足时反复取工具发散：

```
[need_search] "缓存击穿怎么解决？"  预期=[search]
  实际=[search×2, list_documents, get_document×3]  ← 6 次，达步数上限
[multi_step]  "对比击穿和穿透"      预期=[search×2]
  实际=[search×2, list_documents, get_document×3, search, get_document]  ← 7 次，未闭环
```

## 3. 迭代过程：第一版 prompt 的缺点 → 第二版改动

### 第一版为什么不好（eval 实证的 2 个缺点）

1. **system prompt 只说"信息足够后停止"**，没说"不要过度调用同类工具"。模型检索到片段后，为追求"完整文档"反复 `get_document`（换不同 id 绕过去重），信息不满足时发散。
2. **防打转只拦"完全相同 (工具, 参数)"**：`get_document(不同id)` / `search(不同query)` 每次都是"新参数"，绕过去重机制，同一类工具可无限调直到步数上限。

> 根因：**防打转的粒度是"调用对"，不是"工具类"**。模型不会精确重复同一次调用（那会立刻被拦），但会换参数反复调同一类工具。

### 第二版改了什么（2 处）

1. **react_loop 加 per-tool 单轮调用上限**（`react_loop.py`）：
   ```python
   TOOL_CALL_LIMITS = {
       "search_knowledge": 3, "get_document": 2, "list_documents": 1,
       "calculate": 2, "get_current_time": 1,
   }
   ```
   某工具超上限后不再执行，返回"已调用 N 次（单轮上限 X），请基于已有信息回答"。
2. **system prompt 加"信息足够即停止"**（`turn_coordinator.py`）：
   ```
   信息足够即停止：检索到足以回答的片段后，直接给出答案，
   不要为追求完整文档反复 get_document；同类工具避免过度调用
   （search 至多 3 次、get_document 至多 2 次）。
   ```

### 第二版的好处（数据对比）

| 指标 | 修复前 | 修复后 | 变化 |
|---|---|---|---|
| 闭环率 | 0.833 | **1.0** | 门禁通过 |
| exact | 0.5 | **0.667** | ↑ |
| 工具 recall | 1.0 | 1.0 | 保持 |
| direct 不误调 | 1.0 | 1.0 | 保持 |
| 平均步数 | 2.67 | 2.67 | 分布改善（见下） |

工具调用收敛（per-tool 上限阻止最严重发散）：

```
[need_search] 修复前 [search×2, list_documents, get_document×3] (6次, 达上限)
              修复后 [search×2, list_documents, get_document]      (4次, 闭环)
[multi_step]  修复前 [search×2, list_documents, get_document×3, search, get_document] (7次, 未闭环)
              修复后 [search×4]                                    (4次, 闭环)
```

## 4. 门禁判定

| 门禁 | 要求 | 实际 | 判定 |
|---|---|---|---|
| 工具 recall | ≥ 0.70 | **1.0** | ✅ |
| 闭环率 | ≥ 0.90 | **1.0** | ✅ |
| direct 不误调工具 | = 100% | **1.0** | ✅ |

## 5. 已知边界（后续可改进）

- **exact 0.667 仍偏低**：need_search 仍多调 list_documents（模型想枚举文档）。per-tool 上限拦住最严重的（get_document 反复），但"多余辅助工具"（list_documents）仍有。
- **average steps 2.67**：need_search/doc 仍 4 步（多轮）。进一步收紧需 prompt 更明确"检索一次即答"，或调低 search 上限。
- 6 case 样本小：稳定统计需扩充 case 集（每类 3-5 个）。

## 6. 复现

```bash
cd codeaware-py && uv run python scripts/run_tests_safe.py tests/eval/test_agent_eval.py -m live_eval -q
```
（需真实 DeepSeek + Ollama + PG/Redis，约 3 分钟）
