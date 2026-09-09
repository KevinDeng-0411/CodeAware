# Rerank 精排全链路实测走查（可回溯证据）

> 真实 query 走查（2026-08-21）：query 改写 → 多路召回 → RRF 池 → cross-encoder 精排。
> 演示脚本可复现（临时，非提交）：走 `prepare_search` + `hybrid_retriever.search_by_vector` + `reranker.rerank`。
> 面试引用入口：[面试深挖-简历逐条 ⑤ 追问 6/7](../interview/面试深挖-简历逐条.md)。

---

## 1. 全链路结构

```
原始用户问题（rerank_query）
  → QueryRewriter 改写 → 3 个变体子 query
  → 每个子 query 各跑 向量 + 关键词（BM25）混合召回
  → RRF 融合 + 按 chunk.id 去重 → 候选池（目标 reranker_top_n=20，实际 28）
  → cross-encoder rerank(原始 query, 池, top_k=5) → 逐对 (query, chunk) 推理打分
  → 降序取 top5，chunk.score 替换为 xe logits
```

---

## 2. 实测输出（query: "DeepSeek thinking 模式下怎么使用工具调用"）

### 2.1 query 改写 → 3 变体
```
· DeepSeek 思考模式下工具调用的使用方法与配置步骤
· 如何在DeepSeek的思考模式中启用和执行外部工具调用？
· DeepSeek reasoning模式下的函数调用与工具集成技术指南
```

### 2.2 多路召回 RRF 池（前 20，粗排分 rr）
```
#1   rr=+0.031 both  DeepSeek thinking mode thinking 模型用 jso...
#2   rr=+0.031 both  DeepSeek thinking mode thinking 模型用 jso...
#3   rr=+0.030 both  检索决策，不执行外部动作（sandbox/Git/MCP 等锁）
#4   rr=+0.028 both  微服务架构下的数据一致性实践指南            ← 噪声
#5   rr=+0.028 both  缓存击穿 热点Key失效...                    ← 噪声
#6   rr=+0.028 both  RAG 模式（确定性状态机）...
#7   rr=+0.027 both  RAG 检索增强生成 ...
#18  rr=+0.023 both  LongTermMemoryManager Chat 达 2 轮后自动...  ← 真相关被埋
...
```

**观察**：RRF 分挤在 `+0.022~+0.031`，拉不开；噪声（缓存击穿/穿透/雪崩、微服务一致性）因与某改写变体沾边被抬进池；真相关（LongTermMemory）被排到 #18。

### 2.3 cross-encoder 精排 top5
```
#1   xe=+3.458（粗排 #1 ）   DeepSeek thinking mode ...
#2   xe=+3.458（粗排 #2 ）   DeepSeek thinking mode ...
#3   xe=+0.707（粗排 #3 ）   检索决策，不执行外部动作
#4   xe=-1.858（粗排 #6 ）   RAG 模式（确定性状态机）   ← 6→4 上浮
#5   xe=-4.524（粗排 #18）   LongTermMemoryManager      ← 18→5 捞回
（"缓存击穿"等假命中全部踢出 top5）
```

---

## 3. 关键结论（面试弹药）

1. **RRF 分挤在 ~0.02 区间、拉不开真假**——RRF 只融合"排名位置"，不看"实际相关多少"。
2. **cross-encoder 分拉到 -4.5~+3.5**——成对双向交互给每个 (query, doc) 真实相关度，能区分。
3. **动序 + 剔噪**：粗排前5 命中精排前5 = **3/5**；被粗排埋到 #18 的真相关被捞回 top5，无关噪声全踢出。
4. **这解释了 MRR 0.883→0.941 / cross_doc 0.750→1.000 的机理**——不是玄学，是"RRF 管多路命中、reranker 才管真实相关"。
5. bi-encoder（bge-m3）doc 向量可预计算 → 负责全库粗召回；cross-encoder 每对现算不可缓存 → 只在 top20 上精排，成本可控。
