# Архитектура ASfilmbot

## Стек

| | |
|---|---|
| Язык | Python 3.10+ |
| Telegram | python-telegram-bot 20+ (Long Polling) |
| HTTP | httpx (async) |
| БД | SQLite3 (stdlib) |
| Внешние API | TMDB, OpenRouter (DeepSeek) |
| Запуск | `NO_PROXY="*" python asfilmbot.py` |

---

## Архитектура

```
Пользователь
     │
     ▼
Telegram API
     │
     ▼
python-telegram-bot (Dispatcher)
     │
     ├── Handlers (команды и коллбэки)
     │        │
     │        ├── TMDB API      (поиск, жанры, тренды)
     │        ├── OpenRouter    (DeepSeek — рекомендации)
     │        └── SQLite3       (пользователи, просмотренное)
     │
     └── HTTP Server :8080 (Telegram Mini App)
              │
              └── Cloudflare Tunnel
```

---

## Слои

**Handlers** — команды (`/search`, `/top`, `/recommend`, `/similar`, `/profile`) и callback-кнопки

**Service** — `ai_recommend()`, `send_movie_card()`, `build_main_menu()`, пагинация

**Integration** — `tmdb_*()` функции (httpx → TMDB), `ai_recommend()` (httpx → OpenRouter)

**Data** — `db_*()` функции (SQLite3)

---

## База данных

```
users
  tg_id   INTEGER  PK
  genres  TEXT     JSON  ← счётчик жанров по просмотрам

watched
  tg_id, movie_id, title, genres, poster_path, rating, watched_at
  UNIQUE(tg_id, movie_id)

wishlist
  tg_id, movie_id, title, poster_path, added_at
  UNIQUE(tg_id, movie_id)
```

`users.genres` — JSON с частотой просмотров по жанрам, используется в `/top`, `/recommend`, `/profile`.

---

## Команды и коллбэки

| Команда | Что делает |
|---|---|
| `/search` | Поиск: жанр → длина → ценз → результаты |
| `/top` | Трендовые фильмы по топ-3 жанрам пользователя |
| `/recommend` | AI-подборка по 5 последним просмотрам |
| `/similar` | AI-подборка похожих по названию |
| `/profile` | Статистика и предпочтения |

| Коллбэк | Действие |
|---|---|
| `sg_` / `sd_` / `sa_` | Шаги поиска |
| `w_` | Отметить просмотренным |
| `rate_` | Оценить фильм |
| `wdel_` / `detail_` | Удалить / показать карточку |

