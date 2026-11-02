"""Microsoft Teams front-end (Bot Framework protocol, no SDK).

Microsoft posts each message to /api/messages on the voice page's server. Every
request carries a token signed by Microsoft, checked before anything is read.
Only the owner's Microsoft account (TEAMS_OWNER_ID) in the owner's organisation
(TEAMS_TENANT_ID) gets an answer. Replies, approval cards and the morning
briefing are posted back through the Bot Connector REST API.
"""
import asyncio
import html
import io
import json
import logging
import re
import struct
import time
import zipfile
import zlib
from urllib.parse import urlparse

import httpx
import jwt

log = logging.getLogger(__name__)

BOT_ISSUER = "https://api.botframework.com"
JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
CARD = "application/vnd.microsoft.card.adaptive"
REF_KEY = "teams_ref"  # where to post: the owner's personal chat with the bot


def check_token(auth_header: str, app_id: str, service_url: str, keys) -> bool:
    """Is this request really from Microsoft's Bot Connector, for this bot?"""
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    try:
        key = keys.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=app_id,
                            issuer=BOT_ISSUER, leeway=300)
    except Exception as e:
        log.warning("Rejected Teams request: %s", e)
        return False
    claimed = claims.get("serviceurl") or claims.get("serviceUrl")
    return not claimed or claimed.rstrip("/") == service_url.rstrip("/")


def clean_text(text: str) -> str:
    """Teams may send HTML (mentions, formatting); the assistant wants plain words."""
    text = re.sub(r"<at>.*?</at>", "", text or "", flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def approval_card(pid: int, summary: str) -> dict:
    return {"type": "message", "attachments": [{"contentType": CARD, "content": {
        "type": "AdaptiveCard", "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "text": f"Needs your approval (#{pid})", "weight": "Bolder"},
                 {"type": "TextBlock", "text": summary[:3000], "wrap": True}],
        "actions": [{"type": "Action.Submit", "title": "✅ Approve", "data": {"ragnar": "ok", "pid": pid}},
                    {"type": "Action.Submit", "title": "❌ Reject", "data": {"ragnar": "no", "pid": pid}}],
    }}]}


def batch_card(token: str, pending_ids: list[int]) -> dict:
    n = len(pending_ids)
    return {"type": "message", "attachments": [{"contentType": CARD, "content": {
        "type": "AdaptiveCard", "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "wrap": True, "weight": "Bolder",
                  "text": f"{n} actions are waiting (#" + ", #".join(map(str, pending_ids)) + ")"}],
        "actions": [{"type": "Action.Submit", "title": f"✅ Approve all {n}", "data": {"ragnar": "okall", "batch": token}},
                    {"type": "Action.Submit", "title": "❌ Reject all", "data": {"ragnar": "noall", "batch": token}}],
    }}]}


class Teams:
    def __init__(self, bot):
        self.bot = bot  # the assistant's Bot (main.py): agent, db, config, commands
        self.config = bot.config
        self._keys = None
        self._token, self._token_expires = "", 0.0

    # ------------------------------------------------------------ inbound
    def keys(self):
        if self._keys is None:
            self._keys = jwt.PyJWKClient(JWKS_URL, cache_keys=True)
        return self._keys

    async def handle(self, request):
        """aiohttp handler for POST /api/messages."""
        from aiohttp import web
        activity = await request.json()
        ok = await asyncio.to_thread(check_token, request.headers.get("Authorization", ""),
                                     self.config.teams_app_id, activity.get("serviceUrl", ""), self.keys())
        if not ok:
            return web.Response(status=401)
        # Answer Microsoft at once (it retries slow requests); the reply is posted when ready.
        asyncio.get_running_loop().create_task(self.process(activity))
        return web.Response(status=200)

    async def process(self, a: dict) -> None:
        try:
            await self._process(a)
        except Exception:
            log.exception("Teams message failed")

    async def _process(self, a: dict) -> None:
        if a.get("type") != "message":
            return
        ref = {"serviceUrl": a["serviceUrl"], "conversation": a["conversation"]["id"]}
        tenant = ((a.get("channelData") or {}).get("tenant") or {}).get("id") \
            or a["conversation"].get("tenantId")
        if self.config.teams_tenant_id and tenant != self.config.teams_tenant_id:
            log.warning("Ignored Teams message from another organisation (%s)", tenant)
            return
        user = (a.get("from") or {}).get("aadObjectId")
        if not self.config.teams_owner_id:
            await self.send_text(f"Setup step: your Teams ID is {user}\nPut it in the "
                                 "TEAMS_OWNER_ID setting and restart me.", ref)
            return
        if user != self.config.teams_owner_id:
            await self.send_text("Sorry, I'm a private assistant.", ref)
            return
        self.bot.db.set(REF_KEY, ref)  # briefings and reminders can now reach this chat

        value = a.get("value") or {}
        if value.get("ragnar") in ("ok", "no"):  # an approval card was answered
            result = await self.bot.agent.resolve_action(int(value["pid"]), approve=value["ragnar"] == "ok")
            await self.send_text(result, ref)
            return
        if value.get("ragnar") in ("okall", "noall"):
            await self.send_text(await self.bot.resolve_batch(str(value["batch"]),
                                                              approve=value["ragnar"] == "okall"), ref)
            return

        await self.typing(ref)
        attachments = await self._images(a)
        text = clean_text(a.get("text", ""))
        if not text and not attachments:
            return
        if text and not attachments:
            answer = await self.bot.command(text, send=lambda t: self.send_text(t, ref))
            if answer is not None:
                await self.send_text(answer, ref)
                return
            if asked := self.bot.prompt_for(text):
                prompt, label, use_history = asked
                reply = await self.bot.agent.handle(self.bot.home_chat, prompt, record_as=label,
                                                    use_history=use_history)
                await self.send_reply(reply, ref)
                return
        reply = await self.bot.agent.handle(
            self.bot.home_chat, text or "(Sent an image without a comment. Work out what it is "
                                        "and help, e.g. translate it.)", attachments=attachments or None)
        await self.send_reply(reply, ref)

    async def _images(self, a: dict) -> list[dict]:
        """Pictures pasted into the chat, as Claude image blocks."""
        import base64
        blocks = []
        for att in a.get("attachments") or []:
            kind = att.get("contentType", "")
            if not kind.startswith("image/") or not att.get("contentUrl"):
                continue
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    r = await client.get(att["contentUrl"],
                                         headers={"Authorization": f"Bearer {await self.token()}"})
                if r.status_code == 200 and len(r.content) <= 5 * 1024 * 1024:
                    media = kind if kind in ("image/jpeg", "image/png", "image/gif", "image/webp") else "image/png"
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                               "data": base64.b64encode(r.content).decode()}})
            except Exception:
                log.exception("Could not download a Teams image")
        return blocks

    # ----------------------------------------------------------- outbound
    async def token(self) -> str:
        if time.time() < self._token_expires - 60:
            return self._token
        tenant = self.config.teams_tenant_id or "botframework.com"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": self.config.teams_app_id,
                "client_secret": self.config.teams_app_password,
                "scope": "https://api.botframework.com/.default"})
        if r.status_code != 200:
            raise RuntimeError(f"Teams login failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token, self._token_expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def post(self, activity: dict, ref: dict | None = None) -> None:
        ref = ref or self.bot.db.get(REF_KEY)
        if not ref:
            raise RuntimeError("No Teams chat yet: the owner has to message the bot in Teams once.")
        url = f"{ref['serviceUrl'].rstrip('/')}/v3/conversations/{ref['conversation']}/activities"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=activity, headers={"Authorization": f"Bearer {await self.token()}"})
        if r.status_code >= 300:
            raise RuntimeError(f"Teams said {r.status_code}: {r.text[:200]}")

    async def typing(self, ref: dict) -> None:
        try:
            await self.post({"type": "typing"}, ref)
        except Exception:
            log.warning("Teams typing indicator failed")

    async def send_text(self, text: str, ref: dict | None = None) -> None:
        for i in range(0, len(text) or 1, 4000):
            await self.post({"type": "message", "textFormat": "markdown", "text": text[i:i + 4000]}, ref)

    async def send_reply(self, reply, ref: dict | None = None) -> None:
        await self.send_text(reply.text, ref)
        if reply.files:  # Teams bots can't simply attach files: send a private download link
            links = [f"📎 [{name}]({self.bot.web.file_link(name, data)})" for name, data in reply.files]
            await self.send_text("\n\n".join(links), ref)
        for pid in reply.pending_ids:
            row = self.bot.db.query("SELECT summary FROM pending_actions WHERE id=?", (pid,))[0]
            await self.post(approval_card(pid, row["summary"]), ref)
        if len(reply.pending_ids) > 1:
            await self.post(batch_card(self.bot.new_batch(reply.pending_ids), reply.pending_ids), ref)


# --------------------------------------------------------------- app package
def _png(size: int, pixel) -> bytes:
    """A tiny PNG writer, so the icons need no image library. pixel(x, y) -> RGBA."""
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _circle(size: int, rgba, ring: bool = False):
    c, r = (size - 1) / 2, size * 0.42

    def pixel(x, y):
        d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
        inside = d <= r and (not ring or d >= r - size * 0.12)
        return rgba if inside else (0, 0, 0, 0) if ring else (255, 255, 255, 255)
    return pixel


def app_package(config) -> bytes:
    """The zip to upload in Teams (Apps → Manage your apps → Upload an app)."""
    if not (config.teams_app_id and config.web_url):
        raise RuntimeError("TEAMS_APP_ID and a public domain are needed first.")
    name = config.assistant_name[:30]
    commands = [("briefing", "Morning briefing now"), ("review", "Weekly training review"),
                ("tasks", "Open tasks"), ("skills", "What I can do"), ("reset", "Clear chat memory"),
                ("connect_microsoft", "Link Outlook, calendar and OneDrive"),
                ("connect_google", "Link Gmail, Calendar and Drive"), ("talk", "Hands-free voice page")]
    manifest = {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17", "version": "1.0.0", "id": config.teams_app_id,
        "developer": {"name": name, "websiteUrl": config.web_url,
                      "privacyUrl": f"{config.web_url}/privacy", "termsOfUseUrl": f"{config.web_url}/privacy"},
        "name": {"short": name, "full": f"{name}, personal AI assistant"},
        "description": {"short": "Your personal AI assistant for mail, calendar and tasks",
                        "full": "A private assistant that reads your mail and calendar, keeps your tasks and "
                                "notes, writes documents and plans your week. Anything it sends or changes "
                                "waits for your approval."},
        "icons": {"color": "color.png", "outline": "outline.png"}, "accentColor": "#5288C1",
        "bots": [{"botId": config.teams_app_id, "scopes": ["personal"], "supportsFiles": False,
                  "isNotificationOnly": False,
                  "commandLists": [{"scopes": ["personal"],
                                    "commands": [{"title": t, "description": d} for t, d in commands]}]}],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [urlparse(config.web_url).netloc],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("color.png", _png(192, _circle(192, (82, 136, 193, 255))))
        z.writestr("outline.png", _png(32, _circle(32, (255, 255, 255, 255), ring=True)))
    return buf.getvalue()
