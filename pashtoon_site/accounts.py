"""Double-entry books, built like Xero: a chart of accounts with header (parent) accounts, a journal for every
business event, a trial balance, the income statement, balance sheet and cash flow, and a Self Assessment / VAT
tax summary.

Most journals are worked out from what she already records (orders, payments, stock and expenses), so the books are
always up to date and never drift from the dashboard. Things the site can't see (money she puts in, drawings,
equipment, partner payouts, Stripe fees, transfers) are entered as manual journals.
"""
from collections import defaultdict
from datetime import date, timedelta

from .db import execute, q, q1, tx
from .shop import ShopError, iso_now, parse_date, pretty_date, today

# code, name, type, parent. Types: asset, liability, equity, income, cogs (cost of sales), expense. Headers have no
# parent and only add up their children.
CHART = [
    ("1000", "Assets", "asset", None),
    ("1100", "Current assets", "asset", "1000"),
    ("1110", "Cash in hand", "asset", "1100"),
    ("1120", "Bank account", "asset", "1100"),
    ("1130", "Stripe clearing (card payments)", "asset", "1100"),
    ("1140", "Delivery partners owe", "asset", "1100"),
    ("1150", "Customers owe (unpaid orders)", "asset", "1100"),
    ("1160", "Food stock", "asset", "1100"),
    ("1200", "Fixed assets", "asset", "1000"),
    ("1210", "Kitchen equipment", "asset", "1200"),
    ("1220", "Equipment depreciation", "asset", "1200"),
    ("2000", "Liabilities", "liability", None),
    ("2100", "Current liabilities", "liability", "2000"),
    ("2110", "Suppliers owed", "liability", "2100"),
    ("2120", "Customer deposits and refunds owed", "liability", "2100"),
    ("2200", "Loans", "liability", "2000"),
    ("3000", "Equity", "equity", None),
    ("3100", "Owner's capital", "equity", "3000"),
    ("3200", "Owner's drawings", "equity", "3000"),
    ("3300", "Retained profit", "equity", "3000"),
    ("4000", "Income", "income", None),
    ("4100", "Food sales", "income", "4000"),
    ("4200", "Delivery charges", "income", "4000"),
    ("4300", "Delivery partner sales", "income", "4000"),
    ("4900", "Other income", "income", "4000"),
    ("5000", "Cost of sales", "cogs", None),
    ("5100", "Ingredients used", "cogs", "5000"),
    ("5200", "Food waste and write-offs", "cogs", "5000"),
    ("5250", "Stock count adjustments", "cogs", "5000"),
    ("5300", "Spices, herbs and consumables", "cogs", "5000"),
    ("5400", "Packaging", "cogs", "5000"),
    ("6000", "Overheads", "expense", None),
    ("6100", "Gas and electric", "expense", "6000"),
    ("6200", "Delivery costs", "expense", "6000"),
    ("6300", "Small equipment", "expense", "6000"),
    ("6400", "Marketing", "expense", "6000"),
    ("6500", "Card and platform fees", "expense", "6000"),
    ("6600", "Delivery partner commission", "expense", "6000"),
    ("6700", "Phone and internet", "expense", "6000"),
    ("6800", "Depreciation", "expense", "6000"),
    ("6900", "Other expenses", "expense", "6000"),
]
ACCOUNTS = {c: {"code": c, "name": n, "type": t, "parent": p} for c, n, t, p in CHART}
POSTING = [c for c, _, _, p in CHART if p and not any(x[3] == c for x in CHART)]
CASH = ("1110", "1120", "1130")
PAY_ACCOUNT = {"cash": "1110", "bank": "1120", "card": "1130"}
DEBIT_NORMAL = ("asset", "cogs", "expense")
TYPE_NAMES = {"asset": "Assets", "liability": "Liabilities", "equity": "Equity", "income": "Income",
              "cogs": "Cost of sales", "expense": "Overheads"}

# Manual journal shortcuts for things the site can't see: (label, debit account, credit account).
QUICK = {
    "capital": ("Money put into the business", "1120", "3100"),
    "drawings": ("Money taken out for yourself", "3200", "1120"),
    "equipment": ("Bought kitchen equipment", "1210", "1120"),
    "partner_payout": ("Payout from a delivery partner", "1120", "1140"),
    "stripe_payout": ("Stripe paid out to the bank", "1120", "1130"),
    "stripe_fees": ("Stripe fees", "6500", "1130"),
    "cash_to_bank": ("Paid cash into the bank", "1120", "1110"),
    "pay_supplier": ("Paid a supplier you owed", "2110", "1120"),
    "depreciation": ("Equipment depreciation", "6800", "1220"),
    "other_income": ("Other income", "1120", "4900"),
}


def name(code: str) -> str:
    return ACCOUNTS[code]["name"] if code in ACCOUNTS else code


def posting_accounts() -> list[dict]:
    return [ACCOUNTS[c] for c in POSTING]


# ---------------------------------------------------------------- journals

def _j(d, ref, memo, lines, kind):
    """lines: [(account, debit, credit)]; drops zero lines."""
    lines = [(a, round(dr, 2), round(cr, 2)) for a, dr, cr in lines if round(dr, 2) or round(cr, 2)]
    return {"date": d, "ref": ref, "memo": memo, "kind": kind, "lines": lines} if lines else None


def journals(start: str | None = None, end: str | None = None) -> list[dict]:
    """Every journal in date order (both dates inclusive)."""
    out = []
    for o in q("SELECT * FROM orders WHERE status!='cancelled'"):
        ref = f"Order #{o['id']}"
        if o["partner"]:
            out.append(_j(o["date"], ref, f"{o['customer_name']} via {o['partner']}",
                          [("1140", o["total"], 0), ("4300", 0, o["total"])], "sale"))
            out.append(_j(o["date"], ref, "Partner commission", [("6600", o["partner_fee"], 0), ("1140", 0, o["partner_fee"])],
                          "fee"))
            continue
        out.append(_j(o["date"], ref, f"Sale to {o['customer_name']}",
                      [("1150", o["total"], 0), ("4100", 0, o["food_total"]), ("4200", 0, o["delivery_fee"])], "sale"))
        if o["payment_status"] == "paid":
            acct = PAY_ACCOUNT.get(o["payment_method"], "1110")
            out.append(_j(o["paid_at"] or o["date"], ref, f"Payment from {o['customer_name']} ({o['payment_method']})",
                          [(acct, o["total"], 0), ("1150", 0, o["total"])], "receipt"))
    # Paid then cancelled: the money is owed back until a refund journal is entered.
    for o in q("SELECT * FROM orders WHERE status='cancelled' AND payment_status='paid' AND partner IS NULL"):
        acct = PAY_ACCOUNT.get(o["payment_method"], "1110")
        out.append(_j(o["paid_at"] or o["date"], f"Order #{o['id']}", f"Paid, then cancelled: refund due to {o['customer_name']}",
                      [(acct, o["total"], 0), ("2120", 0, o["total"])], "receipt"))
    for m in q("SELECT m.*, i.name FROM stock_moves m JOIN ingredients i ON i.id=m.ingredient_id"):
        cost = abs(m["cost"] or 0)
        if m["kind"] == "purchase":
            src = m["reason"] if m["reason"] in ("1110", "1120", "2110") else "1110"
            out.append(_j(m["date"], f"Stock {m['id']}", f"Bought {m['name']}", [("1160", cost, 0), (src, 0, cost)], "purchase"))
        elif m["kind"] == "use":
            out.append(_j(m["date"], m["reason"] or "Cooking", f"Used {m['name']}", [("5100", cost, 0), ("1160", 0, cost)], "stock"))
        elif m["kind"] == "writeoff":
            out.append(_j(m["date"], f"Stock {m['id']}", f"Wrote off {m['name']}: {m['reason']}",
                          [("5200", cost, 0), ("1160", 0, cost)], "stock"))
        elif m["kind"] == "count":
            gain = (m["cost"] or 0) > 0
            out.append(_j(m["date"], f"Stock {m['id']}", f"Stock count, {m['name']}",
                          [("1160", cost, 0), ("5250", 0, cost)] if gain else [("5250", cost, 0), ("1160", 0, cost)], "stock"))
    for e in q("SELECT * FROM expenses"):
        out.append(_j(e["date"], f"Expense {e['id']}", e["description"],
                      [(e["account"] or "6900", e["amount"], 0), (e["paid_from"] or "1110", 0, e["amount"])], "expense"))
    lines = defaultdict(list)
    for ln in q("SELECT * FROM journal_lines ORDER BY id"):
        lines[ln["journal_id"]].append((ln["account"], ln["debit"], ln["credit"]))
    for j in q("SELECT * FROM journals"):
        out.append(_j(j["date"], f"Journal {j['id']}", j["memo"], lines[j["id"]], "manual") | {"id": j["id"]})
    out = [j for j in out if j and (not start or j["date"] >= start) and (not end or j["date"] <= end)]
    out.sort(key=lambda j: (j["date"], j["ref"]))
    return out


def add_journal(d: str, memo: str, lines: list[tuple[str, float, float]], kind: str = "manual") -> int:
    parse_date(d)
    memo = (memo or "").strip()[:200]
    if not memo:
        raise ShopError("Say what the journal is for.")
    clean = []
    for a, dr, cr in lines:
        if a not in POSTING:
            raise ShopError(f"Account {a} can't be posted to. Use one of the accounts under a heading.")
        dr, cr = round(float(dr or 0), 2), round(float(cr or 0), 2)
        if dr < 0 or cr < 0 or (dr and cr):
            raise ShopError("Each line is either a debit or a credit, and not negative.")
        if dr or cr:
            clean.append((a, dr, cr))
    if len(clean) < 2:
        raise ShopError("A journal needs at least two lines.")
    if abs(sum(x[1] for x in clean) - sum(x[2] for x in clean)) > 0.004:
        raise ShopError("Debits and credits must be equal.")
    with tx() as c:
        jid = c.execute("INSERT INTO journals(date,kind,memo,created_at) VALUES(?,?,?,?)", (d, kind, memo, iso_now())).lastrowid
        for a, dr, cr in clean:
            c.execute("INSERT INTO journal_lines(journal_id,account,debit,credit) VALUES(?,?,?,?)", (jid, a, dr, cr))
    return jid


def quick_journal(kind: str, d: str, amount: float, memo: str = "") -> str:
    if kind not in QUICK:
        raise ShopError("Unknown kind of entry.")
    label, dr, cr = QUICK[kind]
    if not amount or amount <= 0:
        raise ShopError("The amount must be more than zero.")
    add_journal(d, memo or label, [(dr, amount, 0), (cr, 0, amount)], kind)
    return f"Recorded: {memo or label}, £{amount:,.2f} ({name(dr)} / {name(cr)})."


def delete_journal(jid: int) -> None:
    if not q1("SELECT id FROM journals WHERE id=?", (jid,)):
        raise ShopError("That journal doesn't exist.")
    with tx() as c:
        c.execute("DELETE FROM journal_lines WHERE journal_id=?", (jid,))
        c.execute("DELETE FROM journals WHERE id=?", (jid,))


def manual_journals() -> list[dict]:
    return [j for j in journals() if j["kind"] == "manual"][::-1]


# ---------------------------------------------------------------- balances and reports

def balances(start: str | None = None, end: str | None = None) -> dict[str, float]:
    """Net debit balance per posting account for journals in the dates."""
    bal = defaultdict(float)
    for j in journals(start, end):
        for a, dr, cr in j["lines"]:
            bal[a] += dr - cr
    return {a: round(v, 2) for a, v in bal.items()}


def signed(code: str, debit_balance: float) -> float:
    """Show a balance the natural way round: debit for assets and costs, credit for the rest."""
    return debit_balance if ACCOUNTS[code]["type"] in DEBIT_NORMAL else -debit_balance


def trial_balance(end: str) -> dict:
    bal = balances(None, end)
    rows = [{"code": c, "name": name(c), "type": ACCOUNTS[c]["type"], "debit": max(bal.get(c, 0), 0),
             "credit": max(-bal.get(c, 0), 0)} for c in POSTING if abs(bal.get(c, 0)) >= 0.005]
    dr, cr = round(sum(r["debit"] for r in rows), 2), round(sum(r["credit"] for r in rows), 2)
    return {"end": end, "rows": rows, "debit": dr, "credit": cr, "balanced": abs(dr - cr) < 0.01}


def income_statement(start: str, end: str) -> dict:
    bal = balances(start, end)

    def group(t):
        rows = [{"code": k, "name": name(k), "amount": round(signed(k, bal.get(k, 0)), 2)}
                for k in POSTING if ACCOUNTS[k]["type"] == t and abs(bal.get(k, 0)) >= 0.005]
        return rows, round(sum(r["amount"] for r in rows), 2)
    income, ti = group("income")
    cogs, tc = group("cogs")
    over, to = group("expense")
    gross = round(ti - tc, 2)
    net = round(gross - to, 2)
    return {"start": start, "end": end, "income": income, "total_income": ti, "cogs": cogs, "total_cogs": tc,
            "gross_profit": gross, "gross_margin": round(gross / ti * 100, 1) if ti else 0, "overheads": over,
            "total_overheads": to, "net_profit": net, "net_margin": round(net / ti * 100, 1) if ti else 0}


def profit_to(end: str | None) -> float:
    """All profit up to a date (inclusive)."""
    bal = balances(None, end)
    return round(-sum(v for a, v in bal.items() if ACCOUNTS.get(a, {}).get("type") in ("income", "cogs", "expense")), 2)


def balance_sheet(end: str, year_start: str | None = None) -> dict:
    bal = balances(None, end)
    year_start = year_start or tax_year(end)[0]
    prior = profit_to((parse_date(year_start) - timedelta(days=1)).isoformat())
    this_year = round(profit_to(end) - prior, 2)

    def group(parent):
        rows = [{"code": k, "name": name(k), "amount": round(signed(k, bal.get(k, 0)), 2)}
                for k in POSTING if ACCOUNTS[k]["parent"] == parent and abs(bal.get(k, 0)) >= 0.005]
        return {"name": name(parent), "rows": rows, "total": round(sum(r["amount"] for r in rows), 2)}
    assets = [group("1100"), group("1200")]
    liabilities = [group("2100"), group("2200")]
    eq = group("3000")
    eq["rows"] += [{"code": "3300", "name": "Profit from earlier years", "amount": prior},
                   {"code": "", "name": "Profit this year", "amount": this_year}]
    eq["rows"] = [r for r in eq["rows"] if r["code"] != "3300" or r["name"] != name("3300")]
    eq["total"] = round(sum(r["amount"] for r in eq["rows"]), 2)
    ta = round(sum(g["total"] for g in assets), 2)
    tl = round(sum(g["total"] for g in liabilities), 2)
    return {"end": end, "assets": assets, "total_assets": ta, "liabilities": liabilities, "total_liabilities": tl,
            "net_assets": round(ta - tl, 2), "equity": eq, "total_equity": eq["total"],
            "balanced": abs(round(ta - tl, 2) - eq["total"]) < 0.01}


def cash_flow(start: str, end: str) -> dict:
    """Direct method: every movement in cash, bank and Stripe, sorted by what the other side of the journal was."""
    lines = defaultdict(float)
    for j in journals(start, end):
        cash = sum(dr - cr for a, dr, cr in j["lines"] if a in CASH)
        if abs(cash) < 0.005:
            continue
        other = [a for a, dr, cr in j["lines"] if a not in CASH]
        if not other:
            continue    # moving money between cash, bank and Stripe
        a = other[0]
        t = ACCOUNTS[a]["type"]
        if a in ("1150", "1140", "4100", "4200", "4300", "2120"):
            key = ("operating", "Received from customers and partners")
        elif a in ("1160", "2110", "5100", "5300", "5400"):
            key = ("operating", "Paid for food, ingredients and packaging")
        elif a in ("1210",):
            key = ("investing", "Bought equipment")
        elif a in ("3100", "2200"):
            key = ("financing", "Money put in and loans")
        elif a in ("3200",):
            key = ("financing", "Owner's drawings")
        elif t == "income":
            key = ("operating", "Other income received")
        else:
            key = ("operating", "Paid for overheads")
        lines[key] += cash
    opening = sum(v for a, v in balances(None, (parse_date(start) - timedelta(days=1)).isoformat()).items() if a in CASH)
    closing = sum(v for a, v in balances(None, end).items() if a in CASH)
    out = {"start": start, "end": end, "opening": round(opening, 2), "closing": round(closing, 2), "sections": []}
    for sec, title in (("operating", "Day-to-day trading"), ("investing", "Equipment and investment"),
                       ("financing", "Owner and loans")):
        rows = [{"name": k[1], "amount": round(v, 2)} for k, v in lines.items() if k[0] == sec and abs(v) >= 0.005]
        out["sections"].append({"name": title, "rows": rows, "total": round(sum(r["amount"] for r in rows), 2)})
    out["net"] = round(sum(s["total"] for s in out["sections"]), 2)
    out["balanced"] = abs(out["opening"] + out["net"] - out["closing"]) < 0.01
    return out


def ledger(code: str, start: str, end: str) -> dict:
    if code not in POSTING:
        raise ShopError("Choose an account.")
    opening = balances(None, (parse_date(start) - timedelta(days=1)).isoformat()).get(code, 0)
    run, rows = opening, []
    for j in journals(start, end):
        for a, dr, cr in j["lines"]:
            if a == code:
                run += dr - cr
                rows.append({"date": j["date"], "ref": j["ref"], "memo": j["memo"], "debit": dr, "credit": cr, "balance": round(run, 2)})
    return {"code": code, "name": name(code), "opening": round(opening, 2), "rows": rows, "closing": round(run, 2)}


# ---------------------------------------------------------------- tax

# UK sole trader rates for the 2026/27 tax year (England, Wales and Northern Ireland). Check these each April.
TAX = {"year": "2026/27", "personal_allowance": 12570, "basic_limit": 50270, "higher_limit": 125140,
       "basic": 0.20, "higher": 0.40, "additional": 0.45, "class4_main": 0.06, "class4_upper": 0.02,
       "trading_allowance": 1000, "vat_threshold": 90000}

# Self Assessment self-employment (SA103) expense boxes, and which accounts go in each.
SA103 = [
    ("Cost of goods bought for resale or goods used", ("5100", "5200", "5250", "5300", "5400")),
    ("Premises running costs", ("6100",)),
    ("Car, van and travel expenses", ("6200",)),
    ("Phone, fax, stationery and other office costs", ("6700",)),
    ("Advertising and business entertainment costs", ("6400",)),
    ("Bank, credit card and other financial charges", ("6500",)),
    ("Other allowable business expenses", ("6300", "6600", "6900")),
]


def tax_year(d: str | None = None) -> tuple[str, str]:
    """UK tax year (6 April to 5 April) containing the date."""
    x = parse_date(d or today())
    y = x.year if (x.month, x.day) >= (4, 6) else x.year - 1
    return date(y, 4, 6).isoformat(), date(y + 1, 4, 5).isoformat()


def _cash_basis(start: str, end: str) -> dict:
    """HMRC's cash basis (the default for sole traders): income when the money arrives, costs when they're paid."""
    income = expenses = 0.0
    boxes = defaultdict(float)
    capital = 0.0
    for j in journals(start, end):
        for a, dr, cr in j["lines"]:
            t = ACCOUNTS[a]["type"]
            if j["kind"] == "sale" and a in ("4100", "4200") or j["kind"] == "stock":
                continue    # unpaid sales and stock used aren't cash; they're counted when paid / bought
            if a in ("1150",) and j["kind"] == "receipt":
                income += cr
            elif a == "2120" and j["kind"] == "receipt":
                income += cr
            elif a == "4300" or (t == "income" and j["kind"] == "manual"):
                income += cr - dr
            elif a == "1160" and j["kind"] == "purchase":
                boxes["5100"] += dr
            elif a == "1210":
                capital += dr - cr
            elif t in ("cogs", "expense") and a != "6800":
                boxes[a] += dr - cr
    rows = []
    for label, accts in SA103:
        v = round(sum(boxes.get(a, 0) for a in accts), 2)
        rows.append({"name": label, "amount": v})
    expenses = round(sum(r["amount"] for r in rows), 2)
    return {"turnover": round(income, 2), "rows": rows, "expenses": expenses, "capital": round(capital, 2)}


def tax_summary(start: str | None = None) -> dict:
    """Estimate for her Self Assessment, Class 4 National Insurance and the VAT threshold. An estimate only: her other
    income and allowances change the real figure, so an accountant or HMRC's calculator has the final word."""
    start, end = tax_year(start)
    cb = _cash_basis(start, end)
    # Capital allowances: the annual investment allowance gives 100% in the year bought (cash basis does the same).
    allowances = cb["capital"]
    profit_actual = round(cb["turnover"] - cb["expenses"] - allowances, 2)
    profit_trading_allowance = round(max(cb["turnover"] - TAX["trading_allowance"], 0), 2)
    use_allowance = cb["expenses"] + allowances < TAX["trading_allowance"] and cb["turnover"] > 0
    taxable = profit_trading_allowance if use_allowance else max(profit_actual, 0)
    t = TAX
    pa = t["personal_allowance"]
    if taxable > 100000:
        pa = max(0, pa - (taxable - 100000) / 2)
    basic_band = t["basic_limit"] - t["personal_allowance"]
    above_pa = max(taxable - pa, 0)
    basic = min(above_pa, basic_band)
    higher = min(max(above_pa - basic_band, 0), t["higher_limit"] - t["basic_limit"])
    additional = max(above_pa - basic - higher, 0)
    income_tax = round(basic * t["basic"] + higher * t["higher"] + additional * t["additional"], 2)
    c4 = round(max(min(taxable, t["basic_limit"]) - t["personal_allowance"], 0) * t["class4_main"]
               + max(taxable - t["basic_limit"], 0) * t["class4_upper"], 2)
    # VAT: turnover in the last 12 months against the registration threshold.
    vend = min(today(), end)
    vstart = (parse_date(vend) - timedelta(days=364)).isoformat()
    rolling = _cash_basis(vstart, vend)["turnover"]
    y0 = int(start[:4])
    quarters = []
    for a, b in ((f"{y0}-04-06", f"{y0}-07-05"), (f"{y0}-07-06", f"{y0}-10-05"),
                 (f"{y0}-10-06", f"{y0 + 1}-01-05"), (f"{y0 + 1}-01-06", f"{y0 + 1}-04-05")):
        qcb = _cash_basis(a, b)
        quarters.append({"start": a, "end": b, "turnover": qcb["turnover"], "expenses": round(qcb["expenses"] + qcb["capital"], 2)})
    return {"start": start, "end": end, "year": f"{start[:4]}/{end[2:4]}", "rates_year": t["year"], **cb,
            "allowances": allowances, "profit": profit_actual, "use_trading_allowance": use_allowance,
            "profit_trading_allowance": profit_trading_allowance, "taxable": round(taxable, 2),
            "income_tax": income_tax, "class4": c4, "total_tax": round(income_tax + c4, 2),
            "set_aside": round((income_tax + c4) / 12, 2), "vat_rolling": rolling, "vat_threshold": t["vat_threshold"],
            "vat_left": round(t["vat_threshold"] - rolling, 2), "vat_warn": rolling >= t["vat_threshold"] * 0.8,
            "quarters": quarters, "rates": t}
