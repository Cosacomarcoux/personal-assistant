"""The 'brain': talks to Claude, runs tools, and asks for approval when needed."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .skills.base import Skill, Tool, ToolContext

log = logging.getLogger(__name__)

MAX_STEPS = 15  # safety limit on tool calls per message
TOOL_TURNS = 5  # how many recent turns keep their tool calls + results in the history
RESULT_CHARS = 1200  # stored length of each tool result
STATUS_ES = {"done": "hecha", "rejected": "rechazada", "failed": "fallida"}


@dataclass
class AgentReply:
    text: str
    pending_ids: list[int] = field(default_factory=list)
    files: list = field(default_factory=list)  # (filename, bytes) to send with the reply


class Agent:
    def __init__(self, config, db, skills: list[Skill], client=None):
        self.config = config
        self.db = db
        self.skills = skills
        self.client = client or AsyncAnthropic(api_key=config.anthropic_api_key)
        self.tools: dict[str, Tool] = {t.name: t for s in skills for t in s.tools}
        self.server_tools = [st for s in skills for st in s.server_tools()]

    # ------------------------------------------------------------------
    def system_prompt(self, spoken: bool = False) -> list[dict]:
        """Stable instructions (cached together with the tools) + the current time.

        The time changes every minute, so it sits after the cache breakpoint:
        anything before the breakpoint must stay byte-identical between requests.
        """
        now = datetime.now(ZoneInfo(self.config.timezone))
        skill_text = "\n\n".join(
            f"## {s.name}\n{s.instructions}".strip() for s in self.skills if s.instructions
        )
        stable = f"""You are {self.config.assistant_name}, the personal assistant of {self.config.owner_name}.
You chat with them on Telegram, so keep answers short, friendly and easy to read on a phone.
Avoid markdown tables and headings; simple lists are fine.
Your replies can also be read aloud as Telegram voice messages (by default voice messages
get a spoken reply; see the Voice section for how to change the sound).

Language: always reply in {self.config.language}, also in briefings, reminders and
reviews, even when these instructions or tool results are in English.
Default currency: {self.config.currency} (use it for money unless the user names another).

Always interpret times in the user's timezone ({self.config.timezone}) unless told otherwise.
The current date and time is given at the end of these instructions.

Rules:
- Use your tools to get real information. Never invent emails, events or facts.
- Only say you checked, found or confirmed something if a tool result in this conversation
  shows it. If you didn't check, say so, or check now. If you're unsure, check again
  rather than guess.
- Never say an action is queued, sent, booked or deleted unless a tool result in THIS reply
  says so. Never end a reply promising to do something "now": either call the tools in the
  same reply or say plainly what you have not done yet.
- Many similar actions (e.g. deleting 20 events): request them all at once, several tool
  calls in one reply. The user then gets an "Approve all" button.
- You cannot approve actions yourself. If the user approves in text ("ok, do it all"), tell
  them to press the ✅ buttons or "Approve all" under the requests.
- Earlier user messages start with [when they were sent], added by the system; use it to
  tell what "today" or "tomorrow" meant back then. Older turns keep only the text.
- Some actions (sending email, creating/deleting events, etc.) need the user's approval.
  When you call such a tool it is NOT executed yet: the user gets Approve/Reject buttons.
  Tell them briefly what you prepared and that it is waiting for their approval.
- If a request is ambiguous and the wrong guess would matter, ask one short question.
- If a tool fails because an account isn't connected, tell the user how to connect it.

{skill_text}

{self.config.extra_instructions}""".strip()
        volatile = f"Current date and time: {now:%A %d %B %Y, %H:%M} ({self.config.timezone})."
        if spoken:
            volatile += ("\nThe user sent a voice message and your reply will be read aloud. "
                         "Answer in 1-3 short, natural sentences. No lists, links or emojis. "
                         "Offer more detail only if they ask.")
        return [
            # 1 hour: chats come in bursts spread over the day, so the ~10k-token tools +
            # instructions survive the pauses (5-minute entries would be rewritten each time).
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": volatile},
        ]

    def _all_tools(self) -> list[dict]:
        return [t.to_api() for t in self.tools.values()] + self.server_tools

    def _stamp(self, created_at: str | None) -> str:
        if not created_at:
            return ""
        dt = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
        return f"[{dt.astimezone(ZoneInfo(self.config.timezone)):%a %d %b %H:%M}] "

    def _history_messages(self, chat_id: int) -> list[dict]:
        """Past turns, so roles alternate (required by the API).

        User messages carry when they were sent. The last few turns also replay the
        tools that were called and (shortened) what they returned, as real tool
        blocks: the assistant can then see what it actually checked, instead of
        only its own earlier claims.
        """
        rows = self.db.history(chat_id, self.config.history_turns * 3)
        while rows and rows[0]["role"] != "user":  # never start mid-turn
            rows.pop(0)
        tool_rows = [i for i, r in enumerate(rows) if r["role"] == "tools"]
        detailed = set(tool_rows[-TOOL_TURNS:])
        msgs: list[dict] = []

        def add_text(role: str, text: str) -> None:
            if msgs and msgs[-1]["role"] == role and isinstance(msgs[-1]["content"], str):
                msgs[-1]["content"] += "\n\n" + text
            else:
                msgs.append({"role": role, "content": text})

        for i, row in enumerate(rows):
            if row["role"] == "user":
                add_text("user", self._stamp(row.get("created_at")) + row["content"])
            elif row["role"] == "assistant":
                add_text("assistant", row["content"])
            elif i in detailed:
                for step in json.loads(row["content"]):
                    msgs.append({"role": "assistant", "content": [
                        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                        for c in step]})
                    msgs.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": c["id"], "content": c["result"]}
                        for c in step]})
        return msgs

    # ------------------------------------------------------------------
    async def run_tool(self, tool: Tool, args: dict, chat_id: int, files: list | None = None) -> str:
        ctx = ToolContext(db=self.db, config=self.config, chat_id=chat_id,
                          files=files if files is not None else [])
        try:
            result = await asyncio.to_thread(tool.handler, args, ctx)
        except Exception as e:  # the model sees the error and can recover
            log.exception("Tool %s failed", tool.name)
            return f"ERROR: {type(e).__name__}: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    async def handle(self, chat_id: int, user_text: str, remember: bool = True,
                     spoken: bool = False, attachments: list[dict] | None = None,
                     use_history: bool | None = None, record_as: str | None = None) -> AgentReply:
        """attachments: Claude content blocks (photos, PDFs) sent along with this message.
        remember: save this turn; use_history: send earlier turns (default: same as remember);
        record_as: what to save as the user's message (e.g. a short label for a long prompt)."""
        use_history = remember if use_history is None else use_history
        messages = self._history_messages(chat_id) if use_history else []
        prompt = user_text
        if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], str):
            prompt = messages.pop()["content"] + "\n\n" + user_text  # merge, roles must alternate
        content = [*attachments, {"type": "text", "text": prompt}] if attachments else prompt
        messages.append({"role": "user", "content": content})

        pending: list[int] = []
        files: list = []
        steps: list[list[dict]] = []  # tool calls per step, saved with the turn
        final_text = ""
        system = self.system_prompt(spoken)  # build once: must stay identical within a turn
        # Voice: think less and answer shorter, so the spoken reply arrives fast.
        speed = {"output_config": {"effort": self.config.voice_effort}} if spoken else {}
        model = (self.config.voice_model or self.config.model) if spoken else self.config.model
        for _ in range(MAX_STEPS):
            resp = await self.client.messages.create(
                model=model,
                **speed,
                max_tokens=4096,
                # Caches the growing conversation, so each step of the tool loop
                # re-reads the earlier steps at ~10% of the normal input price.
                cache_control={"type": "ephemeral"},
                system=system,
                tools=self._all_tools(),
                messages=messages,
            )
            if usage := getattr(resp, "usage", None):  # check cache hits in the Railway logs
                log.info("tokens: input=%s cache_read=%s cache_write=%s output=%s",
                         usage.input_tokens, usage.cache_read_input_tokens,
                         usage.cache_creation_input_tokens, usage.output_tokens)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                final_text = text

            if resp.stop_reason == "pause_turn":  # long web search: just continue
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": resp.content})
            results, calls = [], []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                tool = self.tools.get(block.name)
                if tool is None:
                    out = f"ERROR: unknown tool {block.name}"
                elif tool.needs_approval:
                    pid = self.db.execute(
                        "INSERT INTO pending_actions(chat_id, tool, args, summary) VALUES(?,?,?,?)",
                        (chat_id, tool.name, json.dumps(block.input), tool.summary(block.input)),
                    )
                    pending.append(pid)
                    out = (f"Not executed yet: queued as action #{pid}. The user now sees "
                           "Approve/Reject buttons. Do not say it is done.")
                else:
                    out = await self.run_tool(tool, block.input, chat_id, files)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                short = out if len(out) <= RESULT_CHARS else out[:RESULT_CHARS] + " …[shortened]"
                calls.append({"id": block.id, "name": block.name, "input": block.input, "result": short})
            messages.append({"role": "user", "content": results})
            steps.append(calls)
        else:
            final_text = final_text or "Perdón, eso llevó demasiados pasos. ¿Lo podés dividir en partes?"

        final_text = final_text or "Listo."
        if remember:
            note = f" [attached {len(attachments)} file(s)]" if attachments else ""
            self.db.add_message(chat_id, "user", (record_as or user_text) + note)
            if steps:  # what was actually checked, so later turns can rely on it
                self.db.add_message(chat_id, "tools", json.dumps(steps, ensure_ascii=False, default=str))
            self.db.add_message(chat_id, "assistant", final_text)
        return AgentReply(final_text, pending, files)

    # ------------------------------------------------------------------
    async def resolve_action(self, action_id: int, approve: bool) -> str:
        rows = self.db.query("SELECT * FROM pending_actions WHERE id=?", (action_id,))
        if not rows:
            return "Esa acción ya no existe."
        row = rows[0]
        if row["status"] != "pending":
            return f"Ya estaba {STATUS_ES.get(row['status'], row['status'])}."
        if not approve:
            self.db.execute("UPDATE pending_actions SET status='rejected' WHERE id=?", (action_id,))
            self.db.add_message(row["chat_id"], "assistant",
                                f"(Action #{action_id} '{row['tool']}' was rejected by the user.)")
            return "Rechazada. No se hizo nada."
        tool = self.tools.get(row["tool"])
        if tool is None:
            return "Esa herramienta ya no está disponible."
        result = await self.run_tool(tool, json.loads(row["args"]), row["chat_id"])
        status = "failed" if result.startswith("ERROR") else "done"
        self.db.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, action_id))
        self.db.add_message(row["chat_id"], "assistant",
                            f"(Action #{action_id} '{row['tool']}' approved; result: {result[:500]})")
        return ("Hecho ✅" if status == "done" else "Falló ❌") + f"\n{result[:1000]}"
