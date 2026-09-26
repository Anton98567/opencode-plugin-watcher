# OpenCode Plugin Watcher

Еженедельный (или ежедневный) дайджест новых и обновлённых **плагинов для
[OpenCode](https://opencode.ai)** в Telegram. Находит плагины в GitHub и npm,
следит за версиями и звёздами, переводит описания на русский и отмечает, что уже
показывал, чтобы не присылать одно и то же повторно.

Ноль зависимостей: только стандартная библиотека Python 3.9+.

```
**🆕 OpenCode Watcher — 26.09.2026**

**🆕 Новое (2)**
• opencode-jev-router (https://github.com/robertn702/opencode-jev-router)
   ⭐ 1
   Адаптивное рассуждение для OpenCode через JEV, с внутрипроцессным плагином и прокси-интерфейсом Responses API

**♻️ Новые версии (1)**
• @sjawhar/opencode-legion-envoy (https://www.npmjs.com/package/@sjawhar/opencode-legion-envoy) _(3.2.6 → 3.2.7)_
   📦 @sjawhar/opencode-legion-envoy · v3.2.7
   Плагин OpenCode для подсистемы Legion's Envoy.

**📋 В курируемых списках**
• orgx-opencode-plugin (https://github.com/useorgx/orgx-opencode-plugin) _(awesome)_
   ⭐ 1
   Узел плагина OrgX для OpenCode — реализует Gateway Protocol v1 + Driver, который управляет сеансом OpenCode пользователя, используя его подписку.
```

## Возможности

* **4 источника.** GitHub Search API, реестр npm, официальный
  [ecosystem](https://opencode.ai/docs/ecosystem/) и
  [awesome-opencode](https://github.com/rickstaa/awesome-opencode).
* **Склейка сущностей.** Один и тот же плагин, найденный в GitHub и npm, схлопывается
  в одну карточку; версии и звёзды не дублируются.
* **Дельты.** Показывает только то, что изменилось с прошлого запуска: новое,
  новые версии, скачок звёзд. Старые записи помечаются `announced=1` и больше не
  повторяются.
* **Перевод на русский.** Каскад бесплатных провайдеров, результат кэшируется по
  хэшу описания.
* **Склейка длинных сообщений.** Дайджест длиннее 3900 символов режется на части по
  границам блоков и отправляется несколькими сообщениями.
* **Outbox.** Каждый дайджест дублируется в `outbox/ГГГГ-ММ-ДД_ЧЧММ.md` — можно
  перечитать, не спрашивая Telegram.
* **Живой приём заявок.** `chat-id` умеет читать `getUpdates`: можно написать боту
  `/start` или дайджест ещё не уйдёт, пока вы его не подтвердите.
* **Ограничение лимитов.** GitHub Search — 10 запросов/мин без токена и 30 с ним;
  таймер добавляет рандомную задержку, чтобы запросы не шли ровно в полную минуту.

## Требования

* Python **3.9** или новее (проверено на 3.12). Зависимостей нет — `pip install` не нужен.
* Аккаунт **Telegram-бота** (получается у [@BotFather](https://t.me/BotFather)).
* Сервер с постоянным доступом в интернет. Подойдёт любой VPS, даже самый дешёвый.

## Быстрый старт

**1. Создай бота.** Открой [@BotFather](https://t.me/BotFather) → `/newbot` → выбери
имя и username. Скопируй токен вида `123456789:AAExample...`.

**2. Скопируй репозиторий.**

```bash
git clone https://github.com/Anton98567/opencode-plugin-watcher.git
cd opencode-plugin-watcher
```

**3. Создай `.env`** из примера и заполни токен и `chat_id`:

```bash
cp .env.example .env
chmod 600 .env
```

```ini
TELEGRAM_BOT_TOKEN=123456789:AAExample_your_token_here
TELEGRAM_CHAT_ID=
GITHUB_TOKEN=
POLLINATIONS_API_KEY=
```

**4. Узнай свой `chat_id`.** Напиши боту что-нибудь в Telegram, затем:

```bash
python3 watcher.py chat-id
```

Скрипт прочитает `getUpdates` и напечатает числовой ID (например `-1001234567890` для
группы). Впиши его в `TELEGRAM_CHAT_ID`. Если бот ещё не получал сообщений — напиши
ему `/start` и повтори команду.

**5. Проверь, что всё работает.** Команда ничего не сканирует и не отправляет, только
печатает тестовое сообщение:

```bash
python3 watcher.py send-test
```

**6. Собери первый дайджест вручную:**

```bash
python3 watcher.py run
```

Готово. Дальше — автоматизация по расписанию.

## Установка на сервере с systemd

```bash
git clone https://github.com/Anton98567/opencode-plugin-watcher.git
cd opencode-plugin-watcher
cp .env.example .env && nano .env    # токен, chat_id
./install.sh weekly                 # или daily / hourly
```

Установщик:

* копирует проект в `/opt/opencode-plugin-watcher` (или в `~/opencode-plugin-watcher`,
  если скрипт запущен не от root);
* создаёт `data/` и `outbox/` и ставит им права `700`;
* кладёт `.env` с правами `600`;
* устанавливает `opencode-watcher.service` и `.timer`, делает `enable --now` таймер;
* печатает расписание и подсказку, что делать дальше.

### Проверить расписание

```bash
systemctl list-timers opencode-watcher.timer
systemctl status  opencode-watcher.timer
systemctl start   opencode-watcher.service   # запустить прямо сейчас, не дожидаясь таймера
journalctl -u opencode-watcher.service -n 100 --no-pager
```

### Другой путь установки

```bash
sudo mkdir -p /opt/opencode-plugin-watcher
sudo cp watcher.py config.json /opt/opencode-plugin-watcher/
sudo cp .env /opt/opencode-plugin-watcher/ && sudo chmod 600 /opt/opencode-plugin-watcher/.env
sudo cp systemd/opencode-watcher.service systemd/opencode-watcher.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now opencode-watcher.timer
```

Юниты по умолчанию ждут проект в `/opt/opencode-plugin-watcher`. Если ставил
в другое место — поправь `WorkingDirectory=` и `ExecStart=` в `.service`.

### Вместо systemd — cron

```cron
17 8 * * 1  cd /opt/opencode-plugin-watcher && /usr/bin/python3 watcher.py run >> /var/log/opencode-watcher.log 2>&1
```

## Команды

| Команда | Что делает |
|---|---|
| `run` | сканирует источники, собирает дельты, переводит, отправляет в Telegram, пишет в `outbox/` |
| `run --demo` | дайджест из последних находок: без сканирования, отметки в базе не трогает |
| `run --dry-run` | всё то же, но без отправки — удобно отлаживать фильтры и перевод |
| `run --no-translate` | описания на английском (быстрее и без внешних запросов) |
| `send-test` | проверочное сообщение в Telegram |
| `chat-id` | показывает `chat_id` из `getUpdates` |
| `stats` | сводка по базе: сколько всего, сколько отправлено, последние запуски |
| `backfill [N]` | показывает N последних элементов, не отправляя (например `backfill 30`) |

Флаги `--demo`, `--dry-run` и `--no-translate` комбинируются между собой.

## Переменные окружения (`.env`)

| Переменная | Обязательна | Назначение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | для отправки | токен от BotFather |
| `TELEGRAM_CHAT_ID` | для отправки | ID чата, группа или личка |
| `GITHUB_TOKEN` | нет | личный токен GitHub: поиск 30/мин вместо 10/мин и снятие лимита на secondary rate limit |
| `POLLINATIONS_API_KEY` | нет | ключ Pollinations для перевода через LLM (лучшее качество, перевод пачками) |

`GITHUB_TOKEN` не обязателен, но без него на больших выборках можно упереться в
rate limit GitHub. Токен подойдёт **без** каких-либо прав (достаточно
public-репозиториев) — генерируется на
[github.com/settings/tokens](https://github.com/settings/tokens) → Fine-grained token
с доступом только на чтение.

Файл `.env` в репозиторий **не** коммитится (см. `.gitignore`), а установщик ставит
ему права `600`.

## Настройка `config.json`

```json
{
  "digest_title": "🆕 OpenCode Watcher",
  "github_window_days": 30,
  "github_queries": ["opencode plugin in:name,description created:>={since}", "..."],
  "npm_queries": ["opencode-plugin", "keywords:opencode-plugin", "..."],
  "filters": {
    "min_stars": 0,
    "star_jump": 10,
    "window_days": 8,
    "npm_max_age_days": 45,
    "max_new": 40
  },
  "translate": {
    "enabled": true,
    "lang": "ru",
    "max_chars": 150,
    "providers": ["pollinations", "mymemory", "google"],
    "model": "openai/gpt-5.4-nano",
    "endpoint": "https://gen.pollinations.ai/v1/chat/completions",
    "batch": 12,
    "mymemory_email": ""
  },
  "sources": { "github": true, "npm": true, "ecosystem": true, "awesome": true }
}
```

### Фильтры — что попадёт в дайджест

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `filters.min_stars` | `0` | отбрасывать GitHub-плагины с меньшим числом звёзд (0 — не отбрасывать ничего) |
| `filters.star_jump` | `10` | показывать «скачок звёзд», когда плагин прибавил не меньше столько за окно |
| `filters.window_days` | `8` | окно, за которое смотрим рост звёзд |
| `filters.npm_max_age_days` | `45` | npm-пакеты старше этого возраста не берутся (анкеты-пустышки) |
| `filters.max_new` | `40` | максимум новых плагинов в одном дайджесте |
| `github_window_days` | `30` | глубина поиска в GitHub (дней с момента создания/обновления) |

Каждый источник можно отключить флагом `false` в `sources`.

## Перевод описаний

Описания переводятся на русский каскадом провайдеров из `translate.providers` —
используется первый, кто ответил. Результат кэшируется в таблице `translations` по
хэшу описания, поэтому повторно одно и то же не переводится.

| Провайдер | Нужен ключ | Качество | Заметка |
|---|---|---|---|
| `pollinations` | `POLLINATIONS_API_KEY` | лучшее | LLM (`translate.model`), переводит пачками по `batch`, понимает контекст и не ломает имена собственные |
| `mymemory` | нет | среднее | бесплатно, ~5000 символов/день; `mymemory_email` поднимает лимит до 50 000 |
| `google` | нет | среднее | недокументированный endpoint, часто отдаёт 429 |

* Если перевод не удался — показывается английский оригинал с бейджем `en`.
* Строки без кириллицы в кэш не пишутся: дешёвые провайдеры иногда возвращают
  исходник без изменений, и такой результат запрашивался бы заново каждый запуск.
* Каждый провайдер отключается сам после 3 ошибок подряд, чтобы не жечь время на
  ретраях (важно: Google отвечает 429 почти сразу при исчерпании лимита).
* Отключить перевод: `run --no-translate` или `"enabled": false`.

## Расписание

По умолчанию — раз в неделю, воскресенье в 07:00 UTC с рандомной задержкой до 15 минут
(таймер systemd `RandomizedDelaySec=900`). Действует `Persistent=true`: если сервер
был выключен в момент запуска, дайджест уйдёт при следующем включении.

Время в юните указано в **UTC**. Московское время — UTC+3, то есть 07:00 UTC = 10:00 МСК.

Другие режимы:

```bash
./install.sh daily     # каждый день в 07:00 UTC
./install.sh hourly    # каждый час в 07 минут
```

Сменить расписание существующего таймера:

```bash
sudo systemctl edit opencode-watcher.timer   # переопределить OnCalendar=
```

Сервер должен быть на **UTC** (`timedatectl set-timezone UTC`), иначе таймер
интерпретирует `07:00` в местной зоне.

## Как это работает

```
источники ──► нормализация ──► склейка в таблицу plugins
                                      │
                    БД (SQLite): last_seen, stars, versions, announced
                                      │
                    дельты: новое / новые версии / скачок звёзд
                                      │
                     перевод (кэш) ──► HTML ──► разбивка на сообщения
                                      │
                          Telegram  +  outbox/*.md
```

* `plugins` — одна строка на плагин, ключ склейки: `name+owner` для GitHub,
  `name` для npm, `name+repo` для ecosystem/awesome. Репозиторий и npm-пакет
  одного проекта объединяются по совпадению имени.
* `versions` — история версий, чтобы ловить апдейты.
* `translations` — кэш переводов по хэшу текста.
* `runs` — журнал запусков: сколько нашлось нового, сколько обновлений, отправлено
  ли, были ли ошибки. Всё это же показывает `stats`.
* `announced` — флаг «уже показывал». Он же служит курсором: первый запуск на
  существующей базе покажет всё найденное, дальше — только дельты.

## Структура проекта

```
watcher.py                         весь код: источники, БД, перевод, Telegram, CLI
config.json                        настройки (без секретов)
.env.example                       шаблон .env с пустыми значениями
install.sh                         установка на сервер + systemd
systemd/
  opencode-watcher.service         юнит запуска
  opencode-watcher.timer           юнит расписания
tests/selftest.py                  самотесты чистых функций, без сети
.github/workflows/ci.yml           прогон тестов на Python 3.9 / 3.11 / 3.12
data/watcher.db                    SQLite, создаётся сама (в git не попадает)
outbox/                            копии дайджестов в markdown
```

Весь код — один файл без зависимостей, чтобы можно было просто скопировать
`watcher.py` на любой сервер.

## Безопасность

* `.env`, `data/` и `outbox/` в `.gitignore` — секреты и логи в репозиторий не попадают.
* Установщик ставит `.env` правами `600`, каталоги — `700`.
* Скрипт не пишет токены в логи: при проблемах с отправкой в журнале будет
  `Telegram: ошибка отправки`, а не сам токен.
* У `GITHUB_TOKEN` достаточно прав только на чтение.
* Бот не принимает команды от посторонних: `chat-id` показывает ID чата, а
  отправляет дайджест только в `TELEGRAM_CHAT_ID`.
* База лежит в `data/` рядом с проектом — ограничьте доступ к каталогу или
  перенесите БД в закрытое место.

## Если что-то не работает

**`Нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID`** — не заполнен `.env`. Копия дайджеста
всё равно появится в `outbox/`, отправить её не получится.

**`chat-id` ничего не показывает** — бот ещё не получал сообщений: откройте чат с
ботом и напишите `/start`, потом повторите команду.

**`HTTP 429 от api.github.com`** — сработал rate limit. Добавь `GITHUB_TOKEN` в `.env`.
Таймер уже добавляет случайную задержку, но при большом числе запросов лимит
всё равно возможен.

**`HTTP 429 от translate.googleapis.com`** — у публичного endpoint Google есть жёсткий
лимит. Произойдёт переключение на `mymemory`; можно убрать `google` из
`translate.providers`, чтобы не тратить на него время.

**`ENOSPC: no space left on device`** — закончился диск. `data/watcher.db` и `outbox/`
растут медленно, проверьте `df -h`. Старые копии дайджестов в `outbox/` можно
удалять.

**Таймер молчит** — `systemctl list-timers opencode-watcher.timer` покажет, когда
следующий запуск; `journalctl -u opencode-watcher.service -n 100` — что было на
последнем. Проверьте, что на сервере `timedatectl` показывает UTC.

**В дайджест приходит много мусора** — поднимите `filters.min_stars` (например `50`),
уменьшите `filters.npm_max_age_days` или отключите источник в `sources`.

**Перевод не нужен** — `run --no-translate`.

## Разработка

```bash
python3 -m py_compile watcher.py        # проверка синтаксиса
python3 tests/selftest.py               # 37 тестов чистых функций, без сети
python3 watcher.py --help               # список команд
python3 watcher.py run --dry-run        # прогон без отправки и без сканирования
```

`tests/selftest.py` проверяет то, что обычно ломается тихо: очистку текста,
экранирование для Telegram, разбиение длинных сообщений без потери содержимого,
конвертацию HTML в markdown, разбор JSON-ответа переводчика, склейку npm-пакета с
репозиторием и работу со схемой SQLite.

В репозитории есть CI (`.github/workflows/ci.yml`): на Python 3.9, 3.11 и 3.12
прогоняются компиляция, `--help`, самотесты и `bash -n install.sh`.

## Лицензия

Лицензия не задана: все права принадлежат автору. Код можно свободно читать и
запускать у себя, но для распространения нужно согласование.

Хотите, чтобы проект можно было свободно форкать — добавьте `LICENSE` с
шаблоном MIT.
