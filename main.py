from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, anthropic, json
from datetime import datetime, date

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DB = "gains.db"
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY")

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            location TEXT DEFAULT 'gym',
            goal TEXT DEFAULT 'mass',
            level TEXT DEFAULT 'beginner',
            days_per_week INTEGER DEFAULT 3,
            body_weight REAL,
            height REAL,
            age INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            trial_start TEXT,
            is_premium INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            date TEXT,
            exercises TEXT,
            completed INTEGER DEFAULT 0,
            FOREIGN KEY(tg_id) REFERENCES users(tg_id)
        );
        CREATE TABLE IF NOT EXISTS exercise_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            workout_id INTEGER,
            exercise TEXT,
            sets INTEGER,
            reps INTEGER,
            weight REAL,
            logged_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS body_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            date TEXT,
            body_weight REAL,
            waist REAL,
            hips REAL,
            chest REAL,
            logged_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
    # migrate existing db
    for col, typ in [("body_weight", "REAL"), ("height", "REAL"), ("age", "INTEGER")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception:
            pass

init_db()

# --- Exercise databases ---
HOME_EXERCISES = {
    "chest_triceps": [
        {"name": "Отжимания", "default_weight": 0, "unit": "повт", "progression": "Обычные → Алмазные → С ногами на возвышении"},
        {"name": "Алмазные отжимания", "default_weight": 0, "unit": "повт"},
        {"name": "Отжимания с ногами на возвышении", "default_weight": 0, "unit": "повт"},
        {"name": "Обратные отжимания от стула", "default_weight": 0, "unit": "повт"},
    ],
    "back_biceps": [
        {"name": "Подтягивания", "default_weight": 0, "unit": "повт"},
        {"name": "Австралийские подтягивания", "default_weight": 0, "unit": "повт"},
        {"name": "Тяга полотенца в двери", "default_weight": 0, "unit": "повт"},
    ],
    "legs": [
        {"name": "Приседания", "default_weight": 0, "unit": "повт"},
        {"name": "Выпады", "default_weight": 0, "unit": "повт на ногу"},
        {"name": "Болгарский сплит-присед", "default_weight": 0, "unit": "повт на ногу"},
        {"name": "Ягодичный мостик", "default_weight": 0, "unit": "повт"},
        {"name": "Приседания на одной ноге (пистолет)", "default_weight": 0, "unit": "повт"},
    ],
    "shoulders": [
        {"name": "Отжимания в стойке на руках у стены", "default_weight": 0, "unit": "повт"},
        {"name": "Пайк-отжимания", "default_weight": 0, "unit": "повт"},
    ],
    "core": [
        {"name": "Планка", "default_weight": 0, "unit": "секунды"},
        {"name": "Скручивания", "default_weight": 0, "unit": "повт"},
        {"name": "Подъём ног лёжа", "default_weight": 0, "unit": "повт"},
        {"name": "Велосипед", "default_weight": 0, "unit": "повт"},
    ]
}

GYM_EXERCISES = {
    "chest": [
        {"name": "Жим штанги лёжа", "default_weight": 40, "unit": "кг"},
        {"name": "Жим гантелей лёжа", "default_weight": 20, "unit": "кг"},
        {"name": "Жим штанги под углом", "default_weight": 35, "unit": "кг"},
    ],
    "back": [
        {"name": "Тяга штанги в наклоне", "default_weight": 40, "unit": "кг"},
        {"name": "Тяга гантели одной рукой", "default_weight": 20, "unit": "кг"},
        {"name": "Подтягивания", "default_weight": 0, "unit": "повт"},
        {"name": "Тяга верхнего блока", "default_weight": 50, "unit": "кг"},
    ],
    "legs": [
        {"name": "Приседания со штангой", "default_weight": 50, "unit": "кг"},
        {"name": "Румынская тяга", "default_weight": 50, "unit": "кг"},
        {"name": "Становая тяга", "default_weight": 60, "unit": "кг"},
        {"name": "Жим ногами", "default_weight": 80, "unit": "кг"},
        {"name": "Болгарский сплит-присед", "default_weight": 20, "unit": "кг"},
        {"name": "Выпады с гантелями", "default_weight": 15, "unit": "кг"},
        {"name": "Икры стоя", "default_weight": 0, "unit": "повт"},
    ],
    "shoulders": [
        {"name": "Жим штанги стоя (OHP)", "default_weight": 30, "unit": "кг"},
        {"name": "Жим гантелей сидя", "default_weight": 15, "unit": "кг"},
        {"name": "Тяга гантелей к подбородку", "default_weight": 20, "unit": "кг"},
        {"name": "Разводка гантелей в стороны", "default_weight": 8, "unit": "кг"},
    ],
    "arms": [
        {"name": "Подъём штанги на бицепс", "default_weight": 25, "unit": "кг"},
        {"name": "Молотковые сгибания", "default_weight": 12, "unit": "кг"},
        {"name": "Жим на трицепс в блоке", "default_weight": 30, "unit": "кг"},
        {"name": "Французский жим", "default_weight": 20, "unit": "кг"},
    ]
}

# Splits by days per week
SPLITS = {
    2: {"days": ["Full Body A", "Full Body B"]},
    3: {"days": ["Push (грудь/плечи/трицепс)", "Pull (спина/бицепс)", "Legs (ноги)"]},
    4: {"days": ["Upper A", "Lower A", "Upper B", "Lower B"]},
    5: {"days": ["Push", "Pull", "Legs", "Upper", "Lower"]},
}

def get_split_day(days_per_week: int, workout_count: int) -> str:
    split = SPLITS.get(days_per_week, SPLITS[3])
    day_idx = workout_count % len(split["days"])
    return split["days"][day_idx]


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


@app.get("/")
def health():
    return {"status": "ok", "service": "gains-api"}


@app.get("/app", response_class=HTMLResponse)
def serve_app():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    from fastapi.responses import Response
    return Response(
        content=content,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
    )


@app.post("/users/setup")
def setup_user(data: UserSetup):
    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE tg_id=?", (data.tg_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET location=?, goal=?, level=?, days_per_week=?, body_weight=?, height=?, age=? WHERE tg_id=?",
                (data.location, data.goal, data.level, data.days_per_week, data.body_weight, data.height, data.age, data.tg_id)
            )
        else:
            conn.execute(
                "INSERT INTO users (tg_id, location, goal, level, days_per_week, body_weight, height, age, trial_start) VALUES (?,?,?,?,?,?,?,?,?)",
                (data.tg_id, data.location, data.goal, data.level, data.days_per_week, data.body_weight, data.height, data.age, date.today().isoformat())
            )
    return {"ok": True}


@app.get("/users/{tg_id}")
def get_user(tg_id: int):
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not user:
            raise HTTPException(404, "User not found")
        return dict(user)


@app.get("/workout/today/{tg_id}")
def get_today_workout(tg_id: int):
    today = date.today().isoformat()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not user:
            raise HTTPException(404, "User not found")

        workout = conn.execute(
            "SELECT * FROM workouts WHERE tg_id=? AND date=?", (tg_id, today)
        ).fetchone()

        if workout:
            logs = conn.execute(
                "SELECT * FROM exercise_logs WHERE workout_id=?", (workout["id"],)
            ).fetchall()
            return {"workout": dict(workout), "logs": [dict(l) for l in logs]}

        # How many workouts done total
        total_workouts = conn.execute(
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,)
        ).fetchone()["cnt"]

        # Recent history
        history = conn.execute(
            """SELECT el.exercise, el.weight, el.reps, el.sets, el.logged_at
               FROM exercise_logs el
               JOIN workouts w ON el.workout_id = w.id
               WHERE el.tg_id=? ORDER BY el.logged_at DESC LIMIT 30""",
            (tg_id,)
        ).fetchall()

        split_day = get_split_day(user["days_per_week"], total_workouts)
        workout_plan = generate_workout(dict(user), [dict(h) for h in history], split_day)

        conn.execute(
            "INSERT INTO workouts (tg_id, date, exercises) VALUES (?,?,?)",
            (tg_id, today, workout_plan)
        )
        new_workout = conn.execute(
            "SELECT * FROM workouts WHERE tg_id=? AND date=?", (tg_id, today)
        ).fetchone()

        return {"workout": dict(new_workout), "split_day": split_day, "logs": []}


def generate_workout(user: dict, history: list, split_day: str) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)

    location = user.get("location", "gym")
    goal = user["goal"]
    level = user["level"]

    goal_map = {"mass": "набор мышечной массы", "strength": "развитие силы", "cut": "рельеф/жиросжигание", "weight_loss": "похудение/снижение веса (высокий объём, суперсеты, кардио-элементы)"}
    level_map = {"beginner": "новичок (до 1 года)", "intermediate": "средний (1-3 года)", "advanced": "продвинутый (3+ лет)"}
    location_map = {"gym": "зал (есть штанга, гантели, тренажёры)", "home": "дома (только вес тела, без железа)"}

    history_text = ""
    if history:
        history_text = "\nПоследние тренировки пользователя:\n"
        for h in history[:15]:
            w = f"{h['weight']}кг" if h["weight"] > 0 else "вес тела"
            history_text += f"- {h['exercise']}: {h['sets']}×{h['reps']} @ {w} ({h['logged_at'][:10]})\n"

    if location == "home":
        exercise_pool = f"""
Доступные упражнения (только вес тела):
- Грудь/Трицепс: Отжимания, Алмазные отжимания, Отжимания с ногами на возвышении, Обратные отжимания от стула
- Спина/Бицепс: Подтягивания, Австралийские подтягивания
- Ноги: Приседания, Выпады, Болгарский сплит-присед, Ягодичный мостик, Приседания на одной ноге
- Плечи: Пайк-отжимания, Отжимания в стойке у стены
- Кор: Планка, Скручивания, Подъём ног лёжа, Велосипед
"""
        weight_note = "Для домашних упражнений weight=0 (вес тела). Для планки — reps=30-60 (секунды)."
    else:
        exercise_pool = f"""
Упражнения для зала (штанга + гантели + тренажёры):
- Грудь: Жим штанги лёжа, Жим гантелей лёжа, Жим под углом
- Спина: Тяга штанги в наклоне, Тяга гантели, Подтягивания, Тяга верхнего блока
- Ноги: Приседания со штангой, Румынская тяга, Становая тяга, Жим ногами, Болгарский сплит-присед, Икры стоя
- Плечи: Жим штанги стоя (OHP), Жим гантелей сидя, Разводка в стороны
- Руки: Подъём штанги на бицепс, Молотковые сгибания, Жим на трицепс в блоке
"""
        weight_note = "Веса основаны на истории (если есть). Для новичка без истории: приседания 40-50кг, жим лёжа 30-40кг, тяга 40-50кг, гантели 10-15кг."

    body_info = ""
    if user.get("body_weight"):
        body_info += f"\n- Вес тела: {user['body_weight']} кг"
    if user.get("height"):
        body_info += f"\n- Рост: {user['height']} см"
    if user.get("age"):
        body_info += f"\n- Возраст: {user['age']} лет"

    prompt = f"""Ты — профессиональный тренер. Составь тренировку.

Пользователь:
- Место: {location_map[location]}
- Цель: {goal_map.get(goal, goal)}
- Уровень: {level_map.get(level, level)}
- День программы: {split_day}{body_info}

{exercise_pool}
{history_text}

{weight_note}

Правила прогрессии:
- Если в истории есть упражнение — увеличь вес на 2.5-5кг ИЛИ повторения на 1-2
- Если история пустая — стандартные начальные веса
- Для домашних: если в прошлый раз делал 3×12 — сегодня 3×15 или усложни вариацию

Верни ТОЛЬКО валидный JSON (без текста до и после, без markdown):
[
  {{"exercise": "Название", "sets": 3, "reps": 10, "weight": 60, "rest_seconds": 90}},
  {{"exercise": "Название", "sets": 3, "reps": 12, "weight": 0, "rest_seconds": 60}}
]

Требования:
- Строго 5-6 упражнений
- Соответствуй дню программы ({split_day})
- Только конкретные числа, никаких диапазонов в JSON"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    # Validate JSON
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        # Extract JSON array from response
        import re
        match = re.search(r'\[[\s\S]*\]', raw)
        if match:
            return match.group()
        return raw


@app.post("/workout/log")
def log_exercise(data: ExerciseLog):
    with db() as conn:
        conn.execute(
            "INSERT INTO exercise_logs (tg_id, workout_id, exercise, sets, reps, weight) VALUES (?,?,?,?,?,?)",
            (data.tg_id, data.workout_id, data.exercise, data.sets, data.reps, data.weight)
        )
    return {"ok": True}


@app.post("/measurements/log")
def log_measurement(data: BodyMeasurement):
    today = date.today().isoformat()
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM body_measurements WHERE tg_id=? AND date=?", (data.tg_id, today)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE body_measurements SET body_weight=?, waist=?, hips=?, chest=? WHERE id=?",
                (data.body_weight, data.waist, data.hips, data.chest, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO body_measurements (tg_id, date, body_weight, waist, hips, chest) VALUES (?,?,?,?,?,?)",
                (data.tg_id, today, data.body_weight, data.waist, data.hips, data.chest)
            )
    return {"ok": True}


@app.get("/measurements/{tg_id}")
def get_measurements(tg_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM body_measurements WHERE tg_id=? ORDER BY date DESC LIMIT 30", (tg_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/workout/{workout_id}/complete")
def complete_workout(workout_id: int):
    with db() as conn:
        conn.execute("UPDATE workouts SET completed=1 WHERE id=?", (workout_id,))
    return {"ok": True}


@app.get("/progress/{tg_id}")
def get_progress(tg_id: int):
    with db() as conn:
        workouts = conn.execute(
            "SELECT date, completed FROM workouts WHERE tg_id=? AND completed=1 ORDER BY date DESC LIMIT 30",
            (tg_id,)
        ).fetchall()

        streak = 0
        today = date.today()
        for i, w in enumerate(workouts):
            from datetime import timedelta
            expected = (today - timedelta(days=i)).isoformat()
            if w["date"] == expected:
                streak += 1
            else:
                break

        exercises = conn.execute(
            """SELECT exercise, MAX(weight) as max_weight, MAX(reps) as max_reps, COUNT(*) as sessions
               FROM exercise_logs WHERE tg_id=?
               GROUP BY exercise ORDER BY sessions DESC LIMIT 6""",
            (tg_id,)
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,)
        ).fetchone()

        return {
            "streak": streak,
            "total_workouts": total["cnt"],
            "top_exercises": [dict(e) for e in exercises]
        }


@app.get("/subscription/{tg_id}")
def check_subscription(tg_id: int):
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not user:
            return {"status": "no_user"}
        if user["is_premium"]:
            return {"status": "premium"}
        # Trial = first 3 completed workouts (not days)
        completed = conn.execute(
            "SELECT COUNT(*) as cnt FROM workouts WHERE tg_id=? AND completed=1", (tg_id,)
        ).fetchone()["cnt"]
        workouts_left = max(0, 3 - completed)
        if workouts_left > 0:
            return {"status": "trial", "workouts_left": workouts_left}
        return {"status": "expired"}


@app.post("/subscription/{tg_id}/activate")
def activate_premium(tg_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET is_premium=1 WHERE tg_id=?", (tg_id,))
    return {"ok": True}
