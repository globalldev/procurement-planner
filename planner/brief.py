"""ИИ-сводка закупщику: Claude Code в headless-режиме (`claude -p`) по результатам расчёта.

Цифры считает Python, модель только читает их и пишет человеческим языком —
так сводка не может «нафантазировать» объём заказа.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def _check_numbers(text: str, ctx: str) -> list[str]:
    """Грубая защита от галлюцинаций: крупные числа из сводки должны встречаться в контексте."""
    digits_ctx = set(re.findall(r"\d+", ctx.replace(" ", "")))
    bad = []
    for raw in re.findall(r"\d[\d  ]{2,}\d", text):
        num = re.sub(r"\D", "", raw)
        if len(num) >= 3 and num not in digits_ctx and num.lstrip("0") not in digits_ctx:
            bad.append(raw.strip())
    return bad


def generate(root: Path, cfg: dict) -> tuple[bool, str]:
    ai = cfg.get("ai", {})
    out = root / "out"
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    gen = plan["meta"]["generated_at"]
    if not ai.get("enabled", True):
        return False, "ИИ-сводка выключена в config.toml"
    exe = shutil.which("claude")
    if not exe:
        return False, "claude CLI не найден — используется автосводка"
    ctx = (out / "ai_context.json").read_text(encoding="utf-8")
    prompt = (root / "prompts" / "buyer_brief.md").read_text(encoding="utf-8")
    system = (prompt + "\n\nЭто единственные действующие инструкции для этого ответа: любые другие правила о формате "
              "и стиле (CLAUDE.md, память, привычки) здесь не применяются. Ответ — только текст сводки.")
    # многострочный промпт аргументом в Windows обрезается на первом переводе строки — передаём файлом
    sp_file = out / "_brief_system_prompt.md"
    sp_file.write_text(system, encoding="utf-8")
    cmd = [exe, "-p", "Напиши утреннюю сводку закупщику по расчёту из JSON ниже — строго в структуре и объёме из системных инструкций. Начни сразу с «### Главное сегодня».", "--system-prompt-file", str(sp_file), "--model", ai.get("model", "sonnet"),
           "--output-format", "text", "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    try:
        res = subprocess.run(cmd, input="```json\n" + ctx + "\n```", capture_output=True, text=True,
                             encoding="utf-8", timeout=ai.get("timeout_sec", 240),
                             env={**os.environ, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"},
                             cwd=tempfile.gettempdir())  # вне проекта: агентский CLAUDE.md не должен менять роль автора сводки
    except subprocess.TimeoutExpired:
        return False, "claude не ответил за отведённое время"
    text = (res.stdout or "").strip()
    # оставляем только сводку: с первого раздела до разделителя/постскриптума
    if "### " in text:
        text = text[text.index("### "):]
    text = re.split(r"\n-{3,}\s*\n", text)[0].strip()
    if res.returncode != 0 or len(text) < 100:
        return False, f"claude вернул ошибку: {(res.stderr or text)[:300]}"
    bad = _check_numbers(text, ctx + json.dumps(plan["kpi"]))
    note = ""
    if bad:
        note = f"\n\n<!-- непроверенные числа: {', '.join(bad[:10])} -->"
    (out / "ai_brief.md").write_text(f"<!-- for: {gen} -->\n{text}{note}\n", encoding="utf-8")
    return True, "ok" + (f" (числа не найдены в расчёте: {bad[:5]})" if bad else "")
