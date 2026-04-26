#!/usr/bin/env python3
"""GAINS push notifications — runs via cron every hour."""
import os, sys, requests
from datetime import datetime, timezone, timedelta

API = "https://web-production-0031f.up.railway.app"
BOT_TOKEN = os.getenv("GAINS_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    env_path = os.path.expanduser("~/.claude-lab/shared/secrets/gains-bot.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if ":" in line and "AAF" in line:
                BOT_TOKEN = line.strip()
                break

MSK = timezone(timedelta(hours=3))
hour = datetime.now(MSK).hour

try:
    resp = requests.get(f"{API}/notify/due", params={"hour": hour}, timeout=10)
    resp.raise_for_status()
    users = resp.json().get("users", [])
except Exception as e:
    print(f"[gains-notify] API error: {e}")
    sys.exit(1)

print(f"[gains-notify] hour={hour}, users_to_notify={len(users)}")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMA_URL = "https://gains-tma.vercel.app/"

for u in users:
    tg_id = u["tg_id"]
    streak = u["streak"]
    total = u["total"]
    is_rest = u.get("is_rest_day", False)

    if is_rest:
        text = (
            "🛌 Сегодня день отдыха\n\n"
            "Мышцы растут во время восстановления.\n"
            "Пей воду, ешь белок, спи 8 часов.\n\n"
            "Завтра — снова в бой 💪"
        )
        btn_text = "Открыть GAINS"
    else:
        if streak > 0:
            s = streak
            form = 'день' if s == 1 else 'дня' if s < 5 else 'дней'
            streak_line = f"🔥 Стрик: {s} {form} — не ломай серию!"
        else:
            streak_line = f"💪 Всего тренировок: {total}. Продолжаем!"
        text = (
            f"Время тренировки!\n\n"
            f"{streak_line}\n"
            f"Открывай GAINS и работай 👊"
        )
        btn_text = "Открыть GAINS 💪"

    try:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": tg_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": btn_text,
                    "web_app": {"url": TMA_URL}
                }]]
            }
        }, timeout=10)
        if r.ok:
            print(f"  ✓ {tg_id} ({'rest' if is_rest else 'train'})")
        else:
            print(f"  ✗ {tg_id}: {r.text[:100]}")
    except Exception as e:
        print(f"  ✗ {tg_id}: {e}")
