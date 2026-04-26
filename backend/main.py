from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import Optional
from contextlib import contextmanager
import os, anthropic, json, re, httpx
from datetime import datetime, date, timezone, timedelta

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TMA_URL = "https://gains-tma.vercel.app/"
CHANNEL_URL = os.getenv("GAINS_CHANNEL", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "alekseimedia")

# --- DATABASE ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    @contextmanager
    def get_db():
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def q(sql):
        """Convert SQLite ? placeholders to psycopg2 %s"""
        return sql.replace("?", "%s")

else:
    import sqlite3

    @contextmanager
    def get_db():
        conn = sqlite3.connect("gains.db")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def q(sql):
        return sql


def db_execute(conn, sql, params=()):
    if USE_POSTGRES:
        cur = conn.cursor()
        cur.execute(q(sql), params)
        return cur
    else:
        return conn.execute(sql, params)


def fetchone(cur_or_conn, sql=None, params=()):
    if sql is not None:
        cur = db_execute(cur_or_conn, sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    else:
        row = cur_or_conn.fetchone()
        return dict(row) if row else None


def fetchall(conn, sql, params=()):
    cur = db_execute(conn, sql, params)
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def init_db():
    with get_db() as conn:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    tg_id BIGINT PRIMARY KEY,
                    location TEXT DEFAULT 'gym',
                    goal TEXT DEFAULT 'mass',
                    level TEXT DEFAULT 'beginner',
                    days_per_week INTEGER DEFAULT 3,
                    body_weight REAL,
                    height REAL,
                    age INTEGER,
                    notify_hour INTEGER DEFAULT 10,
                    created_at TIMESTAMP DEFAULT NOW(),
                    trial_start TEXT,
                    is_premium INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workouts (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT,
                    date TEXT,
                    exercises TEXT,
                    completed INTEGER DEFAULT 0,
                    split_day TEXT DEFAULT 'Тренировка'
                )
            """)
            try:
                cur.execute("ALTER TABLE workouts ADD COLUMN IF NOT EXISTS split_day TEXT DEFAULT 'Тренировка'")
            except Exception:
                pass
            cur.execute("""
                CREATE TABLE IF NOT EXISTS exercise_logs (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT,
                    workout_id INTEGER,
                    exercise TEXT,
                    sets INTEGER,
                    reps INTEGER,
                    weight REAL,
                    logged_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS body_measurements (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT,
                    date TEXT,
                    body_weight REAL,
                    waist REAL,
                    hips REAL,
                    chest REAL,
                    logged_at TIMESTAMP DEFAULT NOW()
                )
            """)
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY, location TEXT DEFAULT 'gym',
                goal TEXT DEFAULT 'mass', level TEXT DEFAULT 'beginner',
                days_per_week INTEGER DEFAULT 3, body_weight REAL, height REAL,
                age INTEGER, notify_hour INTEGER DEFAULT 10,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, trial_start TEXT, is_premium INTEGER DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, date TEXT,
                exercises TEXT, completed INTEGER DEFAULT 0,
                split_day TEXT DEFAULT 'Тренировка')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS exercise_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, workout_id INTEGER,
                exercise TEXT, sets INTEGER, reps INTEGER, weight REAL,
                logged_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS body_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, date TEXT,
                body_weight REAL, waist REAL, hips REAL, chest REAL,
                logged_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            # SQLite migrations
            for col, typ in [("body_weight","REAL"),("height","REAL"),("age","INTEGER"),("notify_hour","INTEGER")]:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
                except Exception:
                    pass
            try:
                conn.execute("ALTER TABLE workouts ADD COLUMN split_day TEXT DEFAULT 'Тренировка'")
            except Exception:
                pass

init_db()

# --- Exercise databases ---
HOME_EXERCISES = {
    "chest_triceps": [
        {"name": "Отжимания", "default_weight": 0},
        {"name": "Алмазные отжимания", "default_weight": 0},
        {"name": "Отжимания с ногами на возвышении", "default_weight": 0},
        {"name": "Обратные отжимания от стула", "default_weight": 0},
    ],
    "back_biceps": [
        {"name": "Подтягивания", "default_weight": 0},
        {"name": "Австралийские подтягивания", "default_weight": 0},
    ],
    "legs": [
        {"name": "Приседания", "default_weight": 0},
        {"name": "Выпады", "default_weight": 0},
        {"name": "Болгарский сплит-присед", "default_weight": 0},
        {"name": "Ягодичный мостик", "default_weight": 0},
    ],
    "shoulders": [
        {"name": "Пайк-отжимания", "default_weight": 0},
        {"name": "Отжимания в стойке у стены", "default_weight": 0},
    ],
    "core": [
        {"name": "Планка", "default_weight": 0},
        {"name": "Скручивания", "default_weight": 0},
        {"name": "Подъём ног лёжа", "default_weight": 0},
    ]
}

GYM_EXERCISES = {
    "chest": [
        {"name": "Жим штанги лёжа", "default_weight": 40},
        {"name": "Жим гантелей лёжа", "default_weight": 20},
        {"name": "Жим штанги под углом", "default_weight": 35},
    ],
    "back": [
        {"name": "Тяга штанги в наклоне", "default_weight": 40},
        {"name": "Тяга гантели одной рукой", "default_weight": 20},
        {"name": "Подтягивания", "default_weight": 0},
        {"name": "Тяга верхнего блока", "default_weight": 50},
    ],
    "legs": [
        {"name": "Приседания со штангой", "default_weight": 50},
        {"name": "Румынская тяга", "default_weight": 50},
        {"name": "Становая тяга", "default_weight": 60},
        {"name": "Жим ногами", "default_weight": 80},
        {"name": "Болгарский сплит-присед", "default_weight": 20},
        {"name": "Выпады с гантелями", "default_weight": 15},
    ],
    "shoulders": [
        {"name": "Жим штанги стоя (OHP)", "default_weight": 30},
        {"name": "Жим гантелей сидя", "default_weight": 15},
        {"name": "Разводка гантелей в стороны", "default_weight": 8},
    ],
    "arms": [
        {"name": "Подъём штанги на бицепс", "default_weight": 25},
        {"name": "Молотковые сгибания", "default_weight": 12},
        {"name": "Жим на трицепс в блоке", "default_weight": 30},
        {"name": "Французский жим", "default_weight": 20},
    ]
}

SPLITS = {
    2: {"days": ["Full Body A", "Full Body B"]},
    3: {"days": ["Push (грудь/плечи/трицепс)", "Pull (спина/бицепс)", "Legs (ноги)"]},
    4: {"days": ["Upper A", "Lower A", "Upper B", "Lower B"]},
    5: {"days": ["Push", "Pull", "Legs", "Upper", "Lower"]},
}

def get_split_day(days_per_week: int, workout_count: int) -> str:
    split = SPLITS.get(days_per_week, SPLITS[3])
    return split["days"][workout_count % len(split["days"])]


# --- Models ---
class UserSetup(BaseModel):
    tg_id: int
    location: str
    goal: str
    level: str
    days_per_week: int
    body_weight: Optional[float] = None
    height: Optional[float] = None
    age: Optional[int] = None

class ExerciseLog(BaseModel):
    tg_id: int
    workout_id: int
    exercise: str
    sets: int
    reps: int
    weight: float

class BodyMeasurement(BaseModel):
    tg_id: int
    body_weight: Optional[float] = None
    waist: Optional[float] = None
    hips: Optional[float] = None
    chest: Optional[float] = None


# --- Endpoints ---
@app.get("/")
def health():
    return {"status": "ok", "service": "gains-api", "db": "postgres" if USE_POSTGRES else "sqlite"}


@app.get("/app", response_class=HTMLResponse)
def serve_app():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content=content, media_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"})
    except FileNotFoundError:
        return Response(content="<h1>GAINS API</h1>", media_type="text/html")


@app.post("/users/setup")
def setup_user(data: UserSetup):
    with get_db() as conn:
        existing = fetchone(conn, "SELECT tg_id FROM users WHERE tg_id=?", (data.tg_id,))
        if existing:
            db_execute(conn,
                "UPDATE users SET location=?, goal=?, level=?, days_per_week=?, body_weight=?, height=?, age=? WHERE tg_id=?",
                (data.location, data.goal, data.level, data.days_per_week, data.body_weight, data.height, data.age, data.tg_id))
        else:
            db_execute(conn,
                "INSERT INTO users (tg_id, location, goal, level, days_per_week, body_weight, height, age, trial_start) VALUES (?,?,?,?,?,?,?,?,?)",
                (data.tg_id, data.location, data.goal, data.level, data.days_per_week, data.body_weight, data.height, data.age, date.today().isoformat()))
    return {"ok": True}


@app.get("/users/{tg_id}")
def get_user(tg_id: int):
    with get_db() as conn:
        user = fetchone(conn, "SELECT * FROM users WHERE tg_id=?", (tg_id,))
        if not user:
            raise HTTPException(404, "User not found")
        return user


@app.get("/workout/today/{tg_id}")
def get_today_workout(tg_id: int):
    today = date.today().isoformat()
    with get_db() as conn:
        user = fetchone(conn, "SELECT * FROM users WHERE tg_id=?", (tg_id,))
        if not user:
            raise HTTPException(404, "User not found")

        workout = fetchone(conn, "SELECT * FROM workouts WHERE tg_id=? AND date=?", (tg_id, today))

        if workout:
            logs = fetchall(conn, "SELECT * FROM exercise_logs WHERE workout_id=?", (workout["id"],))
            completed_count = fetchone(conn,
                "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,))["cnt"]
            next_split = get_split_day(user["days_per_week"], completed_count)
            return {"workout": workout, "logs": logs,
                    "split_day": workout.get("split_day") or "Тренировка",
                    "next_split_day": next_split}

        total_workouts = fetchone(conn,
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,))["cnt"]

        history = fetchall(conn,
            """SELECT el.exercise, el.weight, el.reps, el.sets, el.logged_at
               FROM exercise_logs el JOIN workouts w ON el.workout_id = w.id
               WHERE el.tg_id=? ORDER BY el.logged_at DESC LIMIT 30""", (tg_id,))

        split_day = get_split_day(user["days_per_week"], total_workouts)
        workout_plan = generate_workout(user, history, split_day)

        db_execute(conn, "INSERT INTO workouts (tg_id, date, exercises, split_day) VALUES (?,?,?,?)",
            (tg_id, today, workout_plan, split_day))

        if USE_POSTGRES:
            new_workout = fetchone(conn, "SELECT * FROM workouts WHERE tg_id=? AND date=? ORDER BY id DESC LIMIT 1", (tg_id, today))
        else:
            new_workout = fetchone(conn, "SELECT * FROM workouts WHERE tg_id=? AND date=?", (tg_id, today))

        return {"workout": new_workout, "split_day": split_day, "logs": []}


def generate_workout(user: dict, history: list, split_day: str) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    location = user.get("location", "gym")
    goal = user.get("goal", "mass")
    level = user.get("level", "beginner")

    goal_map = {"mass": "набор мышечной массы", "strength": "развитие силы",
                "cut": "рельеф/жиросжигание", "weight_loss": "похудение (высокий объём, суперсеты)"}
    level_map = {"beginner": "новичок (до 1 года)", "intermediate": "средний (1-3 года)", "advanced": "продвинутый (3+ лет)"}
    location_map = {"gym": "зал (штанга, гантели, тренажёры)", "home": "дома (только вес тела)"}

    history_text = ""
    if history:
        history_text = "\nПоследние тренировки:\n"
        for h in history[:15]:
            w = f"{h['weight']}кг" if (h.get('weight') or 0) > 0 else "вес тела"
            history_text += f"- {h['exercise']}: {h['sets']}×{h['reps']} @ {w}\n"

    body_info = ""
    if user.get("body_weight"): body_info += f"\n- Вес: {user['body_weight']} кг"
    if user.get("height"): body_info += f"\n- Рост: {user['height']} см"
    if user.get("age"): body_info += f"\n- Возраст: {user['age']} лет"

    if location == "home":
        exercise_pool = """Доступные упражнения (только вес тела):
- Грудь/Трицепс: Отжимания, Алмазные отжимания, Обратные отжимания от стула
- Спина/Бицепс: Подтягивания, Австралийские подтягивания
- Ноги: Приседания, Выпады, Болгарский сплит-присед, Ягодичный мостик
- Плечи: Пайк-отжимания, Отжимания в стойке у стены
- Кор: Планка, Скручивания, Подъём ног лёжа"""
        weight_note = "weight=0 для всех. Планка: reps=30-60 (секунды)."
    else:
        exercise_pool = """Упражнения для зала:
- Грудь: Жим штанги лёжа, Жим гантелей лёжа, Жим под углом
- Спина: Тяга штанги в наклоне, Тяга гантели, Подтягивания, Тяга верхнего блока
- Ноги: Приседания со штангой, Румынская тяга, Становая тяга, Жим ногами, Болгарский сплит-присед
- Плечи: Жим штанги стоя (OHP), Жим гантелей сидя, Разводка в стороны
- Руки: Подъём штанги на бицепс, Молотковые сгибания, Жим на трицепс в блоке"""
        weight_note = "Начальные веса для новичка: жим лёжа 30-40кг, приседания 40-50кг, тяга 40-50кг, гантели 10-15кг."

    prompt = f"""Ты — профессиональный тренер. Составь тренировку.

Пользователь:
- Место: {location_map.get(location, location)}
- Цель: {goal_map.get(goal, goal)}
- Уровень: {level_map.get(level, level)}
- День программы: {split_day}{body_info}

{exercise_pool}
{history_text}
{weight_note}

Прогрессия: если упражнение есть в истории — увеличь вес на 2.5-5кг или повторения на 1-2.

Верни ТОЛЬКО валидный JSON массив (без текста, без markdown):
[{{"exercise":"Название","sets":3,"reps":10,"weight":60,"rest_seconds":90}}]

Требования:
- Строго 5-6 СИЛОВЫХ упражнений
- ЗАПРЕЩЕНО: кардио, беговая дорожка, велотренажёр, эллипс
- Соответствуй дню: {split_day}
- Только конкретные числа"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        match = re.search(r'\[[\s\S]*\]', raw)
        return match.group() if match else raw


@app.post("/workout/log")
def log_exercise(data: ExerciseLog):
    with get_db() as conn:
        db_execute(conn,
            "INSERT INTO exercise_logs (tg_id, workout_id, exercise, sets, reps, weight) VALUES (?,?,?,?,?,?)",
            (data.tg_id, data.workout_id, data.exercise, data.sets, data.reps, data.weight))
    return {"ok": True}


@app.post("/measurements/log")
def log_measurement(data: BodyMeasurement):
    today = date.today().isoformat()
    with get_db() as conn:
        existing = fetchone(conn,
            "SELECT id FROM body_measurements WHERE tg_id=? AND date=?", (data.tg_id, today))
        if existing:
            db_execute(conn,
                "UPDATE body_measurements SET body_weight=?, waist=?, hips=?, chest=? WHERE id=?",
                (data.body_weight, data.waist, data.hips, data.chest, existing["id"]))
        else:
            db_execute(conn,
                "INSERT INTO body_measurements (tg_id, date, body_weight, waist, hips, chest) VALUES (?,?,?,?,?,?)",
                (data.tg_id, today, data.body_weight, data.waist, data.hips, data.chest))
    return {"ok": True}


@app.get("/measurements/{tg_id}")
def get_measurements(tg_id: int):
    with get_db() as conn:
        rows = fetchall(conn,
            "SELECT * FROM body_measurements WHERE tg_id=? ORDER BY date DESC LIMIT 10", (tg_id,))
    return rows


@app.post("/workout/{workout_id}/complete")
def complete_workout(workout_id: int):
    with get_db() as conn:
        db_execute(conn, "UPDATE workouts SET completed=1 WHERE id=?", (workout_id,))
    return {"ok": True}


@app.get("/progress/{tg_id}")
def get_progress(tg_id: int):
    with get_db() as conn:
        workouts = fetchall(conn,
            "SELECT date, completed FROM workouts WHERE tg_id=? AND completed=1 ORDER BY date DESC LIMIT 30",
            (tg_id,))

        streak = 0
        today = date.today()
        from datetime import timedelta
        for i, w in enumerate(workouts):
            expected = (today - timedelta(days=i)).isoformat()
            if w["date"] == expected:
                streak += 1
            else:
                break

        exercises = fetchall(conn,
            """SELECT exercise, MAX(weight) as max_weight, MAX(reps) as max_reps, COUNT(*) as sessions
               FROM exercise_logs WHERE tg_id=?
               GROUP BY exercise ORDER BY sessions DESC LIMIT 6""", (tg_id,))

        total = fetchone(conn,
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,))["cnt"]

        recent_workouts = fetchall(conn,
            "SELECT id, date, exercises FROM workouts WHERE tg_id=? AND completed=1 ORDER BY date DESC LIMIT 10",
            (tg_id,))

        sessions = []
        for w in recent_workouts:
            try:
                exs = json.loads(w["exercises"])
                sessions.append({
                    "date": w["date"],
                    "exercise_count": len(exs),
                    "exercises": [e["exercise"] for e in exs[:3]]
                })
            except Exception:
                pass

        return {"streak": streak, "total_workouts": total,
                "top_exercises": exercises, "sessions": sessions}


@app.get("/subscription/{tg_id}")
def check_subscription(tg_id: int):
    with get_db() as conn:
        user = fetchone(conn, "SELECT * FROM users WHERE tg_id=?", (tg_id,))
        if not user:
            return {"status": "no_user"}
        if user["is_premium"]:
            return {"status": "premium"}
        completed = fetchone(conn,
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,))["cnt"]
        workouts_left = max(0, 3 - completed)
        if workouts_left > 0:
            return {"status": "trial", "workouts_left": workouts_left}
        return {"status": "expired"}


@app.post("/subscription/{tg_id}/activate")
def activate_premium(tg_id: int):
    with get_db() as conn:
        db_execute(conn, "UPDATE users SET is_premium=1 WHERE tg_id=?", (tg_id,))
    return {"ok": True}


@app.post("/subscription/{tg_id}/invoice")
async def create_invoice(tg_id: int):
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TG_API}/createInvoiceLink", json={
            "title": "GAINS Premium",
            "description": "AI план · Прогрессия весов · История тренировок · Стрик",
            "payload": f"premium_{tg_id}",
            "currency": "XTR",
            "prices": [{"label": "Подписка на месяц", "amount": PRICE_STARS}]
        }, timeout=10)
        data = r.json()
        if data.get("ok"):
            return {"url": data["result"]}
        raise HTTPException(500, f"Invoice error: {data}")


@app.post("/users/{tg_id}/notify")
def set_notify(tg_id: int, hour: int):
    if hour < 0 or hour > 23:
        raise HTTPException(400, "Invalid hour")
    with get_db() as conn:
        db_execute(conn, "UPDATE users SET notify_hour=? WHERE tg_id=?", (hour, tg_id))
    return {"ok": True}


@app.get("/notify/due")
def get_notify_due(hour: int):
    today = date.today().isoformat()
    with get_db() as conn:
        users = fetchall(conn, "SELECT tg_id, days_per_week FROM users WHERE notify_hour=?", (hour,))
        result = []
        for u in users:
            tg_id = u["tg_id"]
            streak = fetchone(conn,
                "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1 AND date >= %s" if USE_POSTGRES
                else "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1 AND date >= date('now', '-7 days')",
                (tg_id, (date.today() - timedelta(days=7)).isoformat()) if USE_POSTGRES else (tg_id,))["cnt"]
            total = fetchone(conn,
                "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,))["cnt"]
            trained_today = fetchone(conn,
                "SELECT id FROM workouts WHERE tg_id=? AND date=? AND completed=1", (tg_id, today))
            if not trained_today:
                result.append({"tg_id": tg_id, "streak": streak, "total": total})
    return {"users": result}


# --- TELEGRAM BOT ---
async def tg_send(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/sendMessage", json=payload, timeout=10)

async def tg_answer_cb(cb_id: str, text: str = ""):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_API}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text}, timeout=5)


PRICE_RUB = 290
PRICE_STARS = 290

START_TEXT = (
    "Твой тренировочный трекер.\n\n"
    "Вводишь вес — я запоминаю.\n"
    "Следующая тренировка — уже с учётом прогресса.\n\n"
    "• Трекинг каждого упражнения\n"
    "• Адаптация нагрузки под твой результат\n"
    "• Прогресс за неделю и месяц\n"
    "• Стрик тренировок\n\n"
    f"<b>Первые 3 тренировки бесплатно. Потом {PRICE_RUB}₽/мес ({PRICE_STARS} ⭐).</b>"
)

STARS_HOWTO = (
    "⭐ <b>Как купить Telegram Stars</b>\n\n"
    "<b>Способ 1 — через бот (проще всего):</b>\n"
    "1. Открой @PremiumBot\n"
    "2. Выбери «Купить Stars»\n"
    "3. Выбери количество (нужно 150+)\n"
    "4. Оплати картой RU или СБП\n\n"
    "<b>Способ 2 — прямо в Telegram:</b>\n"
    "Настройки → Stars → Пополнить → выбери пакет\n\n"
    "<b>Способ 3 — Fragment.com:</b>\n"
    "fragment.com → Stars → оплата TON или картой\n\n"
    f"После покупки Stars — возвращайся в GAINS и нажми «Оформить подписку» ({PRICE_STARS} ⭐ = {PRICE_RUB}₽)"
)

FAQ_TEXT = (
    "❓ <b>Частые вопросы</b>\n\n"
    "<b>Когда следующая тренировка?</b>\n"
    "На следующий день — приложение откроется с новой программой.\n\n"
    "<b>Сколько тренировок в неделю?</b>\n"
    "Зависит от твоих настроек (3, 4 или 5 дней). В остальные — день отдыха.\n\n"
    "<b>Как оплатить?</b>\n"
    "Через Telegram Stars прямо в приложении: Профиль → Оформить подписку.\n\n"
    "<b>Как отменить подписку?</b>\n"
    "Настройки Telegram → Подписки → GAINS.\n\n"
    "<b>Данные сохраняются?</b>\n"
    "Да, история тренировок и прогресс сохраняются навсегда.\n\n"
    f"Остались вопросы? @{SUPPORT_USERNAME}"
)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    msg = data.get("message", {})
    cb = data.get("callback_query", {})
    pre_checkout = data.get("pre_checkout_query", {})

    if pre_checkout:
        async with httpx.AsyncClient() as client:
            await client.post(f"{TG_API}/answerPreCheckoutQuery",
                json={"pre_checkout_query_id": pre_checkout["id"], "ok": True}, timeout=10)
        return {"ok": True}

    if msg:
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        first_name = msg.get("from", {}).get("first_name", "Атлет")

        if msg.get("successful_payment"):
            tg_id = msg["from"]["id"]
            payload = msg["successful_payment"].get("invoice_payload", "")
            if payload.startswith("premium_"):
                with get_db() as conn:
                    db_execute(conn, "UPDATE users SET is_premium=1 WHERE tg_id=?", (tg_id,))
                await tg_send(chat_id,
                    "✅ <b>GAINS Premium активирован!</b>\n\nТренируйся без ограничений 💪",
                    {"inline_keyboard": [[{"text": "Открыть GAINS 💪", "web_app": {"url": TMA_URL}}]]})
            return {"ok": True}

        if text.startswith("/start"):
            keyboard = {"inline_keyboard": [
                [{"text": "💪 Начать тренировку", "web_app": {"url": TMA_URL}}],
                [{"text": "⭐ Как купить Stars?", "callback_data": "stars"},
                 {"text": "❓ FAQ", "callback_data": "faq"}],
                [{"text": "💬 Поддержка", "url": f"https://t.me/{SUPPORT_USERNAME}"}],
            ]}
            if CHANNEL_URL:
                keyboard["inline_keyboard"].append([{"text": "📢 Канал", "url": CHANNEL_URL}])
            await tg_send(chat_id, f"Привет, {first_name}! 👋\n\n{START_TEXT}", keyboard)

        elif text.startswith("/stars"):
            await tg_send(chat_id, STARS_HOWTO)

        elif text.startswith("/help") or text.startswith("/faq"):
            await tg_send(chat_id, FAQ_TEXT)

        elif text.startswith("/support"):
            await tg_send(chat_id,
                f"✉️ <b>Поддержка</b>\n\nПиши: @{SUPPORT_USERNAME}",
                {"inline_keyboard": [[{"text": "Написать", "url": f"https://t.me/{SUPPORT_USERNAME}"}]]})

    elif cb:
        chat_id = cb.get("from", {}).get("id")
        data_cb = cb.get("data", "")
        await tg_answer_cb(cb["id"])

        if data_cb == "faq":
            await tg_send(chat_id, FAQ_TEXT)
        elif data_cb == "stars":
            await tg_send(chat_id, STARS_HOWTO)
        elif data_cb == "sub":
            await tg_send(chat_id,
                f"💳 <b>Подписка GAINS</b>\n\n{PRICE_RUB}₽/мес ({PRICE_STARS} ⭐ Stars)\n\n"
                "Оформить в приложении: Профиль → Оформить подписку",
                {"inline_keyboard": [[{"text": "💪 Открыть GAINS", "web_app": {"url": TMA_URL}}]]})

    return {"ok": True}


@app.post("/set-webhook")
async def set_webhook_manual(url: str):
    """Call this once to register webhook."""
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TG_API}/setWebhook",
            json={"url": url, "drop_pending_updates": True}, timeout=10)
        return r.json()


@app.on_event("startup")
async def startup():
    if not BOT_TOKEN:
        return
    # Try to auto-set webhook
    webhook_url = os.getenv("WEBHOOK_URL", "")
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not webhook_url and railway_domain:
        webhook_url = f"https://{railway_domain}/webhook"
    if webhook_url:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{TG_API}/setWebhook",
                    json={"url": webhook_url, "drop_pending_updates": True}, timeout=10)
        except Exception:
            pass
