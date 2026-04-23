# Student Experience watcher

Pings you on Telegram the moment a long-stay studio opens up at Amsterdam Amstel, NDSM, Zuidas, Minervahaven, or Leiden. Runs every 10 minutes on GitHub Actions, 24/7, free.

## Setup — 4 steps, ~5 minutes

### 1. Create a Telegram bot

- Open Telegram → chat with **@BotFather** → send `/newbot` → pick a name and a username ending in `_bot`.
- BotFather gives you a **token** (long string like `7123…:AAH…`). Save it.
- Search for your new bot, open a chat with it, send any message (e.g. "hi").
- Open this URL in your browser, replacing `<TOKEN>`:
  `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Find `"chat":{"id":123456789` — that number is your **chat ID**. Save it.

### 2. Create a PRIVATE GitHub repo

- New repo on GitHub → **Private**. Upload all files from this folder (drag & drop in the web UI works).
- Or with git:
  ```
  git init && git add . && git commit -m "initial"
  git branch -M main
  git remote add origin https://github.com/YOU/se-watcher.git
  git push -u origin main
  ```

### 3. Add 2 secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from step 1 |
| `TELEGRAM_CHAT_ID`   | chat id from step 1 |

### 4. Enable Actions & run once

- Repo → **Actions** tab → if asked, click "I understand, enable workflows".
- Click **SE watcher** → **Run workflow** (green button). Wait ~1 min.
- Click into the run. If you see `Parsed N studios total, M match wanted locations` in the log, it works.
- If a studio is currently listed at one of your 5 complexes, you'll get a Telegram ping.

**Done.** It now runs on its own every 10 minutes, forever.

---

## What it does, exactly

- Fetches `https://studentexperience.com/studios?los=longstay`
- Parses the page's embedded Next.js JSON (stable) with HTML fallback
- Filters for: Amsterdam Amstel, NDSM, Zuidas, Minervahaven, Leiden
- Skips studios already alerted (tracked in `seen.json`, auto-committed by the workflow)
- Sends one Telegram message per new studio, with price, size, location, and link

## Notes

- **Expected normal state:** 0 studios. These locations use an automated-draw system with no waitlist — studios appear, have a short response window (hours to a few days), then disappear. You're alerting on the appearance.
- **Respond fast.** When you get a ping, go to the listing link and submit your response within the deadline shown on the listing. Being alerted first means nothing if you take 6 hours to respond.
- **GitHub cron is best-effort.** It usually runs within 1–3 min of schedule but occasionally skips slots during GitHub load spikes. For SE this is fine because listings stay open for hours.
- **Changing locations:** edit `WANTED_LOCATIONS` at the top of `scraper.py`, commit, push.
- **Pause alerts:** Actions tab → SE watcher → `⋯` → Disable workflow.
- **Reset state** (re-alert on everything): delete contents of `seen.json`, commit an empty `[]`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No Telegram message after "Run workflow" + there's a studio on the site | Check the run logs. If `Parsed 0 studios total`, the site probably changed structure — open an issue or paste the `__NEXT_DATA__` shape so the parser can be adjusted. |
| `HTTP 403` in logs | Site is blocking GitHub runner IPs. Rare for this site, but if it happens: run locally via cron (see note below) or use a residential proxy. |
| No message at all, studio should match | Verify `TELEGRAM_CHAT_ID` is correct. Send `/start` to the bot again, re-check `getUpdates`. |
| Workflow fails on commit | Under Settings → Actions → General → "Workflow permissions", select **Read and write permissions**. |

### Running locally instead of GitHub Actions

If GitHub ever blocks the site:
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python scraper.py
```
Then add to your crontab: `*/10 * * * * cd /path/to/se-scraper && /path/to/python scraper.py >> scraper.log 2>&1`
