"""Offline tests: a fake Claude drives the agent loop, no API keys needed."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from assistant.agent import Agent
from assistant.config import Config
from assistant.db import DB
from assistant.skills import load_skills
from assistant.skills.tasks import due_reminders


def text(t):
    return NS(type="text", text=t)


def tool_use(id, name, inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        content, stop = self.script.pop(0)
        return NS(content=content, stop_reason=stop)


def make(tmp_path, script):
    cfg = Config()
    cfg.data_dir = tmp_path
    db = DB(tmp_path / "t.db")
    skills = load_skills(cfg.enabled_skills, db, cfg)
    client = FakeClient(script)
    return Agent(cfg, db, skills, client=client), db, client


def test_tool_runs_and_history_saved(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "task_add", {"title": "Renew passport"})], "tool_use"),
        ([text("Added it!")], "end_turn"),
    ])
    reply = asyncio.run(agent.handle(1, "add renew passport"))
    assert reply.text == "Added it!"
    assert db.query("SELECT title FROM tasks")[0]["title"] == "Renew passport"
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert "Task #1 added" in tool_result["content"]
    assert [m["role"] for m in db.history(1, 10)] == ["user", "tools", "assistant"]
    # tools sent to the API include web search + all client tools
    names = [t["name"] for t in client.calls[0]["tools"]]
    assert "web_search" in names and "gmail_send" in names


def test_approval_flow(tmp_path):
    email = {"to": "a@b.com", "subject": "Hi", "body": "Hello"}
    agent, db, _ = make(tmp_path, [
        ([tool_use("t1", "gmail_send", email)], "tool_use"),
        ([text("Drafted, waiting for approval.")], "end_turn"),
    ])
    reply = asyncio.run(agent.handle(1, "email a@b.com"))
    assert reply.pending_ids == [1]
    row = db.query("SELECT * FROM pending_actions")[0]
    assert row["status"] == "pending" and "a@b.com" in row["summary"]
    # Reject -> nothing executed
    assert "Rechazada" in asyncio.run(agent.resolve_action(1, approve=False))
    assert "Ya estaba rechazada" in asyncio.run(agent.resolve_action(1, approve=True))


def test_approve_runs_tool(tmp_path):
    agent, db, _ = make(tmp_path, [])
    sent = {}
    agent.tools["gmail_send"].handler = lambda a, c: sent.update(a) or "Email sent."
    pid = db.execute("INSERT INTO pending_actions(chat_id, tool, args, summary) VALUES(1,'gmail_send','{\"to\":\"x\"}','s')")
    out = asyncio.run(agent.resolve_action(pid, approve=True))
    assert out.startswith("Hecho") and sent == {"to": "x"}


def test_tool_error_is_reported_not_crash(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "gmail_search", {"query": "x"})], "tool_use"),
        ([text("Google isn't connected yet.")], "end_turn"),
    ])
    reply = asyncio.run(agent.handle(1, "emails?"))
    assert "connected" in reply.text
    assert "ERROR" in client.calls[1]["messages"][-1]["content"][0]["content"]


def test_reminders_due(tmp_path):
    agent, db, client = make(tmp_path, [])
    past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
    db.execute("INSERT INTO reminders(chat_id, text, due_at) VALUES(1,'call mom',?)", (past,))
    assert [r["text"] for r in due_reminders(db)] == ["call mom"]
    assert due_reminders(db) == []


def test_repeating_reminders(tmp_path):
    from assistant.skills.tasks import next_time
    agent, db, client = make(tmp_path, [])
    past = (datetime.now().astimezone() - timedelta(minutes=1)).replace(microsecond=0)
    db.execute("INSERT INTO reminders(chat_id, text, due_at, repeat) VALUES(1,'pay salaries',?,'monthly')",
               (past.isoformat(),))
    assert [r["text"] for r in due_reminders(db)] == ["pay salaries"]
    assert due_reminders(db) == []  # not twice
    row = db.query("SELECT due_at, sent FROM reminders")[0]
    assert row["sent"] == 0 and datetime.fromisoformat(row["due_at"]) == next_time(past, "monthly")
    jan31 = datetime(2026, 1, 31, 9, 0)
    assert next_time(jan31, "monthly") == datetime(2026, 2, 28, 9, 0)
    assert next_time(datetime(2026, 12, 5, 9), "monthly") == datetime(2027, 1, 5, 9)
    assert next_time(datetime(2028, 2, 29), "yearly") == datetime(2029, 2, 28)
    assert next_time(datetime(2026, 11, 7), "weekly") == datetime(2026, 11, 14)


def test_history_alternates(tmp_path):
    agent, db, _ = make(tmp_path, [])
    db.add_message(1, "assistant", "(approved)")
    db.add_message(1, "user", "a")
    db.add_message(1, "assistant", "b")
    db.add_message(1, "assistant", "(action done)")
    msgs = agent._history_messages(1)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_expenses(tmp_path):
    agent, db, _ = make(tmp_path, [])
    from assistant.skills.base import ToolContext
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    agent.tools["expense_add"].handler({"amount": 250, "category": "food"}, ctx)
    s = agent.tools["expense_summary"].handler({}, ctx)
    assert s["totals_by_category"][0]["total"] == 250


def test_prompt_caching(tmp_path):
    agent, db, client = make(tmp_path, [([text("Hi")], "end_turn")])
    asyncio.run(agent.handle(1, "hello"))
    call = client.calls[0]
    assert call["cache_control"] == {"type": "ephemeral"}
    stable, now = call["system"]
    assert stable["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # the time changes every minute, so it must not be inside the cached block
    assert "Current date and time" not in stable["text"] and "Current date and time" in now["text"]
    assert "cache_control" not in now


def test_system_prompt_identical_within_turn(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "task_list", {})], "tool_use"),
        ([text("Nothing to do.")], "end_turn"),
    ])
    asyncio.run(agent.handle(1, "tasks?"))
    assert client.calls[0]["system"] == client.calls[1]["system"]


class FakeCalendar:
    """Just enough of the Google Calendar client for calendar_list_events."""
    def __init__(self, calendars, events):
        self.calendars, self.events_by_cal, self.inserted = calendars, events, None

    def calendarList(self):
        return NS(list=lambda pageToken=None: NS(execute=lambda: {"items": self.calendars}))

    def events(self):
        def list_(calendarId, **kw):
            if calendarId not in self.events_by_cal:
                raise RuntimeError("forbidden")
            return NS(execute=lambda: {"items": self.events_by_cal[calendarId]})

        def insert(calendarId, body, sendUpdates):
            self.inserted, self.body = calendarId, body
            return NS(execute=lambda: {"htmlLink": "link"})
        return NS(list=list_, insert=insert)


def test_calendar_reads_all_calendars(tmp_path, monkeypatch):
    from assistant.skills import google_workspace as gw
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    cal = FakeCalendar(
        [{"id": "me@x.com", "summary": "me@x.com", "primary": True},
         {"id": "fam123", "summary": "Familj"},
         {"id": "broken", "summary": "Broken"}],
        {"me@x.com": [{"id": "a", "summary": "Gym",
                       "start": {"dateTime": "2026-10-21T18:00:00+08:00"},
                       "end": {"dateTime": "2026-10-21T19:00:00+08:00"}}],
         "fam123": [{"id": "b", "summary": "Dinner with mom",
                     "start": {"dateTime": "2026-10-21T10:00:00Z"},
                     "end": {"dateTime": "2026-10-21T11:00:00Z"}},
                    {"id": "c", "summary": "Birthday", "start": {"date": "2026-10-21"},
                     "end": {"date": "2026-10-22"}}]})
    monkeypatch.setattr(gw, "_svc", lambda ctx, name, version: cal)
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    out = gw.calendar_list_events({"start": "2026-10-21", "end": "2026-10-22"}, ctx)
    assert [e.get("title") for e in out[:3]] == ["Birthday", "Gym", "Dinner with mom"]
    assert out[2]["calendar"] == "Familj" and out[2]["calendar_id"] == "fam123"
    assert "Broken" in out[3]["note"]
    gw.calendar_create_event({"title": "Picnic", "start": "2026-10-25T12:00", "calendar": "familj"}, ctx)
    assert cal.inserted == "fam123" and "recurrence" not in cal.body and "reminders" not in cal.body
    gw.calendar_create_event({"title": "Cumple de Sofi", "start": "2026-03-14", "all_day": True,
                              "repeat": "yearly", "remind_minutes_before": 900}, ctx)
    assert cal.body["recurrence"] == ["RRULE:FREQ=YEARLY"] and cal.body["start"] == {"date": "2026-03-14"}
    assert cal.body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 900}]}
    summary = agent.tools["calendar_create_event"].summary(
        {"title": "Sueldos", "start": "2026-12-05T09:00", "repeat": "monthly"})
    assert "Se repite: todos los meses" in summary


def test_fitness(tmp_path):
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    t = agent.tools
    t["program_set"].handler({"program": "Mon: legs. Thu: upper body."}, ctx)
    assert "legs" in t["program_get"].handler({}, ctx)["program"]
    t["workout_log"].handler({"kind": "legs", "date": "2000-01-01", "exercises": [
        {"exercise": "Squat", "sets": 3, "reps": 5, "weight_kg": 100}]}, ctx)
    t["workout_log"].handler({"kind": "legs", "exercises": [
        {"exercise": "squat", "sets": 3, "reps": 5, "weight_kg": 102.5}]}, ctx)
    assert [r["weight_kg"] for r in t["exercise_progress"].handler({"exercise": "squat"}, ctx)] == [100, 102.5]
    recent = t["workout_history"].handler({"days": 7}, ctx)
    assert len(recent) == 1 and recent[0]["exercises"][0]["weight_kg"] == 102.5


def test_voice_needs_key():
    import pytest
    from assistant import voice
    cfg = Config()
    cfg.openai_api_key = ""
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(voice.transcribe(cfg, b"audio"))


def test_voice_transcribes(monkeypatch):
    import httpx
    from assistant import voice
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"text": " hej Ragnar "})

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    cfg = Config()
    cfg.openai_api_key = "sk-test"
    assert asyncio.run(voice.transcribe(cfg, b"audio")) == "hej Ragnar"
    assert seen["auth"] == "Bearer sk-test"


def test_blank_settings_use_defaults(monkeypatch):
    for name in ["MODEL", "TIMEZONE", "HISTORY_TURNS", "DATA_DIR", "ENABLED_SKILLS", "OWNER_TELEGRAM_ID"]:
        monkeypatch.setenv(name, "")
    cfg = Config()
    assert cfg.model and cfg.timezone == "America/Argentina/Buenos_Aires" and cfg.history_turns == 20
    assert "google" in cfg.enabled_skills and cfg.owner_id == 0 and str(cfg.data_dir) == "data"


def test_body_status(tmp_path):
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    agent.tools["status_log"].handler({"sleep_hours": 6, "injuries": "left knee aches"}, ctx)
    agent.tools["status_log"].handler({"weight_kg": 80.5, "energy": 8}, ctx)
    rows = agent.tools["status_history"].handler({"days": 3}, ctx)
    assert rows[0]["injuries"] == "left knee aches" and "weight_kg" not in rows[0]
    assert rows[1]["weight_kg"] == 80.5


def test_strava(tmp_path, monkeypatch):
    import httpx
    from assistant import strava
    agent, db, _ = make(tmp_path, [])
    cfg = agent.config
    cfg.strava_client_id, cfg.strava_client_secret = "123", "secret"
    assert "state=strava" in strava.start(cfg)
    assert strava.is_strava_redirect("http://localhost/?state=strava&code=abc&scope=read")
    assert not strava.is_strava_redirect("http://localhost/?code=abc&scope=https://www.googleapis.com/x")

    calls = []

    def fake_post(url, data, timeout):
        calls.append(data["grant_type"])
        return httpx.Response(200, json={"access_token": f"tok{len(calls)}", "refresh_token": "r",
                                         "expires_at": 0 if len(calls) == 1 else 9999999999})

    def fake_get(url, headers, params, timeout):
        assert headers["Authorization"] == "Bearer tok2"  # expired token was refreshed first
        return httpx.Response(200, json=[{"name": "Morning run", "sport_type": "Run",
                                          "start_date_local": "2026-10-19T07:00:00Z",
                                          "moving_time": 1800, "distance": 6000,
                                          "average_heartrate": 150}])

    monkeypatch.setattr(strava.httpx, "post", fake_post)
    monkeypatch.setattr(strava.httpx, "get", fake_get)
    strava.finish(db, cfg, "http://localhost/?state=strava&code=abc")
    run = strava.activities(db, cfg, 7)[0]
    assert calls == ["authorization_code", "refresh_token"]
    assert run["distance_km"] == 6.0 and run["pace_min_per_km"] == "5:00" and run["moving_min"] == 30
    assert db.get("strava_token")["access_token"] == "tok2"


def test_strava_not_connected(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "strava_activities", {})], "tool_use"),
        ([text("Strava isn't connected.")], "end_turn"),
    ])
    asyncio.run(agent.handle(1, "runs?"))
    assert "connect_strava" in client.calls[1]["messages"][-1]["content"][0]["content"]


def test_voice_modes():
    from assistant.voice import for_speech, wants_voice
    assert wants_voice("auto", spoken=True) and not wants_voice("auto", spoken=False)
    assert wants_voice("always", spoken=False) and not wants_voice("off", spoken=True)
    assert for_speech("**Klart!** Se https://x.se/a?b=1 nu") == "Klart! Se  nu"


def test_voice_speaks(monkeypatch):
    import json
    import httpx
    from assistant import voice
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"OggS-audio")

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    cfg = Config()
    cfg.openai_api_key = "sk-test"
    assert asyncio.run(voice.speak(cfg, "Hej *Fabian*")) == b"OggS-audio"
    assert seen["input"] == "Hej Fabian" and seen["response_format"] == "opus"


def test_voice_settings_tool(tmp_path, monkeypatch):
    import json
    import httpx
    from assistant import voice
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    out = agent.tools["voice_settings"].handler({"voice": "nova", "style": "Very happy"}, ctx)
    assert out["voice"] == "nova" and out["style"] == "Very happy" and out["replies"] == "auto"
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"ogg")

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    agent.config.openai_api_key = "sk-test"
    asyncio.run(voice.speak(agent.config, "Hej", voice=db.get("tts_voice"), style=db.get("tts_style")))
    assert seen["voice"] == "nova" and seen["instructions"].startswith("Very happy")


def test_voice_skill_always_loaded(tmp_path):
    cfg = Config()
    db = DB(tmp_path / "t.db")
    names = [s.name for s in load_skills(["tasks"], db, cfg)]
    assert names == ["Tasks & reminders", "Voice"]


def test_spoken_turn_is_fast(tmp_path):
    agent, db, client = make(tmp_path, [([text("Hej!")], "end_turn"), ([text("Hej igen")], "end_turn")])
    asyncio.run(agent.handle(1, "hej", spoken=True))
    fast = client.calls[0]
    assert fast["output_config"] == {"effort": "low"} and "read aloud" in fast["system"][1]["text"]
    asyncio.run(agent.handle(1, "hej"))
    normal = client.calls[1]
    assert "output_config" not in normal and "read aloud" not in normal["system"][1]["text"]
    assert fast["system"][0] == normal["system"][0]  # cached part unchanged


def _signed_init_data(token, user_id, auth_date=None):
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "q1",
              "user": json.dumps({"id": user_id, "first_name": "Fabian"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_web_auth():
    from assistant import web
    good = _signed_init_data("123:abc", 42)
    assert web.check_init_data(good, "123:abc") == 42
    assert web.check_init_data(good, "999:other") is None                  # wrong bot
    assert web.check_init_data(good.replace("42", "43"), "123:abc") is None  # tampered
    assert web.check_init_data(_signed_init_data("123:abc", 42, auth_date=1), "123:abc") is None  # old
    key = web.make_key("123:abc", 42)
    assert web.check_key(key, "123:abc", 42) and not web.check_key(key, "123:abc", 43)
    assert not web.check_key("1.deadbeef", "123:abc", 42)


def test_web_talk(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from assistant import voice, web

    agent, db, client = make(tmp_path, [([text("Du har gym kl 18.")], "end_turn")])
    agent.config.telegram_token, agent.config.owner_id = "123:abc", 42

    async def fake_transcribe(cfg, audio, filename):
        assert audio == b"sound" and filename == "talk.webm"
        return "Vad har jag idag?"

    async def fake_speak(cfg, text, voice=None, style=None, fmt="opus"):
        assert fmt == "mp3"
        return b"mp3-bytes"

    monkeypatch.setattr(voice, "transcribe", fake_transcribe)
    monkeypatch.setattr(voice, "speak", fake_speak)
    async def deliver(reply, text=True):
        pass
    bot = NS(config=agent.config, agent=agent, db=db, home_chat=42, link_secret=lambda: "123:abc",
             deliver=deliver)

    async def run():
        async with TestClient(TestServer(web.WebApp(bot).app)) as c:
            page = await c.get("/talk")
            assert page.status == 200 and "Hablá con tu secretario" in await page.text()
            denied = await c.post("/api/talk", data=b"sound", headers={"Content-Type": "audio/webm"})
            assert denied.status == 403
            ok = await c.post("/api/talk", data=b"sound", headers={
                "Content-Type": "audio/webm", "X-Init-Data": _signed_init_data("123:abc", 42)})
            return ok.status, await ok.json()

    status, data = asyncio.run(run())
    assert status == 200 and data["heard"] == "Vad har jag idag?"
    assert data["reply"] == "Du har gym kl 18." and data["audio"] == "bXAzLWJ5dGVz"
    assert "read aloud" in client.calls[0]["system"][1]["text"]  # short spoken answer
    assert [m["role"] for m in db.history(42, 10)] == ["user", "assistant"]


def test_extract_office_files(tmp_path):
    import io
    import zipfile
    from openpyxl import Workbook
    from assistant.extract import can_read, text_from_file

    def office(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, xml in files.items():
                z.writestr(name, xml)
        return buf.getvalue()

    docx = office({"word/document.xml": "<w:document><w:p><w:r><w:t>Hej &amp; välkommen</w:t></w:r></w:p>"
                                        "<w:p><w:t>Rad två</w:t></w:p></w:document>"})
    assert text_from_file("Brev.DOCX", docx).splitlines() == ["Hej & välkommen", "Rad två"]
    pptx = office({"ppt/slides/slide2.xml": "<a:p><a:t>Andra</a:t></a:p>",
                   "ppt/slides/slide1.xml": "<a:p><a:t>Första</a:t></a:p>"})
    assert text_from_file("deck.pptx", pptx).index("Första") < text_from_file("deck.pptx", pptx).index("Andra")
    wb = Workbook()
    wb.active.append(["Datum", "Belopp"])
    wb.active.append(["2026-10-01", 250])
    buf = io.BytesIO()
    wb.save(buf)
    assert "2026-10-01,250" in text_from_file("budget.xlsx", buf.getvalue())
    assert text_from_file("video.mp4", b"x") is None and not can_read("video.mp4") and can_read("a.PDF")


def test_flights_search(tmp_path, monkeypatch):
    import httpx
    from assistant.skills import travel
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    assert "SERPAPI_API_KEY" in str(_raises(lambda: travel.flights_search(
        {"from": "TPE", "to": "ARN", "depart_date": "2026-12-10"}, ctx)))
    agent.config.serpapi_api_key = "k"
    seen = {}

    def leg(a, b, t1, t2, no, mins):
        return {"departure_airport": {"id": a, "time": f"2026-12-10 {t1}"},
                "arrival_airport": {"id": b, "time": f"2026-12-10 {t2}"},
                "airline": no[:2], "flight_number": no, "duration": mins}

    def fake_get(url, params, timeout):
        seen.update(params)
        return httpx.Response(200, json={
            "best_flights": [{"price": 14000, "total_duration": 900, "flights": [leg("TPE", "ARN", "08:00", "18:00", "BR 67", 900)]}],
            "other_flights": [{"price": 9800, "total_duration": 1300,
                               "flights": [leg("TPE", "IST", "23:00", "06:00", "TK 25", 780),
                                           leg("IST", "ARN", "09:00", "11:30", "TK 1795", 210)],
                               "layovers": [{"id": "IST", "duration": 180}]}],
            "price_insights": {"price_level": "low", "typical_price_range": [11000, 16000]},
            "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x"}})

    monkeypatch.setattr(travel.httpx, "get", fake_get)
    out = travel.flights_search({"from": "tpe, tsa", "to": "ARN", "depart_date": "2026-12-10",
                                 "return_date": "2027-01-05", "max_stops": "max_1"}, ctx)
    assert seen["departure_id"] == "TPE,TSA" and seen["type"] == 1 and seen["stops"] == 2
    assert seen["currency"] == "ARS" and seen["sort_by"] == 2
    cheapest = out["cheapest"][0]
    assert cheapest["price"] == 9800 and cheapest["stops"] == 1 and cheapest["layovers"] == ["IST 3h00"]
    assert cheapest["legs"][0] == "TPE 23:00 → IST 06:00 (TK 25, 13h00)"
    assert out["price_level"] == "low" and out["google_flights_link"].startswith("https://")


def _raises(fn):
    try:
        fn()
    except Exception as e:
        return e
    raise AssertionError("expected an error")


def test_onedrive(tmp_path, monkeypatch):
    import io
    import time
    import zipfile
    import httpx
    from assistant import microsoft
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    cfg = agent.config
    cfg.ms_client_id = "app-id"
    ctx = ToolContext(db=db, config=cfg, chat_id=1)
    assert "connect_microsoft" in str(_raises(lambda: agent.tools["onedrive_search"].handler({"query": "x"}, ctx)))

    # device-code login: pending once, then success
    answers = [httpx.Response(400, json={"error": "authorization_pending"}),
               httpx.Response(200, json={"access_token": "A1", "refresh_token": "R1", "expires_in": 3600})]

    class FakeAsyncClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, data):
            assert data["device_code"] == "dev" and url.endswith("/token")
            return answers.pop(0)

    async def no_sleep(s): pass
    monkeypatch.setattr(microsoft.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(microsoft.asyncio, "sleep", no_sleep)
    asyncio.run(microsoft.wait_for_login(db, cfg, {"device_code": "dev", "interval": 1, "expires_in": 60}))
    assert db.get(microsoft.TOKEN_KEY)["access_token"] == "A1"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:t>Hyresavtal</w:t></w:p>")
    item = {"id": "1", "name": "Avtal.docx", "size": 100, "webUrl": "https://onedrive/x",
            "file": {}, "parentReference": {"path": "/drive/root:/Dokument"}}

    def fake_get(method, url, params=None, json=None, follow_redirects=True, timeout=60, headers=None):
        assert method == "GET" and headers["Authorization"] == "Bearer A1"
        if "search(q='hyres''avtal')" in url:
            return httpx.Response(200, json={"value": [item]})
        if url.endswith("/items/1/content"):
            return httpx.Response(200, content=buf.getvalue())
        if url.endswith("/items/1"):
            return httpx.Response(200, json=item)
        raise AssertionError(url)

    monkeypatch.setattr(microsoft.httpx, "request", fake_get)
    found = agent.tools["onedrive_search"].handler({"query": "hyres'avtal"}, ctx)
    assert found[0]["name"] == "Avtal.docx" and found[0]["folder"] == "/Dokument"
    read = agent.tools["onedrive_read"].handler({"file_id": "1"}, ctx)
    assert read["content"].strip() == "Hyresavtal"
    db.set(microsoft.TOKEN_KEY, {**db.get(microsoft.TOKEN_KEY), "expires_at": time.time() - 1})
    monkeypatch.setattr(microsoft.httpx, "post", lambda url, timeout, data: httpx.Response(
        200, json={"access_token": "A1", "refresh_token": "R2", "expires_in": 3600}))
    agent.tools["onedrive_search"].handler({"query": "hyres'avtal"}, ctx)  # refreshed first
    assert db.get(microsoft.TOKEN_KEY)["refresh_token"] == "R2"


def test_photo_attachment(tmp_path):
    agent, db, client = make(tmp_path, [([text("Det står 'Nötkött nudlar'.")], "end_turn")])
    db.add_message(1, "user", "hej")  # an unanswered earlier message gets merged in
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA="}}
    asyncio.run(agent.handle(1, "Översätt", attachments=[img]))
    sent = client.calls[0]["messages"][-1]["content"]
    assert sent[0] == img and sent[1]["text"].startswith("[") and sent[1]["text"].endswith("] hej\n\nÖversätt")
    assert db.history(1, 10)[-2]["content"] == "Översätt [attached 1 file(s)]"


def _ctx(tmp_path):
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    return agent, ToolContext(db=db, config=agent.config, chat_id=1)


def test_tool_names_unique_and_valid(tmp_path):
    import re
    from assistant.skills import AVAILABLE
    cfg = Config()
    names = [t.name for s in load_skills(list(AVAILABLE), DB(tmp_path / "t.db"), cfg) for t in s.tools]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in names)


def test_document_is_sent_as_file(tmp_path):
    import io
    from docx import Document
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "document_create", {"title": "Rapport: Bioinspirerad design",
                                             "content": "# Inledning\nText med **fet** stil.\n- punkt ett\n1. steg ett"})], "tool_use"),
        ([text("Här är rapporten.")], "end_turn"),
    ])
    reply = asyncio.run(agent.handle(1, "skriv en rapport"))
    (name, data), = reply.files
    assert name == "Rapport_Bioinspirerad_design.docx"
    doc = Document(io.BytesIO(data))
    texts = [p.text for p in doc.paragraphs]
    assert texts[:3] == ["Rapport: Bioinspirerad design", "Inledning", "Text med fet stil."]
    assert doc.paragraphs[2].runs[1].bold and "punkt ett" in texts and "steg ett" in texts


def test_flashcards_spaced_repetition(tmp_path):
    from assistant.skills.study import next_review
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["flashcards_add"].handler({"deck": "Mandarin", "cards": [{"front": "謝謝", "back": "xièxie – tack"},
                                                               {"front": "你好", "back": "nǐ hǎo – hej"}]}, ctx)
    due = t["flashcards_due"].handler({"deck": "mandarin"}, ctx)
    assert len(due) == 2
    t["flashcard_grade"].handler({"card_id": due[0]["id"], "grade": "good"}, ctx)
    assert len(t["flashcards_due"].handler({}, ctx)) == 1
    assert t["flashcard_decks"].handler({}, ctx)[0] == {"deck": "Mandarin", "cards": 2, "due_now": 1}
    assert next_review(0, 2.5, 0, "good")[0] == 1 and next_review(1, 2.5, 1, "good")[0] == 3
    assert next_review(3, 2.5, 2, "good")[0] == 8 and next_review(8, 2.5, 3, "again")[:1] == (0,)


def test_ideas_content_career(tmp_path):
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["idea_save"].handler({"title": "App som slår ihop 3 appar", "tags": "app,video"}, ctx)
    t["idea_update"].handler({"idea_id": 1, "add_details": "Börja med kalender", "status": "exploring"}, ctx)
    idea = t["idea_list"].handler({"query": "video"}, ctx)[0]
    assert idea["status"] == "exploring" and "kalender" in idea["details"]
    t["content_add"].handler({"title": "3 appar → 1", "platform": "TikTok", "publish_date": "2026-10-24"}, ctx)
    t["content_update"].handler({"content_id": 1, "status": "filmed", "script": "Hook: ..."}, ctx)
    assert t["content_list"].handler({}, ctx)[0]["status"] == "filmed"
    assert t["content_script"].handler({"content_id": 1}, ctx)["script"] == "Hook: ..."
    t["content_update"].handler({"content_id": 1, "status": "published"}, ctx)
    assert t["content_list"].handler({}, ctx) == "Nothing in the content plan."
    t["application_add"].handler({"company": "TSMC", "role": "Intern", "deadline": "2026-11-15"}, ctx)
    t["application_update"].handler({"application_id": 1, "status": "applied", "add_note": "Skickat CV"}, ctx)
    app = t["application_list"].handler({}, ctx)[0]
    assert app["status"] == "applied" and app["applied_on"] and "Skickat CV" in app["notes"]


def test_budget_and_savings(tmp_path):
    from assistant.skills.base import today
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["budget_set"].handler({"category": "Food", "monthly_limit": 6000, "currency": "TWD"}, ctx)
    t["expense_add"].handler({"amount": 4800, "category": "food", "currency": "TWD",
                              "date": today(ctx).isoformat()}, ctx)
    b = t["budget_status"].handler({}, ctx)["budgets"][0]
    assert b["spent"] == 4800 and b["used_percent"] == 80 and b["left"] == 1200
    t["savings_goal_add"].handler({"name": "Japan", "target": 30000, "deadline": "2099-01-01"}, ctx)
    t["savings_goal_update"].handler({"goal_id": 1, "add": 3000}, ctx)
    g = t["savings_goals"].handler({}, ctx)[0]
    assert g["saved"] == 3000 and g["percent"] == 10 and g["needed_per_month"] > 0


def test_nutrition_and_shopping(tmp_path):
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["meal_log"].handler({"description": "牛肉麵", "kcal": 650, "protein_g": 35}, ctx)
    t["meal_log"].handler({"description": "Protein shake", "kcal": 150, "protein_g": 25}, ctx)
    day = t["nutrition_summary"].handler({}, ctx)["per_day"][0]
    assert day["kcal"] == 800 and day["protein_g"] == 60 and day["meals"] == 2
    t["shopping_add"].handler({"items": ["ägg", "kyckling", "ris"]}, ctx)
    t["shopping_done"].handler({"item_ids": [1]}, ctx)
    assert [i["item"] for i in t["shopping_list"].handler({}, ctx)] == ["kyckling", "ris"]
    t["shopping_done"].handler({"all": True}, ctx)
    assert t["shopping_list"].handler({}, ctx) == "Shopping list is empty."


def test_habits_and_goals(tmp_path):
    from datetime import timedelta
    from assistant.skills.base import today
    from assistant.skills.habits import streak
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["habit_add"].handler({"name": "Läsa 20 min"}, ctx)
    now = today(ctx)
    for d in (1, 2):
        t["habit_done"].handler({"habits": ["läsa"], "date": (now - timedelta(days=d)).isoformat()}, ctx)
    h = t["habit_status"].handler({}, ctx)[0]
    assert h["streak_days"] == 2 and not h["done_today"]
    assert t["habit_done"].handler({"habits": ["Läsa", "meditera"]}, ctx) == {"checked": ["Läsa 20 min"], "not_found": ["meditera"]}
    assert t["habit_status"].handler({}, ctx)[0]["streak_days"] == 3
    assert streak(set(), now) == 0
    t["goal_add"].handler({"title": "120 kg knäböj", "target_date": "2027-06-01"}, ctx)
    t["goal_update"].handler({"goal_id": 1, "progress": 40, "note": "105 kg idag"}, ctx)
    g = t["goal_list"].handler({}, ctx)[0]
    assert g["progress"] == 40 and "105 kg" in g["recent_log"]


def test_people_and_birthdays(tmp_path):
    from datetime import date
    from assistant.skills.base import today
    from assistant.skills.people import days_until_birthday
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    now = today(ctx)
    soon = date(2000, now.month, now.day) if (now.month, now.day) != (2, 29) else date(2000, 3, 1)
    t["contact_save"].handler({"name": "Mamma", "relation": "mother", "birthday": soon.isoformat(),
                               "add_gift_idea": "Teservis"}, ctx)
    t["contact_save"].handler({"name": "mamma", "add_note": "Gillar trädgård"}, ctx)  # same person
    c = t["contact_find"].handler({"query": "trädgård"}, ctx)[0]
    assert c["relation"] == "mother" and "Teservis" in c["gift_ideas"]
    b = t["birthdays_upcoming"].handler({"days": 7}, ctx)[0]
    assert b["in_days"] == 0 and b["turns"] == now.year - 2000
    assert t["contacts_neglected"].handler({}, ctx)[0]["name"] == "Mamma"
    t["contact_touch"].handler({"name": "Mamma", "note": "Pratade om jul"}, ctx)
    assert t["contacts_neglected"].handler({}, ctx) == "You're in touch with everyone."
    assert days_until_birthday("02-29", date(2027, 2, 28)) == 1 and days_until_birthday("12-31", date(2026, 1, 1)) == 364


def test_journal(tmp_path):
    agent, ctx = _ctx(tmp_path)
    t = agent.tools
    t["journal_add"].handler({"entry": "Stressig dag inför tentan", "mood": 4, "tags": "plugg"}, ctx)
    t["journal_add"].handler({"entry": "Bra träning", "mood": 8}, ctx)
    assert len(t["journal_read"].handler({}, ctx)) == 2
    assert t["journal_read"].handler({"query": "tentan"}, ctx)[0]["mood"] == 4
    trend = t["mood_trend"].handler({}, ctx)
    assert trend["average"] == 6.0 and trend["lowest"] == 6.0  # one day, averaged


def _roles_alternate(messages):
    roles = [m["role"] for m in messages]
    return roles[0] == "user" and all(a != b for a, b in zip(roles, roles[1:]))


def test_next_turn_sees_what_was_checked(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "reminder_list", {})], "tool_use"),
        ([text("You have one reminder: book lung check-up.")], "end_turn"),
        ([text("Yes, that's the one I found.")], "end_turn"),
    ])
    db.execute("INSERT INTO reminders(chat_id, text, due_at) VALUES(1, 'Book lung check-up', '2099-10-23T10:00:00+08:00')")
    asyncio.run(agent.handle(1, "what reminders do I have?"))
    asyncio.run(agent.handle(1, "did you really check?"))
    msgs = client.calls[2]["messages"]
    assert _roles_alternate(msgs)
    call, result = msgs[1]["content"][0], msgs[2]["content"][0]
    assert call == {"type": "tool_use", "id": "t1", "name": "reminder_list", "input": {}}
    assert result["tool_use_id"] == "t1" and "Book lung check-up" in result["content"]
    assert msgs[3] == {"role": "assistant", "content": "You have one reminder: book lung check-up."}
    assert msgs[0]["content"].startswith("[") and msgs[0]["content"].endswith("] what reminders do I have?")
    assert "Only say you checked" in client.calls[2]["system"][0]["text"]


def test_tool_history_is_bounded(tmp_path):
    from assistant import agent as agent_mod
    agent, db, client = make(tmp_path, [])
    for n in range(agent_mod.TOOL_TURNS + 2):
        db.add_message(1, "user", f"q{n}")
        db.add_message(1, "tools", json.dumps([[{"id": f"t{n}", "name": "task_list", "input": {},
                                                 "result": "x" * 50}]]))
        db.add_message(1, "assistant", f"a{n}")
    msgs = agent._history_messages(1)
    assert _roles_alternate(msgs)
    ids = [b["id"] for m in msgs if isinstance(m["content"], list) for b in m["content"] if b["type"] == "tool_use"]
    assert ids == [f"t{n}" for n in range(2, agent_mod.TOOL_TURNS + 2)]  # only the latest turns keep tools
    db.execute("DELETE FROM messages WHERE content IN ('q0')")  # history now starts at a tools row
    assert _roles_alternate(agent._history_messages(1))


def test_long_results_are_shortened_when_saved(tmp_path):
    from assistant import agent as agent_mod
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "note_save", {"title": "x", "content": "y"})], "tool_use"),
        ([text("Saved.")], "end_turn"),
    ])
    agent.tools["note_save"].handler = lambda a, c: "z" * 5000
    asyncio.run(agent.handle(1, "save"))
    saved = json.loads(db.query("SELECT content FROM messages WHERE role='tools'")[0]["content"])
    assert len(saved[0][0]["result"]) < agent_mod.RESULT_CHARS + 20 and saved[0][0]["result"].endswith("[shortened]")


def test_briefing_is_remembered(tmp_path):
    agent, db, client = make(tmp_path, [([text("Good morning! Lung check-up at 10:00.")], "end_turn"),
                                        ([text("You're welcome!")], "end_turn")])
    db.add_message(1, "user", "old chat")
    db.add_message(1, "assistant", "old answer")
    asyncio.run(agent.handle(1, "LONG BRIEFING PROMPT", use_history=False, record_as="(Asked for my morning briefing.)"))
    assert len(client.calls[0]["messages"]) == 1  # the briefing itself doesn't need old chat
    asyncio.run(agent.handle(1, "thanks for the briefing"))
    sent = client.calls[1]["messages"]
    assert any("Lung check-up at 10:00" in str(m["content"]) for m in sent)
    assert not any("LONG BRIEFING PROMPT" in str(m["content"]) for m in sent)


class FakeGraph:
    """Answers Microsoft Graph calls like Outlook would, and records them."""
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, params=None, json=None, follow_redirects=True, timeout=60, headers=None):
        import httpx
        path = url.split("/v1.0", 1)[1]
        self.calls.append((method, path, params, json, headers))
        routes = {
            ("GET", "/me/calendars"): {"value": [{"id": "c1", "name": "Calendar"}, {"id": "c2", "name": "Team"},
                                                 {"id": "c3", "name": "Broken"}]},
            ("GET", "/me/calendars/c1/calendarView"): {"value": [
                {"id": "e1", "subject": "Board meeting", "isAllDay": False,
                 "start": {"dateTime": "2026-10-28T02:00:00.0000000", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-28T03:00:00.0000000", "timeZone": "UTC"},
                 "onlineMeeting": {"joinUrl": "https://teams/x"}}]},
            ("GET", "/me/calendars/c2/calendarView"): {"value": [
                {"id": "e2", "subject": "Offsite", "isAllDay": True,
                 "start": {"dateTime": "2026-10-28T00:00:00.0000000", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-10-29T00:00:00.0000000", "timeZone": "UTC"}}]},
            ("GET", "/me/messages"): {"value": [{"id": "m1", "subject": "Offert", "isRead": False,
                                                 "from": {"emailAddress": {"name": "Anna", "address": "anna@x.se"}},
                                                 "receivedDateTime": "2026-10-27T08:00:00Z", "bodyPreview": "Hej!"}]},
            ("GET", "/me/messages/m1"): {"id": "m1", "subject": "Offert", "body": {"content": "Hej Maria, ..."},
                                         "from": {"emailAddress": {"address": "anna@x.se"}}, "toRecipients": []},
            ("POST", "/me/calendars/c2/events"): {"id": "new", "webLink": "https://outlook/new",
                                                 "onlineMeeting": {"joinUrl": "https://teams/new"}},
        }
        if path == "/me/calendars/c3/calendarView":
            return httpx.Response(500, text="boom")
        if method in ("POST", "DELETE") and (method, path) not in routes:
            return httpx.Response(202)  # sendMail, reply, move, delete answer with no body
        return httpx.Response(200, json=routes[(method, path)])


def _outlook(tmp_path, monkeypatch):
    import time
    from assistant import microsoft
    from assistant.skills.base import ToolContext
    agent, db, client = make(tmp_path, [])
    agent.config.timezone = "Asia/Taipei"
    db.set(microsoft.TOKEN_KEY, {"access_token": "A", "refresh_token": "R", "expires_at": time.time() + 3600,
                                 "scope": microsoft.SCOPES})
    graph = FakeGraph()
    monkeypatch.setattr(microsoft.httpx, "request", graph)
    return agent, ToolContext(db=db, config=agent.config, chat_id=1), graph


def test_outlook_calendar(tmp_path, monkeypatch):
    agent, ctx, graph = _outlook(tmp_path, monkeypatch)
    t = agent.tools
    out = t["outlook_calendar_list"].handler({"start": "2026-10-28", "end": "2026-10-29"}, ctx)
    assert [e.get("title") for e in out[:2]] == ["Offsite", "Board meeting"]
    board = out[1]
    assert board["start"] == "2026-10-28T10:00:00+08:00" and board["calendar"] == "Calendar"  # UTC -> Taipei
    assert board["teams_link"] == "https://teams/x" and out[0]["start"] == "2026-10-28" and out[0]["all_day"]
    assert "Broken" in out[2]["note"]
    view = [c for c in graph.calls if c[1].endswith("c1/calendarView")][0][2]
    assert view["startDateTime"] == "2026-10-28T00:00:00+08:00"
    msg = t["outlook_create_event"].handler({"title": "Kundmöte", "start": "2026-10-29T14:00",
                                             "attendees": "anna@x.se", "teams_meeting": True,
                                             "calendar": "team"}, ctx)
    assert "https://teams/new" in msg
    body = graph.calls[-1][3]
    assert body["start"] == {"dateTime": "2026-10-29T06:00:00", "timeZone": "UTC"}  # 14:00 Taipei
    assert body["end"]["dateTime"] == "2026-10-29T07:00:00" and body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["attendees"] == [{"emailAddress": {"address": "anna@x.se"}, "type": "required"}]
    t["outlook_delete_event"].handler({"event_id": "e1"}, ctx)
    assert graph.calls[-1][:2] == ("DELETE", "/me/events/e1")


def test_outlook_mail(tmp_path, monkeypatch):
    agent, ctx, graph = _outlook(tmp_path, monkeypatch)
    t = agent.tools
    found = t["outlook_search"].handler({"query": "offert"}, ctx)
    assert found[0] == {"id": "m1", "from": "Anna <anna@x.se>", "subject": "Offert",
                        "date": "2026-10-27T08:00:00Z", "unread": True, "snippet": "Hej!"}
    assert graph.calls[-1][2]["$search"] == '"offert"'
    read = t["outlook_read"].handler({"message_id": "m1"}, ctx)
    assert read["body"].startswith("Hej Maria") and "text" in graph.calls[-1][4]["Prefer"]
    t["outlook_send"].handler({"to": "a@b.se, c@d.se", "subject": "Hej", "body": "Text"}, ctx)
    method, path, _, body, _ = graph.calls[-1]
    assert (method, path) == ("POST", "/me/sendMail") and len(body["message"]["toRecipients"]) == 2
    t["outlook_send"].handler({"reply_to_message_id": "m1", "body": "Tack!"}, ctx)
    assert graph.calls[-1][1] == "/me/messages/m1/reply" and graph.calls[-1][3] == {"comment": "Tack!"}
    t["outlook_archive"].handler({"message_ids": ["m1"]}, ctx)
    assert graph.calls[-1][1:4:2] == ("/me/messages/m1/move", {"destinationId": "archive"})
    for name in ("outlook_send", "outlook_archive", "outlook_create_event", "outlook_delete_event"):
        assert agent.tools[name].needs_approval
    assert not agent.tools["outlook_create_draft"].needs_approval


def test_outlook_send_waits_for_approval(tmp_path, monkeypatch):
    agent, db, client = make(tmp_path, [
        ([tool_use("t1", "outlook_send", {"to": "anna@x.se", "subject": "Offert", "body": "Hej"})], "tool_use"),
        ([text("Klart att godkänna.")], "end_turn"),
    ])
    sent = []
    agent.tools["outlook_send"].handler = lambda a, c: sent.append(a) or "Email sent."
    reply = asyncio.run(agent.handle(1, "mejla anna"))
    assert reply.pending_ids and not sent  # nothing goes out before ✅
    assert "Outlook" in db.query("SELECT summary FROM pending_actions")[0]["summary"]
    asyncio.run(agent.resolve_action(reply.pending_ids[0], approve=True))
    assert sent and sent[0]["to"] == "anna@x.se"


def test_microsoft_old_login_refreshes_with_old_scope(tmp_path, monkeypatch):
    import httpx
    from assistant import microsoft
    agent, db, _ = make(tmp_path, [])
    agent.config.ms_client_id = "app"
    db.set(microsoft.TOKEN_KEY, {"access_token": "old", "refresh_token": "R", "expires_at": 0})  # pre-Outlook login
    seen = {}

    def fake_post(url, timeout, data):
        seen.update(data)
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600, "scope": "Files.Read User.Read"})

    monkeypatch.setattr(microsoft.httpx, "post", fake_post)
    assert microsoft._access_token(db, agent.config) == "new"
    assert seen["scope"] == microsoft.OLD_SCOPES  # never asks for more than was granted
    saved = db.get(microsoft.TOKEN_KEY)
    assert saved["refresh_token"] == "R" and "offline_access" in saved["scope"]
    monkeypatch.setattr(microsoft.httpx, "request", lambda *a, **k: httpx.Response(403, text="denied"))
    assert "connect_microsoft" in str(_raises(lambda: microsoft.graph(db, agent.config, "/me/messages")))


# ------------------------------------------------------------------ Teams
def _rsa():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _bf_token(key, aud="app-id", iss="https://api.botframework.com", service="https://smba.example/", exp=3600):
    import time
    import jwt
    return jwt.encode({"iss": iss, "aud": aud, "exp": int(time.time()) + exp, "nbf": int(time.time()) - 10,
                       "serviceurl": service}, key, algorithm="RS256")


def test_teams_token_check():
    from assistant import teams
    key, other = _rsa(), _rsa()
    keys = NS(get_signing_key_from_jwt=lambda t: NS(key=key.public_key()))
    ok = "Bearer " + _bf_token(key)
    assert teams.check_token(ok, "app-id", "https://smba.example/", keys)
    assert not teams.check_token(ok, "other-app", "https://smba.example/", keys)           # not for us
    assert not teams.check_token(ok, "app-id", "https://evil.example/", keys)              # other service
    assert not teams.check_token("Bearer " + _bf_token(other), "app-id", "https://smba.example/", keys)  # forged
    assert not teams.check_token("Bearer " + _bf_token(key, iss="https://evil"), "app-id", "https://smba.example/", keys)
    assert not teams.check_token("Bearer " + _bf_token(key, exp=-3600), "app-id", "https://smba.example/", keys)
    assert not teams.check_token("", "app-id", "https://smba.example/", keys)


class FakeTeamsHttp:
    """Stands in for Microsoft: hands out bot tokens and records what the bot posts."""
    posts: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, data=None, json=None, headers=None):
        import httpx
        if "login.microsoftonline.com" in url:
            assert data["scope"] == "https://api.botframework.com/.default" and data["client_secret"] == "secret"
            return httpx.Response(200, json={"access_token": "BOT", "expires_in": 3600})
        assert headers["Authorization"] == "Bearer BOT"
        FakeTeamsHttp.posts.append((url, json))
        return httpx.Response(201, json={"id": "1"})


def _teams_bot(tmp_path, monkeypatch, script, owner="aad-owner"):
    from assistant import main, teams, web
    cfg = Config()
    cfg.data_dir, cfg.telegram_token, cfg.owner_id = tmp_path, "", 0
    cfg.teams_app_id, cfg.teams_app_password, cfg.teams_tenant_id = "app-id", "secret", "tenant-1"
    cfg.teams_owner_id, cfg.web_url = owner, "https://ragnar.example"
    bot = main.Bot(cfg)
    bot.agent.client = FakeClient(script)
    bot.teams, bot.web = teams.Teams(bot), web.WebApp(bot)
    FakeTeamsHttp.posts = []
    monkeypatch.setattr(teams.httpx, "AsyncClient", FakeTeamsHttp)
    return bot


def _activity(text="hej", user="aad-owner", tenant="tenant-1", value=None):
    a = {"type": "message", "text": text, "serviceUrl": "https://smba.example/",
         "conversation": {"id": "conv-1"}, "from": {"id": "29:x", "aadObjectId": user},
         "channelData": {"tenant": {"id": tenant}}}
    if value:
        a["value"] = value
    return a


def _texts():
    return [j.get("text") for _, j in FakeTeamsHttp.posts if j.get("type") == "message" and "text" in j]


def test_teams_only_the_owner(tmp_path, monkeypatch):
    bot = _teams_bot(tmp_path, monkeypatch, [], owner="")
    asyncio.run(bot.teams.process(_activity(user="aad-123")))
    assert "aad-123" in _texts()[0] and "TEAMS_OWNER_ID" in _texts()[0]
    bot = _teams_bot(tmp_path / "b", monkeypatch, [])
    asyncio.run(bot.teams.process(_activity(user="someone-else")))
    assert _texts() == ["Sorry, I'm a private assistant."]
    FakeTeamsHttp.posts = []
    asyncio.run(bot.teams.process(_activity(tenant="other-org")))
    assert FakeTeamsHttp.posts == []  # another organisation gets nothing at all


def test_teams_chat_and_approval(tmp_path, monkeypatch):
    email = {"to": "anna@x.se", "subject": "Offert", "body": "Hej"}
    bot = _teams_bot(tmp_path, monkeypatch, [
        ([text("Hej Fabian!")], "end_turn"),
        ([tool_use("t1", "gmail_send", email)], "tool_use"),
        ([text("Väntar på ditt godkännande.")], "end_turn"),
    ])
    asyncio.run(bot.teams.process(_activity("<p>hej</p>")))
    url, sent = FakeTeamsHttp.posts[-1]
    assert url == "https://smba.example/v3/conversations/conv-1/activities" and sent["text"] == "Hej Fabian!"
    assert bot.db.get("teams_ref") == {"serviceUrl": "https://smba.example/", "conversation": "conv-1"}
    assert bot.agent.client.calls[0]["messages"][-1]["content"] == "hej"  # HTML stripped
    asyncio.run(bot.teams.process(_activity("mejla anna")))
    card = FakeTeamsHttp.posts[-1][1]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    approve = card["content"]["actions"][0]["data"]
    assert approve["ragnar"] == "ok" and "anna@x.se" in card["content"]["body"][1]["text"]
    sent_mail = []
    bot.agent.tools["gmail_send"].handler = lambda a, c: sent_mail.append(a) or "Email sent."
    asyncio.run(bot.teams.process(_activity("", value=approve)))  # the ✅ button
    assert sent_mail and _texts()[-1].startswith("Hecho")


def test_teams_commands_and_files(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    bot = _teams_bot(tmp_path, monkeypatch, [
        ([text("Morgon! Inget i kalendern.")], "end_turn"),
        ([tool_use("t1", "document_create", {"title": "Rapport", "content": "Text"})], "tool_use"),
        ([text("Här är rapporten.")], "end_turn"),
    ])
    asyncio.run(bot.teams.process(_activity("skills")))
    assert _texts()[-1].startswith("Funciones activadas")
    asyncio.run(bot.teams.process(_activity("/briefing")))
    assert "morning briefing" in bot.agent.client.calls[0]["messages"][-1]["content"]
    assert bot.db.history(1, 10)[0]["content"] == "(Asked for my morning briefing.)"
    asyncio.run(bot.teams.process(_activity("skriv en rapport")))
    link = _texts()[-1]
    assert "Rapport.docx" in link and "https://ragnar.example/files/" in link
    path = link[link.index("/files/"):link.index(")")].replace("https://ragnar.example", "")

    async def fetch():
        async with TestClient(TestServer(bot.web.app)) as c:
            got = await c.get(path)
            denied = await c.post("/api/messages", json=_activity())  # no Microsoft signature
            zipped = await c.get("/teams/app.zip")
            return got.status, await got.read(), denied.status, zipped.status, await zipped.read()
    status, body, denied, zstatus, zipdata = asyncio.run(fetch())
    assert status == 200 and body[:2] == b"PK"  # a real .docx
    assert denied == 401 and zstatus == 200 and zipdata[:2] == b"PK"


def test_teams_app_package():
    import io
    import zipfile
    from assistant import teams
    cfg = Config()
    cfg.teams_app_id, cfg.web_url, cfg.assistant_name = "app-id", "https://ragnar.example", "Ragnar"
    z = zipfile.ZipFile(io.BytesIO(teams.app_package(cfg)))
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["id"] == manifest["bots"][0]["botId"] == "app-id"
    assert manifest["validDomains"] == ["ragnar.example"] and manifest["bots"][0]["scopes"] == ["personal"]
    for name, size in (("color.png", 192), ("outline.png", 32)):
        png = z.read(name)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == size


def test_notify_targets(tmp_path, monkeypatch):
    bot = _teams_bot(tmp_path, monkeypatch, [])
    assert bot.targets() == []  # nobody has written from Teams yet
    bot.db.set("teams_ref", {"serviceUrl": "https://smba.example/", "conversation": "conv-1"})
    assert bot.targets() == ["teams"]
    bot.app, bot.config.owner_id = NS(bot=None), 5
    assert bot.targets() == ["telegram"]  # auto prefers Telegram when both exist
    bot.config.notify_channel = "both"
    assert bot.targets() == ["telegram", "teams"]
    bot.config.notify_channel = "teams"
    assert bot.targets() == ["teams"]
    assert bot._hhmm("7:30") == "07:30" and bot._hhmm("off") is None


def test_teams_reminder_is_delivered(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    bot = _teams_bot(tmp_path, monkeypatch, [])
    bot.db.set("teams_ref", {"serviceUrl": "https://smba.example/", "conversation": "conv-1"})
    past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat()
    bot.db.execute("INSERT INTO reminders(chat_id, text, due_at) VALUES(1,'ring mamma',?)", (past,))
    asyncio.run(bot.job_reminders())
    assert _texts() == ["⏰ Recordatorio: ring mamma"]


class PagedCalendar:
    """A Google calendar with many events, served 250 at a time like the real API."""
    def __init__(self, n_events, match_every=10):
        self.n, self.every, self.calls = n_events, match_every, []

    def calendarList(self):
        return NS(list=lambda pageToken=None: NS(execute=lambda: {"items": [{"id": "me", "summary": "Mitt"}]}))

    def events(self):
        def list_(calendarId, pageToken=None, maxResults=250, q=None, **kw):
            self.calls.append({"page": pageToken, "q": q})
            items = [{"id": f"e{i}", "summary": f"Snöläge möte {i}" if i % self.every == 0 else f"Annat {i}",
                      "start": {"dateTime": f"2027-01-01T{8 + i % 10:02d}:00:00+08:00"},
                      "end": {"dateTime": f"2027-01-01T{9 + i % 10:02d}:00:00+08:00"}}
                     for i in range(self.n) if not q or i % self.every == 0]
            start = int(pageToken or 0)
            page = items[start:start + maxResults]
            more = start + maxResults < len(items)
            return NS(execute=lambda: {"items": page, **({"nextPageToken": str(start + maxResults)} if more else {})})
        return NS(list=list_)


def test_calendar_search_covers_long_periods(tmp_path, monkeypatch):
    from assistant.skills import google_workspace as gw
    from assistant.skills.base import ToolContext
    agent, db, _ = make(tmp_path, [])
    ctx = ToolContext(db=db, config=agent.config, chat_id=1)
    cal = PagedCalendar(600)
    monkeypatch.setattr(gw, "_svc", lambda ctx, name, version: cal)
    found = gw.calendar_list_events({"start": "2026-10-01", "end": "2027-12-31", "query": "Snöläge"}, ctx)
    assert len(found) == 60 and all("Snöläge" in e["title"] for e in found)  # every match, no cut
    assert cal.calls[0]["q"] == "Snöläge"
    everything = gw.calendar_list_events({"start": "2026-10-01", "end": "2027-12-31"}, ctx)
    assert len(everything) == gw.MAX_EVENTS + 1 and "there are more" in everything[-1]["note"]  # says so


def test_approve_all(tmp_path):
    agent, db, client = make(tmp_path, [
        ([tool_use(f"t{i}", "calendar_delete_event", {"event_id": f"e{i}", "title": f"Snöläge {i}"})
          for i in range(3)], "tool_use"),
        ([text("Tre borttagningar väntar på ditt godkännande.")], "end_turn"),
    ])
    deleted = []
    agent.tools["calendar_delete_event"].handler = lambda a, c: deleted.append(a["event_id"]) or "Event deleted."
    reply = asyncio.run(agent.handle(1, "ta bort snöläge"))
    assert len(reply.pending_ids) == 3 and deleted == []  # one step, three requests, nothing done yet
    from assistant import main
    bot = NS(db=db, agent=agent)
    token = main.Bot.new_batch(bot, reply.pending_ids)
    asyncio.run(agent.resolve_action(reply.pending_ids[0], approve=True))  # one tapped by hand first
    summary = asyncio.run(main.Bot.resolve_batch(bot, token, approve=True))
    assert sorted(deleted) == ["e0", "e1", "e2"]  # each runs exactly once
    assert summary.startswith("Hecho ✅ 2 de 3.") and "1 ya estaban resueltas" in summary
    assert "Rechacé 0 de 3" in asyncio.run(main.Bot.resolve_batch(bot, token, approve=False))


def test_teams_approve_all_card(tmp_path, monkeypatch):
    bot = _teams_bot(tmp_path, monkeypatch, [
        ([tool_use(f"t{i}", "calendar_delete_event", {"event_id": f"e{i}"}) for i in range(2)], "tool_use"),
        ([text("Två väntar.")], "end_turn"),
    ])
    deleted = []
    bot.agent.tools["calendar_delete_event"].handler = lambda a, c: deleted.append(a["event_id"]) or "Event deleted."
    asyncio.run(bot.teams.process(_activity("ta bort båda")))
    batch = FakeTeamsHttp.posts[-1][1]["attachments"][0]["content"]
    approve_all = batch["actions"][0]
    assert approve_all["title"] == "✅ Approve all 2" and deleted == []
    asyncio.run(bot.teams.process(_activity("", value=approve_all["data"])))
    assert sorted(deleted) == ["e0", "e1"] and _texts()[-1].startswith("Hecho ✅ 2 de 2.")


def test_honesty_rules_in_prompt(tmp_path):
    agent, db, client = make(tmp_path, [])
    rules = agent.system_prompt()[0]["text"]
    assert "Never say an action is queued" in rules and "cannot approve actions yourself" in rules


def test_telegram_shows_approve_all(tmp_path):
    from assistant import main
    cfg = Config()
    cfg.data_dir = tmp_path
    bot = main.Bot(cfg)
    ids = [bot.db.execute("INSERT INTO pending_actions(chat_id, tool, args, summary) VALUES(1,'calendar_delete_event','{}','x')")
           for _ in range(25)]
    sent = []

    async def send_message(chat_id, text, reply_markup=None):
        sent.append((text, reply_markup))
    asyncio.run(bot.send_approvals(1, ids, NS(send_message=send_message)))
    assert len(sent) == 26  # one per action, then one for all
    buttons = sent[-1][1].inline_keyboard[0]
    assert buttons[0].text == "✅ Aprobar las 25" and buttons[0].callback_data.startswith("okall:")
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert bot.db.get("batch:" + buttons[0].callback_data.split(":")[1]) == ids


def test_briefing_only_asks_about_enabled_skills():
    from assistant.main import briefing_prompt
    text = briefing_prompt(["google", "tasks", "people", "content"])
    assert "calendar" in text and "birthdays" in text and "content to film" in text
    assert "flashcards" not in text and "workout" not in text and "job application" not in text
