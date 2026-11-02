"""Telegram (and Teams) front-end. Start with:  python -m assistant.main"""
import asyncio
import base64
import logging
import secrets
import time
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile, MenuButtonWebApp,
                      Update, WebAppInfo)
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import google_auth, microsoft, strava, teams, voice, web
from .agent import Agent
from .config import Config
from .db import DB
from .extract import can_read, text_from_file
from .skills import load_skills
from .skills.tasks import due_reminders

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("assistant")

BRIEFING_PROMPT = (
    "Give me my morning briefing. Check: today's calendar events, important unread emails "
    "from the last 24 hours (skip newsletters/promotions), open tasks that are due soon or "
    "overdue, recurring bills due in the next 5 days, today's planned workout (if a training "
    "program is set), and anything else due soon that you can see: flashcards due, content "
    "to film or publish, job application deadlines within a week, birthdays within a week, "
    "habits to do today and budget categories close to their limit. Keep it short and "
    "scannable, then suggest the 1-3 most important things to do today. Skip any part that "
    "has nothing in it or whose account isn't connected."
)
BRIEFING_LABEL = "(Asked for my morning briefing.)"
REVIEW_LABEL = "(Asked for my weekly training review.)"

WEEKLY_REVIEW_PROMPT = (
    "Time for my weekly training review. Compare the last 7 days (workout_history, "
    "strava_activities if Strava is connected, status_history) with my program and goals: "
    "what went well, what was missed, progress on key lifts/runs, and how my body status "
    "(sleep, energy, soreness, injuries) has been. Then propose next week's sessions with "
    "days and concrete targets, adjusted to my status and this week's calendar. Ask if I "
    "want them added to the calendar. Keep it short and motivating."
)

HELP = """Hi! I'm your personal assistant. Just talk to me normally, e.g.:

• "What's on my calendar tomorrow?"
• "Any important emails today? Draft a reply to Anna."
• "Remind me to call mom at 6pm"
• "Add 'renew passport' to my to-do list"
• "Research the best eSIM for Japan"
• "Cheapest flight Taipei → Stockholm in December?"
• "Translate this" (send a photo of a menu or sign)
• "Quiz me on Fin Inve" · "Write a report on … as a Word file"
• "Let's brainstorm video ideas" · "Mom's birthday is 3 May"
• "I spent 250 on lunch"
• "Did legs today: squats 3x5 at 100 kg"
• "Slept 6 hours, left knee aches" (I'll adapt your training)
You can also send voice messages, and I answer them with voice.

Commands:
/connect_google – link Gmail, Calendar & Drive
/briefing – get your daily briefing now
/review – weekly training review now
/connect_microsoft – link Outlook mail, calendar & OneDrive
/connect_strava – link Strava (workouts from your watch)
/tasks – show open tasks
/skills – what I can do
/talk – hands-free voice mode
/voice – spoken replies on/off
/reset – clear our conversation memory (notes and tasks are kept)"""


MAX_FILE = 20 * 1024 * 1024    # Telegram bots can download up to 20 MB
MAX_IMAGE = 5 * 1024 * 1024    # Claude's limit per image
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def split(text: str, n: int = 4000) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


class Bot:
    def __init__(self, config: Config):
        self.config = config
        self.db = DB(config.data_dir / "assistant.db")
        self.skills = load_skills(config.enabled_skills, self.db, config)
        self.agent = Agent(config, self.db, self.skills)
        self.app = None    # Telegram application, when Telegram is set up
        self.teams = None  # Teams front-end, when Teams is set up
        self.web = None
        # One memory for the owner, whichever app they write from.
        self.home_chat = config.owner_id or 1

    # ------------------------------------------------------------ helpers
    def allowed(self, update: Update) -> bool:
        return bool(self.config.owner_id) and update.effective_user.id == self.config.owner_id

    async def guard(self, update: Update) -> bool:
        if self.allowed(update):
            return True
        if not self.config.owner_id:
            await update.effective_message.reply_text(
                f"Setup step: your Telegram ID is {update.effective_user.id}\n"
                "Put it in the OWNER_TELEGRAM_ID setting and restart me.")
        else:
            await update.effective_message.reply_text("Sorry, I'm a private assistant.")
        return False

    async def send_reply(self, chat_id: int, reply, bot) -> None:
        for chunk in split(reply.text):
            await bot.send_message(chat_id, chunk)
        await self.send_files(chat_id, reply.files, bot)
        await self.send_approvals(chat_id, reply.pending_ids, bot)

    async def send_files(self, chat_id: int, files: list, bot) -> None:
        for name, data in files:
            await bot.send_document(chat_id, InputFile(data, filename=name))

    async def send_approvals(self, chat_id: int, pending_ids: list[int], bot) -> None:
        for pid in pending_ids:
            row = self.db.query("SELECT summary FROM pending_actions WHERE id=?", (pid,))[0]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"ok:{pid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"no:{pid}"),
            ]])
            await bot.send_message(chat_id, f"Needs your approval (#{pid}):\n\n{row['summary']}"[:4000],
                                   reply_markup=kb)
        if len(pending_ids) > 1:  # many deletions etc.: one tap instead of one per action
            token = self.new_batch(pending_ids)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Approve all {len(pending_ids)}", callback_data=f"okall:{token}"),
                InlineKeyboardButton("❌ Reject all", callback_data=f"noall:{token}"),
            ]])
            await bot.send_message(chat_id, f"{len(pending_ids)} actions are waiting (#"
                                   + ", #".join(map(str, pending_ids)) + ").", reply_markup=kb)

    def new_batch(self, pending_ids: list[int]) -> str:
        token = secrets.token_hex(4)  # button data is limited to 64 bytes, so store the ids
        self.db.set(f"batch:{token}", pending_ids)
        return token

    async def resolve_batch(self, token: str, approve: bool) -> str:
        ids = self.db.get(f"batch:{token}") or []
        results = [await self.agent.resolve_action(pid, approve=approve) for pid in ids]
        if not approve:
            return f"Rejected {sum(r.startswith('Rejected') for r in results)} of {len(ids)}. Nothing was done."
        done = sum(r.startswith("Done") for r in results)
        failed = [f"#{pid}: {r[:200]}" for pid, r in zip(ids, results) if r.startswith("It failed")]
        skipped = len(ids) - done - len(failed)
        lines = [f"Done ✅ {done} of {len(ids)}."]
        if skipped:
            lines.append(f"{skipped} already handled earlier.")
        if failed:
            lines.append("Failed ❌:\n" + "\n".join(failed))
        return "\n".join(lines)

    # ------------------------------------------------ shared by all apps
    def link_secret(self) -> str:
        return self.config.telegram_token or self.config.teams_app_password

    def targets(self) -> list[str]:
        """Where briefings, reminders and reviews go."""
        tg = bool(self.app and self.config.owner_id)
        tm = bool(self.teams and self.db.get(teams.REF_KEY))
        mode = self.config.notify_channel.lower()
        if mode == "both":
            return [n for n, ok in (("telegram", tg), ("teams", tm)) if ok]
        if mode in ("telegram", "teams"):
            return [mode] if {"telegram": tg, "teams": tm}[mode] else []
        return ["telegram"] if tg else ["teams"] if tm else []

    async def notify(self, text: str) -> None:
        for target in self.targets():
            if target == "telegram":
                await self.app.bot.send_message(self.config.owner_id, text[:4000])
            else:
                await self.teams.send_text(text)

    async def deliver(self, reply, text: bool = True) -> None:
        """Send an agent reply (or just its files and approvals) to the owner's apps."""
        for target in self.targets():
            if target == "telegram":
                if text:
                    await self.send_reply(self.config.owner_id, reply, self.app.bot)
                else:
                    await self.send_files(self.config.owner_id, reply.files, self.app.bot)
                    await self.send_approvals(self.config.owner_id, reply.pending_ids, self.app.bot)
            elif text:
                await self.teams.send_reply(reply)
            else:
                await self.teams.send_reply(type(reply)("", reply.pending_ids, reply.files))

    def finish_login(self, chat_id: int, text: str) -> str | None:
        """A pasted http://localhost address finishes a Google or Strava login."""
        text = text.strip()
        if not text.startswith("http://localhost"):
            return None
        if strava.is_strava_redirect(text):
            try:
                strava.finish(self.db, self.config, text)
                return "Strava connected ✅ Try: 'What did I train this week?'"
            except Exception as e:
                return f"That didn't work: {e}\nTry /connect_strava again."
        if google_auth.is_waiting(chat_id):
            try:
                google_auth.finish(self.db, chat_id, text)
                return "Google connected ✅ Try: 'What's on my calendar today?'"
            except Exception as e:
                return f"That didn't work: {e}\nTry /connect_google again."
        return None

    def google_text(self, chat_id: int) -> str:
        try:
            url = google_auth.start(self.config, chat_id)
        except Exception as e:
            return f"Can't start Google connection: {e}"
        return ("1) Open this link and allow access:\n" + url +
                "\n\n2) Google may warn the app is unverified – click 'Advanced' → 'Go to … (unsafe)'. "
                "That's fine: it's your own app.\n"
                "3) You'll end on a page that says it can't be reached. That's expected! "
                "Copy the FULL address from the browser bar (starts with http://localhost) "
                "and paste it here.")

    def strava_text(self) -> str:
        try:
            url = strava.start(self.config)
        except Exception as e:
            return f"Can't start Strava connection: {e}"
        return ("1) Open this link and click 'Authorize':\n" + url +
                "\n\n2) You'll end on a page that says it can't be reached. That's expected! "
                "Copy the FULL address from the browser bar (starts with http://localhost) "
                "and paste it here.")

    async def microsoft_text(self, send) -> str:
        """Starts the code login; `send` posts the result to the same app when it's done."""
        try:
            flow = await microsoft.start(self.config)
        except Exception as e:
            return f"Can't start Microsoft connection: {e}"

        async def finish():
            try:
                await microsoft.wait_for_login(self.db, self.config, flow)
                await send("Microsoft connected ✅ (Outlook mail, calendar, OneDrive). "
                           "Try: 'What's in my Outlook calendar this week?'")
            except Exception as e:
                await send(f"Microsoft connection failed: {e}")
        asyncio.get_running_loop().create_task(finish())
        return (f"1) Open {flow['verification_uri']}\n2) Enter this code: {flow['user_code']}\n"
                "3) Sign in with your Microsoft account (Outlook / Microsoft 365) and accept.\n"
                "I'll tell you here when it's done.")

    def prompt_for(self, text: str):
        """Commands that are answered by the assistant: (prompt, saved as, use history)."""
        cmd = text.split()[0].lstrip("/").lower() if text.strip() else ""
        return {"briefing": (BRIEFING_PROMPT, BRIEFING_LABEL, False),
                "review": (WEEKLY_REVIEW_PROMPT, REVIEW_LABEL, None),
                "tasks": ("Show my open tasks.", None, None)}.get(cmd)

    async def command(self, text: str, send) -> str | None:
        """Commands typed in Teams (with or without '/'). None = not a command."""
        if (done := self.finish_login(self.home_chat, text)) is not None:
            return done
        cmd = text.split()[0].lstrip("/").lower() if text.strip() else ""
        if cmd in ("help", "start"):
            return HELP
        if cmd == "reset":
            self.db.clear_history(self.home_chat)
            return "Conversation memory cleared."
        if cmd == "skills":
            return "Skills switched on:\n" + "\n".join(f"• {s.name}: {s.description}" for s in self.skills)
        if cmd == "connect_google":
            return self.google_text(self.home_chat)
        if cmd == "connect_strava":
            return self.strava_text()
        if cmd in ("connect_microsoft", "connect_onedrive"):
            return await self.microsoft_text(send)
        if cmd == "talk":
            if not self.config.web_url:
                return "The voice page needs a public domain first (Railway → Settings → Networking)."
            return (f"Hands-free voice page: {self.talk_url()}?k={web.make_key(self.link_secret(), self.home_chat)}"
                    "\nOpen it, tap the microphone once and talk.")
        return None

    def voice_mode(self) -> str:
        return self.db.get("voice_replies", self.config.voice_replies)

    async def ask_agent(self, chat_id: int, text: str, bot, remember=True, spoken=False,
                        attachments=None, use_history=None, record_as=None) -> None:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        t0 = time.monotonic()
        try:
            reply = await self.agent.handle(chat_id, text, remember=remember, spoken=spoken,
                                            attachments=attachments, use_history=use_history,
                                            record_as=record_as)
        except Exception as e:
            log.exception("Agent error")
            await bot.send_message(chat_id, f"Something went wrong: {e}")
            return
        t1 = time.monotonic()
        await self.send_reply(chat_id, reply, bot)  # text first: shows up while the audio is made
        if voice.wants_voice(self.voice_mode(), spoken) and self.config.openai_api_key:
            try:
                await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
                audio = await voice.speak(self.config, reply.text,
                                          voice=self.db.get("tts_voice"), style=self.db.get("tts_style"))
                await bot.send_voice(chat_id, audio)
                log.info("timing: thinking %.1fs, speech %.1fs", t1 - t0, time.monotonic() - t1)
            except Exception:  # the text reply already arrived
                log.exception("Text-to-speech failed")

    # ----------------------------------------------------------- handlers
    async def on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if await self.guard(update):
            await update.message.reply_text(HELP)

    async def on_reset(self, update: Update, ctx):
        if await self.guard(update):
            self.db.clear_history(update.effective_chat.id)
            await update.message.reply_text("Conversation memory cleared.")

    async def on_skills(self, update: Update, ctx):
        if await self.guard(update):
            lines = [f"• {s.name}: {s.description}" for s in self.skills]
            await update.message.reply_text("Skills switched on:\n" + "\n".join(lines))

    async def on_tasks(self, update: Update, ctx):
        if await self.guard(update):
            await self.ask_agent(update.effective_chat.id, "Show my open tasks.", ctx.bot)

    async def on_briefing(self, update: Update, ctx):
        if await self.guard(update):
            await self.ask_agent(update.effective_chat.id, BRIEFING_PROMPT, ctx.bot,
                                 use_history=False, record_as=BRIEFING_LABEL)

    def talk_url(self) -> str:
        return f"{self.config.web_url}/talk"

    async def on_talk(self, update: Update, ctx):
        if not await self.guard(update):
            return
        if not self.config.web_url:
            await update.message.reply_text(
                "The voice page isn't switched on yet. In Railway: worker → Settings → Networking "
                "→ Generate Domain (port 8080). Then send /talk again.")
            return
        key = web.make_key(self.link_secret(), self.home_chat)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎙 Talk to me", web_app=WebAppInfo(self.talk_url()))],
            [InlineKeyboardButton("🌐 Open in browser", url=f"{self.talk_url()}?k={key}")],
        ])
        await update.message.reply_text(
            "Hands-free mode: tap the microphone once, then just talk. I answer out loud and "
            "listen again. You'll also find it under the 🎙 button next to the message field.",
            reply_markup=kb)

    async def on_review(self, update: Update, ctx):
        if await self.guard(update):
            await self.ask_agent(update.effective_chat.id, WEEKLY_REVIEW_PROMPT, ctx.bot,
                                 record_as=REVIEW_LABEL)

    async def on_connect_strava(self, update: Update, ctx):
        if await self.guard(update):
            await update.message.reply_text(self.strava_text())

    async def on_connect_google(self, update: Update, ctx):
        if await self.guard(update):
            await update.message.reply_text(self.google_text(update.effective_chat.id))

    async def on_text(self, update: Update, ctx):
        if not await self.guard(update):
            return
        chat_id = update.effective_chat.id
        text = update.message.text or ""
        if (done := self.finish_login(chat_id, text)) is not None:
            await update.message.reply_text(done)
            return
        await self.ask_agent(chat_id, text, ctx.bot)

    async def on_voice(self, update: Update, ctx):
        if not await self.guard(update):
            return
        chat_id = update.effective_chat.id
        media = update.message.voice or update.message.audio
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        t0 = time.monotonic()
        try:
            file = await media.get_file()
            audio = bytes(await file.download_as_bytearray())
            text = await voice.transcribe(self.config, audio,
                                          getattr(media, "file_name", None) or "voice.ogg")
        except Exception as e:
            log.exception("Transcription failed")
            await update.message.reply_text(f"I couldn't listen to that voice message: {e}")
            return
        if not text:
            await update.message.reply_text("I couldn't hear any words in that voice message.")
            return
        log.info("timing: listening %.1fs", time.monotonic() - t0)
        await update.message.reply_text(f"🎤 \"{text[:500]}\"")  # so mishearings are visible
        await self.ask_agent(chat_id, text, ctx.bot, spoken=True)

    async def on_file(self, update: Update, ctx):
        """Photos (menus, signs, receipts...), PDFs and documents sent in the chat."""
        if not await self.guard(update):
            return
        msg, chat_id = update.message, update.effective_chat.id
        if msg.photo:
            media, mime, name = msg.photo[-1], "image/jpeg", "photo.jpg"
        else:
            media = msg.document
            mime, name = media.mime_type or "", media.file_name or "file"
        if (media.file_size or 0) > MAX_FILE:
            await msg.reply_text("That file is too big for me (max 20 MB).")
            return
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        data = bytes(await (await media.get_file()).download_as_bytearray())
        b64 = base64.b64encode(data).decode()
        if mime in IMAGE_TYPES and len(data) <= MAX_IMAGE:
            block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
        elif mime == "application/pdf":
            block = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
        elif can_read(name):
            try:
                block = {"type": "text", "text": f"[File: {name}]\n{text_from_file(name, data)[:30000]}"}
            except Exception:
                log.exception("Could not read %s", name)
                await msg.reply_text(f"I couldn't open {name}; the file may be damaged.")
                return
        else:
            await msg.reply_text("I can read photos, PDFs, Word, Excel, PowerPoint and text files. "
                                 "Big images: send them as a photo instead of a file.")
            return
        caption = msg.caption or ("(Sent without a comment. Work out what it is and help: e.g. "
                                  "translate it if it's in another language, or log it if it's a receipt.)")
        await self.ask_agent(chat_id, caption, ctx.bot, attachments=[block])

    async def on_connect_microsoft(self, update: Update, ctx):
        if not await self.guard(update):
            return
        chat_id = update.effective_chat.id

        async def send(text):
            await ctx.bot.send_message(chat_id, text)
        await update.message.reply_text(await self.microsoft_text(send))

    async def on_voice_setting(self, update: Update, ctx):
        if not await self.guard(update):
            return
        if ctx.args and ctx.args[0].lower() in voice.MODES:
            self.db.set("voice_replies", ctx.args[0].lower())
        await update.message.reply_text(
            f"Spoken replies: {self.voice_mode()}\n\n"
            "/voice auto – I answer voice messages with voice\n"
            "/voice always – I always answer with voice too\n"
            "/voice off – text only")

    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        if not self.allowed(update):
            await q.answer("Not allowed")
            return
        await q.answer()
        kind, ref = q.data.split(":", 1)
        if kind in ("okall", "noall"):
            result = await self.resolve_batch(ref, approve=kind == "okall")
        else:
            result = await self.agent.resolve_action(int(ref), approve=(kind == "ok"))
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(result[:4000])

    # ------------------------------------------------------------- jobs
    async def run_scheduled(self, prompt: str, label: str, use_history: bool | None = None) -> None:
        if not self.targets():
            return
        try:  # saved, so a reply like "thanks" or "move that" knows what it said
            reply = await self.agent.handle(self.home_chat, prompt, use_history=use_history, record_as=label)
        except Exception:
            log.exception("Scheduled %s failed", label)
            return
        await self.deliver(reply)

    async def job_reminders(self, ctx=None):
        for r in due_reminders(self.db):
            await self.notify(f"⏰ Reminder: {r['text']}")

    async def job_briefing(self, ctx=None):
        await self.run_scheduled(BRIEFING_PROMPT, BRIEFING_LABEL, use_history=False)

    async def job_weekly_review(self, ctx=None):
        await self.run_scheduled(WEEKLY_REVIEW_PROMPT, REVIEW_LABEL)

    def _hhmm(self, value: str) -> str | None:
        if value.lower() == "off":
            return None
        hh, mm = map(int, value.split(":"))
        return f"{hh:02d}:{mm:02d}"

    async def scheduler(self) -> None:
        """Reminders, briefing and weekly review when Telegram's job queue isn't running."""
        tz, done = ZoneInfo(self.config.timezone), set()
        briefing = self._hhmm(self.config.briefing_time)
        review = self._hhmm(self.config.weekly_review_time) if "fitness" in self.config.enabled_skills else None
        while True:
            try:
                await self.job_reminders()
                now = datetime.now(tz)
                hm, day = now.strftime("%H:%M"), now.date()
                if hm == briefing and ("briefing", day) not in done:
                    done.add(("briefing", day))
                    await self.job_briefing()
                if hm == review and now.weekday() == 6 and ("review", day) not in done:
                    done.add(("review", day))
                    await self.job_weekly_review()
            except Exception:
                log.exception("Scheduled job failed")
            await asyncio.sleep(30)

    # -------------------------------------------------------------- run
    async def start_services(self) -> None:
        """The web server (voice page, Teams, file links) runs next to the bot."""
        if self.config.teams_app_id:
            self.teams = teams.Teams(self)
        self.web = web.WebApp(self)
        await self.web.start()

    async def post_init(self, app: Application) -> None:
        """Runs inside the Telegram bot's event loop."""
        await self.start_services()
        if self.config.web_url and self.config.owner_id:
            try:  # the button next to the message field opens the voice page
                await app.bot.set_chat_menu_button(
                    chat_id=self.config.owner_id,
                    menu_button=MenuButtonWebApp("🎙 Talk", WebAppInfo(self.talk_url())))
            except Exception:
                log.exception("Could not set the voice page menu button")

    async def post_shutdown(self, app: Application) -> None:
        if self.web:  # not started if Telegram couldn't be reached at start-up
            await self.web.stop()

    async def run_without_telegram(self) -> None:
        await self.start_services()
        log.info("Assistant running (Teams only) with skills: %s", ", ".join(s.name for s in self.skills))
        try:
            await self.scheduler()
        finally:
            await self.web.stop()

    def run(self):
        if not self.config.telegram_token:
            asyncio.run(self.run_without_telegram())
            return
        app = (Application.builder().token(self.config.telegram_token)
               .post_init(self.post_init).post_shutdown(self.post_shutdown).build())
        self.app = app
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("skills", self.on_skills))
        app.add_handler(CommandHandler("tasks", self.on_tasks))
        app.add_handler(CommandHandler("briefing", self.on_briefing))
        app.add_handler(CommandHandler("connect_google", self.on_connect_google))
        app.add_handler(CommandHandler("connect_strava", self.on_connect_strava))
        app.add_handler(CommandHandler("review", self.on_review))
        app.add_handler(CommandHandler("voice", self.on_voice_setting))
        app.add_handler(CommandHandler("talk", self.on_talk))
        app.add_handler(CommandHandler(["connect_microsoft", "connect_onedrive"], self.on_connect_microsoft))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.on_voice))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.on_file))

        app.job_queue.run_repeating(self.job_reminders, interval=30, first=10)
        if briefing := self._hhmm(self.config.briefing_time):
            hh, mm = map(int, briefing.split(":"))
            app.job_queue.run_daily(self.job_briefing,
                                    time=dtime(hh, mm, tzinfo=ZoneInfo(self.config.timezone)))
        if "fitness" in self.config.enabled_skills and (review := self._hhmm(self.config.weekly_review_time)):
            hh, mm = map(int, review.split(":"))
            app.job_queue.run_daily(self.job_weekly_review, days=(0,),  # 0 = Sunday
                                    time=dtime(hh, mm, tzinfo=ZoneInfo(self.config.timezone)))
        log.info("Assistant running with skills: %s", ", ".join(s.name for s in self.skills))
        app.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    config = Config()
    missing = config.missing()
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)} (see README)")
    Bot(config).run()


if __name__ == "__main__":
    main()
