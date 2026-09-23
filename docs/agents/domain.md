# Domain Docs

Engineering skills 探索 codebase 时，应如何消费这个 repo 的 domain documentation。

## Before exploring, read these

- **`docs/adr/`** — 读取与你即将处理区域相关的 ADRs（ADR-0001 ~ ADR-0005）。
- **`docs/词汇表.md`** — 术语表（暂代 glossary）。
- **`docs/路线图.md`** — 路线图。
- 根目录的 `CONTEXT.md`（如果未来创建）。

如果某个文件不存在，**静默继续**。不要标记缺失；不要提前建议创建。

## File structure

Single-context repo：

```
/
├── docs/adr/        ← 架构决策（ADR-0001 ~ ADR-0005）
├── docs/词汇表.md   ← 术语
├── docs/路线图.md   ← 路线
├── backend/         ← FastAPI + DeepAgents 后端
└── frontend/        ← Next.js 前端
```

## Use the glossary's vocabulary

当你的输出命名某个 domain concept 时（issue title、refactor proposal、hypothesis、test name），使用 `docs/词汇表.md` 中定义的 term。不要漂移到 glossary 明确避免的 synonyms。

如果需要的概念还不在词汇表中，这是一个信号：要么正在发明项目没有使用的语言（重新考虑），要么确实存在缺口（记录下来）。

## Flag ADR conflicts

如果你的输出与现有 ADR 矛盾，明确指出，而不是静默覆盖：

> _Contradicts ADR-0005（多 SubAgent 拆分）— but worth reopening because…_
