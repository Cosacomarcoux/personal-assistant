"""Outlook mail and calendar (Microsoft 365 / Outlook.com) via Microsoft Graph."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import microsoft
from .base import Skill, Tool, schema

MAX_BODY = 8000
MAX_EVENTS = 250  # per call; beyond this the tool says so instead of silently cutting
TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}
LIST_FIELDS = "id,subject,from,receivedDateTime,isRead,bodyPreview,conversationId"


def _g(ctx, path, **kw):
    return microsoft.graph(ctx.db, ctx.config, path, **kw)


def _who(r: dict | None) -> str | None:
    e = (r or {}).get("emailAddress") or {}
    return f"{e.get('name')} <{e.get('address')}>" if e.get("name") else e.get("address")


def _recipients(value: str | None) -> list[dict]:
    return [{"emailAddress": {"address": a.strip()}} for a in (value or "").split(",") if a.strip()]


# ------------------------------------------------------------------ mail
def outlook_search(a, ctx):
    n = min(int(a.get("max_results", 10)), 25)
    if a.get("query"):  # Outlook's search can't be combined with sorting; newest-first is its default
        params = {"$search": f'"{a["query"]}"', "$top": n, "$select": LIST_FIELDS}
        res = _g(ctx, "/me/messages", params=params)
    else:
        params = {"$top": n, "$select": LIST_FIELDS, "$orderby": "receivedDateTime desc"}
        if a.get("unread_only"):
            params["$filter"] = "isRead eq false"
        res = _g(ctx, f"/me/mailFolders/{a.get('folder', 'inbox')}/messages", params=params)
    return [{"id": m["id"], "from": _who(m.get("from")), "subject": m.get("subject"),
             "date": m.get("receivedDateTime"), "unread": not m.get("isRead"),
             "snippet": (m.get("bodyPreview") or "")[:200]}
            for m in res.get("value", [])] or "No emails found."


def outlook_read(a, ctx):
    m = _g(ctx, f"/me/messages/{a['message_id']}", headers=TEXT_BODY, params={
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,webLink"})
    return {"id": m["id"], "from": _who(m.get("from")), "subject": m.get("subject"),
            "to": [_who(r) for r in m.get("toRecipients", [])],
            "cc": [_who(r) for r in m.get("ccRecipients", [])],
            "date": m.get("receivedDateTime"), "link": m.get("webLink"),
            "body": ((m.get("body") or {}).get("content") or "")[:MAX_BODY]}


def _message(a) -> dict:
    msg = {"subject": a.get("subject", ""), "body": {"contentType": "Text", "content": a["body"]},
           "toRecipients": _recipients(a.get("to"))}
    if a.get("cc"):
        msg["ccRecipients"] = _recipients(a["cc"])
    return msg


def outlook_create_draft(a, ctx):
    if a.get("reply_to_message_id"):  # keeps the thread and quotes the original
        d = _g(ctx, f"/me/messages/{a['reply_to_message_id']}/createReply", method="POST",
               body={"comment": a["body"]})
    else:
        d = _g(ctx, "/me/messages", method="POST", body=_message(a))
    return f"Draft saved in Outlook: {d.get('webLink', d.get('id'))}"


def outlook_send(a, ctx):
    if a.get("reply_to_message_id"):
        body = {"comment": a["body"]}
        if a.get("to"):
            body["message"] = {"toRecipients": _recipients(a["to"])}
        _g(ctx, f"/me/messages/{a['reply_to_message_id']}/reply", method="POST", body=body)
    else:
        _g(ctx, "/me/sendMail", method="POST", body={"message": _message(a), "saveToSentItems": True})
    return "Email sent."


def outlook_archive(a, ctx):
    for mid in a["message_ids"]:
        _g(ctx, f"/me/messages/{mid}/move", method="POST", body={"destinationId": "archive"})
    return f"Archived {len(a['message_ids'])} email(s)."


def _describe_send(a):
    head = "📧 Reply (Outlook)" if a.get("reply_to_message_id") else "📧 Send email (Outlook)"
    to = f"\nTo: {a['to']}" if a.get("to") else ""
    return f"{head}{to}\nSubject: {a.get('subject', '(reply)')}\n\n{a.get('body', '')[:1500]}"


# -------------------------------------------------------------- calendar
def _tz(ctx):
    return ZoneInfo(ctx.config.timezone)


def _local(value: str, ctx) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz(ctx))


def _utc(value: str, ctx) -> dict:
    """Graph time in UTC: certain to be understood, shown in the user's own zone by Outlook."""
    dt = _local(value, ctx).astimezone(timezone.utc)
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


def _from_graph(t: dict, ctx, all_day: bool) -> str:
    stamp = t["dateTime"][:19]
    if all_day:
        return stamp[:10]
    tz = timezone.utc if t.get("timeZone", "UTC") == "UTC" else _tz(ctx)
    return datetime.fromisoformat(stamp).replace(tzinfo=tz).astimezone(_tz(ctx)).isoformat()


def _calendars(ctx) -> list[dict]:
    return _g(ctx, "/me/calendars", params={"$select": "id,name,isDefaultCalendar,canEdit"}).get("value", [])


def _calendar_path(ctx, name: str | None) -> str:
    if not name:
        return "/me/events"
    for c in _calendars(ctx):
        if name in (c["id"], c.get("name")) or name.lower() == (c.get("name") or "").lower():
            return f"/me/calendars/{c['id']}/events"
    raise ValueError(f"No Outlook calendar called '{name}'.")


def outlook_calendar_list(a, ctx):
    now = datetime.now(_tz(ctx))
    start = _local(a["start"], ctx) if a.get("start") else now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = _local(a["end"], ctx) if a.get("end") else now + timedelta(days=7)
    window = {"startDateTime": start.isoformat(), "endDateTime": end.isoformat(), "$top": 250,
              "$select": "id,subject,start,end,isAllDay,location,attendees,onlineMeeting,webLink,bodyPreview"}
    query = (a.get("query") or "").lower()
    events, failed, more = [], [], False
    for cal in _calendars(ctx):
        items, path, params = [], f"/me/calendars/{cal['id']}/calendarView", window
        try:
            while True:  # follow Outlook's pages until the period is covered
                res = _g(ctx, path, params=params)
                items += res.get("value", [])
                link = res.get("@odata.nextLink")
                if not link or len(items) >= MAX_EVENTS:
                    more = more or bool(link)
                    break
                path, params = link.split("/v1.0", 1)[1], None
        except Exception:  # one shared calendar failing shouldn't hide the rest
            failed.append(cal.get("name"))
            continue
        for e in items:
            text = " ".join([e.get("subject") or "", (e.get("location") or {}).get("displayName") or "",
                             e.get("bodyPreview") or ""]).lower()
            if query and query not in text:
                continue
            all_day = bool(e.get("isAllDay"))
            events.append({
                "id": e["id"], "title": e.get("subject") or "(no title)", "calendar": cal.get("name"),
                "start": _from_graph(e["start"], ctx, all_day), "end": _from_graph(e["end"], ctx, all_day),
                "all_day": all_day, "location": (e.get("location") or {}).get("displayName") or None,
                "attendees": [_who(x) for x in e.get("attendees", [])][:10],
                "teams_link": (e.get("onlineMeeting") or {}).get("joinUrl")})
    events.sort(key=lambda e: e["start"] if not e["all_day"] else e["start"] + "T00:00")
    if len(events) > MAX_EVENTS:
        events, more = events[:MAX_EVENTS], True
    if more:
        events.append({"note": f"Only the first {MAX_EVENTS} events are shown; there are more. "
                               "Use a shorter period or a query to see the rest."})
    if failed:
        events.append({"note": f"Could not read calendar(s): {', '.join(failed)}"})
    return events or "No events in that period."


def outlook_create_event(a, ctx):
    if a.get("all_day"):
        day = a["start"][:10]
        last = (a.get("end") or day)[:10]
        end_day = (datetime.fromisoformat(last) + timedelta(days=1)).date().isoformat()
        start = {"dateTime": f"{day}T00:00:00", "timeZone": "UTC"}
        end = {"dateTime": f"{end_day}T00:00:00", "timeZone": "UTC"}
    else:
        start = _utc(a["start"], ctx)
        end_raw = a.get("end") or (_local(a["start"], ctx) + timedelta(hours=1)).isoformat()
        end = _utc(end_raw, ctx)
    body = {"subject": a["title"], "start": start, "end": end, "isAllDay": bool(a.get("all_day")),
            "body": {"contentType": "Text", "content": a.get("description", "")}}
    if a.get("location"):
        body["location"] = {"displayName": a["location"]}
    if a.get("attendees"):
        body["attendees"] = [{**r, "type": "required"} for r in _recipients(a["attendees"])]
    if a.get("teams_meeting"):
        body |= {"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"}
    ev = _g(ctx, _calendar_path(ctx, a.get("calendar")), method="POST", body=body)
    join = (ev.get("onlineMeeting") or {}).get("joinUrl")
    return f"Event created: {ev.get('webLink', ev.get('id'))}" + (f"\nTeams link: {join}" if join else "")


def outlook_delete_event(a, ctx):
    _g(ctx, f"/me/events/{a['event_id']}", method="DELETE")
    return "Event deleted."


EMAIL_PROPS = {
    "to": {"type": "string", "description": "Recipient email(s), comma separated"},
    "cc": {"type": "string"},
    "subject": {"type": "string"},
    "body": {"type": "string", "description": "Plain-text body, including greeting and sign-off"},
    "reply_to_message_id": {"type": "string",
                            "description": "Outlook message id when replying (keeps the thread)"},
}


class OutlookSkill(Skill):
    def __init__(self):
        super().__init__(
            name="Outlook (Microsoft 365)",
            description="Outlook mail and calendar: read, draft, send and book with approval.",
            instructions=(
                "Use the outlook_* tools for the user's Microsoft mail and calendar (work or "
                "Outlook.com). If both Google and Outlook are connected, check both when asked "
                "about 'my calendar' or 'my email', and say which account things are in. Read an "
                "email before replying to it. Prefer outlook_create_draft when the user wants a "
                "draft; outlook_send needs approval. Before booking, check outlook_calendar_list "
                "for conflicts; offer a Teams meeting (teams_meeting) for work meetings with "
                "others. To delete an event, use the id from outlook_calendar_list. Times are ISO "
                "in the user's timezone, e.g. 2026-10-21T14:00."
            ),
            tools=[
                Tool("outlook_search", "Search or list Outlook emails (newest first).",
                     schema({"query": {"type": "string", "description": "Words, a name or an address"},
                             "unread_only": {"type": "boolean"},
                             "folder": {"type": "string", "description": "When listing: inbox (default), sentitems, archive"},
                             "max_results": {"type": "integer"}}), outlook_search),
                Tool("outlook_read", "Read the full text of one Outlook email.",
                     schema({"message_id": {"type": "string"}}, ["message_id"]), outlook_read),
                Tool("outlook_create_draft", "Save an Outlook draft (not sent). For replies, only body is used.",
                     schema(EMAIL_PROPS, ["body"]), outlook_create_draft),
                Tool("outlook_send", "Send an Outlook email or reply. Requires user approval.",
                     schema(EMAIL_PROPS, ["body"]), outlook_send,
                     needs_approval=True, describe=_describe_send),
                Tool("outlook_archive", "Move Outlook emails to Archive. Requires approval.",
                     schema({"message_ids": {"type": "array", "items": {"type": "string"}}},
                            ["message_ids"]), outlook_archive, needs_approval=True,
                     describe=lambda a: f"🗄 Archive {len(a.get('message_ids', []))} Outlook email(s)"),
                Tool("outlook_calendar_list",
                     "Events from all Outlook calendars between start and end (ISO). Default: today to "
                     "+7 days. Use a query to find events over long periods.",
                     schema({"start": {"type": "string"}, "end": {"type": "string"},
                             "query": {"type": "string", "description": "Words in title, place or notes"}}),
                     outlook_calendar_list),
                Tool("outlook_create_event", "Book an Outlook calendar event. Requires approval.",
                     schema({"title": {"type": "string"}, "start": {"type": "string"},
                             "end": {"type": "string"}, "all_day": {"type": "boolean"},
                             "location": {"type": "string"}, "description": {"type": "string"},
                             "attendees": {"type": "string", "description": "Comma-separated emails"},
                             "teams_meeting": {"type": "boolean", "description": "Add a Teams link (work accounts)"},
                             "calendar": {"type": "string", "description": "Calendar name; default calendar if omitted"}},
                            ["title", "start"]),
                     outlook_create_event, needs_approval=True,
                     describe=lambda a: (f"📅 New Outlook event: {a.get('title')}\nStart: {a.get('start')}"
                                         f"\nEnd: {a.get('end', '(1 hour)')}"
                                         + (f"\nWith: {a['attendees']}" if a.get("attendees") else "")
                                         + ("\nTeams meeting: yes" if a.get("teams_meeting") else "")
                                         + (f"\nWhere: {a['location']}" if a.get("location") else ""))),
                Tool("outlook_delete_event", "Delete an Outlook calendar event. Requires approval.",
                     schema({"event_id": {"type": "string"},
                             "title": {"type": "string", "description": "Event title, shown to the user for approval"}},
                            ["event_id"]),
                     outlook_delete_event, needs_approval=True,
                     describe=lambda a: f"🗑 Delete Outlook event: {a.get('title', a.get('event_id'))}"),
            ],
        )
