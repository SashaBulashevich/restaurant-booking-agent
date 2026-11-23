# Restaurant Booking Agent

[![tests](https://github.com/SashaBulashevich/restaurant-booking-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/SashaBulashevich/restaurant-booking-agent/actions/workflows/tests.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

AI-агент, який сам пише менеджеру ресторану в Telegram і бронює столик. Він веде розмову ввічливою британською англійською, реагує на все, що каже менеджер, нічого не вигадує і наприкінці повторює підтверджену бронь: час, дату, кількість гостей і ресторан. Агент розуміє голосові повідомлення і відповідає голосом. Рішення, які приймає агент, він сам записує в лог.

Пет-проєкт, на якому я відпрацьовую підхід «LLM вирішує, код контролює». Агент працює з живою людиною, тому головне тут не «красиво відповісти», а **ніколи не сказати менеджеру того, чого агент не знає**, і безпечно поводитися, коли модель недоступна.

## Як виглядає розмова

Фрагмент реального прогону з моделлю (`logs/scenarios/happy_path.md`). Менеджера грає сценарій, оператора — людина:

> **Agent:** Good afternoon, I am an AI assistant booking on behalf of my client. Would a table for two at 17:00 today be available?
> **Manager:** Good afternoon! Yes, 5pm for two is fine. What name shall I put it under?
> **Agent:** Let me just check that and come back to you in a moment.
> *(агент питає оператора: «What name should I use for the booking?», оператор відповідає: «Sasha»)*
> **Agent:** Thank you, I've noted the name Sasha. Could you please let me know the restaurant name?
> **Manager:** Lovely, that's booked for Sasha at The Golden Lion, 5pm today, table for two.
> **Agent:** Wonderful, thank you ever so much. Just to confirm the booking: a table for two (2 people) at 17:00 (5 pm) today, Sunday, 22 November 2026, at The Golden Lion under the name Sasha. We look forward to it. Many thanks again!

Ще 5 сценаріїв лежать у [`logs/scenarios/`](logs/scenarios/): невідома назва ресторану, інший час (оператор погоджує або відмовляє), депозит карткою плюс питання «ви бот?», питання про алергії.

## Архітектура

```
Менеджер (Telegram) ⇄ транспорт ⇄ agent/core.py ⇄ agent/brain.py (Groq LLM)
                        │              │                 └─ фактчекер (окремий LLM-виклик)
                        │              ├─ agent/voice.py (Whisper STT, edge-tts en-GB)
                        │              └─ logs/decision_log.md (лог рішень)
Оператор (людина)  ⇄ ───┘
```

Логіка агента не залежить від транспорту. Є три способи запуску:

| Транспорт | Файл | Особливість |
|---|---|---|
| Userbot (Telethon) | `run_telegram.py` | Агент пише від імені вашого акаунта і **може почати розмову першим**. Оператор відповідає в Saved Messages. |
| Звичайний бот (Bot API) | `run_bot.py` | Токен від @BotFather. Telegram дозволяє боту писати лише після того, як людина натисне Start, тому агент пише перше повідомлення одразу після Start. Оператор відповідає в чаті з ботом. |
| Консоль | `run_console.py` | Ви граєте менеджера в терміналі. Підходить для налагодження. |

## Як агент уникає вигадок

1. **Факти задає людина, а не модель.** Кількість гостей, час, ім'я й телефон беруться з `.env`. Дата «сьогодні» береться з годинника часового поясу ресторану.
2. **Модель повертає строгий JSON** з полями `understood`, `decision`, `action`, `reply`, `restaurant_name`, `manager_agreed`, `manager_time`. Будь-яке порушення формату вважається збоєм, а не відповіддю.
3. **Код перевіряє витягнуті дані:**
   - назву ресторану агент приймає, тільки якщо її справді написав менеджер;
   - ім'я гостя — тільки якщо воно є в конфігурації або його дав оператор;
   - час, відмінний від запитаного, потребує явного «так» від оператора;
   - телефони й email, яких немає у фактах, у відповідь не потрапляють.
4. **Фактчекер.** Кожну відповідь моделі перед відправкою перевіряє окремий LLM-виклик: чи немає в ній непідтверджених фактів. Його я додав після того, як на тесті модель сама відповіла менеджеру «no allergies».
5. **Невідоме йде до людини.** Ім'я, алергії, депозит, інший час: агент пише менеджеру «Let me just check…» і ставить питання оператору.
6. **Підсумок броні складає код,** а не модель, і лише після явної згоди менеджера.

## Fail-safe

- Агент перебирає моделі: основна `gpt-oss-120b`, резервна `qwen3.8-27b`, по дві спроби на кожну. Якщо всі недоступні, **менеджеру нічого не надсилається**, а оператор отримує сповіщення і команду `/retry`.
- Якщо недоступний фактчекер, відповідь теж не надсилається.
- Якщо не спрацювало озвучення, відповідь надсилається текстом. Якщо не вдалося розпізнати голосове, агент ввічливо просить менеджера написати текстом.
- Стан розмови зберігається після кожного кроку. Після перезапуску агент продовжує ту саму розмову, а userbot дочитує повідомлення, пропущені за час простою.
- Повідомлення сторонніх людей агент ігнорує: він розрізняє менеджера й оператора за username та ID.

## Запуск

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # вписати GROQ_API_KEY і MANAGER_USERNAME
```

Без Telegram:
```powershell
.venv\Scripts\python run_console.py   # ви граєте менеджера, відповідь оператора: op: текст
.venv\Scripts\python scenarios.py     # 6 сценаріїв з реальною моделлю, результат у logs/scenarios/
.venv\Scripts\python -m pytest        # 20 автотестів без мережі
```

Через бота (потрібні `BOT_TOKEN` і `OPERATOR_USERNAME`):
```powershell
.venv\Scripts\python run_bot.py --selftest   # пробне голосове й текст оператору
.venv\Scripts\python run_bot.py --start      # почати розмову
```

Через userbot (потрібні `TG_API_ID` і `TG_API_HASH` з my.telegram.org):
```powershell
.venv\Scripts\python run_telegram.py --login     # одноразовий вхід
.venv\Scripts\python run_telegram.py --selftest  # перевірка без повідомлень менеджеру
.venv\Scripts\python run_telegram.py --start     # почати розмову
```

> Файл сеансу userbot (`*.session`) дає повний доступ до акаунта. Він внесений у `.gitignore`. Після експериментів завершіть сеанс у Telegram → Settings → Devices.

## Інструменти і чому саме вони

- **Groq** (`gpt-oss-120b`, `qwen3.8-27b`): відповідає приблизно за секунду, тож розмова йде в темпі живого чату. Має JSON-режим, а Whisper для голосу доступний з тим самим ключем.
- **Telethon** для userbot, бо лише так агент може написати першим. Для бота використано чистий Bot API через `httpx`, без важких фреймворків.
- **edge-tts** з голосом `en-GB-RyanNeural`, щоб звучав британський акцент. **imageio-ffmpeg** конвертує звук в OGG/Opus, бо тільки такий файл Telegram показує як голосове.
- **pytest** з фальшивим LLM-клієнтом і фальшивим Telegram, щоб тести fail-safe і захисних перевірок не залежали від мережі.

## Відомі обмеження

- LLM недетермінована. Фактчекер — теж модель, і він може помилитися, хоча за збою блокує відправку, а не пропускає її.
- Таймауту очікування оператора немає: менеджер бачить «Let me just check…», доки людина не відповість.
- Агент перевірено на сценаріях і в консолі. Повну розмову з реальним рестораном він ще не проводив.

## Файли

| Файл | Що робить |
|---|---|
| `agent/brain.py` | промпт, виклик LLM з fallback, перевірка JSON, фактчекер |
| `agent/core.py` | логіка розмови, захисні перевірки, підсумок броні, fail-safe |
| `agent/state.py` | стан розмови (`logs/conversation_state.json`) |
| `agent/logbook.py` | лог рішень (`logs/decision_log.md`, `.jsonl`) |
| `agent/voice.py` | розпізнавання і синтез голосу |
| `run_telegram.py`, `run_bot.py`, `run_console.py` | транспорти |
| `scenarios.py`, `tests/` | сценарії з реальною моделлю й автотести |
| `.github/workflows/tests.yml` | CI: автотести на кожен push |

## Відповідальне використання

Агент пише живим людям, тому:
- **писати лише тим, хто очікує на повідомлення** (тестовий акаунт, свій ресторан, домовленість). Масові розсилки незнайомим порушують правила Telegram і можуть призвести до обмеження акаунта;
- **агент завжди чесно каже, що він AI-асистент,** якщо його про це питають. Це закладено в промпт;
- **платіжних даних агент не має і не передає.**

## Ліцензія

[MIT](LICENSE)
