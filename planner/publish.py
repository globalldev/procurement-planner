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


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")


def publish(root: Path, cfg: dict) -> tuple[bool, str]:
    """Выложить дашборд. Возвращает (успех, сообщение): сбой публикации должен быть виден по коду возврата."""
    p = cfg.get("publish", {})
    method = p.get("method", "none")
    site = root / "docs"
    if method == "none":
        return True, "публикация выключена (config.toml → [publish])"
    if method == "git":
        # репозиторий проекта на GitHub, Pages раздаёт папку docs/
        asof = json.loads((root / "out" / "plan.json").read_text(encoding="utf-8"))["meta"]["asof"]
        r = _git(root, "add", "--", "docs")
        if r.returncode != 0:
            return False, f"git add: {r.stderr.strip()[:200]}"
        if _git(root, "diff", "--cached", "--quiet", "--", "docs").returncode != 0:
            # только docs/: всё остальное, что уже лежит в индексе, в коммит публикации попасть не должно
            r = _git(root, "commit", "-m", f"План закупок: срез {asof}", "--", "docs")
            if r.returncode != 0:
                return False, f"git commit: {(r.stderr or r.stdout).strip()[:200]}"
        r = _git(root, "push")
        if r.returncode != 0:
            return False, f"git push: {r.stderr.strip()[:200]}"
        return True, f"опубликовано: {p.get('url', '')}"
    if method == "scp":
        ssh_cfg = os.path.expanduser(p.get("ssh_config", ""))
        base = ["scp", "-q"] + (["-F", ssh_cfg] if ssh_cfg else [])
        r = subprocess.run(base + [str(site / "index.html"), str(site / "plan.xlsx"), p["target"]],
                           capture_output=True, text=True)
        return (True, f"опубликовано: {p.get('url', '')}") if r.returncode == 0 else (False, f"scp: {r.stderr.strip()[:200]}")
    return False, f"неизвестный метод публикации: {method}"


def notify(root: Path, cfg: dict, publish_error: str = "") -> str:
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
        lines.append(f"С прошлой выгрузки: +{len(d['new_order_now'])} к заказу, {len(d['new_deficit'])} новых рисков")
    lines += [f"Внимание: {w}" for w in plan["meta"].get("warnings", [])]
    if publish_error:
        lines.append(f"Дашборд не обновился: {publish_error}")
    elif url:
        lines.append(url)
    data = urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines), "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        return "отправлено в Telegram"
    except Exception as e:  # noqa: BLE001
        return f"Telegram: {e}"
