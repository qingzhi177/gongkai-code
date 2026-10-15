# 进度状态文件 progress.md

> 更新于 2026-07-28。上一版（07-26）记录的"非流式标题守卫"早已完成并提交
> （commit 125a4be），本版重写为当前最新快照。详细技术细节见 handoff.md 第八节。

---

## 当前状态：需求3 全部完成 + L0 重复根治完成

### ✅ 需求3（修复1~8）全部完成

见 handoff.md 8.1。要点：修复7 落在 `main.py`（非 server.js），用**批次级联动**
`_cascade_l1_on_version_switch`（因 source_msg_id 是批次首条 id，非单条 id，
handoff 原方案不成立）。

### ✅ 新功能：单条 L0 删除联动清 L1（commit 72183e8）

`delete_l0_message` 复用批次级联动。接口和 Dashboard 按钮此前已有，仅补联动。

### ✅ L0 重复存储根治（commit c71b215）

- **根因**：msg_idx 用「含空消息的原始数组下标」，Kelivo 工具调用轮次拆出的空
  消息（tool_use/tool_result 归一化为空）占位 → 真实消息 msg_idx 跨轮漂移 →
  dedup 按 (conv,msg_idx) 失配 → 重复 insert。非 conv_id 漂移、非空格误判为主因。
- **根治**：网关 `cleanMessagesForSave` 保存前清洗（去 system/去空消息/去相邻同
  role 重复，空白不敏感）。`toPlainText` 与 `normalize_content` 对齐。
- **存量**：`scripts/dedup_l0_adjacent.py`（清 5 条重复）+ `scripts/reindex_l0_msgidx.py`
  （活跃对话 msg_idx 重编号对齐新网关）。
- **验证**：单测 + 集成 + 3 轮真实 Kelivo 工具调用，全部零重复、idx 连续。

---

### ✅ 问题1：孤儿 L1 清理（commit 5b04cf4）

`scripts/clean_orphan_l1.py` 清 17 条孤儿（active L1 且所属 conv 的 active L0=0）。
关键避坑：判据排除 `client='ai_self'` 的自述记忆（conv_id 空、非孤儿）。软删可逆 +
删向量。验证：剩余孤儿 0、ai_self 记忆 54 条完好未动。详见 handoff.md 8.5。

---

## ⏳ 目前无已知待办

需求3（修复1~8）、单条 L0 删除、L0 重复根治、问题1 孤儿清理均已完成并验证。

---

## 关键环境提醒（不变）

- 免密 sudo 只配了 `systemctl restart memory-gateway/memory-service`（单服务，
  一次 restart 两个会要密码）。
- journald 读不了（要密码），排查靠代码内写文件到 `data/*.log`。
- 阿里云 embedding 维度 **1024**。
- ChromaDB 向量 ID 格式 `l1_{数字}`。
- SQLite `CURRENT_TIMESTAMP` 存 UTC，本地 +8。
- 版本管理：改完一个功能 `git commit` + `./push-all.sh`（同步 origin 私有 +
  public 公开，注意公开仓库脱敏）。

*本文件由 Claude Opus 4.8 更新于 2026-07-28。*
