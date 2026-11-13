"""Сборка статического дашборда: шаблон + данные последнего запуска в один index.html."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path



def _md(text: str) -> str:
    """Мини-рендер markdown → HTML (жирный, списки, абзацы, заголовки) без внешних зависимостей."""
    import html

    out, in_list = [], False
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for raw in text.strip().splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if re.match(r"^\s*[-*] ", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + re.sub(r"^\s*[-*] ", "", line) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            continue
        m = re.match(r"^(#{1,4}) (.*)", line)
        out.append(f"<h4>{m[2]}</h4>" if m else f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def build(root: Path) -> Path:
    out = root / "out"
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    gen = plan["meta"]["generated_at"]
    brief_src = "fallback"
    brief = (out / "brief_fallback.md").read_text(encoding="utf-8")
    ai = out / "ai_brief.md"
    if ai.exists():
        txt = ai.read_text(encoding="utf-8")
        m = re.match(r"<!-- for: (.+?) -->\n", txt)
        if m and m[1] == gen:
            brief, brief_src = txt[m.end():], "ai"
    plan["brief_verified"] = "<!-- unverified -->" not in brief
    plan["brief_html"] = _md(brief)
    plan["brief_src"] = brief_src

    tpl = (root / "dashboard" / "template.html").read_text(encoding="utf-8")
    from .report import clean

    payload = json.dumps(clean(plan), ensure_ascii=False, default=str, allow_nan=False).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)
    site = root / "docs"  # GitHub Pages раздаёт /docs
    site.mkdir(exist_ok=True)
    (site / "index.html").write_text(html, encoding="utf-8")
    if (out / "plan.xlsx").exists():
        shutil.copy2(out / "plan.xlsx", site / "plan.xlsx")
    return site / "index.html"
