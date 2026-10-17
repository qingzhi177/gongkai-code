"""近期摘要（Recent Summary）：把最近的对话压缩成一段摘要。从 main.py 原样搬来。

- generate_summary()：生成一版新摘要并存库（手动生成的接口、worker 里的 summary 任务都调用它）；
- get_summary_pending()：距上一版摘要新增了多少条消息（定时检查据此决定要不要投递任务）。
"""
import timeutil
from app.config import get_conn
from app.llm import call_llm, get_model_config


async def generate_summary():
    """生成 Recent Summary"""
    try:
        # 获取配置
        conn = get_conn()
        c = conn.cursor()
        config_row = c.execute("SELECT summary_max_turns, summary_max_tokens FROM narrative_config WHERE id=1").fetchone()
        max_turns = config_row[0] if config_row else 100

        # 获取最近的对话消息
        messages = c.execute(
            "SELECT role, content, ts FROM l0_messages WHERE status='active' ORDER BY ts DESC LIMIT ?",
            (max_turns,)
        ).fetchall()

        # 水位线取「当前最大 l0 id」而非入选消息的最大 id：入选是按 ts 截断的，
        # 若只记入选范围，那些 id 更大但 ts 更早的消息会永远算作增量，导致反复触发。
        new_watermark = c.execute(
            "SELECT COALESCE(MAX(id), 0) FROM l0_messages WHERE status='active'"
        ).fetchone()[0]

        conn.close()

        if not messages:
            return {"status": "error", "message": "没有足够的对话内容"}

        # 获取 summary 模型配置
        model_config = await get_model_config("summary")
        if not model_config["configured"]:
            return {"status": "error", "message": "Summary 模型未配置"}

        # 构建摘要提示词
        prompt = build_summary_prompt(messages)

        # 调用 LLM 生成摘要
        summary_content = await call_llm_for_summary(model_config, prompt)

        if not summary_content:
            return {"status": "error", "message": "生成失败"}

        # 保存摘要
        conn = get_conn()
        c = conn.cursor()

        # 标记旧摘要为 superseded
        c.execute("UPDATE recent_summary SET status='superseded' WHERE status='active'")

        period_start = messages[-1][2] if messages else None
        period_end = messages[0][2] if messages else None

        c.execute(
            "INSERT INTO recent_summary (content, period_start, period_end, msg_count, status, last_l0_id) VALUES (?, ?, ?, ?, 'active', ?)",
            (summary_content, period_start, period_end, len(messages), new_watermark)
        )

        conn.commit()
        new_id = c.lastrowid
        conn.close()

        return {"status": "ok", "id": new_id, "content": summary_content}

    except Exception as e:
        print(f"Summary generation error: {e}")
        return {"status": "error", "message": str(e)}


def get_summary_pending():
    """返回（上次摘要水位线, 距上次摘要新增的消息数）。"""
    conn = get_conn()
    c = conn.cursor()
    row = c.execute(
        "SELECT last_l0_id FROM recent_summary WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    watermark = (row[0] or 0) if row else 0
    new_count = c.execute(
        "SELECT COUNT(*) FROM l0_messages WHERE status='active' AND id > ?", (watermark,)
    ).fetchone()[0]
    conn.close()
    return watermark, new_count


def build_summary_prompt(messages: list) -> str:
    """构建 Summary 提示词"""
    msg_text = ""
    last_day = None
    for msg in reversed(messages[-100:]):  # 最近100条，倒序
        # 换天的地方插一行日期（用户时区）：下面要求「每天发生的事各自成句」，没有日期模型分不出哪天是哪天
        day = timeutil.local_day_label(msg[2]) if len(msg) > 2 else ""
        if day and day != last_day:
            msg_text += f"—— {day} ——\n\n"
            last_day = day
        role = "用户" if msg[0] == "user" else "AI"
        msg_text += f"{role}: {msg[1][:500]}\n\n"

    prompt = f"""请为以下对话生成一份简洁的近期摘要（300字以内）：

{msg_text}

摘要要求：
- 提取关键事件和话题
- 保留时间顺序
- 突出重要信息
- 简洁明了
- 【AI 第一人称】以 AI（[我]）的第一人称视角记录；对话中 [我] 是 AI，[你] 是对话的用户；提及用户不要用 "用户"，用 ta / 她 / 宝宝 / 小猫，保持事件与信息主语清晰
- 【保留关系质感】保留对话的温度，但以事实为主，不要捏造没有的关系性或事实性内容
- 【每天成句】每天发生的事各自成句；绝不许把两天的事并成一句话——那等于把其中一天弄丢了
- 【只说一半就写一半】用户只说了 "那个前端"，就写 "那个前端"；绝不许自己编名字、编职位
- 【亲密含蓄】亲密内容用含蓄的说法概括（如 "度过了亲密的一晚"）；事实和约定照记；注意内容语义去重；细节可适度提及，但不要逐字复述

摘要："""

    return prompt


async def call_llm_for_summary(config: dict, prompt: str) -> str:
    """调用 LLM 生成 Summary"""
    # 推理模型（如 deepseek-v4-pro）的思考也算在 max_tokens 里：给 1000 时思考就用完了，正文是空的
    return await call_llm(config, prompt, max_tokens=8000)
