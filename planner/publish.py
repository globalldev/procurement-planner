"""Публикация дашборда и уведомление закупщику после запуска."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path


def _env(root: Path) -> dict:
    env = dict(os.environ)
    f = root / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    return env


def publish(root: Path, cfg: dict) -> str:
    p = cfg.get("publish", {})
    method = p.get("method", "none")
    site = root / "docs"
    if method == "none":
        return "публикация выключена (config.toml → [publish])"
    if method == "git":
        # репозиторий проекта на GitHub, Pages раздаёт папку docs/
        asof = json.loads((root / "out" / "plan.json").read_text(encoding="utf-8"))["meta"]["asof"]
        cmds = [["git", "add", "docs"], ["git", "commit", "-m", f"План закупок: срез {asof}"], ["git", "push"]]
        for c in cmds:
            r = subprocess.run(c, cwd=root, capture_output=True, text=True)
            if r.returncode != 0 and c[1] != "commit":
                return f"git: {r.stderr.strip()[:200]}"
        return f"опубликовано: {p.get('url', '')}"
    if method == "scp":
        ssh_cfg = os.path.expanduser(p.get("ssh_config", ""))
        base = ["scp", "-q"] + (["-F", ssh_cfg] if ssh_cfg else [])
        r = subprocess.run(base + [str(site / "index.html"), str(site / "plan.xlsx"), p["target"]],
                           capture_output=True, text=True)
        return f"опубликовано: {p.get('url', '')}" if r.returncode == 0 else f"scp: {r.stderr.strip()[:200]}"
    return f"неизвестный метод публикации: {method}"


def notify(root: Path, cfg: dict) -> str:
    if not cfg.get("notify", {}).get("telegram"):
        return "уведомления выключены"
    env = _env(root)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env"
    plan = json.loads((root / "out" / "plan.json").read_text(encoding="utf-8"))
    k, d = plan["kpi"], plan.get("diff", {})
    url = cfg.get("publish", {}).get("url", "")
    lines = [
        f"План закупок на {plan['meta']['asof']}",
        f"Заказать сейчас: {k['order_now_skus']} SKU / {k['order_now_units']:,} шт".replace(",", " "),
        f"Риск дефицита: {k['deficit_critical']} крит., {k['deficit_high']} выс.",
        f"Избыток: {k['excess_skus']} SKU; пересмотр цены: {k['low_markup_skus']}",
    ]
    if d and not d.get("first_run"):
        lines.append(f"С прошлого запуска: +{len(d['new_order_now'])} к заказу, {len(d['new_deficit'])} новых рисков")
    if url:
        lines.append(url)
    data = urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines), "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        return "отправлено в Telegram"
    except Exception as e:  # noqa: BLE001
        return f"Telegram: {e}"
