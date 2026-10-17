"""共同经历叙事（Shared Narrative）：把重要记忆续写进一篇共同叙事。从 main.py 原样搬来。

- run_narrative(req)（= _do_narrative）：增量演化（或 force 时全量重写），带忠实度校验，不通过不发布；
- get_narrative_pending()：水位线之后新增了多少条重要记忆（定时检查据此决定要不要投递任务）。
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

import db
import timeutil
from app.config import SQLITE_PATH, get_conn
from app.llm import call_llm_for_narrative, get_model_config

logger = logging.getLogger(__name__)


# 「重要记忆」判定：is_core 由人工/后续流程标记，arousal 由提取时的提示词
# 要求「情感浓度高的对话标注 arousal >= 0.6」，两者取并集。
IMPORTANT_L1_WHERE = "status='active' AND (is_core=1 OR arousal >= 0.6)"


class NarrativeGenerateRequest(BaseModel):
    trigger_type: str = "manual"
    force: bool = False
    reason: Optional[str] = None


# 单次演化最多消费的重要记忆条数。超出的留给下一轮（水位线只推进到
# 本次实际消费的最大 id），避免一次生成 prompt 过长。
NARRATIVE_BATCH_LIMIT = 60


async def _do_narrative(req: NarrativeGenerateRequest):
    """生成/增量演化 Shared Narrative。

    更新检查周期.md：existing narrative + new important memories = updated
    narrative。已有叙事时只消费水位线之后的新增重要记忆，往后续写；
    没有新增则跳过（force=true 强制用全部重要记忆重写）。
    """
    try:
        # 1. 获取现有 narrative
        conn = get_conn()
        c = conn.cursor()
        existing = c.execute(
            "SELECT content FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        existing_narrative = existing[0] if existing else ""

        # 2. 获取增量状态（上次处理到哪条 L1）
        state = c.execute("SELECT last_processed_l1_id FROM narrative_state WHERE id=1").fetchone()
        last_id = state[0] if state else 0

        # 3. 如果是强制重生成，忽略增量，读取全部
        if req.force or not existing_narrative:
            # 全量：读取所有重要记忆（包含 event_msg_id 用于获取 L0 原文）
            new_memories = c.execute(
                f"SELECT id, content, quote, ts, event_msg_id FROM l1_memories WHERE {IMPORTANT_L1_WHERE} ORDER BY id ASC LIMIT ?",
                (NARRATIVE_BATCH_LIMIT,)
            ).fetchall()
        else:
            # 增量：只读上次之后新增的重要记忆（包含 event_msg_id）
            new_memories = c.execute(
                f"SELECT id, content, quote, ts, event_msg_id FROM l1_memories WHERE {IMPORTANT_L1_WHERE} AND id > ? ORDER BY id ASC LIMIT ?",
                (last_id, NARRATIVE_BATCH_LIMIT)
            ).fetchall()

        if not new_memories:
            conn.close()
            return {"status": "skipped", "message": f"新增记忆不足，跳过更新（上次已处理到 L1 id={last_id}）"}

        # 如果新增不足 5 条且非强制，跳过（内容太少不值得更新）
        if len(new_memories) < 5 and not req.force and existing_narrative:
            conn.close()
            return {"status": "skipped", "message": f"新增记忆不足（{len(new_memories)}条），跳过更新"}

        # 4. 获取最新 L1 id，用于更新 state
        latest_l1 = c.execute(
            "SELECT MAX(id) FROM l1_memories WHERE status='active'"
        ).fetchone()
        latest_l1_id = latest_l1[0] if latest_l1 and latest_l1[0] else 0

        consumed_ids = [m[0] for m in new_memories]
        conn.close()

        # 5. 获取模型配置
        config = await get_model_config("narrative")
        if not config["configured"]:
            return {"status": "error", "message": "Narrative 模型未配置"}

        # 6. 构建提示词并调用 LLM
        prompt_parts = await build_narrative_prompt(existing_narrative, new_memories)
        new_narrative = await call_llm_for_narrative(config, prompt_parts)

        if not new_narrative:
            return {"status": "error", "message": "生成失败"}

        # 6.5. 忠实度校验（检查是否编造）。增量模式只校验新增句子（B14）
        is_full = bool(req.force or not existing)
        verification = await verify_narrative_faithfulness(
            new_narrative, new_memories, "" if is_full else existing_narrative)

        # B14：校验结果为 fail 时不发布——草稿存为 rejected（不替换线上版本、不推进水位线），下次自动检查重试。
        # 同一批素材最多拦截 2 次，第 3 次照常发布并带上校验报告，避免无限重试消耗额度。强制重写不拦截。
        if verification.get("status") == "fail" and not req.force:
            conn = get_conn()
            c = conn.cursor()
            prev_rejects = c.execute(
                "SELECT COUNT(*) FROM shared_narrative WHERE status='rejected' AND last_l1_id=?",
                (consumed_ids[-1],)
            ).fetchone()[0]
            if prev_rejects < 2:
                last_version = c.execute("SELECT MAX(version) FROM shared_narrative").fetchone()[0] or 0
                c.execute(
                    "INSERT INTO shared_narrative (content, version, trigger_type, trigger_details, status, last_l1_id) "
                    "VALUES (?, ?, ?, ?, 'rejected', ?)",
                    (new_narrative, last_version + 1, req.trigger_type,
                     json.dumps({"timestamp": timeutil.utc_now_str(), "reason": "忠实度校验未通过，未发布",
                                 "l1_ids": consumed_ids, "verification": verification}, ensure_ascii=False),
                     consumed_ids[-1])
                )
                conn.commit()
                conn.close()
                logger.warning(f"[NARRATIVE] 校验未通过（第 {prev_rejects + 1} 次），草稿已存为 rejected：{verification.get('issues')}")
                return {"status": "rejected", "message": "叙事忠实度校验未通过，线上版本保持不变",
                        "verification": verification}
            conn.close()
            verification["published_after_rejects"] = prev_rejects

        # 7. 保存新版本 + 更新增量状态
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE shared_narrative SET status='superseded' WHERE status='active'")
        last_version = c.execute("SELECT MAX(version) FROM shared_narrative").fetchone()[0] or 0
        new_version = last_version + 1

        # 溯源信息：为何更新、基于哪些记忆（Dashboard Observatory 数据源）+ 忠实度校验结果
        trigger_details = json.dumps({
            "timestamp": timeutil.utc_now_str(),
            "reason": req.reason or ("强制全量重写" if req.force else
                                     "首次生成" if not existing else
                                     f"新增 {len(consumed_ids)} 条重要记忆"),
            "mode": "full" if (req.force or not existing) else "incremental",
            "l1_ids": consumed_ids,
            "l1_count": len(consumed_ids),
            "verification": verification  # 忠实度校验结果
        }, ensure_ascii=False)

        c.execute(
            "INSERT INTO shared_narrative (content, version, trigger_type, trigger_details, status, last_l1_id) VALUES (?, ?, ?, ?, 'active', ?)",
            (new_narrative, new_version, req.trigger_type, trigger_details, consumed_ids[-1])
        )

        # 更新增量状态
        # B12 修复：水位线必须是「本批最后一条已消费的 L1 id」，而不是全表 MAX(id)。
        # 否则一次新增超过 NARRATIVE_BATCH_LIMIT 条重要记忆时，多出来的会被永久跳过；
        # 同时与 shared_narrative.last_l1_id（自动检查用的水位线）保持一致。
        # 剩余未消费的记忆会在下一次自动检查时继续处理。
        c.execute(
            "INSERT OR REPLACE INTO narrative_state (id, last_processed_l1_id, updated_at) VALUES (1, ?, ?)",
            (consumed_ids[-1], timeutil.utc_now_str())
        )
        conn.commit()
        new_id = c.lastrowid
        conn.close()

        return {
            "status": "ok",
            "id": new_id,
            "version": new_version,
            "mode": "full" if (req.force or not existing) else "incremental",
            "consumed_l1": len(consumed_ids),
            "content": new_narrative
        }

    except Exception as e:
        logger.error(f"Narrative generation error: {e}")
        import traceback; traceback.print_exc()
        return {"status": "error", "message": str(e)}


def get_narrative_pending():
    """返回（上次叙事水位线, 距上次叙事新增的重要 L1 数）。"""
    conn = get_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT last_l1_id FROM shared_narrative WHERE status='active' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    watermark = (row[0] or 0) if row else 0
    new_count = c.execute(
        f"SELECT COUNT(*) FROM l1_memories WHERE {IMPORTANT_L1_WHERE} AND id > ?", (watermark,)
    ).fetchone()[0]
    conn.close()
    return watermark, new_count


def get_l0_context_by_msg_id(sqlite_path, msg_id):
    """根据 L0 消息 id 获取前后上下文（用于叙事生成）

    msg_id: l0_messages.id（事件触发消息）
    返回：前后各2条消息的文本（共5条）
    """
    try:
        conn = db.connect(sqlite_path)
        c = conn.cursor()

        # 先查出这条消息的 msg_idx 和 conv_id
        row = c.execute(
            'SELECT msg_idx, conv_id FROM l0_messages WHERE id=? AND status=?',
            (msg_id, 'active')
        ).fetchone()

        if not row:
            conn.close()
            return ""

        msg_idx, conv_id = row

        # 前后各2条（共5条上下文）
        rows = c.execute(
            'SELECT role, content FROM l0_messages WHERE conv_id=? AND status=? AND msg_idx BETWEEN ? AND ? ORDER BY msg_idx ASC',
            (conv_id, 'active', max(0, msg_idx - 2), msg_idx + 2)
        ).fetchall()

        conn.close()

        if not rows:
            return ""

        # 格式化为对话片段
        parts = []
        for role, content in rows:
            prefix = "你" if role == "user" else "我"
            parts.append(f"{prefix}: {content[:300]}")  # 限制长度避免过长

        return "\n".join(parts)
    except Exception as e:
        print(f"获取 L0 上下文失败 (msg_id={msg_id}): {e}")
        return ""


async def build_narrative_prompt(existing_narrative: str, new_memories: list) -> dict:
    """构建 Narrative 生成提示词，返回 {system, user} 两段。

    new_memories: [(id, content, quote, ts), ...] 按提取顺序（≈时间顺序）。
    已有叙事 → 增量演化（现有全文保留，只续写新增）；否则从头生成。
    """
    # 读取系统角色指令（Narrative生成提示词.md）
    prompt_file = Path(__file__).parent.parent.parent / "Narrative生成提示词.md"
    try:
        with open(prompt_file, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except:
        system_prompt = """你是一段亲密关系的记忆守护者。维护一份连续的共同经历叙事。
按时间顺序分章节书写，保留时间锚点、情感转折点、共同经历、关系演化，
重要的原话用引号保留。温暖但不矫饰、连贯流动。"""

    # 构建记忆列表（带时间戳和 L0 原文，构成时间线骨架）
    memory_text = "### 核心记忆\n"
    for m in new_memories:
        ts_date = timeutil.local_date(m[3]) or "未知时间"   # 用户时区的日期（以前直接截 UTC，凌晨的事会算成前一天）
        event_msg_id = m[4] if len(m) > 4 else None  # event_msg_id

        memory_text += f"- [{ts_date}] {m[1]}\n"
        if m[2]:
            memory_text += f"  > 原话：{m[2]}\n"

        # 附上 L0 原文片段（防止编造）
        if event_msg_id:
            l0_context = get_l0_context_by_msg_id(SQLITE_PATH, event_msg_id)
            if l0_context:
                memory_text += f"  > 原文片段：\n    {l0_context.replace(chr(10), chr(10) + '    ')}\n"
            else:
                memory_text += f"  > 原文片段：(不可考，请浅写)\n"
        else:
            memory_text += f"  > 原文片段：(不可考，请浅写)\n"

    # 构建 user message（待处理内容）
    user_message = f"""请基于以下材料，更新我们的共同经历叙事。

## 现有叙事
{existing_narrative or "（这是第一次生成，请基于记忆从头写起）"}

## 新增记忆材料
{memory_text}

请生成更新后的完整叙事（按时间顺序，markdown 章节格式）："""

    return {"system": system_prompt, "user": user_message}


async def verify_narrative_faithfulness(narrative: str, memories: list, existing_narrative: str = "") -> dict:
    """忠实度校验：检查叙事是否编造内容

    Args:
        narrative: 生成的叙事文本
        memories: [(id, content, quote, ts, event_msg_id), ...]
        existing_narrative: 增量模式下的旧叙事。B14 修复：传入后只校验"新增的句子"——
            以前拿整篇叙事去对照本批新素材，旧叙事里的引号、篇幅都会被误判为编造，导致校验结果没法用来拦截

    Returns:
        {
            "status": "pass" | "warning" | "fail",
            "issues": [...],  # 发现的问题列表
            "score": 0.0-1.0  # 忠实度分数
        }
    """
    try:
        # 收集所有记忆的原文和提炼
        all_l0_texts = []
        all_l1_summaries = []

        for mem in memories:
            mem_id, content, quote, ts, event_msg_id = mem
            all_l1_summaries.append(content)
            if quote:
                all_l1_summaries.append(quote)

            # 获取 L0 原文
            if event_msg_id:
                l0_context = get_l0_context_by_msg_id(SQLITE_PATH, event_msg_id)
                if l0_context:
                    all_l0_texts.append(l0_context)

        # B14：增量模式只校验新增句子（旧叙事里已有的句子不再重复判定）
        full_narrative = narrative
        if existing_narrative:
            def _sents(t):
                return [x.strip() for x in re.split(r'[。！？!?\n]+', t or '') if x.strip()]
            def _nk(t):
                return re.sub(r'[\s，。！？、,.!?「」『』“”"]', '', t)
            old_keys = {_nk(x) for x in _sents(existing_narrative)}
            added = [x for x in _sents(narrative) if _nk(x) not in old_keys]
            narrative = '。'.join(added)

        # 简单启发式检查（避免调用 LLM，节省成本）
        issues = []

        # 1. 检查引号内的话是否在原文中（re 已在文件顶部导入；这里不能再局部 import，否则上面的嵌套函数取不到 re）
        quoted_parts = re.findall(r'[「『""]([^」』""]+)[」』""]', narrative)

        for quoted in quoted_parts:
            # 去除标点和空格后检查
            quoted_clean = re.sub(r'[，。！？、\s]', '', quoted)
            found = False

            for l0_text in all_l0_texts:
                l0_clean = re.sub(r'[，。！？、\s]', '', l0_text)
                if quoted_clean in l0_clean or len(quoted_clean) < 5:
                    found = True
                    break

            if not found and len(quoted_clean) > 5:
                issues.append(f"引号内容可能编造: {quoted[:50]}...")

        # 2. 检查叙事长度 vs 素材量（过长可能有编造）
        narrative_len = len(narrative)
        material_len = sum(len(s) for s in all_l1_summaries) + sum(len(t) for t in all_l0_texts)

        if material_len > 0 and narrative_len > material_len * 3:
            issues.append(f"叙事长度 ({narrative_len}) 远超素材量 ({material_len})，可能有过度展开")

        # 计算忠实度分数
        score = 1.0
        if len(issues) > 0:
            score = max(0.0, 1.0 - len(issues) * 0.15)

        status = "pass" if score >= 0.8 else ("warning" if score >= 0.6 else "fail")

        return {
            "status": status,
            "issues": issues,
            "score": round(score, 2),
            "quoted_count": len(quoted_parts),
            "material_length": material_len,
            "narrative_length": narrative_len,          # 增量模式下是"新增部分"的长度
            "full_length": len(full_narrative),
            "checked": "added_only" if existing_narrative else "full"
        }

    except Exception as e:
        logger.error(f"忠实度校验失败: {e}")
        return {
            "status": "error",
            "issues": [f"校验失败: {str(e)}"],
            "score": 0.0
        }


# worker 和以后的模块用这个名字
run_narrative = _do_narrative
