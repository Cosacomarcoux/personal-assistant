# Personal Assistant (Telegram + Claude)

> 🇦🇷 **Guía en español: [GUIA.md](GUIA.md)**. Este fork está configurado para Argentina: español rioplatense, pesos (ARS), hora de Buenos Aires, eventos y recordatorios que se repiten.

Your own AI personal assistant that you chat with on Telegram. It can:

- **Email & calendar**: read and search Gmail, draft and send replies, check and book Google Calendar events, search Google Drive
- **Tasks & reminders**: keep a to-do list and send you reminders on Telegram at the right time
- **Research & notes**: search the web and remember things for you
- **Life admin**: log expenses, give monthly spending summaries, and track recurring bills
- **Training coach**: keeps your training program, logs workouts and how your body feels (sleep, energy, injuries), adapts sessions to that, and sends a weekly review every Sunday
- **Strava**: workouts from Garmin, Apple Watch, Polar etc. come in automatically (optional)
- **Voice messages**: talk instead of typing, and get spoken answers back (needs an OpenAI key, see below)
- **Travel**: cheapest flights and hotels (Google Flights/Hotels prices), trip plans and budgets
- **Languages**: translates anything; send a photo of a menu, sign or letter and it translates it
- **Microsoft Teams**: chat with the assistant in Teams instead of (or as well as) Telegram, with approval cards (optional)
- **Outlook / Microsoft 365**: read, draft and send Outlook email, see and book Outlook calendar events (with Teams links), and read OneDrive files (optional)
- **Studies**: flashcards with spaced repetition, quizzes, explanations and exam study plans
- **Writing**: reports, essays, letters and CVs, delivered as Word files in the chat
- **Ideas**: brainstorm partner and an idea bank
- **Content**: video/post pipeline, scripts, hooks, captions and LinkedIn posts
- **Career**: application tracker, tailored CVs and cover letters, mock interviews
- **Budget**: monthly budgets per category and savings goals
- **Food**: meal log (also from photos) with calories/protein, recipes, shopping list
- **Habits & goals**: habit streaks and goals with progress
- **People**: birthdays, notes and gift ideas, keeping in touch
- **Journal**: diary with mood, reflection and help with decisions
- **Daily briefing**: every morning (default 07:30), a summary of your day

**Safety:** it never sends an email or changes your calendar without asking. You get **✅ Approve / ❌ Reject** buttons first. Only *you* can talk to it.

---

## Setup (about 30–45 minutes, no coding)

You'll collect 4 keys, then paste them into the hosting site. Keep them private, like passwords.

### Step 1: Create the Telegram bot (2 min)
1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, then pick a name (e.g. *Jarvis*) and a username ending in `bot`.
3. BotFather replies with a **token** like `123456:ABC-xyz…`. Save it as `TELEGRAM_BOT_TOKEN`.

### Step 2: Get a Claude API key (5 min)
1. Go to **platform.claude.com** and sign in. This is separate from your Claude app subscription.
2. Go to **Billing** and add some credit (start with $5–10). Setting a monthly spend limit is a good idea.
3. Go to **API Keys → Create Key**. Save it as `ANTHROPIC_API_KEY`.

### Step 3: Let it access Google (15 min)
1. Go to **console.cloud.google.com** and create a new project (top bar → *New project* → name it "Assistant").
2. Go to **APIs & Services → Library** and enable these three: **Gmail API**, **Google Calendar API**, **Google Drive API**.
3. Go to **APIs & Services → OAuth consent screen** (may be called *Google Auth Platform*):
   - User type: **External**. App name: "Assistant". Use your email for the support and contact fields.
   - Under **Audience / Test users**, add your own Gmail address.
   - Then click **Publish app** (set the status to *In production*). ⚠️ If you skip this, Google logs the assistant out every 7 days. You don't need verification for personal use.
4. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**. Click Create.
   - Save the **Client ID** as `GOOGLE_CLIENT_ID` and the **Client secret** as `GOOGLE_CLIENT_SECRET`.

### Step 4: Put it online with Railway (10 min, about $5/month)
1. Go to **railway.com** and log in **with GitHub**.
2. Click **New Project → Deploy from GitHub repo** and pick this repository.
3. Open the service, go to the **Variables** tab, then **Raw Editor**, and paste:
   ```
   ANTHROPIC_API_KEY=your-key
   TELEGRAM_BOT_TOKEN=your-token
   GOOGLE_CLIENT_ID=your-client-id
   GOOGLE_CLIENT_SECRET=your-secret
   TIMEZONE=Asia/Taipei
   OWNER_NAME=Fabian
   ASSISTANT_NAME=Jarvis
   BRIEFING_TIME=07:30
   DATA_DIR=/data
   ```
4. Add a disk so it remembers things across restarts: right-click the service → **Attach Volume** → mount path `/data`.
5. Wait for the deploy to turn green.

### Step 5: Lock it to you and connect Google (3 min)
1. In Telegram, send your bot any message. It replies with **your Telegram ID**.
2. In Railway → Variables, add `OWNER_TELEGRAM_ID=<that number>`. Railway redeploys automatically.
3. Send `/connect_google` to the bot and follow its three instructions.
4. Try it: *"What's on my calendar this week?"* or `/briefing`.

### Optional: voice messages (5 min, about $0.003 per minute heard, $0.015 per minute spoken)
Claude can't listen to audio, so voice messages are turned into text by OpenAI's speech-to-text first.
1. Go to **platform.openai.com**, add a little credit under **Billing**, then **API keys → Create new secret key**.
2. In Railway → Variables, add `OPENAI_API_KEY=<that key>`.

The bot shows what it heard (🎤 "...") and then answers as if you had typed it, both as a voice
message and as text. Change this with `/voice auto` (default: voice in → voice out), `/voice always`
or `/voice off`. To change how it sounds, just ask: *"make your voice happier"* or *"use a female voice"*.

### Optional: Strava (10 min, free)
Lets the training coach see runs, rides and other workouts from your watch. Garmin, Apple Watch (via an app like HealthFit), Polar, Coros and others can sync to Strava automatically.
1. Log in at **strava.com**, then open **strava.com/settings/api**.
2. Create an app: name "Ragnar", category "Training", website `http://localhost`, **Authorization Callback Domain** `localhost`. Upload any icon if it asks for one.
3. Copy the **Client ID** and **Client Secret** into Railway → Variables as `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`.
4. Send `/connect_strava` to the bot and follow its instructions (same copy-the-address trick as Google).

### Optional: hands-free voice mode (2 min, free)
A voice page inside Telegram: tap the microphone once, then just talk. Ragnar notices when you stop,
answers out loud straight away (no play button) and listens again.
1. In Railway: **worker → Settings → Networking → Generate Domain**, port `8080`.
2. Wait for the redeploy, then send `/talk` to the bot, or use the 🎙 button next to the message field.
3. The first time, allow microphone access for Telegram.

Only your Telegram account can use the page (Telegram signs every request).

### Optional: flight and hotel prices (5 min, free up to ~250 searches/month)
1. Sign up at **serpapi.com** (free plan).
2. Copy your **API key** from the dashboard into Railway → Variables as `SERPAPI_API_KEY`.
3. Optional: `CURRENCY=TWD` (or EUR, USD…); the default is SEK.

Try: *"Cheapest flight Taipei → Stockholm 10 Dec, back 5 Jan?"* Ragnar finds the prices and gives a
Google Flights link to book; he never books or pays himself.

### Optional: Outlook mail, calendar and OneDrive (10 min, free)
1. Go to **portal.azure.com** and sign in with your Microsoft account.
   (If it asks you to create a free Azure account or directory first, do that.)
2. Search for **App registrations** → **New registration**:
   name `Ragnar`, account type **"Accounts in any organizational directory and personal Microsoft
   accounts"**, no redirect URI → **Register**.
3. Copy the **Application (client) ID** into Railway → Variables as `MS_CLIENT_ID`.
4. In the app: **Authentication** → **Allow public client flows** → **Yes** → **Save**.
5. Send `/connect_microsoft` to the bot, open the link, type the code and sign in.

Ragnar can read and draft Outlook email and read the calendar freely; sending, archiving, booking and
deleting always wait for your ✅. OneDrive is read-only. A work or school account may need approval
from that organisation's IT. Connected OneDrive before Outlook support was added? Send
`/connect_microsoft` once more to allow mail and calendar.

### Optional: Microsoft Teams (20 min, free)
Lets the assistant live in Teams. Telegram becomes optional: leave `TELEGRAM_BOT_TOKEN` out for a
Teams-only assistant. Needs the public domain from the voice-mode step (Generate Domain, port 8080).
1. **portal.azure.com → Create a resource → Azure Bot.** Pricing tier **Free (F0)**, type of app
   **Single Tenant**, "Create new Microsoft App ID" → Create.
2. In the bot: **Configuration** → Messaging endpoint `https://<your-domain>/api/messages` → Apply.
   Copy **Microsoft App ID** and **App Tenant ID**.
3. **Manage Password** (next to the App ID) → **New client secret** → copy its **Value** (shown once).
4. **Channels → Microsoft Teams** → accept → Apply.
5. Railway → Variables: `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD` (the secret's value), `TEAMS_TENANT_ID`.
6. Download `https://<your-domain>/teams/app.zip`. In Teams: **Apps → Manage your apps → Upload an
   app → Upload a custom app** (if that's blocked, the company's Teams admin uploads it).
7. Open the app in Teams and write "hej". It replies with your Teams ID: add it as `TEAMS_OWNER_ID`.

In Teams, type commands with or without `/`: `briefing`, `skills`, `connect_microsoft`, `talk`…
Approvals arrive as cards with ✅/❌. Word files arrive as private download links (valid 7 days).
Briefings and reminders go to Telegram if it's set up, otherwise Teams; `NOTIFY_CHANNEL=teams` or
`both` changes that. The client secret expires (at most after 2 years): renew it in Azure in time.

---

## Using it
Just chat normally. Useful commands:

| Command | What it does |
|---|---|
| `/briefing` | Daily summary right now |
| `/tasks` | Open to-dos |
| `/connect_google` | (Re)connect Gmail/Calendar/Drive |
| `/connect_microsoft` | Connect Outlook mail, calendar and OneDrive |
| `/connect_strava` | Connect Strava |
| `/talk` | Hands-free voice mode |
| `/voice` | Spoken replies: `auto`, `always` or `off` |
| `/review` | Weekly training review right now (it also comes automatically on Sundays, 19:00) |
| `/skills` | What it can do |
| `/reset` | Clear chat memory (tasks, notes and expenses are kept) |

**Make it yours:** set `EXTRA_INSTRUCTIONS` in Railway, e.g.
`I work at X. Reply to emails in English unless they wrote in Chinese. Keep my mornings before 10:00 free.`

**Costs:** Railway is about $5/month. Claude API use is pay-per-use; light daily use is usually a few dollars a month. To make it cheaper, set `MODEL=claude-haiku-5-5`. For a smarter assistant, set `MODEL=claude-opus-5-5`.

---

## Adding more agents later
The assistant is built from **skills** (specialists) in `assistant/skills/`. Each one is a single file. To add a new one, e.g. a fitness coach, a travel planner or a job-application tracker:

1. Open this repo in **Claude → Code** and say: *"Add a new skill that …"*.
2. Claude creates `assistant/skills/<name>.py` (based on `_template.py`) and registers it in `skills/__init__.py`.
3. Add its key to `ENABLED_SKILLS` in Railway, e.g. `google,tasks,research,admin,fitness`
   (if you never set `ENABLED_SKILLS`, the defaults in `config.py` are used).

Railway redeploys automatically whenever the GitHub repo changes.

## How it works (for the curious)
```
Telegram ──► assistant/main.py (bot, reminders, daily briefing)
                 │
                 ▼
             assistant/agent.py  ── Claude decides which tools to use
                 │                   (risky tools wait for your approval)
                 ▼
             assistant/skills/   google_workspace · tasks · research · admin · …
                 │
                 ▼
             SQLite database in /data (memory, tasks, notes, expenses, Google login)
```

## Troubleshooting
- **Bot doesn't answer:** check Railway → Deployments → Logs. "Missing settings" means a variable is missing.
- **"Google is not connected":** send `/connect_google`. If it keeps happening weekly, publish the Google app (Step 3.3).
- **It forgot everything after a redeploy:** the volume at `/data` is missing (Step 4.4).
- **Run the tests:** `pip install -r requirements.txt pytest && pytest`
