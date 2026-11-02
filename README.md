# Telegram Reminder Bot

A personal accountability bot:

- **Set reminders from a picture** — send a photo of any schedule (screenshot, notebook, timetable). OCR reads the times (and text next to them as labels), shows them to you, and creates the reminders after you confirm.
- **Or type times** — `09:30 gym, 14:00 client meeting` works too.
- **Outcome buttons when a reminder fires**:
  - ✅ **Done** — logged, no charge
  - ❌ **Skipped** — adds the person's **charge amount** to their outstanding balance
  - 🟩 **Compensated** — deducts **half** the charge amount from the balance
- **📊 Daily summary** — auto-sent at 23:00 (Tashkent time) with everyone's done/skipped/compensated counts, charges, and current balances.

All times are handled in `Asia/Tashkent`, data lives in SQLite.

---

## 1. Create the bot with @BotFather (one time)

1. Open Telegram and search for **@BotFather** (blue verified checkmark).
2. Send: `/newbot`
3. BotFather asks for a **name** — e.g. `My Reminders`
4. Then a **username** — must end with `bot`, e.g. `ramazon_reminders_bot`
5. BotFather replies with a **token** that looks like:
   `123456789:AAHfiqksKZ8WbR5WMmS9XnJ3mYwE4TtZqTo`
   Copy it — that's your `BOT_TOKEN`.

> ⚠️ Never share this token. If you lose it, message BotFather with `/mybots` → your bot → API Token → Regenerate.

## 2. Run it on your server (Docker)

SSH into your server, copy this `reminder-telegram-bot/` folder over (git or `scp -r`), then:

```bash
cd reminder-telegram-bot
cp .env.example .env
nano .env            # paste your BOT_TOKEN; adjust SUMMARY_TIME / OCR_LANG if needed
docker compose up -d --build
docker compose logs -f   # watch it start; Ctrl+C to stop watching
```

The bot is live. Data persists in `./data/bot.db` across restarts and rebuilds. Pending reminders are automatically re-scheduled after a restart.

**Update later:** `git pull` (or re-copy files), then `docker compose up -d --build`.

## 3. Run locally (for testing)

```bash
pip install -r requirements.txt
# macOS: brew install tesseract        |  Ubuntu/Debian: apt install tesseract-ocr
cp .env.example .env                   # add BOT_TOKEN
python bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/start` | Register (asks for your charge amount) + account status |
| `/help` | Full guide |
| `/list` | Upcoming reminders with their ids |
| `/cancel 12` | Cancel reminder #12 |
| `/charge` | View or change your charge amount (quick buttons + custom) |
| `/balance` | Your outstanding balance |
| `/summary` | Today's summary on demand |
| *(send a photo)* | OCR reads times → confirm → reminders created |
| *(type times)* | e.g. `09:00 breakfast 14:30 lunch 21:00 run` |

## How the flow works

1. On first `/start` the bot asks for the **charge per skipped reminder** (quick amounts or custom).
2. Send a **photo** of a schedule. The bot reads it with Tesseract OCR, lists **time — label**, and you press **✅ Add reminders** or **✖️ Cancel**. Times that already passed today go to **tomorrow**.
3. When a reminder fires, you get `⏰ Reminder — 14:30` with **Done / Skipped / Compensated** buttons.
   - Skips charge you `+charge`; compensation takes back `−charge/2` (balance can't go below zero).
4. At **23:00** every registered user receives the group's daily summary with counts and balances.

## Configuration (.env)

| Var | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — (required) | from @BotFather |
| `TZ_NAME` | `Asia/Tashkent` | timezone for all times |
| `SUMMARY_TIME` | `23:00` | daily summary send time |
| `OCR_LANG` | `eng` | Tesseract languages, e.g. `eng+rus+uzb` (both packs are installed in the Docker image) |
| `DB_PATH` | `data/bot.db` | SQLite location |

## Notes

- Each user sees and manages **their own** reminders and can only press buttons on their own reminders.
- Anyone who `/start`s the bot can use it — per their choice this bot is open registration.
- OCR works best on clear, well-lit, reasonably large text. Black-on-white prints/screenshots are ideal. If a label language isn't in the OCR pack, times still read fine (digits are language-independent); add packs with `apt install tesseract-ocr-<lang>` in the Dockerfile.

---

## Alternative deployment: GitHub Actions (free, no card, no server)

The bot can live entirely inside a GitHub repository — no server, no card:

- A workflow wakes the bot every **5 minutes**. Telegram queues your messages and button presses while it sleeps (up to 24 h) and delivers them on the next wake-up, so nothing is lost.
- Each wake-up: drains queued updates → fires overdue reminders → stays live ~4 minutes → exits (runs chain back-to-back, so it's effectively always awake).
- The state (`bot.db`) is **encrypted with AES-256** (`DATA_KEY` secret) and committed back to the repo after every run. Use a **public** repo for truly unlimited free minutes (a private repo's free tier caps minutes and would run out).
- Consequences of the model: reminders may fire up to ~5–10 min late; the daily summary is sent on the next wake-up after its time.

### Set it up

1. Create a **public** GitHub repo (e.g. `reminder-bot`), then from this folder:
   ```bash
   git init && git add -A && git commit -m "reminder bot"
   git branch -M main
   git remote add origin https://github.com/YOURUSER/reminder-bot.git
   git push -u origin main
   ```
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `BOT_TOKEN` = your @BotFather token
   - `DATA_KEY` = a random key you generate once: `openssl rand -hex 32` (keep a copy — it unlocks your data!)
3. First run: **Actions tab** → *reminder-bot* → **Run workflow** (green button). Watch it go green.
4. Change nothing else — it re-runs on its own every 5 minutes from then on.

### Updating the bot under GitHub Actions

```bash
git add -A && git commit -m "update" && git push
```

The next wake-up runs the new code.
