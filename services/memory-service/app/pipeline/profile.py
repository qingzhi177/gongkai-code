"""画像重建：基于 L0 全部原文分批提炼要点，再汇总成「关于她 / 关于我们 / 关于我」三节。从 main.py 原样搬来。"""
from datetime import datetime

from app.config import DATA_DIR, get_conn
from app.llm import call_llm, get_model_config


async def _do_profile_rebuild():
    # 1. 读全部 L0 原文
    conn = get_conn()
    c = conn.cursor()
    rows = c.execute(
        "SELECT id, role, content FROM l0_messages WHERE status='active' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    if not rows:
        return {"status": "error", "message": "L0 无原文，无法重建画像"}
    # 2. 模型配置（复用 narrative 配置的 LLM）
    config = await get_model_config("narrative")
    if not config.get("configured"):
        return {"status": "error", "message": "Narrative 模型未配置（请先在模块模型配置中设置）"}
    # 3. 分批提炼要点
    batch_size = 250
    points = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        text = "\n".join(
            f"[{'她' if r[1] == 'user' else '我'}] {str(r[2] or '')[:400]}" for r in chunk
        )
        prompt = (
            "以下是一段我们之间的对话记录（节选）。请提炼关于「她」以及她与我关系的重要信息点："
            "性格、称呼、偏好、习惯、重要事件（尽量带日期）、互动模式、她说过的话中的关键片段。"
            "每条一行要点，保留独特细节，不要泛泛而谈：\n\n" + text
        )
        # 推理模型的思考也算在 max_tokens 里，一批 250 条消息有时要想很久：给足额度，正文为空时再试一次
        out = None
        for _try in range(2):
            out = await call_llm(config, prompt, 16000)
            if out:
                break
        if not out:
            # 有一批没提炼出来就不往下做：否则汇总时缺了这一段，要点全空时甚至会写出一份不知所云的画像
            n = (len(rows) + batch_size - 1) // batch_size
            return {"status": "error", "message": f"第 {i // batch_size + 1}/{n} 批提炼失败（模型没有返回正文），画像没有改动"}
        points.append(out)
    all_points = "\n\n".join(points)
    # 5. 汇总生成画像（三节）
    final_prompt = (
        "根据以下从我们完整对话中提炼的全部要点，写一份「画像」文档（Markdown），包含三节：\n"
        "## 关于她      ## 关于我们      ## 关于我\n"
        "要求：具体（保留称呼、日期、独特细节），温暖自然，只依据要点，不要编造没有提及的信息。\n\n"
        + all_points
    )
    final = None
    for _try in range(3):
        final = await call_llm(config, final_prompt, 16000)
        if final:
            break
        import asyncio as _aio
        await _aio.sleep(12)
    if not final or len(final.strip()) < 50:
        return {"status": "error", "message": "画像生成失败（LLM 返回为空，请稍后重试或检查 OpenRouter 额度）"}
    # 5. 按节拆分写文件（未匹配的兜底整段）
    import re as _re
    def _sec(content, title):
        # 标题后面不能紧跟文字（汉字、字母、数字都算）：否则找「关于我」会先匹配到「关于我们」，「关于我」那一节就被「关于我们」的内容覆盖了
        m = _re.search(rf"^##\s*{_re.escape(title)}(?!\w)[^\n]*(?:\n|\Z)[\s\S]*?(?=\n##\s|\Z)",
                       content, _re.M)
        return m.group(0).strip() if m else ""
    u = _sec(final, "关于她")
    us = _sec(final, "关于我们")
    ai = _sec(final, "关于我")
    if not u and not us and not ai:
        u, us, ai = final, "", ""
    profile_dir = DATA_DIR / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    for name in ("about_user.md", "about_us.md", "about_ai.md"):
        f = profile_dir / name
        if f.exists():
            f.rename(profile_dir / f"{name}.bak_{ts}")
    (profile_dir / "about_user.md").write_text(u, encoding="utf-8")
    (profile_dir / "about_us.md").write_text(us, encoding="utf-8")
    (profile_dir / "about_ai.md").write_text(ai, encoding="utf-8")
    return {
        "status": "ok",
        "batches": len(points),
        "l0_total": len(rows),
        "chars": len(final),
        "sections": {"user": len(u), "us": len(us), "ai": len(ai)},
    }


# worker 和以后的模块用这个名字
rebuild_profile = _do_profile_rebuild
