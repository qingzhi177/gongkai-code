# docs 文档索引

> 2026-10-16 整理：原先散在仓库根目录的设计、指南、分析、交接和需求文档都收到这里，内容没有改动，只是换了位置（用 `git mv` 移动，历史保留）。

## plan/ — 当前的优化方案与进度
- `progress-2026-10.md`：本轮优化（Phase 0 起）的逐项改动记录
- 《gongkai-code 优化与架构完善方案》可以放在这里（目前在 claude.ai 项目文档中）

## design/ — 仍然有效的设计理念与方案
- `给ClaudeCode的任务说明-记忆理念工程化.md`：记忆理念（陪伴感 > 检索率等）的工程化原则
- `给ClaudeCode的任务说明-记忆呈现与随机触发.md`：念头式浮现与随机触发的设计
- `Narrative生成提示词.md`：Shared Narrative 的生成规则（"我们"视角、禁止编造、浅写）
- `Shared-Narrative与缓存优化实施文档.md`：Memory Continuity Context + Shared Narrative + Anthropic Cache 实施文档（原根目录文件 `Shared`）
- `implementation-plan.md`：早期实现计划

## guides/ — 使用与测试指南
- `快速使用指南.md`
- `L1_EXPORT_IMPORT_GUIDE.md`：L1 导出导入
- `MEMORY_SURFACING_README.md`：记忆浮现说明
- `test_memory_surfacing.md`：浮现测试记录

## archive/ — 已完成的历史文档（只读归档）
- `analysis/`：根因分析与修复方案（叙事虚构与 recall 原文缺失、L1 保留、feel 导出导入、时间感知、L1 删除与重提取）
- `bugs/`：bug1–bug4、bug4 修复报告、待办排查问题
- `handoff/`：交接文档（handoff、handoff2）、进度与实现总结
- `requirements/`：需求 1–3、Dashboard 扩展、TG 对话功能（TG 桥因 bug 暂未启用，代码保留）、待实现功能

## 其他位置
- `tests/manual/test_new_apis.sh`：原根目录的接口手动测试脚本
