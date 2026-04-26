from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, anthropic
from datetime import datetime, date

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DB = "gains.db"
CLAUDE_KEY = os.getenv("ANTHROPIC_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            goal TEXT DEFAULT 'mass',
            level TEXT DEFAULT 'beginner',
            days_per_week INTEGER DEFAULT 3,
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
        """)

init_db()


class UserSetup(BaseModel):
    tg_id: int
    goal: str
    level: str
    days_per_week: int

class ExerciseLog(BaseModel):
    tg_id: int
    workout_id: int
    exercise: str
    sets: int
    reps: int
    weight: float


@app.post("/users/setup")
def setup_user(data: UserSetup):
    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE tg_id=?", (data.tg_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET goal=?, level=?, days_per_week=? WHERE tg_id=?",
                (data.goal, data.level, data.days_per_week, data.tg_id)
            )
        else:
            conn.execute(
                "INSERT INTO users (tg_id, goal, level, days_per_week, trial_start) VALUES (?,?,?,?,?)",
                (data.tg_id, data.goal, data.level, data.days_per_week, date.today().isoformat())
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

        # Generate new workout via Claude
        history = conn.execute(
            """SELECT el.exercise, el.weight, el.reps, el.sets, el.logged_at
               FROM exercise_logs el
               JOIN workouts w ON el.workout_id = w.id
               WHERE el.tg_id=? ORDER BY el.logged_at DESC LIMIT 30""",
            (tg_id,)
        ).fetchall()

        workout_plan = generate_workout(dict(user), [dict(h) for h in history])

        conn.execute(
            "INSERT INTO workouts (tg_id, date, exercises) VALUES (?,?,?)",
            (tg_id, today, workout_plan)
        )
        new_workout = conn.execute(
            "SELECT * FROM workouts WHERE tg_id=? AND date=?", (tg_id, today)
        ).fetchone()

        return {"workout": dict(new_workout), "logs": []}


def generate_workout(user: dict, history: list) -> str:
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)

    history_text = ""
    if history:
        history_text = "Последние тренировки:\n"
        for h in history[:15]:
            history_text += f"- {h['exercise']}: {h['sets']}x{h['reps']} @ {h['weight']}кг ({h['logged_at'][:10]})\n"

    prompt = f"""Ты — тренер. Составь тренировку на сегодня.

Пользователь:
- Цель: {user['goal']} (mass=набор массы, cut=рельеф, strength=сила)
- Уровень: {user['level']} (beginner/intermediate/advanced)
- Дней в неделю: {user['days_per_week']}

{history_text}

Верни ТОЛЬКО JSON список упражнений (без текста до и после):
[
  {{"exercise": "Жим штанги лёжа", "sets": 4, "reps": 8, "weight": 60, "rest_seconds": 120}},
  ...
]

Правила:
1. 4-6 упражнений
2. Веса основаны на истории (если есть) — немного больше прошлого
3. Если истории нет — начни с базовых весов для уровня
4. Чередуй мышечные группы относительно прошлых тренировок"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


@app.post("/workout/log")
def log_exercise(data: ExerciseLog):
    with db() as conn:
        conn.execute(
            "INSERT INTO exercise_logs (tg_id, workout_id, exercise, sets, reps, weight) VALUES (?,?,?,?,?,?)",
            (data.tg_id, data.workout_id, data.exercise, data.sets, data.reps, data.weight)
        )
    return {"ok": True}


@app.post("/workout/{workout_id}/complete")
def complete_workout(workout_id: int):
    with db() as conn:
        conn.execute("UPDATE workouts SET completed=1 WHERE id=?", (workout_id,))
    return {"ok": True}


@app.get("/progress/{tg_id}")
def get_progress(tg_id: int):
    with db() as conn:
        # Streak
        workouts = conn.execute(
            "SELECT date, completed FROM workouts WHERE tg_id=? AND completed=1 ORDER BY date DESC LIMIT 30",
            (tg_id,)
        ).fetchall()

        streak = 0
        today = date.today()
        for i, w in enumerate(workouts):
            expected = (today - __import__('datetime').timedelta(days=i)).isoformat()
            if w["date"] == expected:
                streak += 1
            else:
                break

        # Top exercises progress
        exercises = conn.execute(
            """SELECT exercise, MAX(weight) as max_weight, COUNT(*) as sessions
               FROM exercise_logs WHERE tg_id=?
               GROUP BY exercise ORDER BY sessions DESC LIMIT 5""",
            (tg_id,)
        ).fetchall()

        # Total workouts
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
        if user["trial_start"]:
            trial_start = date.fromisoformat(user["trial_start"])
            days_left = 7 - (date.today() - trial_start).days
            if days_left > 0:
                return {"status": "trial", "days_left": days_left}
        return {"status": "expired"}


@app.post("/subscription/{tg_id}/activate")
def activate_premium(tg_id: int):
    with db() as conn:
        conn.execute("UPDATE users SET is_premium=1 WHERE tg_id=?", (tg_id,))
    return {"ok": True}
