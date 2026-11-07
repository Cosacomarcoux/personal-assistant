"""All settings come from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Like os.getenv, but an empty value (e.g. a blank Railway variable) means 'use the default'."""
    return os.getenv(name) or default


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: _env("MODEL", "claude-sonnet-5-5"))
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    # Only this Telegram user may talk to the bot. Leave empty on first run:
    # the bot will tell you your ID.
    owner_id: int = field(default_factory=lambda: int(_env("OWNER_TELEGRAM_ID", "0")))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "America/Argentina/Buenos_Aires"))
    assistant_name: str = field(default_factory=lambda: _env("ASSISTANT_NAME", "Secretario"))
    owner_name: str = field(default_factory=lambda: _env("OWNER_NAME", "Roberto"))
    # Comma-separated list of skills to switch on.
    enabled_skills: list[str] = field(
        default_factory=lambda: _list(_env("ENABLED_SKILLS", "google,outlook,tasks,research,admin,fitness,travel,onedrive,study,writing,ideas,content,career,budget,nutrition,habits,people,journal"))
    )
    # Language for every reply (also the briefing and reminders).
    language: str = field(default_factory=lambda: _env(
        "LANGUAGE", "Spanish from Argentina (rioplatense: use 'vos', natural and direct)"))
    # Daily briefing time, HH:MM in your timezone. "off" = no briefing.
    briefing_time: str = field(default_factory=lambda: _env("BRIEFING_TIME", "07:00"))
    # Weekly training review, sent on Sundays at HH:MM (needs the fitness skill). "off" = none.
    weekly_review_time: str = field(default_factory=lambda: _env("WEEKLY_REVIEW_TIME", "19:00"))
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET", ""))
    # Strava (brings in workouts from Garmin/Apple Watch etc.) - connect with /connect_strava
    strava_client_id: str = field(default_factory=lambda: _env("STRAVA_CLIENT_ID", ""))
    strava_client_secret: str = field(default_factory=lambda: _env("STRAVA_CLIENT_SECRET", ""))
    # Flight and hotel prices (Google Flights/Hotels via serpapi.com, free tier ~250 searches/month)
    serpapi_api_key: str = field(default_factory=lambda: _env("SERPAPI_API_KEY", ""))
    currency: str = field(default_factory=lambda: _env("CURRENCY", "ARS"))
    # Microsoft (Outlook mail + calendar, OneDrive) - connect with /connect_microsoft
    ms_client_id: str = field(default_factory=lambda: _env("MS_CLIENT_ID", ""))
    ms_tenant: str = field(default_factory=lambda: _env("MS_TENANT", "common"))
    # Microsoft Teams bot (Azure Bot resource, single tenant). Empty = Teams off.
    teams_app_id: str = field(default_factory=lambda: _env("TEAMS_APP_ID", ""))
    teams_app_password: str = field(default_factory=lambda: _env("TEAMS_APP_PASSWORD", ""))
    teams_tenant_id: str = field(default_factory=lambda: _env("TEAMS_TENANT_ID", ""))
    teams_owner_id: str = field(default_factory=lambda: _env("TEAMS_OWNER_ID", ""))
    # Where briefings, reminders and reviews go: auto (Telegram if set up, else Teams),
    # telegram, teams or both.
    notify_channel: str = field(default_factory=lambda: _env("NOTIFY_CHANNEL", "auto"))
    # Voice messages are transcribed with OpenAI's speech-to-text API. Empty = voice off.
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY", ""))
    transcribe_model: str = field(
        default_factory=lambda: _env("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"))
    # Railway volume at /data if there is one, otherwise a local folder.
    # Spoken replies: "auto" = answer voice messages with voice, "always", or "off".
    # Can also be changed from Telegram with /voice.
    voice_replies: str = field(default_factory=lambda: _env("VOICE_REPLIES", "auto"))
    # Voice messages are answered with less thinking (and optionally a faster model) for speed.
    voice_effort: str = field(default_factory=lambda: _env("VOICE_EFFORT", "low"))
    voice_model: str = field(default_factory=lambda: _env("VOICE_MODEL", ""))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "gpt-4o-mini-tts"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "onyx"))
    # How the voice should sound (Ragnar can change this himself when asked).
    tts_style: str = field(default_factory=lambda: _env(
        "TTS_STYLE", "Warm, cheerful and upbeat, like a friendly personal coach who is smiling. "
                     "Natural energy and a lively but relaxed pace."))
    # Hands-free voice page. Railway sets RAILWAY_PUBLIC_DOMAIN once you generate a domain.
    web_url: str = field(default_factory=lambda: _env("WEB_URL") or (
        f"https://{_env('RAILWAY_PUBLIC_DOMAIN')}" if _env("RAILWAY_PUBLIC_DOMAIN") else ""))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    data_dir: Path = field(default_factory=lambda: Path(
        _env("DATA_DIR", "/data" if Path("/data").is_dir() else "./data")))
    history_turns: int = field(default_factory=lambda: int(_env("HISTORY_TURNS", "20")))
    extra_instructions: str = field(default_factory=lambda: _env("EXTRA_INSTRUCTIONS", ""))

    def missing(self) -> list[str]:
        out = []
        if not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY")
        if not self.telegram_token and not self.teams_app_id:
            out.append("TELEGRAM_BOT_TOKEN or TEAMS_APP_ID")
        if self.teams_app_id and not self.teams_app_password:
            out.append("TEAMS_APP_PASSWORD")
        return out
