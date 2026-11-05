"""Excel workbook and PDF versions of every financial statement, for her and her accountant."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import accounts as A
from .shop import pretty_date, setting

REPORTS = {
    "income": "Income statement",
    "balance": "Balance sheet",
    "cashflow": "Cash flow statement",
    "trial": "Trial balance",
    "tax": "Tax summary",
    "journal": "Journal",
    "chart": "Chart of accounts",
}
GREEN = "00473C"


def build(kind: str, start: str, end: str) -> dict:
    """A report as plain rows: {title, subtitle, cols, rows, bold (row indexes), notes}. Both Excel and PDF use it."""
    title = REPORTS[kind]
    if kind == "income":
        r = A.income_statement(start, end)
        rows, bold = [], set()

        def sec(name, items, total_label, total):
            rows.append([name, ""]); bold.add(len(rows) - 1)
            rows.extend([[f"  {x['code']}  {x['name']}", x["amount"]] for x in items])
            rows.append([total_label, total]); bold.add(len(rows) - 1)
        sec("Income", r["income"], "Total income", r["total_income"])
        sec("Cost of sales", r["cogs"], "Total cost of sales", r["total_cogs"])
        rows.append(["Gross profit", r["gross_profit"]]); bold.add(len(rows) - 1)
        rows.append([f"  Gross margin {r['gross_margin']}%", ""])
        sec("Overheads", r["overheads"], "Total overheads", r["total_overheads"])
        rows.append(["Net profit", r["net_profit"]]); bold.add(len(rows) - 1)
        rows.append([f"  Net margin {r['net_margin']}%", ""])
        return {"title": title, "subtitle": f"{pretty_date(start)} to {pretty_date(end)}", "cols": ["", "£"], "rows": rows,
                "bold": bold, "notes": []}
    if kind == "balance":
        r = A.balance_sheet(end)
        rows, bold = [], set()

        def block(head, groups, total_label, total):
            rows.append([head, ""]); bold.add(len(rows) - 1)
            for g in groups:
                if g["rows"]:
                    rows.append([f"  {g['name']}", ""])
                    rows.extend([[f"    {x['code']}  {x['name']}".replace("    ", "    ").rstrip(), x["amount"]] for x in g["rows"]])
                    rows.append([f"  Total {g['name'].lower()}", g["total"]])
            rows.append([total_label, total]); bold.add(len(rows) - 1)
        block("Assets", r["assets"], "Total assets", r["total_assets"])
        block("Liabilities", r["liabilities"], "Total liabilities", r["total_liabilities"])
        rows.append(["Net assets", r["net_assets"]]); bold.add(len(rows) - 1)
        block("Equity", [r["equity"]], "Total equity", r["total_equity"])
        return {"title": title, "subtitle": f"As at {pretty_date(end)}", "cols": ["", "£"], "rows": rows, "bold": bold,
                "notes": [] if r["balanced"] else ["Does not balance: please tell your accountant."]}
    if kind == "cashflow":
        r = A.cash_flow(start, end)
        rows, bold = [["Cash at the start", r["opening"]]], {0}
        for s in r["sections"]:
            rows.append([s["name"], ""]); bold.add(len(rows) - 1)
            rows.extend([[f"  {x['name']}", x["amount"]] for x in s["rows"]])
            rows.append([f"  Net from {s['name'].lower()}", s["total"]])
        rows.append(["Net change in cash", r["net"]]); bold.add(len(rows) - 1)
        rows.append(["Cash at the end", r["closing"]]); bold.add(len(rows) - 1)
        return {"title": title, "subtitle": f"{pretty_date(start)} to {pretty_date(end)} · cash, bank and Stripe",
                "cols": ["", "£"], "rows": rows, "bold": bold, "notes": []}
    if kind == "trial":
        r = A.trial_balance(end)
        rows = [[x["code"], x["name"], x["debit"] or "", x["credit"] or ""] for x in r["rows"]]
        rows.append(["", "Total", r["debit"], r["credit"]])
        return {"title": title, "subtitle": f"As at {pretty_date(end)}", "cols": ["Code", "Account", "Debit £", "Credit £"],
                "rows": rows, "bold": {len(rows) - 1}, "notes": [] if r["balanced"] else ["Debits and credits differ."]}
    if kind == "tax":
        t = A.tax_summary(start)
        rows, bold = [], set()

        def line(label, val, b=False):
            rows.append([label, val])
            if b:
                bold.add(len(rows) - 1)
        line("Self Assessment, self-employment page (cash basis)", "", True)
        line("Turnover (money received)", t["turnover"])
        for x in t["rows"]:
            line(f"  {x['name']}", x["amount"])
        line("Total allowable expenses", t["expenses"], True)
        line("Equipment bought (annual investment allowance)", t["allowances"])
        line("Net profit", t["profit"], True)
        if t["use_trading_allowance"]:
            line("Using the £1,000 trading allowance instead is better; taxable profit", t["taxable"])
        line("Estimated tax for the year", "", True)
        line("Income tax (if this is your only income)", t["income_tax"])
        line("Class 4 National Insurance", t["class4"])
        line("Total to put aside", t["total_tax"], True)
        line(f"  About a month: £{t['set_aside']:,.2f} to put aside", "")
        line("VAT check", "", True)
        line("Turnover over the last 12 months", t["vat_rolling"])
        line(f"VAT registration threshold", t["vat_threshold"])
        line("Room left before you must register", t["vat_left"], True)
        line("Quarterly turnover and expenses (for Making Tax Digital updates)", "", True)
        for qt in t["quarters"]:
            line(f"  {pretty_date(qt['start'])} to {pretty_date(qt['end'])}: turnover / expenses", f"{qt['turnover']:,.2f} / {qt['expenses']:,.2f}")
        return {"title": title, "subtitle": f"Tax year {t['year']} ({pretty_date(t['start'])} to {pretty_date(t['end'])})",
                "cols": ["", "£"], "rows": rows, "bold": bold,
                "notes": [f"An estimate using {t['rates_year']} rates for a sole trader whose only income is this business. "
                          "Your other income, the real allowances and Scottish rates change the final bill. Check with an "
                          "accountant or HMRC before you file."]}
    if kind == "journal":
        rows = []
        for j in A.journals(start, end):
            for i, (a, dr, cr) in enumerate(j["lines"]):
                rows.append([j["date"] if i == 0 else "", j["ref"] if i == 0 else "", j["memo"] if i == 0 else "",
                             f"{a} {A.name(a)}", dr or "", cr or ""])
        return {"title": title, "subtitle": f"{pretty_date(start)} to {pretty_date(end)}",
                "cols": ["Date", "Ref", "Details", "Account", "Debit £", "Credit £"], "rows": rows, "bold": set(), "notes": []}
    rows = [[c, n, A.TYPE_NAMES[t], (A.name(p) if p else "Heading"), "post" if c in A.POSTING else "heading"]
            for c, n, t, p in A.CHART]
    return {"title": title, "subtitle": "", "cols": ["Code", "Account", "Type", "Under", "Use"], "rows": rows, "bold": set(), "notes": []}


# ---------------------------------------------------------------- Excel

def _sheet(ws, rep):
    ws.append([setting("shop_name")])
    ws.append([rep["title"]])
    ws.append([rep["subtitle"]])
    ws.append([])
    ws.append(rep["cols"])
    head = ws.max_row
    for c in ws[head]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=GREEN)
    ws["A1"].font = Font(bold=True, size=14, color=GREEN)
    ws["A2"].font = Font(bold=True, size=12)
    for i, row in enumerate(rep["rows"]):
        ws.append(row)
        for c in ws[ws.max_row]:
            if isinstance(c.value, (int, float)):
                c.number_format = '#,##0.00;[Red]-#,##0.00'
                c.alignment = Alignment(horizontal="right")
            if i in rep["bold"]:
                c.font = Font(bold=True)
    for n in rep["notes"]:
        ws.append([])
        ws.append([n])
    for col in range(1, len(rep["cols"]) + 1):
        width = max((len(str(c.value)) for c in ws[get_column_letter(col)] if c.value is not None), default=10)
        ws.column_dimensions[get_column_letter(col)].width = min(max(width + 2, 10), 70)
    ws.freeze_panes = ws.cell(row=head + 1, column=1)


def excel(kind: str, start: str, end: str) -> bytes:
    """One report, or kind='all' for a workbook with every statement as its own sheet (like a Xero export)."""
    wb = Workbook()
    wb.remove(wb.active)
    for k in (list(REPORTS) if kind == "all" else [kind]):
        ws = wb.create_sheet(REPORTS[k][:31])
        _sheet(ws, build(k, start, end))
    if kind == "all":
        for code in A.POSTING:        # general ledger: one sheet per busy account would be long, so one combined sheet
            pass
        ws = wb.create_sheet("General ledger")
        ws.append([setting("shop_name")]); ws.append(["General ledger"]); ws.append([f"{pretty_date(start)} to {pretty_date(end)}"]); ws.append([])
        ws.append(["Account", "Date", "Ref", "Details", "Debit £", "Credit £", "Balance £"])
        for c in ws[5]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=GREEN)
        for code in A.POSTING:
            led = A.ledger(code, start, end)
            if not led["rows"] and not led["opening"]:
                continue
            ws.append([f"{code} {led['name']}", "", "", "Opening balance", "", "", led["opening"]])
            for r in led["rows"]:
                ws.append(["", r["date"], r["ref"], r["memo"], r["debit"] or None, r["credit"] or None, r["balance"]])
        for col, w in zip("ABCDEFG", (34, 12, 14, 44, 12, 12, 12)):
            ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

def _story(rep, styles):
    out = [Paragraph(setting("shop_name"), ParagraphStyle("shop", parent=styles["Normal"], textColor=colors.HexColor("#" + GREEN),
                                                           fontSize=10)),
           Paragraph(rep["title"], styles["Title"] if False else ParagraphStyle("t", parent=styles["Heading1"], fontSize=20, spaceAfter=2)),
           Paragraph(rep["subtitle"], styles["Normal"]), Spacer(1, 8)]
    data = [rep["cols"]] + [[f"{c:,.2f}" if isinstance(c, (int, float)) else c for c in row] for row in rep["rows"]]
    wide = len(rep["cols"]) > 2
    widths = None if wide else [125 * mm, 40 * mm]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + GREEN)), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTSIZE", (0, 0), (-1, -1), 8 if wide else 9.5), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
             ("ALIGN", (-1, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#d8e5d6")),
             ("VALIGN", (0, 0), (-1, -1), "TOP")]
    if len(rep["cols"]) == 4:
        style.append(("ALIGN", (2, 0), (-1, -1), "RIGHT"))
    if len(rep["cols"]) == 6:
        style.append(("ALIGN", (4, 0), (-1, -1), "RIGHT"))
    for i in rep["bold"]:
        style += [("FONTNAME", (0, i + 1), (-1, i + 1), "Helvetica-Bold"), ("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#f4f3e7"))]
    t.setStyle(TableStyle(style))
    out.append(t)
    for n in rep["notes"]:
        out += [Spacer(1, 8), Paragraph(n, ParagraphStyle("n", parent=styles["Normal"], fontSize=8, textColor=colors.grey))]
    return out


def pdf(kind: str, start: str, end: str) -> bytes:
    """One report, or kind='all' for the full pack."""
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=REPORTS.get(kind, "Financial statements"), author=setting("shop_name"))
    story = []
    kinds = [k for k in REPORTS if k != "chart"] if kind == "all" else [kind]
    for n, k in enumerate(kinds):
        if n:
            story.append(PageBreak())
        story += _story(build(k, start, end), styles)
    doc.build(story)
    return buf.getvalue()
