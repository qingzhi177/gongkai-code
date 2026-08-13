# Memory System v1.6+ 交接文档

**生成时间**: 2026-08-13  
**任务**: 深度检查需求文档与实现，生成详细执行清单

---

## 一、核心需求文档概览

已阅读的需求文档：
1. **Dashboard扩展.md** - Memory Observatory 可视化管理扩展
2. **更新检查周期.md** - Summary 滚动更新 + Narrative 增量演化机制
3. **Narrative生成提示词.md** - 共同经历叙事的连续性要求
4. **进度检查.md** - 6大核心问题确认清单

---

## 二、已完成功能清单

### ✅ 2.1 基础设施 (完整)
**文件**: `services/memory-service/main.py`

- **数据库表结构** (已完成)
  - `l0_messages` - L0 对话原文存储 ✅
  - `l1_memories` - L1 记忆存储 ✅
  - `providers` - 供应商配置表 ✅
  - `active_config` - 当前选中供应商 ✅
  - `shared_narrative` - 共同经历叙事表 ✅
  - `recent_summary` - 近期摘要表 ✅
  - `narrative_config` - 配置表（阈值设置）✅
  - `model_configs` - 模块模型配置表 ✅

- **核心 API 端点** (已完成)
  - `/search` - 记忆检索（向量+BM25融合）✅
  - `/save_conversation` - 保存对话到 L0 ✅
  - `/l1/list` - L1 列表查询 ✅
  - `/l1/{id}` PUT/DELETE - L1 编辑/删除 ✅
  - `/l1/{id}/reextract` - 单条重提取 ✅

### ✅ 2.2 Shared Narrative API (完整)
**文件**: `services/memory-service/main.py` (行 1276-1399)

- `/narrative/current` GET - 获取当前 Narrative ✅
- `/narrative/history` GET - 获取历史版本 ✅
- `/narrative/generate` POST - 生成/更新 Narrative ✅
- `/narrative/{id}` PUT - 编辑 Narrative ✅

**辅助函数**:
- `build_narrative_prompt()` - 构建提示词 ✅
- `call_llm_for_narrative()` - 调用 LLM 生成 ✅

### ✅ 2.3 Recent Summary API (完整)
**文件**: `services/memory-service/main.py` (行 1401-1550)

- `/summary/current` GET - 获取当前摘要 ✅
- `/summary/generate` POST - 生成摘要 ✅
- `/summary/config` GET - 获取配置 ✅
- `/summary/config` PUT - 更新配置 ✅

**配置字段** (narrative_config 表):
- `auto_update_enabled` - 自动更新开关 ✅
- `check_threshold_turns` - 检查阈值（轮数）✅
- `check_threshold_l1` - 检查阈值（L1数）✅
- `summary_max_turns` - 最大轮数 ✅
- `summary_max_tokens` - 最大Token ✅

### ✅ 2.4 L1 Extraction Status API (完整)
**文件**: `services/memory-service/main.py` (行 1556-1620)

- `/l1/extraction_status` GET - 提取状态统计 ✅
- `/l1/extract_now` POST - 手动触发提取 ✅

**返回数据**:
- 总消息数 / 已提取 / 待提取 ✅
- 批次列表（按日期分组）✅
- 批次状态（extracted/partial/pending）✅

### ✅ 2.5 Pipeline Status API (完整)
**文件**: `services/memory-service/main.py` (行 1622-1659)

- `/pipeline/status` GET - 整体管线状态 ✅

**返回数据**:
- L0: total, percent ✅
- L1: extracted, total, pending, percent ✅
- Embedding: count, last_update ✅
- Narrative: version, last_update ✅

### ✅ 2.6 模块模型配置 API (完整)
**文件**: `services/memory-service/main.py` (行 1661-1757)

- `/config/models` GET - 获取所有模块配置 ✅
- `/config/models/{purpose}` PUT - 更新指定模块配置 ✅
- `get_model_config(purpose)` - 内部查询函数 ✅

**支持的模块**:
- `chat` - 主聊天 ✅
- `l1_extract` - L1 提取 ✅
- `embedding` - 向量嵌入 ✅
- `narrative` - 共同经历叙事 ✅
- `summary` - 近期摘要 ✅

### ✅ 2.7 Dashboard 前端页面 (完整)
**文件**: `services/dashboard/index.html`

**已实现页面**:
- 总览 (Overview) ✅
- L0 原文 ✅
- L1 记忆 ✅
- 画像 (Profile) ✅
- 记忆管线 (Pipeline) - 月相进度条 ✅
- 记忆提取 (Extraction) ✅
- 共同经历 (Narrative) ✅
- 近期摘要 (Summary) ✅
- 导入 (Import) ✅
- 配置 (Config) - 供应商管理 + 模块模型配置 ✅

**月相进度条** (行 186-220, 767-837):
- Canvas 绘制月相 ✅
- 进度百分比显示 ✅
- 月相纹理效果（噪点）✅
- 4 个阶段：L0/L1/Embedding/Narrative ✅

**Summary 配置界面** (行 266-304):
- 自动更新开关 ✅
- 检查阈值（轮数/L1数）✅
- 最大轮数/Token 配置 ✅
- 保存配置按钮 ✅

**模块配置界面** (行 1033-1169):
- 5 个模块独立配置 ✅
- 供应商下拉选择 ✅
- 模型下拉选择 ✅
- 保存按钮 + 状态提示 ✅

---

## 三、未完成 / 部分完成功能

### ❌ 3.1 自动检查与触发机制 (核心缺失)

**问题**: 所有配置和 API 都已就绪，但缺少**自动触发**的执行入口。

**现状**:
- ✅ 配置表 `narrative_config` 存在
- ✅ Dashboard 可以修改配置
- ✅ `/summary/generate` 和 `/narrative/generate` 手动接口可用
- ❌ **没有后台任务**定期检查阈值并自动触发

**缺失组件**:
1. **检查周期触发器**
   - 没有 cron job 或后台线程
   - 没有检测"新增 N 轮对话"或"新增 N 条 L1"
   - 没有判断逻辑："是否达到 check_threshold_turns / check_threshold_l1"

2. **Recent Summary 自动滚动**
   - `summary_max_turns` 和 `summary_max_tokens` 配置已存在
   - 但没有代码检查"当前摘要是否超出范围"
   - 没有代码在达到阈值时自动调用 `/summary/generate`

3. **Shared Narrative 自动更新**
   - `check_threshold_l1` 配置已存在
   - 但没有代码检查"距上次 Narrative 更新已新增多少条 L1"
   - 没有代码在达到阈值时自动调用 `/narrative/generate`

**影响范围**:
- **进度检查.md 问题 1**: ❌ 检查周期/轮换机制无自动触发
- **进度检查.md 问题 2**: ❌ Recent Summary 无自动滚动
- **进度检查.md 问题 4**: ❌ Narrative 无自动更新触发

### ⚠️ 3.2 Shared Narrative 增量演化机制 (设计偏差)

**需求方向** (更新检查周期.md):
> Shared Narrative 不是普通摘要，不应该周期性重写。  
> 设计应该：旧 Narrative + 新增重要经历 + 已有连续性 → 增量演化的新 Narrative

**当前实现** (`main.py` 行 1320-1386):
```python
async def generate_narrative(req: NarrativeGenerateRequest):
    # 1. 获取现有 narrative
    existing_narrative = ...
    
    # 2. 获取重要的 L1 记忆（核心记忆 + 最近的高唤醒记忆）
    core_memories = ...  # 最近 20 条核心记忆
    recent_memories = ... # arousal >= 0.5 的最近 30 条
    
    # 3. 构建提示词（传入 existing_narrative）
    prompt = await build_narrative_prompt(existing_narrative, core_memories, recent_memories)
    
    # 4. 调用 LLM 生成
    new_narrative = await call_llm_for_narrative(config, prompt)
    
    # 5. 标记旧版本为 superseded，插入新版本
    ...
```

**问题分析**:
- ✅ 读取了旧 Narrative (`existing_narrative`)
- ✅ 传给 LLM 作为上下文
- ⚠️ **缺少增量约束**:
  - 没有明确告诉 LLM "只新增变化，不重写全文"
  - 没有"变化检测"：哪些 L1 是上次生成后新增的？
  - 没有"重要事件筛选"：是否需要过滤掉不值得写入 Narrative 的琐碎记忆？
  - 没有"版本 diff 说明"：为什么发生更新？新增了什么？

**Prompt 模板** (`Narrative生成提示词.md`):
- ✅ 要求"保留时间锚点、情感转折点、共同经历"
- ✅ 要求"温暖但不矫饰、连续流动"
- ⚠️ **缺少明确的增量演化指令**：
  - 没有"只新增变化"的约束
  - 没有"不要重写已有部分"的要求
  - 没有"标记新增内容"的指示

**改进方向**:
1. **变化检测**: 记录上次生成时的 L1 版本号，只传"新增的核心/高唤醒记忆"
2. **增量提示**: Prompt 明确要求"在现有叙事末尾追加新章节"或"在对应时间点插入新内容"
3. **更新说明**: 在 `trigger_details` 记录"本次更新涉及的 L1 ID"、"为何触发"

### ⚠️ 3.3 Dashboard Memory Observatory 展示内容 (部分缺失)

**需求** (Dashboard扩展.md + 进度检查.md 问题 5):
> Dashboard 不只是配置页面，而是记忆系统的可观察、可编辑控制中心。  
> 用户能够理解："AI 为什么知道这些？"、"这些记忆从哪里来？"

**当前 Narrative 页面** (index.html 行 248-263):
- ✅ 显示当前 Narrative 内容
- ✅ 显示版本号、最后更新时间、触发方式
- ✅ 生成/更新、编辑、查看历史按钮
- ❌ **缺少**:
  - Narrative 来源 L1 列表（哪些记忆导致了这个叙事？）
  - 为什么发生更新（trigger_details 只存了时间戳）
  - 哪些 memory 导致变化（没有记录）
  - Pipeline 状态（哪一步完成/pending）

**当前 Summary 页面** (index.html 行 266-304):
- ✅ 显示当前摘要内容
- ✅ 显示时间段、消息数
- ✅ 配置界面（阈值、最大轮数/Token）
- ❌ **缺少**:
  - 哪些对话被纳入了这次摘要
  - 上次摘要覆盖的时间段 vs 本次摘要
  - 自动更新状态：下次更新预计何时触发

**改进方向**:
1. **Narrative Observatory**:
   - 新增"来源记忆"Tab，展示生成本版本时用到的 L1 ID
   - `trigger_details` 改存 JSON: `{"timestamp": "...", "l1_ids": [123, 456], "reason": "新增10条核心记忆"}`
   - 点击 L1 ID 可跳转到该记忆详情

2. **Summary Observatory**:
   - 展示"上次摘要范围" vs "本次摘要范围"
   - 展示"下次更新预计"：距离阈值还差 N 轮/N 条 L1
   - 展示自动更新状态：enabled/disabled

### ⚠️ 3.4 Prompt 设计不足 (需增强)

**需求** (Narrative生成提示词.md + 进度检查.md 问题 6):
> Prompt 是否满足：  
> - 保留旧 Narrative 连续性  
> - 不虚构经历  
> - 只新增变化  
> - 保留时间线  
> - 保留关系演化

**当前 Prompt** (`main.py` 行 1761-1796):
```python
async def build_narrative_prompt(existing_narrative: str, core_memories: list, recent_memories: list) -> str:
    # 读取模板文件 Narrative生成提示词.md
    prompt_file = Path(__file__).parent.parent.parent / "Narrative生成提示词.md"
    template = ...  # 读取模板
    
    # 构建记忆列表
    memory_text = "### 核心记忆\n"
    for m in core_memories:
        memory_text += f"- {m[0]}\n"  # m[0] = content
        if m[1]:  # m[1] = quote
            memory_text += f"  > {m[1]}\n"
    
    memory_text += "\n### 最近记忆\n"
    for m in recent_memories[:20]:
        memory_text += f"- {m[0]}\n"
    
    # 替换占位符
    prompt = template.replace("{existing}", existing_narrative or "（首次生成）")
    prompt = prompt.replace("{memories}", memory_text)
    
    return prompt
```

**分析**:
- ✅ 传入了 `existing_narrative`
- ✅ 传入了核心记忆和最近记忆
- ⚠️ **记忆没有时间戳**：`m[0]` 是 content，`m[1]` 是 quote，但 `m[2]` 是 ts（没用上）
- ⚠️ **没有过滤"新增记忆"**：传的是"最近 20 条核心"+"最近 30 条高唤醒"，不是"上次生成后新增的"
- ⚠️ **没有增量演化指令**：模板里缺少"只新增变化"的明确要求

**改进方向**:
1. **记忆带时间戳**:
   ```python
   memory_text = "### 核心记忆\n"
   for m in core_memories:
       ts_date = m[2][:10] if m[2] else "未知时间"
       memory_text += f"- [{ts_date}] {m[0]}\n"
   ```

2. **过滤新增记忆**:
   - 在 `shared_narrative` 表加字段 `last_l1_id` 记录"生成时最新的 L1 ID"
   - 下次生成时只查 `id > last_l1_id` 的核心/高唤醒记忆

3. **增量演化 Prompt**:
   ```
   ## 任务
   基于现有叙事和**新增记忆**，**增量更新**叙事内容。
   
   **重要约束**：
   - 现有叙事的内容**完全保留**，不要重写
   - 只在时间线的对应位置**插入新章节**或**追加新经历**
   - 新增内容用"---"标记，格式：
     ### YYYY-MM-DD - YYYY-MM-DD：[章节标题]
     [新增内容]
   - 不虚构经历，严格基于新增记忆
   - 保留时间锚点、情感转折点、关系演化
   ```

### ❌ 3.5 月相进度条设计感不足 (需美化)

**需求** (Dashboard扩展.md + 进度检查.md):
> 月相设计仅用于进度展示动画，需更具审美感和设计感。  
> 一整轮盈亏展示进度，进度对应月相的细分相位，表面有极淡的噪声纹理（月海感）。

**当前实现** (index.html 行 767-837):
```javascript
function drawMoonPhase(canvasId, percent) {
    // 背景（暗面）
    ctx.fillStyle = '#1a1a1a';
    
    // 亮面
    ctx.fillStyle = '#e8e8d0';
    
    // 纹理效果（噪点）
    ctx.fillStyle = 'rgba(255, 255, 255, 0.1)';
    for (var i = 0; i < 50; i++) {
        // 随机噪点
    }
    
    // 百分比文字
    ctx.fillStyle = percent > 50 ? '#333' : '#fff';
    ctx.fillText(percent + '%', centerX, centerY);
}
```

**现状**:
- ✅ 基本月相效果（新月→满月）
- ✅ 随机噪点纹理
- ⚠️ **设计感不足**:
  - 颜色单调（黑+米黄）
  - 噪点太简单（随机圆点）
  - 没有光影渐变
  - 没有边缘柔化
  - 百分比文字直接显示在月相上（视觉冲突）

**改进方向**:
1. **月海纹理**:
   - 用 Perlin noise 或多层圆形渐变模拟陨石坑
   - 亮面用放射状渐变（中心亮→边缘暗）
   - 暗面不纯黑，用深灰 `#0a0a0a` + 微弱高光

2. **边缘柔化**:
   - 月相边缘加 shadow 或 blur
   - 用 `radialGradient` 模拟立体感

3. **百分比显示**:
   - 不放在月相上，而是放在月相下方
   - 或者用半透明深色背景圆圈托底

4. **动画**:
   - 加载时从 0% 过渡到当前百分比（动画效果）
   - 鼠标 hover 显示详细信息

---

## 四、设计偏差分析

### 4.1 核心偏差：自动触发机制缺失

**偏离点**: 需求文档强调"自动更新检查策略"，但当前只有手动触发接口。

**根本原因**: 
- 后端是 FastAPI（同步/异步混合），没有内置定时任务框架
- 需要额外的后台任务机制（APScheduler / celery / 独立 cron 脚本）

**影响**: 
- 配置界面成了摆设（改了阈值但不生效）
- Summary 不会自动滚动
- Narrative 不会自动演化
- 系统无法真正"长期运行"

### 4.2 次要偏差：Narrative 增量演化不明确

**偏离点**: 
- 需求说"增量演化"，但实现更像"基于旧版本的重新生成"
- 没有强制约束"只新增，不重写"

**根本原因**: 
- Prompt 设计不够明确
- LLM 容易"理解偏差"：把"更新叙事"理解成"重写叙事"
- 没有技术手段强制增量（如 diff 比对）

**影响**: 
- Narrative 可能每次都大幅改写
- 失去"持续记录"的感觉
- 变成"定期总结"而非"持续书写"

### 4.3 次要偏差：Dashboard 观测能力不足

**偏离点**: 
- 需求说"Memory Observatory"，但当前只有"Memory Editor"
- 缺少"为什么"和"从哪来"的展示

**根本原因**: 
- 数据库设计时没有预留"溯源字段"
- `trigger_details` 只存了时间戳，没存 L1 ID / 触发原因

**影响**: 
- 用户看到 Narrative 但不知道基于哪些记忆
- 看到 Summary 但不知道覆盖哪些对话
- "黑箱感"依然存在

---

## 五、下一阶段执行清单

### 🎯 优先级 P0（必须完成）

#### P0-1: 实现自动检查与触发机制

**目标**: 让配置的阈值真正生效，自动触发 Summary/Narrative 更新。

**方案选择**:
1. **方案 A**: 使用 APScheduler 在 FastAPI 内部运行后台任务
   - 优点：无需额外进程，代码集中在 `main.py`
   - 缺点：FastAPI 重启会中断任务

2. **方案 B**: 独立 cron 脚本 + systemd timer
   - 优点：独立运行，不受服务重启影响
   - 缺点：需要额外的脚本文件和系统配置

3. **推荐方案**: 方案 A（APScheduler）
   - 先求快速可用，生产环境再优化

**实现步骤**:
1. **安装依赖**:
   ```bash
   cd /home/qingzhi/memory-system/services/memory-service
   source venv/bin/activate
   pip install apscheduler
   ```

2. **在 `main.py` 添加后台任务**:
   ```python
   from apscheduler.schedulers.asyncio import AsyncIOScheduler
   from apscheduler.triggers.interval import IntervalTrigger
   
   scheduler = AsyncIOScheduler()
   
   async def check_and_update_summary():
       """检查是否需要更新 Summary"""
       conn = sqlite3.connect(str(SQLITE_PATH))
       c = conn.cursor()
       
       # 读取配置
       config = c.execute("SELECT auto_update_enabled, check_threshold_turns, summary_max_turns FROM narrative_config WHERE id=1").fetchone()
       if not config or config[0] != 1:
           conn.close()
           return
       
       check_threshold = config[1]
       
       # 获取上次 Summary 的 period_end
       last_summary = c.execute("SELECT period_end FROM recent_summary WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
       last_end = last_summary[0] if last_summary else None
       
       # 计算距上次摘要新增的消息数
       if last_end:
           new_count = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active' AND ts > ?", (last_end,)).fetchone()[0]
       else:
           new_count = c.execute("SELECT COUNT(*) FROM l0_messages WHERE status='active'").fetchone()[0]
       
       conn.close()
       
       # 达到阈值则触发
       if new_count >= check_threshold:
           print(f"[AUTO] 触发 Summary 更新：新增 {new_count} 条消息（阈值 {check_threshold}）")
           await generate_summary()
   
   async def check_and_update_narrative():
       """检查是否需要更新 Narrative"""
       conn = sqlite3.connect(str(SQLITE_PATH))
       c = conn.cursor()
       
       # 读取配置
       config = c.execute("SELECT auto_update_enabled, check_threshold_l1 FROM narrative_config WHERE id=1").fetchone()
       if not config or config[0] != 1:
           conn.close()
           return
       
       threshold_l1 = config[1]
       
       # 获取上次 Narrative 的时间
       last_narrative = c.execute("SELECT ts FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1").fetchone()
       last_ts = last_narrative[0] if last_narrative else None
       
       # 计算距上次 Narrative 新增的核心记忆数
       if last_ts:
           new_l1_count = c.execute("SELECT COUNT(*) FROM l1_memories WHERE status='active' AND is_core=1 AND ts > ?", (last_ts,)).fetchone()[0]
       else:
           new_l1_count = c.execute("SELECT COUNT(*) FROM l1_memories WHERE status='active' AND is_core=1").fetchone()[0]
       
       conn.close()
       
       # 达到阈值则触发
       if new_l1_count >= threshold_l1:
           print(f"[AUTO] 触发 Narrative 更新：新增 {new_l1_count} 条核心记忆（阈值 {threshold_l1}）")
           await generate_narrative(NarrativeGenerateRequest(trigger_type="auto", force=False))
   
   @app.on_event("startup")
   async def startup_event():
       # 每 10 分钟检查一次
       scheduler.add_job(check_and_update_summary, IntervalTrigger(minutes=10), id="check_summary")
       scheduler.add_job(check_and_update_narrative, IntervalTrigger(minutes=10), id="check_narrative")
       scheduler.start()
       print("[SCHEDULER] 自动检查任务已启动")
   
   @app.on_event("shutdown")
   async def shutdown_event():
       scheduler.shutdown()
   ```

3. **测试**:
   - 修改 `check_threshold_turns` 为 5
   - 手动导入几条对话
   - 等待 10 分钟，观察日志是否触发

4. **提交**:
   ```bash
   git add services/memory-service/main.py
   git commit -m "功能: 自动检查与触发机制（APScheduler）

   - 新增 check_and_update_summary() 后台任务
   - 新增 check_and_update_narrative() 后台任务
   - 每 10 分钟检查一次阈值
   - 达到阈值自动触发生成

   Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
   ```

#### P0-2: 增强 Narrative 增量演化 Prompt

**目标**: 让 LLM 明确"只新增变化，不重写全文"。

**实现步骤**:
1. **修改 `Narrative生成提示词.md`**:
   - 在"## 任务"部分加粗强调"增量更新"
   - 加入"## 约束条件"明确"不重写已有内容"

2. **修改 `build_narrative_prompt()`**:
   - 记忆加时间戳
   - 过滤新增记忆（需先加字段 `last_l1_id`）

3. **数据库迁移**: 给 `shared_narrative` 表加字段 `last_l1_id`
   ```sql
   ALTER TABLE shared_narrative ADD COLUMN last_l1_id INTEGER DEFAULT 0;
   ```

4. **修改生成逻辑**: 记录本次生成时的最新 L1 ID

**执行**: 先说方案再改代码。

---

### 🎯 优先级 P1（重要）

#### P1-1: Dashboard Narrative Observatory 增强

**目标**: 展示"来源记忆"、"更新原因"。

**实现步骤**:
1. 修改 `trigger_details` 存储格式（JSON）
2. Narrative 页面加"来源记忆"Tab
3. 展示 L1 ID 列表，可点击跳转

#### P1-2: Dashboard Summary Observatory 增强

**目标**: 展示"覆盖范围"、"下次更新预计"。

**实现步骤**:
1. Summary 页面加"覆盖范围"显示
2. 计算"距离阈值还差 N 轮"
3. 展示自动更新状态

#### P1-3: 月相进度条美化

**目标**: 更具审美感和设计感。

**实现步骤**:
1. 改进纹理（月海效果）
2. 加光影渐变
3. 边缘柔化
4. 百分比显示优化

---

### 🎯 优先级 P2（可选优化）

#### P2-1: 单元测试

- 测试自动触发逻辑
- 测试增量演化效果
- 测试阈值边界条件

#### P2-2: 性能优化

- Narrative 生成可能很慢（调 LLM），加 loading 提示
- Pipeline 状态查询优化（缓存）

#### P2-3: 监控与日志

- 自动触发日志记录到文件
- Dashboard 加"系统日志"页面

---

## 六、特别说明

### 月相设计的审美方向

**当前问题**: 
- 颜色单调（黑+米黄）
- 纹理简单（随机圆点）
- 无光影渐变
- 无立体感

**改进建议**:
1. **配色方案**:
   - 暗面：深空灰 `#0f0f0f` → 边缘微亮 `#1a1a1a`
   - 亮面：月光白 `#f5f3e8` → 边缘金黄 `#ffd700`
   - 纹理：深灰陨石坑 `rgba(80,80,80,0.3)`

2. **月海纹理**:
   - 用多个圆形渐变叠加模拟陨石坑
   - 位置固定（不随机），保持月相的一致性
   - 半透明叠加，不遮挡主体

3. **光影效果**:
   - 亮面中心用 radialGradient: 白 → 米黄 → 金黄
   - 暗面边缘加微弱高光（模拟地球反照）

4. **文字显示**:
   - 不放在月相上，而是放在下方
   - 或者用半透明黑色圆圈托底
   - 字体：等宽数字 `font-variant-numeric: tabular-nums`

5. **动画**:
   - 首次加载时从 0% 过渡到当前值（1秒 ease-out）
   - hover 时月相微微放大（scale 1.05）
   - 点击时显示详细统计浮层

---

## 七、执行原则

### 改代码前必须先说方案

**流程**:
1. **提出方案** → 2. **等待确认** → 3. **实施修改** → 4. **测试验证** → 5. **git commit**

**每次修改范围**:
- 一次只改一个功能点
- 改完立即测试
- 测试通过再提交
- 不要一次性改多个文件

**测试要求**:
- P0 功能必须人工测试通过
- P1 功能建议测试
- P2 功能可选测试

**提交规范**:
```bash
git add <files>
git commit -m "类型: 简短描述

- 详细说明1
- 详细说明2

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"

# 类型：功能/修复/重构/文档/优化
```

---

## 八、总结

### 已完成率: ~75%

**完全OK**:
- 数据库结构 ✅
- 基础 API ✅
- Dashboard 基础页面 ✅
- 供应商配置 ✅
- 模块模型配置 ✅

**部分完成**:
- Narrative/Summary 生成 ⚠️ (手动 OK，自动缺失)
- Prompt 设计 ⚠️ (基本可用，增量演化不明确)
- Dashboard Observatory ⚠️ (展示有，溯源缺)

**完全缺失**:
- 自动检查触发机制 ❌
- Narrative 增量演化强制约束 ❌
- Summary 自动滚动逻辑 ❌

### 核心待办 (Top 3)

1. **P0-1: 自动检查触发** - 这是让整个系统"活起来"的关键
2. **P0-2: Narrative 增量演化** - 这是设计初衷的核心
3. **P1-1: Dashboard Observatory** - 这是"可观察性"的体现

### 预计工作量

- P0-1: 2-3 小时（含测试）
- P0-2: 1-2 小时（含 Prompt 优化）
- P1 全部: 3-4 小时
- P2 全部: 可选

**总计**: 6-9 小时可完成核心功能。

---

**交接完毕，等待执行指令。**
