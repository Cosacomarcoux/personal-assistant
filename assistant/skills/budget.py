"""Money: monthly budgets per category (on top of the logged expenses) and savings goals."""
from datetime import date

from .admin import TABLES as EXPENSE_TABLES
from .base import Skill, Tool, schema, today

TABLES = """
CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY, monthly_limit REAL, currency TEXT
);
CREATE TABLE IF NOT EXISTS savings_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, target REAL, saved REAL DEFAULT 0,
    currency TEXT, deadline TEXT
);
"""


def budget_set(a, ctx):
    ctx.db.execute("INSERT INTO budgets(category, monthly_limit, currency) VALUES(?,?,?) "
                   "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit, "
                   "currency=excluded.currency",
                   (a["category"].lower(), float(a["monthly_limit"]), a.get("currency") or ctx.config.currency))
    return f"Budget for {a['category']}: {a['monthly_limit']} {a.get('currency') or ctx.config.currency}/month."


def budget_status(a, ctx):
    now = today(ctx)
    month = a.get("month") or now.strftime("%Y-%m")
    out = []
    for b in ctx.db.query("SELECT * FROM budgets ORDER BY category"):
        spent = ctx.db.query(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM expenses WHERE lower(category)=? AND "
            "currency=? AND day LIKE ?", (b["category"], b["currency"], month + "%"))[0]["s"]
        used = round(100 * spent / b["monthly_limit"]) if b["monthly_limit"] else 0
        out.append({"category": b["category"], "limit": b["monthly_limit"], "spent": round(spent, 2),
                    "left": round(b["monthly_limit"] - spent, 2), "used_percent": used,
                    "currency": b["currency"]})
    if not out:
        return "No budgets set yet."
    if month == now.strftime("%Y-%m"):
        days_in_month = (date(now.year + now.month // 12, now.month % 12 + 1, 1) - date(now.year, now.month, 1)).days
        return {"month": month, "month_passed_percent": round(100 * now.day / days_in_month), "budgets": out}
    return {"month": month, "budgets": out}


def savings_goal_add(a, ctx):
    gid = ctx.db.execute("INSERT INTO savings_goals(name, target, saved, currency, deadline) VALUES(?,?,?,?,?)",
                         (a["name"], float(a["target"]), float(a.get("saved", 0)),
                          a.get("currency") or ctx.config.currency, a.get("deadline")))
    return f"Savings goal #{gid} created."


def savings_goal_update(a, ctx):
    rows = ctx.db.query("SELECT * FROM savings_goals WHERE id=?", (a["goal_id"],))
    if not rows:
        return "Goal not found."
    saved = float(a["set_saved"]) if a.get("set_saved") is not None else rows[0]["saved"] + float(a.get("add", 0))
    ctx.db.execute("UPDATE savings_goals SET saved=? WHERE id=?", (saved, a["goal_id"]))
    return f"Goal #{a['goal_id']}: {saved} of {rows[0]['target']} {rows[0]['currency']} saved."


def savings_goals(a, ctx):
    now = today(ctx)
    out = []
    for g in ctx.db.query("SELECT * FROM savings_goals ORDER BY deadline IS NULL, deadline"):
        item = {**g, "percent": round(100 * g["saved"] / g["target"]) if g["target"] else 0}
        if g["deadline"]:
            d = date.fromisoformat(g["deadline"])
            months = max((d.year - now.year) * 12 + d.month - now.month, 1)
            item["needed_per_month"] = round(max(g["target"] - g["saved"], 0) / months, 2)
        out.append(item)
    return out or "No savings goals yet."


class BudgetSkill(Skill):
    def __init__(self):
        super().__init__(
            name="Budget & savings",
            description="Monthly budgets per category and savings goals.",
            instructions=(
                "Budgets compare logged expenses (same category names as expense_add) with "
                "monthly limits. Warn when a category passes ~80% early in the month, and suggest "
                "concrete, realistic cuts. For savings goals (trip, laptop, buffer) say how much "
                "is needed per month and whether the current budget allows it. For travel, "
                "connect the trip budget to a savings goal. Use web_search for exchange rates."
            ),
            tools=[
                Tool("budget_set", "Set a monthly budget for a spending category.",
                     schema({"category": {"type": "string"}, "monthly_limit": {"type": "number"},
                             "currency": {"type": "string"}}, ["category", "monthly_limit"]), budget_set),
                Tool("budget_status", "Spent vs budget per category for a month (YYYY-MM).",
                     schema({"month": {"type": "string"}}), budget_status),
                Tool("savings_goal_add", "Create a savings goal.",
                     schema({"name": {"type": "string"}, "target": {"type": "number"},
                             "saved": {"type": "number"}, "currency": {"type": "string"},
                             "deadline": {"type": "string", "description": "YYYY-MM-DD"}},
                            ["name", "target"]), savings_goal_add),
                Tool("savings_goal_update", "Add money to a goal (add) or set the saved total (set_saved).",
                     schema({"goal_id": {"type": "integer"}, "add": {"type": "number"},
                             "set_saved": {"type": "number"}}, ["goal_id"]), savings_goal_update),
                Tool("savings_goals", "Savings goals with progress and amount needed per month.",
                     schema({}), savings_goals),
            ],
        )

    def setup(self, db, config):
        db.executescript(EXPENSE_TABLES + TABLES)
