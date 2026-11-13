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


_SP = "    "                                   # пробелы-разделители разрядов
_CODE = re.compile(r"\b(?:SKU|PO|SUP|BRD)-[0-9A-Z]{3,}\b")
_DATE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")
_NUM = re.compile(rf"(?<![\w.,])(?:\d{{1,3}}(?:[{_SP}]\d{{3}})+|\d+)(?:[.,]\d+)?(?:[{_SP}]?%)?")


def _canon(s: str) -> str:
    """Число в едином виде: без разделителей разрядов, точка, без хвостовых нулей."""
    s = re.sub(rf"[{_SP}%]", "", s).replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.lstrip("0") or "0"


def _known(obj, nums: set[str], texts: list[str]) -> None:
    """Все числа расчёта: как есть, округлённые до 0–2 знаков и доли в виде процентов."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            texts.append(str(k))
            _known(v, nums, texts)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _known(v, nums, texts)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        x = abs(float(obj))
        for v in ((x, x * 100) if 0 < x < 1 else (x,)):
            nums.update(_canon(f"{v:.{d}f}") for d in (0, 1, 2))
    else:
        texts.append(str(obj))
        nums.update(_canon(n) for n in re.findall(r"\d+(?:\.\d+)?", str(obj)))


def _check_numbers(text: str, ctx: str) -> list[str]:
    """Защита от выдуманных цифр: всё проверяемое в сводке должно быть в расчёте.

    Проверяются коды (SKU-, PO-, SUP-, BRD-), даты ДД.ММ.ГГГГ, целые числа от трёх знаков, любые дробные числа и
    проценты. Одно-двузначные целые («3 позиции», «12 месяцев») не проверяются — это счёт и нормативы в тексте.
    """
    nums: set[str] = set()
    texts: list[str] = []
    for chunk in re.findall(r"\{.*\}", ctx, flags=re.S) or [ctx]:
        try:
            dec, pos = json.JSONDecoder(), 0
            while pos < len(chunk):                    # ctx может быть склейкой нескольких JSON-объектов
                obj, end = dec.raw_decode(chunk, pos)
                _known(obj, nums, texts)
                pos = end
                while pos < len(chunk) and chunk[pos] in " \r\n\t":
                    pos += 1
        except ValueError:
            texts.append(chunk)
            nums.update(_canon(n) for n in re.findall(r"\d+(?:\.\d+)?", chunk))
    known_text = "\n".join(texts)

    bad = [c for c in dict.fromkeys(_CODE.findall(text)) if c not in known_text]
    rest = _CODE.sub(" ", text)
    for m in _DATE.finditer(rest):
        if f"{m[3]}-{m[2]}-{m[1]}" not in known_text:
            bad.append(m[0])
    rest = _DATE.sub(" ", rest)
    for raw in _NUM.findall(rest):
        raw = raw.strip()
        num = _canon(raw)
        checked = "." in num or raw.endswith("%") or len(num) >= 3
        if checked and num not in nums:
            bad.append(raw)
    return list(dict.fromkeys(bad))


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
    bad = _check_numbers(text, ctx + "\n" + json.dumps(plan["kpi"]))
    rejected = out / "ai_brief_rejected.md"
    rejected.unlink(missing_ok=True)
    if bad and ai.get("on_unverified", "fallback") != "warn":
        # число, код или дата не из расчёта: закупщику такую сводку не показываем, на дашборде остаётся автосводка
        rejected.write_text(f"<!-- for: {gen}; нет в расчёте: {', '.join(bad)} -->\n{text}\n", encoding="utf-8")
        return False, f"сводка ИИ отклонена, в расчёте нет: {', '.join(bad[:8])} (текст — {rejected.name})"
    note = ""
    if bad:
        note = (f"\n\n**Не сверено с расчётом:** {', '.join(bad[:10])} — проверить по таблице."
                "\n<!-- unverified -->")
    (out / "ai_brief.md").write_text(f"<!-- for: {gen} -->\n{text}{note}\n", encoding="utf-8")
    return True, "ok" + (f" (нет в расчёте: {', '.join(bad[:5])})" if bad else "")
