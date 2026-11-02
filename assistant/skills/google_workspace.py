"""Gmail, Google Calendar and Google Drive."""
import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from .. import google_auth
from ..extract import can_read, text_from_file
from .base import Skill, Tool, ToolContext, schema

MAX_BODY = 8000


def _svc(ctx: ToolContext, name: str, version: str):
    creds = google_auth.get_credentials(ctx.db)
    return build(name, version, credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------- Gmail
def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _plain_text(payload: dict) -> str:
    """Find the text/plain part of an email (fall back to stripped HTML)."""
    def walk(p):
        yield p
        for c in p.get("parts", []) or []:
            yield from walk(c)

    html = ""
    for part in walk(payload):
        data = part.get("body", {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
        if part.get("mimeType") == "text/plain":
            return text
        if part.get("mimeType") == "text/html" and not html:
            html = text
    if html:
        import re
        html = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S | re.I)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return ""


def gmail_search(args, ctx):
    svc = _svc(ctx, "gmail", "v1")
    res = svc.users().messages().list(
        userId="me", q=args.get("query", "in:inbox"),
        maxResults=min(int(args.get("max_results", 10)), 25)).execute()
    out = []
    for m in res.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        h = _headers(msg)
        out.append({"id": m["id"], "thread_id": msg["threadId"], "from": h.get("from"),
                    "subject": h.get("subject"), "date": h.get("date"),
                    "unread": "UNREAD" in msg.get("labelIds", []),
                    "snippet": msg.get("snippet")})
    return out or "No emails found."


def gmail_read(args, ctx):
    svc = _svc(ctx, "gmail", "v1")
    msg = svc.users().messages().get(userId="me", id=args["message_id"], format="full").execute()
    h = _headers(msg)
    return {"id": msg["id"], "thread_id": msg["threadId"], "from": h.get("from"),
            "to": h.get("to"), "cc": h.get("cc"), "subject": h.get("subject"),
            "date": h.get("date"), "body": _plain_text(msg["payload"])[:MAX_BODY]}


def _build_raw(ctx, args):
    svc = _svc(ctx, "gmail", "v1")
    em = EmailMessage()
    em["To"] = args["to"]
    if args.get("cc"):
        em["Cc"] = args["cc"]
    em["Subject"] = args["subject"]
    em.set_content(args["body"])
    body = {}
    if args.get("reply_to_message_id"):
        orig = svc.users().messages().get(
            userId="me", id=args["reply_to_message_id"], format="metadata",
            metadataHeaders=["Message-ID", "References"]).execute()
        h = _headers(orig)
        if h.get("message-id"):
            em["In-Reply-To"] = h["message-id"]
            em["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
        body["threadId"] = orig["threadId"]
    body["raw"] = base64.urlsafe_b64encode(em.as_bytes()).decode()
    return svc, body


def gmail_create_draft(args, ctx):
    svc, body = _build_raw(ctx, args)
    d = svc.users().drafts().create(userId="me", body={"message": body}).execute()
    return f"Draft saved in Gmail (draft id {d['id']})."


def gmail_send(args, ctx):
    svc, body = _build_raw(ctx, args)
    m = svc.users().messages().send(userId="me", body=body).execute()
    return f"Email sent (id {m['id']})."


def gmail_archive(args, ctx):
    svc = _svc(ctx, "gmail", "v1")
    ids = args["message_ids"]
    svc.users().messages().batchModify(
        userId="me", body={"ids": ids, "removeLabelIds": ["INBOX", "UNREAD"]}).execute()
    return f"Archived {len(ids)} email(s)."


EMAIL_PROPS = {
    "to": {"type": "string", "description": "Recipient email(s), comma separated"},
    "cc": {"type": "string"},
    "subject": {"type": "string"},
    "body": {"type": "string", "description": "Plain-text body, including greeting and sign-off"},
    "reply_to_message_id": {"type": "string",
                            "description": "Gmail message id when replying, to keep the thread"},
}


def _describe_email(a):
    return f"📧 Send email\nTo: {a.get('to')}\nSubject: {a.get('subject')}\n\n{a.get('body', '')[:1500]}"


# ------------------------------------------------------------- Calendar
def _tz(ctx):
    return ZoneInfo(ctx.config.timezone)


def _iso(value: str, ctx) -> str:
    """Accept '2026-10-21' or '2026-10-21T14:00' and return RFC3339 with timezone."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(ctx))
    return dt.isoformat()


def _calendars(svc) -> list[dict]:
    """Every calendar in the user's list (primary, 'Familj', shared, subscribed...)."""
    out, token = [], None
    while True:
        res = svc.calendarList().list(pageToken=token).execute()
        out += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            return out


def _calendar_id(svc, value: str | None) -> str:
    """Accept a calendar id or name (e.g. 'Familj'); default is the primary calendar."""
    if not value or value == "primary":
        return "primary"
    for c in _calendars(svc):
        name = c.get("summaryOverride") or c.get("summary", "")
        if value == c["id"] or value.lower() == name.lower():
            return c["id"]
    raise ValueError(f"No calendar called '{value}'. Use calendar_list to see the names.")


def _sort_key(when: str, ctx) -> datetime:
    dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz(ctx))


def calendar_list(args, ctx):
    svc = _svc(ctx, "calendar", "v3")
    return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary"),
             "primary": c.get("primary", False), "access": c.get("accessRole")}
            for c in _calendars(svc)]


MAX_EVENTS = 250  # per call; beyond this the tool says so instead of silently cutting


def calendar_list_events(args, ctx):
    svc = _svc(ctx, "calendar", "v3")
    now = datetime.now(_tz(ctx))
    start = _iso(args["start"], ctx) if args.get("start") else now.replace(hour=0, minute=0).isoformat()
    end = _iso(args["end"], ctx) if args.get("end") else (now + timedelta(days=7)).isoformat()
    if args.get("calendar"):
        cals = [{"id": _calendar_id(svc, args["calendar"]), "summary": args["calendar"]}]
    else:
        cals = _calendars(svc)
    search = {"q": args["query"]} if args.get("query") else {}
    events, failed, more = [], [], False
    for cal in cals:
        name = cal.get("summaryOverride") or cal.get("summary") or cal["id"]
        page = None
        while True:
            try:
                res = svc.events().list(calendarId=cal["id"], timeMin=start, timeMax=end, singleEvents=True,
                                        orderBy="startTime", maxResults=250, pageToken=page, **search).execute()
            except Exception:  # one broken/unshared calendar shouldn't hide the others
                failed.append(name)
                break
            events += [{
                "id": e["id"], "title": e.get("summary", "(no title)"),
                "calendar": name, "calendar_id": cal["id"],
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
                "location": e.get("location"),
                "attendees": [a.get("email") for a in e.get("attendees", [])][:10],
                **({"description": (e.get("description") or "")[:150]} if search else {}),
            } for e in res.get("items", [])]
            page = res.get("nextPageToken")
            if not page:
                break
            if len(events) >= MAX_EVENTS:
                more = True
                break
    events.sort(key=lambda e: _sort_key(e["start"], ctx))
    if len(events) > MAX_EVENTS:
        events, more = events[:MAX_EVENTS], True
    if more:
        events.append({"note": f"Only the first {MAX_EVENTS} events are shown; there are more. "
                               "Use a shorter period or a query to see the rest."})
    if failed:
        events.append({"note": f"Could not read calendar(s): {', '.join(failed)}"})
    return events or "No events in that period."


def calendar_create_event(args, ctx):
    svc = _svc(ctx, "calendar", "v3")
    tz = ctx.config.timezone
    if args.get("all_day"):
        start = {"date": args["start"][:10]}
        end = {"date": (args.get("end") or args["start"])[:10]}
        if end["date"] == start["date"]:
            end["date"] = (datetime.fromisoformat(start["date"]) + timedelta(days=1)).date().isoformat()
    else:
        start = {"dateTime": _iso(args["start"], ctx), "timeZone": tz}
        end_raw = args.get("end") or (datetime.fromisoformat(args["start"]) + timedelta(hours=1)).isoformat()
        end = {"dateTime": _iso(end_raw, ctx), "timeZone": tz}
    body = {"summary": args["title"], "start": start, "end": end,
            "description": args.get("description", ""), "location": args.get("location", "")}
    if args.get("attendees"):
        body["attendees"] = [{"email": e.strip()} for e in args["attendees"].split(",")]
    ev = svc.events().insert(calendarId=_calendar_id(svc, args.get("calendar")), body=body,
                             sendUpdates="all" if args.get("attendees") else "none").execute()
    return f"Event created: {ev.get('htmlLink')}"


def calendar_delete_event(args, ctx):
    svc = _svc(ctx, "calendar", "v3")
    svc.events().delete(calendarId=args.get("calendar_id") or "primary",
                        eventId=args["event_id"]).execute()
    return "Event deleted."


# ---------------------------------------------------------------- Drive
def drive_search(args, ctx):
    svc = _svc(ctx, "drive", "v3")
    q = args["query"].replace("'", "\\'")
    res = svc.files().list(
        q=f"(name contains '{q}' or fullText contains '{q}') and trashed=false",
        pageSize=15, fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        orderBy="modifiedTime desc").execute()
    return res.get("files") or "No files found."


def drive_read(args, ctx):
    svc = _svc(ctx, "drive", "v3")
    meta = svc.files().get(fileId=args["file_id"], fields="id,name,mimeType,webViewLink").execute()
    exports = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    mt = meta["mimeType"]
    if mt in exports:
        data = svc.files().export(fileId=meta["id"], mimeType=exports[mt]).execute()
    elif mt.startswith("text/") or mt in ("application/json", "text/csv"):
        data = svc.files().get_media(fileId=meta["id"]).execute()
    elif can_read(meta["name"]):  # PDF, Word, Excel, PowerPoint files uploaded to Drive
        text = text_from_file(meta["name"], svc.files().get_media(fileId=meta["id"]).execute())
        return {**meta, "content": text[:MAX_BODY]}
    else:
        return {**meta, "note": "Can't read this file type as text; share the link instead."}
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return {**meta, "content": text[:MAX_BODY]}


# ---------------------------------------------------------------- skill
class GoogleSkill(Skill):
    def __init__(self):
        super().__init__(
            name="Email, calendar & Drive (Google)",
            description="Read/search/draft/send Gmail, manage Google Calendar, search Drive.",
            instructions=(
                "Gmail search uses Gmail syntax, e.g. 'is:unread newer_than:1d', "
                "'from:anna subject:invoice'. Read an email before replying to it. "
                "Prefer gmail_create_draft when the user just wants a draft; use gmail_send "
                "(needs approval) when they want it sent. Match the user's language and tone. "
                "calendar_list_events reads ALL the user's calendars (e.g. 'Familj') and "
                "tags each event with its calendar. Before creating an event, check for "
                "conflicts. New events go to the primary calendar unless the user names one. "
                "Dates/times are ISO format in the user's timezone, e.g. 2026-10-21T14:00."
            ),
            tools=[
                Tool("gmail_search", "Search Gmail. Returns id, sender, subject, date, snippet.",
                     schema({"query": {"type": "string", "description": "Gmail search query"},
                             "max_results": {"type": "integer"}}), gmail_search),
                Tool("gmail_read", "Read the full text of one email.",
                     schema({"message_id": {"type": "string"}}, ["message_id"]), gmail_read),
                Tool("gmail_create_draft", "Save an email as a Gmail draft (not sent).",
                     schema(EMAIL_PROPS, ["to", "subject", "body"]), gmail_create_draft),
                Tool("gmail_send", "Send an email. Requires user approval.",
                     schema(EMAIL_PROPS, ["to", "subject", "body"]), gmail_send,
                     needs_approval=True, describe=_describe_email),
                Tool("gmail_archive", "Archive emails (remove from inbox, mark read). Requires approval.",
                     schema({"message_ids": {"type": "array", "items": {"type": "string"}}},
                            ["message_ids"]), gmail_archive, needs_approval=True,
                     describe=lambda a: f"🗄 Archive {len(a.get('message_ids', []))} email(s)"),
                Tool("calendar_list", "List the user's Google calendars (names and ids).",
                     schema({}), calendar_list),
                Tool("calendar_list_events",
                     "List events from all calendars between start and end (ISO). "
                     "Defaults: today to +7 days. Optionally limit to one calendar, or search "
                     "(query matches title, description, location and guests) - use a query "
                     "to find events over long periods.",
                     schema({"start": {"type": "string"}, "end": {"type": "string"},
                             "query": {"type": "string", "description": "Words to search for, e.g. 'Snöläge'"},
                             "calendar": {"type": "string",
                                          "description": "Calendar name or id; omit for all"}}),
                     calendar_list_events),
                Tool("calendar_create_event", "Create a calendar event. Requires approval.",
                     schema({"title": {"type": "string"}, "start": {"type": "string"},
                             "end": {"type": "string"}, "all_day": {"type": "boolean"},
                             "location": {"type": "string"}, "description": {"type": "string"},
                             "attendees": {"type": "string", "description": "Comma-separated emails"},
                             "calendar": {"type": "string",
                                          "description": "Calendar name or id; default primary"}},
                            ["title", "start"]),
                     calendar_create_event, needs_approval=True,
                     describe=lambda a: (f"📅 New event: {a.get('title')}\nStart: {a.get('start')}"
                                         f"\nEnd: {a.get('end', '(1 hour)')}"
                                         + (f"\nWith: {a['attendees']}" if a.get("attendees") else "")
                                         + (f"\nWhere: {a['location']}" if a.get("location") else "")
                                         + (f"\nCalendar: {a['calendar']}" if a.get("calendar") else ""))),
                Tool("calendar_delete_event", "Delete a calendar event. Requires approval.",
                     schema({"event_id": {"type": "string"},
                             "calendar_id": {"type": "string",
                                             "description": "calendar_id from calendar_list_events"},
                             "title": {"type": "string",
                                       "description": "Event title, shown to the user for approval"}},
                            ["event_id"]),
                     calendar_delete_event, needs_approval=True,
                     describe=lambda a: f"🗑 Delete event: {a.get('title', a.get('event_id'))}"),
                Tool("drive_search", "Search Google Drive files by name or content.",
                     schema({"query": {"type": "string"}}, ["query"]), drive_search),
                Tool("drive_read", "Read the text of a Drive file (Docs, Sheets, text).",
                     schema({"file_id": {"type": "string"}}, ["file_id"]), drive_read),
            ],
        )
