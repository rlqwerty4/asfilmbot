import os
import re
import json
import queue
import asyncio
import logging
import sqlite3
import subprocess
import threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler

import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
from telegram.request import HTTPXRequest

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8455009017:AAFUbv4wRZOPHNCcBm7lZAEaL09GGHDQvUU")
OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY","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")
TMDB_API_KEY     = "9d1cbb04591d30eb42ceb762815a24a8"
TMDB_BASE        = "https://api.themoviedb.org/3"
TMDB_IMG         = "https://image.tmdb.org/t/p/w500"
DB_PATH          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "movies_bot.db")
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
WEB_APP_URL      = os.getenv("WEB_APP_URL", "")
API_PORT         = int(os.getenv("API_PORT", "8080"))
CLOUDFLARED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe")
TUNNEL_PROC      = None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

MOVIE_CACHE: dict = {}
GENRES_MAP:  dict = {}

DURATION_OPTIONS = {
    "short":  (None, 89,   "< 90 мин"),
    "medium": (90,  120,  "90–120 мин"),
    "long":   (121, None, "> 120 мин"),
    "any":    (None, None, "Любая"),
}
POPULAR_GENRE_IDS = [28, 12, 16, 35, 80, 18, 14, 27, 9648, 10749, 878, 53, 10752, 37]
MENU_TEXTS = {"🔍 Поиск", "🏆 Топ", "🤖 Подборка", "🎬 Похожие", "📋 Просмотренные", "👤 Профиль", "🔎 По названию"}


def build_main_menu(tg_id: int = 0) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("🔍 Поиск"),        KeyboardButton("🏆 Топ")],
        [KeyboardButton("🤖 Подборка"),     KeyboardButton("🎬 Похожие")],
        [KeyboardButton("🔎 По названию"),  KeyboardButton("📋 Просмотренные")],
        [KeyboardButton("👤 Профиль")],
    ]
    if WEB_APP_URL:
        url = f"{WEB_APP_URL}?user_id={tg_id}" if tg_id else WEB_APP_URL
        rows.append([KeyboardButton("🌐 Открыть приложение", web_app=WebAppInfo(url=url))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ─── DATABASE ────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id  INTEGER PRIMARY KEY,
                genres TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watched (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                movie_id    INTEGER NOT NULL,
                title       TEXT NOT NULL,
                genres      TEXT NOT NULL DEFAULT '[]',
                poster_path TEXT,
                rating      INTEGER,
                watched_at  TEXT NOT NULL,
                UNIQUE(tg_id, movie_id)
            )
        """)
        for col in ("rating INTEGER", "poster_path TEXT"):
            try:
                conn.execute(f"ALTER TABLE watched ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id       INTEGER NOT NULL,
                movie_id    INTEGER NOT NULL,
                title       TEXT NOT NULL,
                poster_path TEXT,
                added_at    TEXT NOT NULL,
                UNIQUE(tg_id, movie_id)
            )
        """)
        conn.commit()


def db_upsert_user(tg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
        conn.commit()


def db_get_user(tg_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT genres FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return {"tg_id": tg_id, "genres": json.loads(row[0])} if row else None


def db_get_watched_ids(tg_id: int) -> set:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT movie_id FROM watched WHERE tg_id=?", (tg_id,)).fetchall()
    return {r[0] for r in rows}


def db_get_last_watched(tg_id: int, limit: int = 5) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT movie_id, title, genres FROM watched WHERE tg_id=? ORDER BY watched_at DESC LIMIT ?",
            (tg_id, limit),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "genres": json.loads(r[2])} for r in rows]


def db_get_watched_list(tg_id: int, page: int = 1, per_page: int = 7):
    offset = (page - 1) * per_page
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM watched WHERE tg_id=?", (tg_id,)).fetchone()[0]
        rows = conn.execute(
            "SELECT movie_id, title, rating, watched_at, poster_path "
            "FROM watched WHERE tg_id=? ORDER BY watched_at DESC LIMIT ? OFFSET ?",
            (tg_id, per_page, offset),
        ).fetchall()
    items = [
        {"id": r[0], "title": r[1], "rating": r[2],
         "date": (r[3] or "")[:10], "poster_path": r[4] or ""}
        for r in rows
    ]
    return items, total, max(1, (total + per_page - 1) // per_page)


def db_watched_count(tg_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM watched WHERE tg_id=?", (tg_id,)).fetchone()[0]


def db_add_watched(tg_id: int, movie_id: int, title: str,
                   genres: list, poster_path: str = "") -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watched "
                "(tg_id, movie_id, title, genres, poster_path, watched_at) VALUES (?,?,?,?,?,?)",
                (tg_id, movie_id, title, json.dumps(genres),
                 poster_path or "", datetime.now().isoformat()),
            )
            row = conn.execute("SELECT genres FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if row:
                gmap = {g["id"]: g for g in json.loads(row[0])}
                for g in genres:
                    gid = g["id"]
                    if gid in gmap:
                        gmap[gid]["count"] = gmap[gid].get("count", 0) + 1
                    else:
                        gmap[gid] = {"id": gid, "name": g.get("name", "?"), "count": 1}
                conn.execute(
                    "UPDATE users SET genres=? WHERE tg_id=?",
                    (json.dumps(sorted(gmap.values(), key=lambda x: x["count"], reverse=True)), tg_id),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"db_add_watched: {e}")
        return False


def db_add_wishlist(tg_id: int, movie_id: int, title: str, poster_path: str = "") -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO wishlist (tg_id, movie_id, title, poster_path, added_at) VALUES (?,?,?,?,?)",
                (tg_id, movie_id, title, poster_path or "", datetime.now().isoformat()),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"db_add_wishlist: {e}")
        return False


def db_remove_wishlist(tg_id: int, movie_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM wishlist WHERE tg_id=? AND movie_id=?", (tg_id, movie_id))
        conn.commit()


def db_in_wishlist(tg_id: int, movie_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM wishlist WHERE tg_id=? AND movie_id=?", (tg_id, movie_id)
        ).fetchone()
    return row is not None


def db_get_wishlist(tg_id: int) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT movie_id, title, poster_path, added_at FROM wishlist WHERE tg_id=? ORDER BY added_at DESC",
            (tg_id,),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "poster_path": r[2] or "", "date": (r[3] or "")[:10]}
            for r in rows]


def db_update_rating(tg_id: int, movie_id: int, rating: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE watched SET rating=? WHERE tg_id=? AND movie_id=?",
            (rating, tg_id, movie_id),
        )
        conn.commit()


def db_remove_watched(tg_id: int, movie_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM watched WHERE tg_id=? AND movie_id=?", (tg_id, movie_id))
        # Пересчитываем жанровую статистику из оставшихся записей
        rows = conn.execute(
            "SELECT genres FROM watched WHERE tg_id=?", (tg_id,)
        ).fetchall()
        gmap: dict = {}
        for (genres_json,) in rows:
            for g in json.loads(genres_json):
                gid = g["id"]
                if gid in gmap:
                    gmap[gid]["count"] += 1
                else:
                    gmap[gid] = {"id": gid, "name": g.get("name", "?"), "count": 1}
        conn.execute(
            "UPDATE users SET genres=? WHERE tg_id=?",
            (json.dumps(sorted(gmap.values(), key=lambda x: x["count"], reverse=True)), tg_id),
        )
        conn.commit()


# ─── TMDB API ────────────────────────────────────────────────────────────────

async def tmdb_get(path: str, params: dict = None) -> dict:
    p = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
    if params:
        p.update(params)
    async with httpx.AsyncClient(timeout=15, proxy=None) as client:
        resp = await client.get(f"{TMDB_BASE}{path}", params=p)
        resp.raise_for_status()
        return resp.json()


async def tmdb_genres() -> list:
    return (await tmdb_get("/genre/movie/list"))["genres"]


async def tmdb_discover(genre_ids=None, min_runtime=None, max_runtime=None,
                         certification=None, page=1):
    params = {"sort_by": "vote_average.desc", "vote_count.gte": 200, "page": page}
    if genre_ids:
        params["with_genres"] = ",".join(str(g) for g in genre_ids)
    if min_runtime is not None:
        params["with_runtime.gte"] = min_runtime
    if max_runtime is not None:
        params["with_runtime.lte"] = max_runtime
    if certification:
        params["certification_country"] = "US"
        params["certification.lte"] = certification
    data = await tmdb_get("/discover/movie", params)
    return data.get("results", []), min(data.get("total_pages", 1), 10)


async def tmdb_trending(period: str = "week") -> list:
    return (await tmdb_get(f"/trending/movie/{period}")).get("results", [])


async def tmdb_search(title: str) -> list:
    return (await tmdb_get("/search/movie", {"query": title})).get("results", [])


async def tmdb_popular_by_genres(genre_ids: list) -> list:
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    data = await tmdb_get("/discover/movie", {
        "sort_by": "popularity.desc",
        "with_genres": ",".join(str(g) for g in genre_ids),
        "primary_release_date.gte": month_ago,
        "vote_count.gte": 20,
    })
    return sorted(data.get("results", []), key=lambda x: x.get("vote_average", 0), reverse=True)


# ─── DEEPSEEK ────────────────────────────────────────────────────────────────

async def ai_recommend(watched_movies: list = None, target_movie: str = None) -> list:
    if target_movie:
        prompt = (f"Recommend 5 movies similar to '{target_movie}'. "
                  "Reply ONLY with English movie titles, one per line, no numbering.")
    else:
        titles = [m["title"] for m in (watched_movies or [])]
        prompt = (f"User recently watched: {', '.join(titles)}. "
                  "Recommend 5 movies they would enjoy. "
                  "Reply ONLY with English movie titles, one per line, no numbering.")
    async with httpx.AsyncClient(timeout=30, proxy=None) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek/deepseek-chat",
                "max_tokens": 150,
                "messages": [
                    {"role": "system", "content": "You are a film expert. Give concise recommendations."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    titles = [ln.strip().lstrip("-•0123456789. )").strip() for ln in text.splitlines() if ln.strip()]
    return [t for t in titles if t][:5]


# ─── HELPERS ─────────────────────────────────────────────────────────────────

async def ensure_genres():
    global GENRES_MAP
    if not GENRES_MAP:
        GENRES_MAP = {g["id"]: g["name"] for g in await tmdb_genres()}


def cache_movies(movies: list):
    for m in movies:
        MOVIE_CACHE[m["id"]] = m


async def send_movie_card(bot, chat_id: int, movie: dict, watched_ids: set):
    """Send a single movie as photo+caption with watched button."""
    mid     = movie["id"]
    title   = movie.get("title", "?")
    year    = (movie.get("release_date") or "")[:4] or "?"
    rating  = movie.get("vote_average", 0)
    overview = (movie.get("overview") or "")[:200]
    if overview and not overview.endswith((".", "!", "?")):
        overview += "..."
    gids   = movie.get("genre_ids", [])
    gnames = [GENRES_MAP.get(gid, "?") for gid in gids[:3]]
    if not gnames and movie.get("genres"):
        gnames = [g["name"] for g in movie["genres"][:3]]

    icon   = "✅ Видел" if mid in watched_ids else "⬜ Отметить"
    kb     = InlineKeyboardMarkup([[InlineKeyboardButton(icon, callback_data=f"w_{mid}")]])
    caption = (
        f"*{title}* ({year})\n"
        f"⭐ {rating:.1f}" + (f" | {', '.join(gnames)}" if gnames else "") +
        (f"\n\n_{overview}_" if overview else "")
    )

    poster = movie.get("poster_path")
    if poster:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=f"{TMDB_IMG}{poster}",
                caption=caption,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"send_photo failed: {e}")

    return await bot.send_message(chat_id=chat_id, text=caption,
                                  parse_mode="Markdown", reply_markup=kb)


def fmt_movies(movies: list, watched_ids: set, page: int, total: int) -> str:
    lines = [f"🎬 *Результаты* (стр. {page}/{total})\n"]
    for i, m in enumerate(movies, 1):
        w = "✅" if m["id"] in watched_ids else "⬜"
        year   = (m.get("release_date") or "")[:4] or "?"
        rating = m.get("vote_average", 0)
        gnames = [GENRES_MAP.get(gid, "?") for gid in m.get("genre_ids", [])[:2]]
        lines.append(f"{i}. {w} *{m.get('title','?')}* ({year})\n   ⭐ {rating:.1f} | {', '.join(gnames)}")
    return "\n\n".join(lines[:1]) + "\n" + "\n\n".join(lines[1:])


def movies_kb(movies: list, watched_ids: set, page: int, total: int,
               pfx: str = "srchp") -> InlineKeyboardMarkup:
    buttons = []
    for m in movies:
        mid  = m["id"]
        icon = "✅" if mid in watched_ids else "⬜"
        buttons.append([InlineKeyboardButton(f"{icon} {m.get('title','?')[:24]}",
                                             callback_data=f"w_{mid}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=f"{pfx}_{page-1}"))
    if page < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"{pfx}_{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def rating_kb(movie_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(i), callback_data=f"rate_{movie_id}_{i}") for i in range(1, 6)],
        [InlineKeyboardButton(str(i), callback_data=f"rate_{movie_id}_{i}") for i in range(6, 11)],
        [InlineKeyboardButton("⏭ Пропустить", callback_data=f"rate_{movie_id}_0")],
    ])


# ─── COMMAND HANDLERS ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_upsert_user(update.effective_user.id)
    await update.message.reply_text(
        "🎬 *Привет! Я кинобот.*\n\nВыбери действие:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(update.effective_user.id),
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    db_upsert_user(tg_id)
    user  = db_get_user(tg_id)
    count = db_watched_count(tg_id)
    genres = user["genres"][:5] if user else []
    glines = (
        "\n".join(f"  {i+1}. {g['name']} ({g.get('count',0)} фильмов)" for i, g in enumerate(genres))
        or "  _Нет данных — отмечай просмотренные!_"
    )
    await update.message.reply_text(
        f"👤 *Профиль*\n\n🎬 Просмотрено: {count}\n\n❤️ Любимые жанры:\n{glines}",
        parse_mode="Markdown",
    )


def _build_wlist(items: list, total: int, page: int, total_pages: int):
    lines = [f"📋 *Просмотренные* ({total} фильмов, стр. {page}/{total_pages})\n"]
    buttons = []
    for i, m in enumerate(items, (page - 1) * 7 + 1):
        r = m["rating"]
        r_str = f"{'⭐' * (r // 2)}{'½' if r % 2 else ''} {r}/10" if r else "_без оценки_"
        lines.append(f"{i}. *{m['title']}*\n   {r_str} | {m['date']}")
        buttons.append([InlineKeyboardButton(
            f"🗑 {m['title'][:22]}", callback_data=f"wdel_{m['id']}"
        )])
    text = "\n\n".join(lines[:1]) + "\n" + "\n\n".join(lines[1:])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=f"wlp_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"wlp_{page+1}"))
    if nav:
        buttons.append(nav)
    return text, InlineKeyboardMarkup(buttons) if buttons else None


async def cmd_watched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    page  = context.user_data.get("wl_page", 1)
    items, total, total_pages = db_get_watched_list(tg_id, page)
    if not items:
        await update.message.reply_text(
            "📋 Список пуст.\nОтмечай фильмы в *Поиске*!", parse_mode="Markdown"
        )
        return
    text, kb = _build_wlist(items, total, page, total_pages)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ── Search (all via callbacks, no ConversationHandler) ──

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_genres()
    buttons, row = [], []
    for gid in POPULAR_GENRE_IDS:
        row.append(InlineKeyboardButton(GENRES_MAP.get(gid, "?"), callback_data=f"sg_{gid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🎲 Любой", callback_data="sg_any")])
    await update.message.reply_text(
        "🎭 *Шаг 1/3 — Жанр:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def search_genre_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data[3:]
    if val == "any":
        context.user_data["s_genre_ids"] = None
        context.user_data["s_genre_lbl"] = "Любой"
    else:
        gid = int(val)
        context.user_data["s_genre_ids"] = [gid]
        context.user_data["s_genre_lbl"] = GENRES_MAP.get(gid, "?")
    await q.edit_message_text(
        f"✅ Жанр: *{context.user_data['s_genre_lbl']}*\n\n⏱ *Шаг 2/3 — Длительность:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏃 < 90 мин",   callback_data="sd_short"),
             InlineKeyboardButton("🎯 90–120 мин", callback_data="sd_medium")],
            [InlineKeyboardButton("🎬 > 120 мин",  callback_data="sd_long"),
             InlineKeyboardButton("⏱ Любая",       callback_data="sd_any")],
        ]),
    )


async def search_dur_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data[3:]
    min_r, max_r, lbl = DURATION_OPTIONS[key]
    context.user_data["s_min_r"]   = min_r
    context.user_data["s_max_r"]   = max_r
    context.user_data["s_dur_lbl"] = lbl
    await q.edit_message_text(
        f"✅ Жанр: *{context.user_data['s_genre_lbl']}* | Длит.: *{lbl}*\n\n"
        f"🔞 *Шаг 3/3 — Возрастной ценз:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("G (0+)",      callback_data="sa_G"),
             InlineKeyboardButton("PG (6+)",     callback_data="sa_PG")],
            [InlineKeyboardButton("PG-13 (12+)", callback_data="sa_PG-13"),
             InlineKeyboardButton("R (16+)",     callback_data="sa_R")],
            [InlineKeyboardButton("🚫 Без ограничений", callback_data="sa_any")],
        ]),
    )


async def search_age_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    age = q.data[3:]
    context.user_data["s_age"]  = None if age == "any" else age
    context.user_data["s_page"] = 1
    await q.edit_message_text("🔍 _Ищу фильмы..._", parse_mode="Markdown")
    await _show_search_page(q.message, context)


async def _show_search_page(loading_msg, context: ContextTypes.DEFAULT_TYPE):
    bot     = context.bot
    chat_id = loading_msg.chat_id
    page    = context.user_data.get("s_page", 1)
    movies, total = await tmdb_discover(
        genre_ids=context.user_data.get("s_genre_ids"),
        min_runtime=context.user_data.get("s_min_r"),
        max_runtime=context.user_data.get("s_max_r"),
        certification=context.user_data.get("s_age"),
        page=page,
    )
    if not movies:
        await loading_msg.edit_text("😕 Ничего не найдено. Попробуй другие параметры.")
        return
    cache_movies(movies)
    watched_ids = db_get_watched_ids(chat_id)

    await loading_msg.edit_text(
        f"🎬 *Результаты* (стр. {page}/{total}):", parse_mode="Markdown"
    )
    sent_ids = [loading_msg.message_id]

    for m in movies[:5]:
        msg = await send_movie_card(bot, chat_id, m, watched_ids)
        if msg:
            sent_ids.append(msg.message_id)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=f"srchp_{page-1}"))
    if page < total:
        nav.append(InlineKeyboardButton("▶", callback_data=f"srchp_{page+1}"))
    if nav:
        nav_msg = await bot.send_message(
            chat_id, "Страницы:", reply_markup=InlineKeyboardMarkup([nav])
        )
        sent_ids.append(nav_msg.message_id)

    context.user_data["s_msg_ids"] = sent_ids


async def search_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        context.user_data["s_page"] = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        return
    chat_id = q.message.chat_id
    for mid in context.user_data.pop("s_msg_ids", []):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    loading = await context.bot.send_message(chat_id, "🔍 _Загружаю..._", parse_mode="Markdown")
    await _show_search_page(loading, context)


# ── Top ──

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 *Топ по твоим жанрам:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Топ недели",  callback_data="top_week"),
            InlineKeyboardButton("📆 Топ месяца", callback_data="top_month"),
        ]]),
    )


async def top_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = q.from_user.id
    db_upsert_user(tg_id)
    await ensure_genres()

    user       = db_get_user(tg_id)
    top_genres = user["genres"][:3] if user else []
    watched_ids = db_get_watched_ids(tg_id)
    period     = q.data.split("_")[1]

    if period == "week":
        all_movies = await tmdb_trending("week")
        label = "📅 Топ недели"
        if top_genres:
            gids     = {g["id"] for g in top_genres}
            filtered = [m for m in all_movies if any(gid in gids for gid in m.get("genre_ids", []))]
            movies   = (filtered or all_movies)[:3]
        else:
            movies = all_movies[:3]
    else:
        label = "📆 Топ месяца"
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        data = await tmdb_get("/discover/movie", {
            "sort_by": "popularity.desc",
            "primary_release_date.gte": month_ago,
            "vote_count.gte": 10,
        })
        all_movies = data.get("results", [])
        if top_genres:
            gids     = {g["id"] for g in top_genres}
            filtered = [m for m in all_movies if any(gid in gids for gid in m.get("genre_ids", []))]
            movies   = (filtered or all_movies)[:3]
        else:
            movies = all_movies[:3]

    cache_movies(movies)

    genre_note = (f"_По жанрам: {', '.join(g['name'] for g in top_genres)}_"
                  if top_genres else "_Отмечай фильмы для персонализации_")
    await q.edit_message_text(f"🏆 *{label}*\n{genre_note}", parse_mode="Markdown")

    for m in movies:
        await send_movie_card(context.bot, q.message.chat_id, m, watched_ids)


# ── Recommend ──

async def cmd_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    db_upsert_user(tg_id)
    last = db_get_last_watched(tg_id, 5)
    if len(last) < 2:
        await update.message.reply_text(
            "😕 Нужно хотя бы 2 просмотренных фильма.\n"
            "Используй *Поиск* → нажми ⬜ чтобы отметить.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        f"🤖 _Анализирую: {', '.join(m['title'] for m in last)}..._",
        parse_mode="Markdown",
    )
    try:
        rec_titles = await ai_recommend(watched_movies=last)
    except Exception as e:
        logger.error(f"ai_recommend: {e}")
        await update.message.reply_text("❌ Ошибка ИИ. Попробуй позже.")
        return
    await _show_recs(context.bot, update.effective_chat.id,
                     rec_titles, "🎯 *Рекомендации для тебя:*", tg_id)


# ── Similar ──

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_find"] = True
    await update.message.reply_text("🔎 Введи название фильма:")


async def find_title_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    tg_id = update.effective_user.id
    context.user_data["awaiting_find"] = False
    await ensure_genres()
    results = await tmdb_search(title)
    if not results:
        await update.message.reply_text("😕 Ничего не найдено.")
        return
    movies = results[:10]
    cache_movies(movies)
    watched_ids = db_get_watched_ids(tg_id)
    await _show_movie_list(
        update.message, movies, watched_ids,
        header=f"🔎 *По запросу «{title}»:*"
    )


async def cmd_similar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_similar"] = True
    await update.message.reply_text("🎬 Введи название фильма:")


async def similar_title_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    tg_id = update.effective_user.id
    context.user_data["awaiting_similar"] = False
    await update.message.reply_text(f"🤖 _Ищу похожие на «{title}»..._", parse_mode="Markdown")
    try:
        rec_titles = await ai_recommend(target_movie=title)
    except Exception as e:
        logger.error(f"ai_similar: {e}")
        await update.message.reply_text("❌ Ошибка ИИ. Попробуй позже.")
        return
    await _show_recs(context.bot, update.effective_chat.id,
                     rec_titles, f"🎯 *Похожие на «{title}»:*", tg_id, as_list=True)


async def _show_movie_list(message, movies: list, watched_ids: set, header: str):
    """Numbered text list + one ℹ️ button per movie to open a detail card."""
    lines = [header + "\n"]
    buttons = []
    for i, m in enumerate(movies, 1):
        w     = "✅" if m["id"] in watched_ids else "⬜"
        year  = (m.get("release_date") or "")[:4] or "?"
        rat   = m.get("vote_average", 0)
        gids  = m.get("genre_ids", [])
        gname = GENRES_MAP.get(gids[0], "") if gids else ""
        lines.append(f"{i}. {w} *{m.get('title','?')}* ({year}) ⭐{rat:.1f}"
                     + (f" · {gname}" if gname else ""))
        buttons.append([InlineKeyboardButton(
            f"ℹ️ {m.get('title','?')[:30]}", callback_data=f"detail_{m['id']}"
        )])
    text = "\n".join(lines)
    await message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _show_recs(bot, chat_id: int, rec_titles: list, header: str, tg_id: int,
                     as_list: bool = False):
    watched_ids = db_get_watched_ids(tg_id)
    rec_movies  = []
    for title in rec_titles:
        results = await tmdb_search(title)
        if results:
            m = results[0]
            cache_movies([m])
            rec_movies.append(m)
    if not rec_movies:
        await bot.send_message(chat_id=chat_id, text="😕 Не удалось найти рекомендации. Попробуй позже.")
        return

    if as_list:
        # Создаём объект-обёртку чтобы переиспользовать _show_movie_list
        class _Msg:
            def __init__(self, bot, chat_id):
                self._bot = bot
                self.chat_id = chat_id
            async def reply_text(self, *args, **kwargs):
                return await self._bot.send_message(self.chat_id, *args, **kwargs)
        await _show_movie_list(_Msg(bot, chat_id), rec_movies, watched_ids, header)
    else:
        await bot.send_message(chat_id=chat_id, text=header, parse_mode="Markdown")
        for m in rec_movies:
            await send_movie_card(bot, chat_id, m, watched_ids)


# ─── CALLBACK HANDLERS ───────────────────────────────────────────────────────

async def detail_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает карточку фильма по нажатию ℹ️ в списке."""
    q = update.callback_query
    await q.answer()
    try:
        movie_id = int(q.data.split("_", 1)[1])
    except (IndexError, ValueError):
        return
    tg_id = q.from_user.id
    movie = MOVIE_CACHE.get(movie_id)
    if not movie:
        await q.message.reply_text("❌ Данные не найдены")
        return
    watched_ids = db_get_watched_ids(tg_id)
    await ensure_genres()
    await send_movie_card(context.bot, q.message.chat_id, movie, watched_ids)


async def watched_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg_id    = q.from_user.id
    movie_id = int(q.data.split("_", 1)[1])

    if movie_id in db_get_watched_ids(tg_id):
        await q.answer("Уже в просмотренных ✅", show_alert=True)
        return

    movie = MOVIE_CACHE.get(movie_id)
    if not movie:
        await q.answer("❌ Данные не найдены", show_alert=True)
        return

    title       = movie.get("title", "?")
    poster_path = movie.get("poster_path", "") or ""
    genres_full = movie.get("genres", [])
    if genres_full:
        genres = [{"id": g["id"], "name": g["name"]} for g in genres_full]
    else:
        await ensure_genres()
        genres = [{"id": gid, "name": GENRES_MAP.get(gid, "?")} for gid in movie.get("genre_ids", [])]

    if not db_add_watched(tg_id, movie_id, title, genres, poster_path):
        await q.answer("❌ Ошибка сохранения", show_alert=True)
        return

    await q.answer(f"✅ «{title}» добавлен!")

    # Update button icon
    try:
        new_rows = [
            [InlineKeyboardButton(f"✅ {title[:24]}", callback_data=f"w_{movie_id}")
             if btn.callback_data == f"w_{movie_id}" else btn
             for btn in row]
            for row in q.message.reply_markup.inline_keyboard
        ]
        await q.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
    except Exception:
        pass

    # Ask for rating
    await q.message.reply_text(
        f"⭐ Оцени «*{title}*» от 1 до 10:",
        parse_mode="Markdown",
        reply_markup=rating_kb(movie_id),
    )


async def rate_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    parts  = q.data.split("_")
    try:
        mid, score = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        await q.answer("❌ Некорректные данные", show_alert=True)
        return
    tg_id  = q.from_user.id

    if score == 0:
        await q.answer("Пропущено")
        await q.edit_message_text("_Оценка пропущена_", parse_mode="Markdown")
        return

    db_update_rating(tg_id, mid, score)
    title = MOVIE_CACHE.get(mid, {}).get("title", "фильм")
    stars = "⭐" * min(score // 2, 5)
    await q.answer(f"{score}/10 сохранено!")
    await q.edit_message_text(
        f"✅ *{title}*\nТвоя оценка: *{score}/10* {stars}",
        parse_mode="Markdown",
    )


async def wlist_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    try:
        page = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        return
    context.user_data["wl_page"] = page
    tg_id = q.from_user.id
    items, total, total_pages = db_get_watched_list(tg_id, page)
    if not items:
        await q.edit_message_text("📋 Список пуст.", parse_mode="Markdown")
        return
    text, kb = _build_wlist(items, total, page, total_pages)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def wdel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    tg_id    = q.from_user.id
    try:
        movie_id = int(q.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await q.answer("❌ Некорректные данные", show_alert=True)
        return

    title = MOVIE_CACHE.get(movie_id, {}).get("title")
    if not title:
        # достаём из БД
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT title FROM watched WHERE tg_id=? AND movie_id=?", (tg_id, movie_id)
            ).fetchone()
        title = row[0] if row else "фильм"

    db_remove_watched(tg_id, movie_id)
    await q.answer(f"🗑 «{title}» удалён из просмотренных")

    # обновляем список на той же странице
    page = context.user_data.get("wl_page", 1)
    items, total, total_pages = db_get_watched_list(tg_id, page)
    if page > total_pages:
        page = max(1, total_pages)
        context.user_data["wl_page"] = page
        items, total, total_pages = db_get_watched_list(tg_id, page)

    if not items:
        await q.edit_message_text("📋 Список пуст.", parse_mode="Markdown")
        return
    text, kb = _build_wlist(items, total, page, total_pages)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ── Web App data ──

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    db_upsert_user(tg_id)
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception:
        return

    action = data.get("action")

    if action == "watch":
        mid    = int(data["movie_id"])
        title  = data.get("title", "?")
        genres = data.get("genres", [])
        poster = data.get("poster_path", "")
        MOVIE_CACHE[mid] = {"id": mid, "title": title, "genres": genres, "poster_path": poster}
        if db_add_watched(tg_id, mid, title, genres, poster):
            await update.message.reply_text(
                f"✅ «*{title}*» добавлен в просмотренные!\n\nОцени его:",
                parse_mode="Markdown",
                reply_markup=rating_kb(mid),
            )
        else:
            await update.message.reply_text(f"«{title}» уже в просмотренных.")

    elif action == "rate":
        mid   = int(data["movie_id"])
        score = int(data.get("rating", 0))
        title = data.get("title", "фильм")
        if score:
            db_update_rating(tg_id, mid, score)
            stars = "⭐" * min(score // 2, 5)
            await update.message.reply_text(
                f"✅ «*{title}*» — оценка *{score}/10* {stars}",
                parse_mode="Markdown",
            )


# ── Menu text ──

async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dispatch = {
        "🔍 Поиск":         cmd_search,
        "🏆 Топ":           cmd_top,
        "🤖 Подборка":      cmd_recommend,
        "🎬 Похожие":       cmd_similar,
        "👤 Профиль":       cmd_profile,
        "📋 Просмотренные": lambda u, c: (
            c.user_data.__setitem__("wl_page", 1) or cmd_watched(u, c)
        ),
        "🔎 По названию": cmd_find,
    }
    if text in dispatch:
        await dispatch[text](update, context)
    elif context.user_data.get("awaiting_find"):
        await find_title_msg(update, context)
    elif context.user_data.get("awaiting_similar"):
        await similar_title_msg(update, context)


# ─── API SERVER ───────────────────────────────────────────────────────────────

class BotAPIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.join(SCRIPT_DIR, "webapp"), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api(parsed)
        else:
            super().do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json({"error": "invalid json"}, 400)
            return

        tg_id    = int(body.get("user_id",  0))
        movie_id = int(body.get("movie_id", 0))

        if parsed.path == "/api/mark_watched":
            title   = body.get("title", "")
            genres  = body.get("genres", [])
            poster  = body.get("poster_path", "")
            if not (tg_id and movie_id):
                self._json({"error": "missing params"}, 400)
                return
            db_upsert_user(tg_id)
            ok = db_add_watched(tg_id, movie_id, title, genres, poster)
            self._json({"ok": ok})

        elif parsed.path == "/api/rate":
            rating = int(body.get("rating", 0))
            if not (tg_id and movie_id and rating):
                self._json({"error": "missing params"}, 400)
                return
            db_update_rating(tg_id, movie_id, rating)
            self._json({"ok": True})

        else:
            self._json({"error": "not found"}, 404)

    def _handle_api(self, parsed):
        params  = urllib.parse.parse_qs(parsed.query)
        path    = parsed.path
        tg_id   = int(params.get("user_id", ["0"])[0])


        if path == "/api/watched":
            page     = int(params.get("page",     ["1"])[0])
            per_page = int(params.get("per_page", ["20"])[0])
            items, total, pages = db_get_watched_list(tg_id, page, per_page)
            self._json({"items": items, "total": total, "pages": pages})

        elif path == "/api/wishlist":
            self._json({"items": db_get_wishlist(tg_id)})

        elif path == "/api/wishlist/add":
            movie_id = int(params.get("movie_id", ["0"])[0])
            title    = params.get("title",  [""])[0]
            poster   = params.get("poster_path", [""])[0]
            if tg_id and movie_id:
                db_add_wishlist(tg_id, movie_id, title, poster)
                self._json({"ok": True})
            else:
                self._json({"error": "missing params"}, 400)

        elif path == "/api/wishlist/remove":
            movie_id = int(params.get("movie_id", ["0"])[0])
            if tg_id and movie_id:
                db_remove_wishlist(tg_id, movie_id)
                self._json({"ok": True})
            else:
                self._json({"error": "missing params"}, 400)

        elif path == "/api/profile":
            user   = db_get_user(tg_id)
            count  = db_watched_count(tg_id)
            genres = user["genres"][:5] if user else []
            self._json({"genres": genres, "count": count})

        else:
            self._json({"error": "not found"}, 404)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self._raw(body, status, "application/json; charset=utf-8")

    def _raw(self, data: bytes, status: int = 200, content_type: str = "application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_api_server():
    server = HTTPServer(("0.0.0.0", API_PORT), BotAPIHandler)
    logger.info(f"API server on port {API_PORT}")
    server.serve_forever()


# ─── TUNNEL (Cloudflare) ─────────────────────────────────────────────────────

async def setup_tunnel() -> str:
    """Download cloudflared if needed, start tunnel, return HTTPS URL."""
    global TUNNEL_PROC, WEB_APP_URL

    # Download cloudflared.exe on first run
    if not os.path.exists(CLOUDFLARED_PATH):
        url = ("https://github.com/cloudflare/cloudflared/releases/latest"
               "/download/cloudflared-windows-amd64.exe")
        print("Скачиваю cloudflared (первый раз ~35 MB)...", flush=True)
        try:
            async with httpx.AsyncClient(proxy=None, follow_redirects=True,
                                          timeout=180) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    done  = 0
                    with open(CLOUDFLARED_PATH, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                print(f"\r   {done*100//total}%", end="", flush=True)
            print("\rcloudflared downloaded!      ")
        except Exception as e:
            logger.error(f"Не удалось скачать cloudflared: {e}")
            return ""

    # Start tunnel
    print("Starting tunnel...", flush=True)
    log_path = os.path.join(SCRIPT_DIR, "cloudflared_log.txt")
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        # Clean env: strip broken system proxy, force http2 for VPN compat
        clean_env = os.environ.copy()
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "ALL_PROXY", "all_proxy"]:
            clean_env.pop(k, None)
        clean_env["NO_PROXY"] = "*"
        proc = subprocess.Popen(
            [CLOUDFLARED_PATH, "tunnel", "--protocol", "http2",
             "--url", f"http://localhost:{API_PORT}"],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=clean_env,
        )
        TUNNEL_PROC = proc

        # Poll log file until URL appears (max 30 seconds)
        for _ in range(30):
            await asyncio.sleep(1)
            try:
                with open(log_path, encoding="utf-8") as f:
                    content = f.read()
                m = re.search(r"https://[\w-]+\.trycloudflare\.com", content)
                if m:
                    tunnel_url = m.group(0)
                    WEB_APP_URL = tunnel_url
                    print(f"Mini-app: {tunnel_url}", flush=True)
                    return tunnel_url
            except Exception:
                pass

        logger.warning("Tunnel URL not found in 30s — mini-app unavailable")
        return ""

    except Exception as e:
        logger.error(f"Tunnel error: {e}")
        return ""


# ─── GLOBAL ERROR HANDLER ────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception", exc_info=context.error)

    # Определяем куда отправить сообщение об ошибке
    chat_id = None
    if isinstance(update, Update):
        if update.effective_chat:
            chat_id = update.effective_chat.id
        # Если это callback query — сначала отвечаем, чтобы убрать часики
        if update.callback_query:
            try:
                await update.callback_query.answer("❌ Что-то пошло не так", show_alert=True)
            except Exception:
                pass

    if chat_id:
        try:
            await context.bot.send_message(
                chat_id,
                "⚠️ Произошла ошибка. Попробуй ещё раз или начни заново — /start",
            )
        except Exception:
            pass


# ─── MAIN ────────────────────────────────────────────────────────────────────

async def post_init(application):
    """Run setup_tunnel inside the bot event loop."""
    await setup_tunnel()


def main():
    init_db()

    # Start API server in background thread
    t = threading.Thread(target=start_api_server, daemon=True)
    t.start()

    logger.info("Starting Movie Bot...")
    request = HTTPXRequest(proxy=None)
    app = Application.builder().token(TELEGRAM_TOKEN).request(request).post_init(post_init).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("search",    cmd_search))
    app.add_handler(CommandHandler("top",       cmd_top))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CommandHandler("similar",   cmd_similar))
    app.add_handler(CommandHandler("watched",   cmd_watched))
    app.add_handler(CommandHandler("profile",   cmd_profile))

    app.add_handler(CommandHandler("find",      cmd_find))

    app.add_handler(CallbackQueryHandler(detail_cb,       pattern="^detail_"))
    app.add_handler(CallbackQueryHandler(search_genre_cb, pattern="^sg_"))
    app.add_handler(CallbackQueryHandler(search_dur_cb,   pattern="^sd_"))
    app.add_handler(CallbackQueryHandler(search_age_cb,   pattern="^sa_"))
    app.add_handler(CallbackQueryHandler(search_page_cb,  pattern="^srchp_"))
    app.add_handler(CallbackQueryHandler(top_cb,          pattern="^top_"))
    app.add_handler(CallbackQueryHandler(watched_cb,      pattern="^w_"))
    app.add_handler(CallbackQueryHandler(rate_cb,         pattern="^rate_"))
    app.add_handler(CallbackQueryHandler(wlist_page_cb,   pattern="^wlp_"))
    app.add_handler(CallbackQueryHandler(wdel_cb,         pattern="^wdel_"))

    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))

    app.add_error_handler(error_handler)

    logger.info("Bot running. Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
