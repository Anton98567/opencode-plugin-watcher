#!/usr/bin/env python3
"""
OpenCode Plugin Watcher
Следит за экосистемой OpenCode (плагины, npm-пакеты, курируемые списки)
и присылает недельный дайджест в Telegram.

Только стандартная библиотека Python 3.9+.

Команды:
    python3 watcher.py run              # сканировать + отправить дайджест
    python3 watcher.py run --dry-run    # сканировать, показать, не отправлять
    python3 watcher.py scan             # только сканирование, без отправки
    python3 watcher.py send-test        # тестовое сообщение в Telegram
    python3 watcher.py stats            # статистика по базе
    python3 watcher.py list --limit 30  # последние найденные элементы
    python3 watcher.py mark-sent        # пометить всё текущее как отправленное
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
DB_PATH = os.path.join(BASE_DIR, "data", "watcher.db")
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")
LOCK_PATH = os.path.join(BASE_DIR, "data", "watcher.lock")

UA = "opencode-plugin-watcher/1.0 (+https://opencode.ai)"
TG_API = "https://api.telegram.org/bot{token}/{method}"
GH_API = "https://api.github.com"
NPM_SEARCH = "https://registry.npmjs.org/-/v1/search"
NPM_DOC = "https://registry.npmjs.org/{name}"
ECOSYSTEM_URL = "https://opencode.ai/docs/ecosystem/"
AWESOME_URL = "https://raw.githubusercontent.com/awesome-opencode/awesome-opencode/main/README.md"

REPO_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
OPENCODE_RE = re.compile(r"(?i)open[\s_-]?code")
V2_RE = re.compile(r"(?i)(opencode\s*v?2|\bv2\b|version\s*2|REL1_41|rel_?41)")


# --------------------------------------------------------------------------- #
# утилиты
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_env(path: str = ENV_PATH) -> dict:
    """Минимальный парсер .env (KEY=VALUE, # комментарии, опциональные кавычки)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def http_get(url: str, headers: dict | None = None, timeout: int = 25, retries: int = 3) -> str:
    """GET с ретраями на 429/5xx. Бросает исключение при упорной ошибке."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code in (403, 429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                log(f"  HTTP {exc.code} на {url[:90]}… → повтор через {wait}с")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_err  # pragma: no cover


def http_get_json(url: str, headers: dict | None = None, **kw) -> dict:
    return json.loads(http_get(url, headers=headers, **kw))


def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 90) -> dict:
    hdrs = {"User-Agent": UA, "Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def clean_text(value: str | None, limit: int = 220) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = text.replace("|", "/").replace("*", "").replace("_", " ")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -—:")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def norm_repo(url: str | None) -> str | None:
    if not url:
        return None
    match = REPO_RE.search(url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner.lower() in {"sponsors", "features"}:
        return None
    return f"{owner}/{repo}".lower()


# --------------------------------------------------------------------------- #
# база
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    kind          TEXT,
    npm           TEXT,
    repo          TEXT,
    url           TEXT,
    description   TEXT,
    stars         INTEGER DEFAULT 0,
    version       TEXT,
    published     TEXT,
    pushed        TEXT,
    topics        TEXT,
    v2            INTEGER DEFAULT 0,
    sources       TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    stars_prev    INTEGER,
    announced     INTEGER DEFAULT 0,
    prev_version  TEXT
);
CREATE TABLE IF NOT EXISTS translations (
    k       TEXT PRIMARY KEY,
    lang    TEXT,
    text    TEXT,
    model   TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started    TEXT,
    finished   TEXT,
    new_items  INTEGER DEFAULT 0,
    updates    INTEGER DEFAULT 0,
    errors     INTEGER DEFAULT 0,
    sent       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_items_seen ON items(first_seen DESC);
"""


def db_connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


def classify(kind: str | None) -> str:
    mapping = {
        "plugin": "🧩 Плагин",
        "theme": "🎨 Тема",
        "agent": "🤖 Агент/скилл",
        "project": "🛠 Проект",
        "mcp": "🔌 MCP",
        "skill": "🧠 Скилл",
        "resource": "📚 Ресурс",
    }
    return mapping.get((kind or "plugin").lower(), "🧩 Плагин")


# --------------------------------------------------------------------------- #
# перевод описаний на русский
# --------------------------------------------------------------------------- #
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def is_translated(src: str, out: str) -> bool:
    """True, если в результате перевода появилась кириллица.

    Нужен, потому что дешёвые провайдеры иногда возвращают исходную строку
    без изменений — такой результат нельзя писать в кэш.
    """
    if not out:
        return False
    if not LATIN_RE.search(src):
        return True
    return bool(CYRILLIC_RE.search(out))


def translation_cache_key(desc: str, lang: str, max_chars: int) -> str:
    return hashlib.sha1(f"{lang}|{max_chars}|{desc}".encode("utf-8")).hexdigest()


def parse_json_list(content: str) -> list[dict]:
    """Достаёт JSON-массив из ответа модели, даже если она обернула его в ```."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```$", "", text.strip())
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
        return []
    return [v for v in data if isinstance(v, dict)]


def llm_translate(todo: dict, lang: str, max_chars: int, tcfg: dict) -> dict[str, str]:
    """Пакетный перевод через OpenAI-совместимый API (по умолчанию Pollinations)."""
    endpoint = tcfg.get("endpoint", "https://gen.pollinations.ai/v1/chat/completions")
    model = tcfg.get("model", "openai/gpt-5.4-nano")
    api_key = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    batch = int(tcfg.get("batch", 12))
    timeout = int(tcfg.get("timeout", 90))
    names = {"ru": "русский", "en": "английский", "de": "немецкий", "fr": "французский"}
    out: dict[str, str] = {}
    items = list(todo.items())
    for start in range(0, len(items), batch):
        chunk = items[start : start + batch]
        listing = "\n".join(f"{num}. {desc}" for num, (_, desc) in enumerate(chunk, 1))
        prompt = (
            f"Ниже описания проектов и плагинов экосистемы OpenCode (английский).\n"
            f"Для каждого пункта напиши ПО-РУССКИ коротко, что он делает: 1 предложение, до {max_chars} символов.\n"
            f"Правила: без маркетинговых прикрас, без «этот плагин позволяет вам»;\n"
            f"имена продуктов, npm, MCP, LSP, TUI, Bun, Zed, Claude Code оставляй как есть;\n"
            f"не выдумывай функций, которых нет в описании.\n"
            f"Верни ТОЛЬКО JSON-массив объектов {{\"n\": <номер пункта>, \"ru\": \"<текст>\"}} "
            f"длиной {len(chunk)}.\n\n{listing}"
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        raw = http_post_json(endpoint, payload, {"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        content = raw["choices"][0]["message"]["content"]
        for obj in parse_json_list(content):
            try:
                num = int(obj.get("n", 0))
            except (TypeError, ValueError):
                continue
            text = clean_text(str(obj.get("ru") or ""), max_chars)
            source_desc = chunk[num - 1][1] if 1 <= num <= len(chunk) else ""
            if text and is_translated(source_desc, text):
                out[chunk[num - 1][0]] = text
        time.sleep(0.4)
    return out


def google_translate(todo: dict, lang: str, tcfg: dict) -> dict[str, str]:
    """Бесплатный публичный endpoint Google Translate, без ключа.

    Эндпоинт официально не документирован и часто отвечает 429, поэтому:
    без ретраев и с предохранителем — 3 ошибки подряд, и провайдер отключается.
    """
    out: dict[str, str] = {}
    delay = float(tcfg.get("delay", 0.35))
    fails = 0
    for cache_key, desc in todo.items():
        if fails >= 3:
            log("  google: 3 ошибки подряд — провайдер отключён")
            break
        url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "auto", "tl": lang, "dt": "t", "q": desc[:480]}
        )
        try:
            data = http_get_json(url, timeout=20, retries=1)
            segments = data[0] or []
            text = clean_text("".join(seg[0] for seg in segments if seg and seg[0]), int(tcfg.get("max_chars", 150)) * 2)
        except Exception:  # noqa: BLE001
            fails += 1
            continue
        if is_translated(desc, text):
            out[cache_key] = text
        else:
            fails += 1
        time.sleep(delay)
    return out


def mymemory_translate(todo: dict, lang: str, tcfg: dict) -> dict[str, str]:
    """MyMemory — бесплатный API без ключа (с email в config лимит выше)."""
    out: dict[str, str] = {}
    email = tcfg.get("mymemory_email", "")
    max_chars = int(tcfg.get("max_chars", 150))
    fails = 0
    for cache_key, desc in todo.items():
        if fails >= 3:
            log("  mymemory: 3 ошибки подряд — провайдер отключён")
            break
        params = {"q": desc[:480], "langpair": f"en|{lang}"}
        if email:
            params["de"] = email
        url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params)
        try:
            data = http_get_json(url, timeout=20, retries=1)
            if int(data.get("responseStatus") or 0) != 200:
                raise RuntimeError(f"status {data.get('responseStatus')}")
            text = clean_text((data.get("responseData") or {}).get("translatedText") or "", max_chars * 2)
        except Exception:  # noqa: BLE001
            fails += 1
            continue
        upper = text.upper()
        if "ALL AVAILABLE FREE TRANSLATIONS" in upper or "QUOTA" in upper:
            log("  mymemory: дневная бесплатная квота исчерпана — провайдер отключён")
            break
        if not text or "MYMEMORY WARNING" in upper or "QUERY LENGTH LIMIT" in upper:
            continue
        if not is_translated(desc, text):
            fails += 1
            continue
        out[cache_key] = text
        time.sleep(0.4)
    return out


def translate_rows(conn: sqlite3.Connection, rows, cfg: dict) -> dict[str, str]:
    """Переводит описания показываемых элементов на русский. Возвращает {key: текст}."""
    tcfg = cfg.get("translate") or {}
    if not tcfg.get("enabled", True):
        return {}
    lang = tcfg.get("lang", "ru")
    max_chars = int(tcfg.get("max_chars", 150))

    result: dict[str, str] = {}
    todo: dict[str, str] = {}
    rows_by_cache: dict[str, list[str]] = {}
    for row in rows:
        desc = (row["description"] or "").strip()
        if not desc or not LATIN_RE.search(desc):
            continue
        cache_key = translation_cache_key(desc, lang, max_chars)
        cached = conn.execute("SELECT text FROM translations WHERE k=?", (cache_key,)).fetchone()
        if cached:
            result[row["key"]] = cached["text"]
        else:
            todo.setdefault(cache_key, desc)
            rows_by_cache.setdefault(cache_key, []).append(row["key"])

    if not todo:
        log(f"Перевод: всё из кэша ({len(result)} шт.)")
        return result
    log(f"Перевод: {len(todo)} описаний (из кэша {len(result)})")

    providers = tcfg.get("providers", ["pollinations", "google", "mymemory"])
    for provider in providers:
        if not todo:
            break
        try:
            if provider == "pollinations":
                if not os.environ.get("POLLINATIONS_API_KEY", "").strip():
                    log("  pollinations: нет POLLINATIONS_API_KEY в .env — пропуск")
                    continue
                got = llm_translate(todo, lang, max_chars, tcfg)
            elif provider == "google":
                got = google_translate(todo, lang, tcfg)
            elif provider == "mymemory":
                got = mymemory_translate(todo, lang, tcfg)
            else:
                log(f"  неизвестный провайдер перевода: {provider}")
                continue
        except Exception as exc:  # noqa: BLE001
            log(f"  {provider}: ошибка — {exc}")
            continue
        if not got:
            continue
        log(f"  {provider}: переведено {len(got)}")
        for cache_key, text in got.items():
            conn.execute(
                "INSERT OR REPLACE INTO translations(k,lang,text,model,created) VALUES(?,?,?,?,?)",
                (cache_key, lang, text, provider, iso(now_utc())),
            )
            for row_key in rows_by_cache.get(cache_key, []):
                result[row_key] = text
            todo.pop(cache_key, None)
        conn.commit()

    if todo:
        log(f"  ⚠️  не переведено {len(todo)} — показан оригинал")
    return result


# --------------------------------------------------------------------------- #
# источники
# --------------------------------------------------------------------------- #
def source_github(cfg: dict, since: datetime) -> list[dict]:
    """Поиск репозиториев через GitHub Search API."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    since_str = since.strftime("%Y-%m-%d")
    min_stars = int(cfg.get("filters", {}).get("min_stars", 0))
    items: list[dict] = []
    seen_repos: set[str] = set()

    for template in cfg.get("github_queries", []):
        query = template.format(since=since_str, since7=(now_utc() - timedelta(days=7)).strftime("%Y-%m-%d"))
        url = (
            f"{GH_API}/search/repositories?"
            + urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": 100})
        )
        log(f"GitHub: {query}")
        try:
            data = http_get_json(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! ошибка GitHub: {exc}")
            continue
        for raw in data.get("items", []):
            if raw.get("fork") or raw.get("archived"):
                continue
            repo = (raw.get("full_name") or "").lower()
            if not repo or repo in seen_repos:
                continue
            topics = [t.lower() for t in (raw.get("topics") or [])]
            desc = raw.get("description") or ""
            relevant = (
                any(t in ("opencode", "opencode-plugin") for t in topics)
                or OPENCODE_RE.search(repo.replace("/", "-"))
                or re.search(r"(?i)opencode[-_ ]?(plugin|extension|toolkit|integration)", desc)
            )
            if not relevant:
                continue
            stars = int(raw.get("stargazers_count") or 0)
            if stars < min_stars:
                continue
            seen_repos.add(repo)
            items.append(
                {
                    "key": f"gh:{repo}",
                    "title": raw.get("name") or repo.split("/")[-1],
                    "kind": "plugin",
                    "repo": repo,
                    "url": raw.get("html_url"),
                    "description": clean_text(desc),
                    "stars": stars,
                    "pushed": raw.get("pushed_at"),
                    "topics": ",".join(topics[:8]),
                    "v2": 1 if (V2_RE.search(desc) or "opencode-v2" in topics or "v2" in topics) else 0,
                    "sources": "github",
                }
            )
    return items


def fetch_npm_meta(name: str) -> dict:
    """Полный документ пакета: версия, репозиторий, время публикации."""
    url = NPM_DOC.format(name=urllib.parse.quote(name, safe="@/"))
    data = http_get_json(url, timeout=25, retries=2)
    latest = (data.get("dist-tags") or {}).get("latest") or ""
    times = data.get("time") or {}
    repo_url = ""
    if isinstance(data.get("repository"), dict):
        repo_url = data["repository"].get("url") or ""
    elif isinstance(data.get("repository"), str):
        repo_url = data["repository"]
    repo_url = repo_url or (data.get("homepage") or "")
    return {
        "version": latest,
        "published": times.get(latest) or times.get("modified") or "",
        "modified": times.get("modified") or "",
        "repo": norm_repo(repo_url),
        "description": clean_text(data.get("description") or ""),
        "topics": ",".join(list(data.get("keywords") or [])[:8]),
        "v2": 1 if V2_RE.search(f"{name} {data.get('description') or ''}") else 0,
    }


def source_npm(cfg: dict) -> list[dict]:
    """Поиск npm-пакетов. Релевантность фильтруем сами: search отдаёт мусор."""
    min_stars = int(cfg.get("filters", {}).get("min_stars", 0))
    max_age = int(cfg.get("filters", {}).get("npm_max_age_days", 45))
    cutoff = now_utc() - timedelta(days=max_age)
    items: list[dict] = []
    seen: set[str] = set()

    for text in cfg.get("npm_queries", ["opencode-plugin", "keywords:opencode-plugin", "opencode tui plugin"]):
        url = NPM_SEARCH + "?" + urllib.parse.urlencode({"text": text, "size": 50})
        log(f"npm: {text}")
        try:
            data = http_get_json(url)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! ошибка npm: {exc}")
            continue
        for obj in data.get("objects", []):
            pkg = obj.get("package") or {}
            name = pkg.get("name") or ""
            if not name or name in seen:
                continue
            keywords = " ".join(pkg.get("keywords") or [])
            haystack = f"{name} {keywords} {pkg.get('description') or ''}"
            if not OPENCODE_RE.search(haystack):
                continue
            published = parse_date(pkg.get("date"))
            if published and published < cutoff:
                continue
            links = pkg.get("links") or {}
            repo = norm_repo(links.get("repository")) or norm_repo(links.get("npm"))
            seen.add(name)
            item = {
                "key": f"npm:{name}",
                "title": name,
                "kind": "plugin",
                "npm": name,
                "repo": repo,
                "url": links.get("npm") or f"https://www.npmjs.com/package/{name}",
                "description": clean_text(pkg.get("description") or ""),
                "stars": 0,
                "version": pkg.get("version") or "",
                "published": pkg.get("date") or "",
                "topics": keywords[:200],
                "v2": 1 if V2_RE.search(haystack) else 0,
                "sources": "npm",
            }
            items.append(item)

    # добираем метаданные (репозиторий, дата публикации) только для новых кандидатов
    for item in items:
        if item.get("repo") and item.get("published"):
            continue
        try:
            meta = fetch_npm_meta(item["npm"])
        except Exception as exc:  # noqa: BLE001
            log(f"  ! npm meta {item['npm']}: {exc}")
            continue
        item["repo"] = item.get("repo") or meta.get("repo")
        item["published"] = item.get("published") or meta.get("published")
        item["version"] = item.get("version") or meta.get("version")
        item["description"] = item["description"] or meta.get("description")
        item["topics"] = (item.get("topics") or meta.get("topics") or "")[:200]
        item["v2"] = item.get("v2") or meta.get("v2")
    return items


def source_ecosystem() -> list[dict]:
    """Курируемый список с официальной страницы opencode.ai/docs/ecosystem."""
    log("ecosystem: opencode.ai/docs/ecosystem")
    try:
        page = http_get(ECOSYSTEM_URL, headers={"Accept": "text/html"}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! ошибка ecosystem: {exc}")
        return []
    items: list[dict] = []
    for match in re.finditer(r'<h2[^>]*id="(plugins|projects|agents)"[^>]*>(.*?)</h2>(.*?)(?=<h2|</main>|$)', page, re.S):
        section = match.group(1)
        kind = {"plugins": "plugin", "projects": "project", "agents": "agent"}[section]
        block = match.group(3)
        for row in re.finditer(r"<tr><td><a href=\"([^\"]+)\"[^>]*>(.*?)</a></td><td>(.*?)</td></tr>", block, re.S):
            url, title, desc = row.group(1), row.group(2), row.group(3)
            repo = norm_repo(url)
            if repo == "anomalyco/opencode":
                continue
            items.append(
                {
                    "key": f"gh:{repo}" if repo else f"eco:{url.rstrip('/').lower()}",
                    "title": clean_text(title, 80),
                    "kind": kind,
                    "repo": repo,
                    "url": url,
                    "description": clean_text(desc),
                    "stars": 0,
                    "v2": 1 if V2_RE.search(desc or "") else 0,
                    "sources": "ecosystem",
                }
            )
    log(f"  найдено записей: {len(items)}")
    return items


AWESOME_ITEM_RE = re.compile(
    r"<summary><b>(?P<name>[^<]+)</b>\s*"
    r'<img src="https://badgen\.net/github/stars/(?P<owner>[^/"]+)/(?P<repo>[^"/]+)"[^>]*/>\s*'
    r"-\s*(?:<i>(?P<desc>.*?)</i>)?.*?</summary>(?P<body>.*?)</details>",
    re.S,
)


def source_awesome() -> list[dict]:
    """awesome-opencode. Секции — <div id="..."></div>, карточки — <details>/<summary>."""
    log("awesome: awesome-opencode/awesome-opencode")
    try:
        readme = http_get(AWESOME_URL, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! ошибка awesome: {exc}")
        return []
    kind_map = {
        "plugins": "plugin",
        "themes": "theme",
        "agents": "agent",
        "projects": "project",
        "resources": "resource",
    }
    items: list[dict] = []
    markers = list(re.finditer(r'<div id="([a-z]+)"></div>', readme))
    for idx, marker in enumerate(markers):
        kind = kind_map.get(marker.group(1))
        if not kind:
            continue
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(readme)
        block = readme[marker.end() : end]
        found = 0
        for card in AWESOME_ITEM_RE.finditer(block):
            found += 1
            owner, repo_name = card.group("owner"), card.group("repo")
            repo = f"{owner}/{repo_name}".lower()
            if "anomalyco/opencode" in repo:
                continue
            body = card.group("body") or ""
            link = re.search(r'<a href="(https?://[^"]+)"', body)
            url = link.group(1) if link else f"https://github.com/{owner}/{repo_name}"
            npm = re.search(r"npmjs\.com/package/([@A-Za-z0-9_.\-/]+)", body)
            desc = clean_text(card.group("desc") or "")
            if not desc:
                first_line = re.sub(r"<[^>]+>", " ", body).split("\n")[0]
                desc = clean_text(first_line)
            items.append(
                {
                    "key": f"gh:{repo}",
                    "title": clean_text(card.group("name"), 90),
                    "kind": kind,
                    "npm": npm.group(1) if npm else None,
                    "repo": repo,
                    "url": url,
                    "description": desc,
                    "stars": 0,
                    "v2": 1 if V2_RE.search(desc or "") else 0,
                    "sources": "awesome",
                }
            )
        # запасной путь: старый формат markdown-таблиц
        if not found:
            for line in block.splitlines():
                line = line.strip()
                if not line.startswith("|") or set(line) <= set("| :-"):
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                link = re.search(r"\[([^\]]+)\]\((https?://[^)]+)\)", cells[0] if cells else "")
                if not link:
                    continue
                desc = clean_text(cells[-1] if len(cells) >= 3 else "")
                items.append(
                    {
                        "key": f"gh:{norm_repo(link.group(2))}" if norm_repo(link.group(2)) else f"aws:{link.group(2)}",
                        "title": clean_text(link.group(1), 90),
                        "kind": kind,
                        "repo": norm_repo(link.group(2)),
                        "url": link.group(2),
                        "description": desc,
                        "stars": 0,
                        "v2": 1 if V2_RE.search(desc) else 0,
                        "sources": "awesome",
                    }
                )
    log(f"  найдено записей: {len(items)}")
    return items


# --------------------------------------------------------------------------- #
# слияние и запись в базу
# --------------------------------------------------------------------------- #
def item_names(item: dict) -> set[str]:
    """Имена, по которым запись одного проекта может совпасть с другой.

    Нужны, чтобы склеить репозиторий `owner/tool` с npm-пакетом `tool`
    или `@scope/tool`: сравниваются basename репозитория, заголовок и
    имя пакета без скоупа.
    """
    names: set[str] = set()
    repo = item.get("repo") or ""
    if "/" in repo:
        names.add(repo.rsplit("/", 1)[-1])
    title = (item.get("title") or "").strip()
    if title:
        names.add(title)
    npm_name = (item.get("npm") or "").strip()
    if npm_name:
        names.add(npm_name)
        if npm_name.startswith("@") and "/" in npm_name:
            names.add(npm_name.split("/", 1)[-1])
    return {n.lower() for n in names if n}


def merge_items(raw: list[dict]) -> list[dict]:
    """Склеивает npm-пакет, его GitHub-репозиторий и записи курируемых списков."""
    by_name: dict[str, dict] = {}
    # сначала записи без npm, затем npm-версии перезаписывают их:
    # пакет из реестра — каноничная запись, репозиторий к ней присоединяется
    for item in raw:
        if item.get("npm"):
            continue
        for name in item_names(item):
            by_name.setdefault(name, item)
    for item in raw:
        if not item.get("npm"):
            continue
        for name in item_names(item):
            by_name[name] = item

    curated = {"awesome", "ecosystem"}
    merged: dict[str, dict] = {}
    for item in raw:
        key = item["key"]
        if not item.get("npm"):
            # репозиторий/запись из курируемого списка присоединяется к npm-пакету
            for name in item_names(item):
                twin = by_name.get(name)
                if twin is not None and twin.get("npm") and twin["key"] != key:
                    key = twin["key"]
                    break
        if key in merged:
            target = merged[key]
            for field in ("description", "url", "published", "pushed", "version", "topics", "npm", "repo"):
                if not target.get(field) and item.get(field):
                    target[field] = item[field]
            target["stars"] = max(target.get("stars") or 0, item.get("stars") or 0)
            target["v2"] = max(target.get("v2") or 0, item.get("v2") or 0)
            # вид (плагин/тема/агент) берём из курируемого списка — он точнее
            srcs = set((target.get("sources") or "").split(","))
            srcs.update((item.get("sources") or "").split(","))
            srcs.discard("")
            if srcs & curated and target.get("sources") not in curated:
                target["kind"] = item.get("kind") or target.get("kind")
            target["sources"] = ",".join(sorted(srcs))
            continue
        item = dict(item)
        item["key"] = key
        merged[key] = item
    return list(merged.values())


def store_items(conn: sqlite3.Connection, items: list[dict]) -> tuple[int, int]:
    """Пишет элементы в базу. Возвращает (новых, обновлений)."""
    new_count = 0
    update_count = 0
    stamp = iso(now_utc())
    for item in items:
        row = conn.execute("SELECT * FROM items WHERE key=?", (item["key"],)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO items
                   (key,title,kind,npm,repo,url,description,stars,version,published,pushed,topics,v2,sources,first_seen,last_seen,stars_prev,announced)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    item["key"],
                    item.get("title") or item["key"],
                    item.get("kind") or "plugin",
                    item.get("npm"),
                    item.get("repo"),
                    item.get("url"),
                    item.get("description") or "",
                    int(item.get("stars") or 0),
                    item.get("version") or "",
                    item.get("published") or "",
                    item.get("pushed") or "",
                    item.get("topics") or "",
                    int(item.get("v2") or 0),
                    item.get("sources") or "",
                    stamp,
                    stamp,
                    int(item.get("stars") or 0),
                ),
            )
            new_count += 1
            continue

        changes = []
        if item.get("version") and row["version"] and item["version"] != row["version"]:
            changes.append(f"{row['version']}→{item['version']}")
        if (item.get("stars") or 0) > (row["stars"] or 0):
            changes.append(f"⭐{row['stars']}→{item['stars']}")
        if item.get("pushed") and item["pushed"] != row["pushed"]:
            changes.append("обновлён")
        if item.get("description") and item["description"] != row["description"]:
            changes.append("описание")

        conn.execute(
            """UPDATE items SET title=COALESCE(NULLIF(?,''),title), kind=COALESCE(NULLIF(?,''),kind),
                 npm=COALESCE(?,npm), repo=COALESCE(?,repo), url=COALESCE(?,url),
                 description=COALESCE(NULLIF(?,''),description), stars=?, version=COALESCE(NULLIF(?,''),version),
                 published=COALESCE(NULLIF(?,''),published), pushed=COALESCE(NULLIF(?,''),pushed),
                 topics=COALESCE(NULLIF(?,''),topics), v2=MAX(v2,?), sources=?,
                 last_seen=?, stars_prev=?, prev_version=version
               WHERE key=?""",
            (
                item.get("title") or "",
                item.get("kind") or "",
                item.get("npm"),
                item.get("repo"),
                item.get("url"),
                item.get("description") or "",
                int(item.get("stars") or 0),
                item.get("version") or "",
                item.get("published") or "",
                item.get("pushed") or "",
                item.get("topics") or "",
                int(item.get("v2") or 0),
                item.get("sources") or "",
                stamp,
                int(row["stars"] or 0),
                item["key"],
            ),
        )
        if changes:
            update_count += 1
    conn.commit()
    return new_count, update_count


# --------------------------------------------------------------------------- #
# сборка дайджеста
# --------------------------------------------------------------------------- #
def build_digest(
    conn: sqlite3.Connection, cfg: dict, demo: bool = False, do_translate: bool = True
) -> tuple[str, list[str]]:
    """Формирует HTML-дайджест. Возвращает (текст, ключи отправленных элементов).

    demo=True — показать последние находки независимо от отметок «отправлено»
    и не помечать их (для проверки рендера в Telegram).
    """
    filters = cfg.get("filters", {})
    min_stars = int(filters.get("min_stars", 0))
    since_first = iso(now_utc() - timedelta(days=int(filters.get("window_days", 8))))
    last_digest = meta_get(conn, "last_digest_at") or since_first

    if demo:
        fresh = conn.execute(
            """SELECT * FROM items WHERE (stars >= ? OR npm IS NOT NULL)
               ORDER BY first_seen DESC, stars DESC LIMIT 25""",
            (min_stars,),
        ).fetchall()
    else:
        fresh = conn.execute(
            """SELECT * FROM items WHERE first_seen > ? AND announced = 0
               ORDER BY (npm IS NULL), stars DESC, first_seen DESC""",
            (last_digest,),
        ).fetchall()

    grown = conn.execute(
        """SELECT * FROM items WHERE announced = 1 AND stars_prev IS NOT NULL AND stars - stars_prev >= ?
           ORDER BY (stars - stars_prev) DESC LIMIT 15""",
        (int(filters.get("star_jump", 10)),),
    ).fetchall()

    versioned = conn.execute(
        """SELECT * FROM items WHERE announced = 1 AND prev_version IS NOT NULL AND version != ''
           AND prev_version != '' AND version != prev_version
           ORDER BY published DESC LIMIT 20"""
    ).fetchall()

    listed = [] if demo else conn.execute(
        """SELECT * FROM items WHERE announced = 1
           AND (sources LIKE '%ecosystem%' OR sources LIKE '%awesome%')
           ORDER BY last_seen DESC LIMIT 10"""
    ).fetchall()

    fresh = [r for r in fresh if (r["stars"] or 0) >= min_stars or r["npm"]]
    max_new = int(cfg.get("filters", {}).get("max_new", 40))
    shown = fresh[:max_new]

    title = cfg.get("digest_title", "🆕 OpenCode Watcher")
    stamp = now_utc().astimezone().strftime("%d.%m.%Y")

    if not fresh and not grown and not versioned:
        return "\n".join(
            [
                f"<b>{title} — {esc(stamp)}</b>",
                "\n<i>За прошлый период новых плагинов и обновлений не найдено.</i>",
            ]
        ), []

    translations: dict[str, str] = {}
    if do_translate:
        translations = translate_rows(conn, list(shown) + list(versioned) + list(grown) + list(listed), cfg)

    parts = [f"<b>{title} — {esc(stamp)}</b>"]
    keys: list[str] = []
    html_out: list[str] = []

    def render(row: sqlite3.Row, note: str = "") -> str:
        badges = []
        if row["npm"]:
            badges.append("📦 " + esc(row["npm"]))
        if row["stars"]:
            badges.append(f"⭐ {row['stars']}")
        if row["version"]:
            badges.append(f"v{esc(row['version'])}")
        if row["v2"]:
            badges.append("🧩 V2")
        original = (row["description"] or "").strip()
        translated = translations.get(row["key"])
        if original and not translated:
            badges.append("en")  # перевода нет — показан оригинал
        badge_line = " · ".join(badges)
        desc = esc(translated or original)
        url = row["url"] or ""
        name = esc(row["title"] or row["key"])
        head = f"<a href=\"{html.escape(url, quote=True)}\">{name}</a>" if url else f"<b>{name}</b>"
        line = f"• {head}"
        if note:
            line += f" <i>({esc(note)})</i>"
        out = [line]
        if badge_line:
            out.append(f"   {badge_line}")
        if desc:
            out.append(f"   {desc}")
        return "\n".join(out)

    if fresh:
        hidden = len(fresh) - len(shown)
        html_out.append(f"\n<b>🆕 Новое ({len(fresh)})</b>")
        for row in shown:
            html_out.append(render(row))
            keys.append(row["key"])
        if hidden > 0:
            html_out.append(f"<i>…и ещё {hidden}. Полный список: python3 watcher.py list --limit 100</i>")

    if versioned:
        html_out.append(f"\n<b>♻️ Новые версии ({len(versioned)})</b>")
        for row in versioned:
            note = f"{row['prev_version']} → {row['version']}"
            html_out.append(render(row, note))

    if grown:
        html_out.append(f"\n<b>🔥 Рост звёзд</b>")
        for row in grown:
            delta = (row["stars"] or 0) - (row["stars_prev"] or 0)
            html_out.append(render(row, f"+{delta} ⭐"))

    if listed:
        html_out.append(f"\n<b>📋 В курируемых списках</b>")
        for row in listed:
            src = "ecosystem" if "ecosystem" in (row["sources"] or "") else "awesome"
            html_out.append(render(row, src))

    plugins = [r["npm"] for r in fresh if r["npm"]]
    if plugins:
        snippet = ", ".join(f'"{p}"' for p in plugins[:10])
        html_out.append(f"\n<b>⚙️ Установка в opencode.json</b>\n<pre>{esc(snippet)}</pre>")

    parts.extend(html_out)
    return "\n".join(parts), keys


def html_to_md(html: str) -> str:
    """Грубый конвертер HTML-дайджеста в markdown (для файлов в outbox/)."""
    out = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 (\1)", html, flags=re.S)
    out = re.sub(r"<b>(.*?)</b>", r"**\1**", out, flags=re.S)
    out = re.sub(r"<i>(.*?)</i>", r"_\1_", out, flags=re.S)
    out = re.sub(r"<code>(.*?)</code>", r"`\1`", out, flags=re.S)
    out = re.sub(r"<[^>]+>", "", out)
    out = out.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def split_message(text: str, limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in text.split("\n"):
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(token: str, chat_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        log("DRY-RUN: сообщение не отправлено")
        return True
    for chunk in split_message(text):
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        req = urllib.request.Request(
            TG_API.format(token=token, method="sendMessage"),
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Telegram {exc.code}: {body}") from exc
        time.sleep(0.5)
    return True


# --------------------------------------------------------------------------- #
# команды
# --------------------------------------------------------------------------- #
def collect(cfg: dict, conn: sqlite3.Connection) -> tuple[int, int, int]:
    filters = cfg.get("filters", {})
    window = int(filters.get("github_window_days", 30))
    since = now_utc() - timedelta(days=window)
    raw: list[dict] = []
    errors = 0
    for name, func in (
        ("github", lambda: source_github(cfg, since)),
        ("npm", lambda: source_npm(cfg)),
        ("ecosystem", source_ecosystem),
        ("awesome", source_awesome),
    ):
        if not cfg.get("sources", {}).get(name, True):
            continue
        try:
            found = func()
            if not found:
                log(f"  ⚠️  {name}: 0 кандидатов — источник мог отдать пустую выдачу, проверь лог")
            else:
                log(f"  {name}: {len(found)} кандидатов")
            raw.extend(found)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log(f"  ! источник {name} упал: {exc}")
    items = merge_items(raw)
    new_count, update_count = store_items(conn, items)
    return new_count, update_count, errors


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    env = load_env()
    os.environ.setdefault("GITHUB_TOKEN", env.get("GITHUB_TOKEN", ""))
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")

    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Другой запуск уже идёт — выходим.")
            return 0

        conn = db_connect()
        started = iso(now_utc())
        log("Сканирование…")
        if args.demo:
            new_count, update_count, errors = 0, 0, 0
            log("DEMO: источники не опрашиваются, дайджест строится по базе")
        else:
            new_count, update_count, errors = collect(cfg, conn)
            log(f"Новых: {new_count}, обновлений: {update_count}, ошибок источников: {errors}")

        text, keys = build_digest(conn, cfg, demo=args.demo, do_translate=not args.no_translate)
        print("\n" + "=" * 60)
        print(html.unescape(re.sub(r"<[^>]+>", "", text)))
        print("=" * 60 + "\n")

        os.makedirs(OUTBOX_DIR, exist_ok=True)
        fname = now_utc().astimezone().strftime("%Y-%m-%d_%H%M")
        with open(os.path.join(OUTBOX_DIR, f"{fname}.md"), "w", encoding="utf-8") as fh:
            fh.write(html_to_md(text))

        sent = 0
        if not args.dry_run:
            if not (token and chat_id):
                log("! Нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — дайджест сохранён, но не отправлен.")
            else:
                send_telegram(token, chat_id, text)
                sent = 1
                log("Отправлено в Telegram.")
            if not args.demo:
                meta_set(conn, "last_digest_at", iso(now_utc()))
                if keys:
                    conn.execute(
                        "UPDATE items SET announced=1 WHERE key IN (%s)" % ",".join("?" * len(keys)), keys
                    )
                conn.commit()
        else:
            log("DRY-RUN: отправка пропущена, отметки в базе не тронуты.")

        if not args.demo:
            conn.execute(
                "INSERT INTO runs(started,finished,new_items,updates,errors,sent) VALUES(?,?,?,?,?,?)",
                (started, iso(now_utc()), new_count, update_count, errors, sent),
            )
            conn.commit()
        log(f"Готово. Копия дайджеста: outbox/{fname}.md")
        return 0


def cmd_scan(args: argparse.Namespace) -> int:
    cfg = load_config()
    env = load_env()
    os.environ.setdefault("GITHUB_TOKEN", env.get("GITHUB_TOKEN", ""))
    conn = db_connect()
    new_count, update_count, errors = collect(cfg, conn)
    print(f"\nНовых: {new_count} | обновлений: {update_count} | ошибок: {errors}")
    print("\nТоп-20 по звёздам среди найденного:")
    for row in conn.execute("SELECT title, npm, stars, version, url FROM items ORDER BY stars DESC LIMIT 20"):
        print(f"  {row['stars']:>5}  {(row['npm'] or row['title'])[:40]:<40} {row['version'] or '-':<10} {row['url'] or ''}")
    return 0


def cmd_send_test(args: argparse.Namespace) -> int:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token:
        log("! TELEGRAM_BOT_TOKEN не задан в .env")
        return 1
    text = (
        "✅ <b>OpenCode Watcher</b> подключён.\n"
        f"chat_id: <code>{chat_id or 'НЕ ЗАДАН — укажи в .env'}</code>\n"
        "Дальше: воскресенье 08:00 — недельный дайджест."
    )
    if not chat_id:
        log("! TELEGRAM_CHAT_ID не задан. Напиши боту любое сообщение и запусти: python3 watcher.py chatid")
        return 1
    send_telegram(token, chat_id, text)
    log("Тест отправлен.")
    return 0


def cmd_chatid(args: argparse.Namespace) -> int:
    """Достаёт chat_id из свежих getUpdates — сначала напиши боту в Telegram."""
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log("! TELEGRAM_BOT_TOKEN не задан в .env")
        return 1
    data = http_get_json(TG_API.format(token=token, method="getUpdates"), timeout=30)
    if not data.get("ok"):
        log(f"! Telegram API: {data}")
        return 1
    chats = {}
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("title") or chat.get("type") or chat.get("username") or "?"
    if not chats:
        log("Нет ни одного сообщения. Напиши боту что-нибудь в Telegram и повтори.")
        return 1
    for cid, title in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}\t# {title}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db_connect()
    total = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    announced = conn.execute("SELECT COUNT(*) c FROM items WHERE announced=1").fetchone()["c"]
    by_src = conn.execute(
        "SELECT sources, COUNT(*) c FROM items GROUP BY sources ORDER BY c DESC LIMIT 8"
    ).fetchall()
    print(f"Всего в базе: {total} (отправлено в дайджестах: {announced})")
    print(f"Последний дайджест: {meta_get(conn, 'last_digest_at') or '—'}")
    print("\nПо источникам:")
    for row in by_src:
        print(f"  {row['sources'] or '—':<28} {row['c']}")
    print("\nПоследние запуски:")
    for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
        print(f"  {row['started']} → новых {row['new_items']}, обновлений {row['updates']}, отправлено {row['sent']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM items ORDER BY first_seen DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for row in rows:
        flag = "✓" if row["announced"] else "·"
        print(f"{flag} {row['first_seen'][:10]} ⭐{row['stars']:<5} {(row['npm'] or row['title'])[:38]:<38} {row['sources']}")
    return 0


def cmd_mark_sent(args: argparse.Namespace) -> int:
    conn = db_connect()
    count = conn.execute("SELECT COUNT(*) c FROM items WHERE announced=0").fetchone()["c"]
    conn.execute("UPDATE items SET announced=1 WHERE announced=0")
    conn.execute("UPDATE items SET stars_prev=stars, prev_version=version")
    meta_set(conn, "last_digest_at", iso(now_utc()))
    conn.commit()
    log(f"Помечено как отправленное: {count}. Следующий дайджест покажет только новое.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode Plugin Watcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="сканировать и отправить дайджест")
    p_run.add_argument("--dry-run", action="store_true", help="не отправлять в Telegram")
    p_run.add_argument(
        "--demo",
        action="store_true",
        help="собрать дайджест из последних находок без сканирования и не трогать отметки",
    )
    p_run.add_argument("--no-translate", action="store_true", help="показать описания на английском")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("scan", help="только сканирование").set_defaults(func=cmd_scan)
    sub.add_parser("send-test", help="тестовое сообщение").set_defaults(func=cmd_send_test)
    sub.add_parser("chatid", help="показать chat_id из getUpdates").set_defaults(func=cmd_chatid)
    sub.add_parser("stats", help="статистика по базе").set_defaults(func=cmd_stats)
    sub.add_parser("mark-sent", help="пометить всё отправленным").set_defaults(func=cmd_mark_sent)

    p_list = sub.add_parser("list", help="последние найденные")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("Прервано.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
