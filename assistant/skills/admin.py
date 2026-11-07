"""Life admin: expenses and recurring bills."""
from .base import Skill, Tool, schema, today

TABLES = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, amount REAL, currency TEXT,
    category TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, amount REAL, currency TEXT,
    day_of_month INTEGER, notes TEXT
);
"""


def expense_add(a, ctx):
    eid = ctx.db.execute(
        "INSERT INTO expenses(day, amount, currency, category, description) VALUES(?,?,?,?,?)",
        (a.get("date") or today(ctx).isoformat(), float(a["amount"]),
         a.get("currency") or ctx.config.currency, a.get("category", "other"), a.get("description", "")))
    return f"Expense #{eid} logged."


def expense_summary(a, ctx):
    month = a.get("month") or today(ctx).strftime("%Y-%m")
    totals = ctx.db.query(
        "SELECT category, currency, ROUND(SUM(amount), 2) AS total, COUNT(*) AS n FROM expenses "
        "WHERE day LIKE ? GROUP BY category, currency ORDER BY total DESC", (month + "%",))
    items = ctx.db.query(
        "SELECT id, day, amount, currency, category, description FROM expenses "
        "WHERE day LIKE ? ORDER BY day DESC LIMIT 50", (month + "%",))
    return {"month": month, "totals_by_category": totals, "items": items}


def expense_delete(a, ctx):
    ctx.db.execute("DELETE FROM expenses WHERE id=?", (a["expense_id"],))
    return f"Expense #{a['expense_id']} deleted."


def bill_add(a, ctx):
    bid = ctx.db.execute(
        "INSERT INTO bills(name, amount, currency, day_of_month, notes) VALUES(?,?,?,?,?)",
        (a["name"], a.get("amount"), a.get("currency") or ctx.config.currency, int(a["day_of_month"]),
         a.get("notes", "")))
    return f"Bill #{bid} saved. It will appear in the daily briefing a few days before it's due."


def bill_list(a, ctx):
    return ctx.db.query("SELECT * FROM bills ORDER BY day_of_month") or "No bills saved."


def bill_delete(a, ctx):
    ctx.db.execute("DELETE FROM bills WHERE id=?", (a["bill_id"],))
    return f"Bill #{a['bill_id']} removed."


class AdminSkill(Skill):
    def __init__(self):
        super().__init__(
            name="Life admin (expenses & bills)",
            description="Track spending and recurring bills.",
            instructions=(
                "Log expenses when the user mentions spending money, in the default currency "
                "unless they say otherwise. Recurring bills (rent, phone, subscriptions) go in "
                "bills; mention upcoming ones in the morning briefing."
            ),
            tools=[
                Tool("expense_add", "Log an expense.",
                     schema({"amount": {"type": "number"}, "currency": {"type": "string"},
                             "category": {"type": "string",
                                          "description": "food, transport, shopping, bills, fun, health, other"},
                             "description": {"type": "string"},
                             "date": {"type": "string", "description": "YYYY-MM-DD, default today"}},
                            ["amount"]), expense_add),
                Tool("expense_summary", "Spending summary for a month (YYYY-MM).",
                     schema({"month": {"type": "string"}}), expense_summary),
                Tool("expense_delete", "Delete an expense entry.",
                     schema({"expense_id": {"type": "integer"}}, ["expense_id"]), expense_delete),
                Tool("bill_add", "Save a recurring monthly bill.",
                     schema({"name": {"type": "string"}, "amount": {"type": "number"},
                             "currency": {"type": "string"}, "day_of_month": {"type": "integer"},
                             "notes": {"type": "string"}}, ["name", "day_of_month"]), bill_add),
                Tool("bill_list", "List recurring bills.", schema({}), bill_list),
                Tool("bill_delete", "Remove a recurring bill.",
                     schema({"bill_id": {"type": "integer"}}, ["bill_id"]), bill_delete),
            ],
        )

    def setup(self, db, config):
        db.executescript(TABLES)
