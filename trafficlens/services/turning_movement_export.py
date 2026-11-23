"""Excel export of one turning-movement study. Needs openpyxl.

The workbook never contains the review warnings, so it is safe to hand to the client.
Totals and rates are live Excel formulas, so edits to the counts recalculate."""
import io
import re

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

TURNS = ("u_turn", "left", "through", "right")
LABEL = {"u_turn": "U-turn", "left": "Left", "through": "Through", "right": "Right"}
COLOR = {"u_turn": "7B8794", "left": "8A4FB3", "through": "0B7A75", "right": "2F6FD0"}
STEP_KIND = ("u_turn", "right", "through", "left")       # by (to - from) mod 4, traffic driving on the right
SIDE = ("right of the drawing", "top", "left", "bottom")

HEAD_FILL = PatternFill("solid", fgColor="2A3340")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TOTAL_BORDER = Border(top=Side(style="thin", color="2A3340"))
THOUSANDS, PERCENT = "#,##0", "0.0%"


def _sheet_name(label, used):
    base = re.sub(r"[\[\]:*?/\\]", " ", str(label)).strip()[:28] or "Period"
    name, n = base, 2
    while name.lower() in used:
        name, n = f"{base[:25]} {n}", n + 1
    used.add(name.lower())
    return name


def _fit_to_page(ws, landscape):
    """Print each sheet one page wide, so the chart and tables are not cut across pages."""
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def _head(ws, row, values, first_col=1):
    for i, v in enumerate(values):
        c = ws.cell(row=row, column=first_col + i, value=v)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="left" if i == 0 else "right")


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _period_sheet(wb, name, study, p):
    ws = wb.create_sheet(name)
    ws.sheet_properties.tabColor = "0B7A75"
    legs = study["legs"]
    ws["A1"] = f"{study['title']}, {p['label']}"
    ws["A1"].font = Font(bold=True, size=14)

    ws["A3"] = "Entering and leaving"; ws["A3"].font = Font(bold=True)
    _head(ws, 4, ["Leg", "Entering", "Leaving"])
    for i in range(4):
        ws.cell(row=5 + i, column=1, value=legs[i])
        ws.cell(row=5 + i, column=2, value=p["inflows"][i]).number_format = THOUSANDS
        ws.cell(row=5 + i, column=3, value=p["outflows"][i]).number_format = THOUSANDS
    ws["A9"] = "Total"
    for col in "BC":
        ws[f"{col}9"] = f"=SUM({col}5:{col}8)"; ws[f"{col}9"].number_format = THOUSANDS
    for col in "ABC":
        ws[f"{col}9"].font, ws[f"{col}9"].border = Font(bold=True), TOTAL_BORDER

    ws["A11"] = "Turning movements, in vehicles"; ws["A11"].font = Font(bold=True)
    _head(ws, 12, ["From leg"] + [LABEL[k] for k in TURNS] + ["Total"])
    for i in range(4):
        r = 13 + i
        ws.cell(row=r, column=1, value=legs[i])
        for j, kind in enumerate(TURNS):
            ws.cell(row=r, column=2 + j, value=p["result"]["movements"][i][kind]).number_format = THOUSANDS
        ws.cell(row=r, column=6, value=f"=SUM(B{r}:E{r})").number_format = THOUSANDS
    ws["A17"] = "Total"
    for col in "BCDEF":
        ws[f"{col}17"] = f"=SUM({col}13:{col}16)"; ws[f"{col}17"].number_format = THOUSANDS
    for col in "ABCDEF":
        ws[f"{col}17"].font, ws[f"{col}17"].border = Font(bold=True), TOTAL_BORDER

    ws["A19"] = "Turning rates, as a share of each leg's entering vehicles"; ws["A19"].font = Font(bold=True)
    _head(ws, 20, ["From leg"] + [LABEL[k] for k in TURNS] + ["Total"])
    for i in range(4):
        r, m = 21 + i, 13 + i
        ws.cell(row=r, column=1, value=legs[i])
        for j in range(4):
            col = get_column_letter(2 + j)
            ws.cell(row=r, column=2 + j, value=f'=IF($F{m}=0,"",{col}{m}/$F{m})').number_format = PERCENT
        ws.cell(row=r, column=6, value=f'=IF($F{m}=0,"",SUM(B{r}:E{r}))').number_format = PERCENT

    if p["result"].get("balanced_by_scaling"):
        ws["A26"] = "Entering and leaving totals differed, so the leaving counts were scaled to match before estimating."
        ws["A26"].font = Font(italic=True, color="666666")

    chart = BarChart(); chart.type, chart.grouping, chart.overlap = "col", "percentStacked", 100
    chart.title, chart.height, chart.width = "Share of each leg's vehicles by turn", 8.5, 14
    chart.add_data(Reference(ws, min_col=2, max_col=5, min_row=12, max_row=16), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=13, max_row=16))
    for series, kind in zip(chart.series, TURNS):
        series.graphicalProperties.solidFill = COLOR[kind]
    ws.add_chart(chart, "H3")
    _widths(ws, [26, 12, 12, 12, 12, 12])
    ws.freeze_panes = "A3"
    _fit_to_page(ws, landscape=True)
    return ws


def build_workbook(study, exported_at, include_internal=False):
    """study: the study as the person may see it (admin: full; client: after client_view, results released)."""
    periods = [p for p in study["periods"] if p.get("result")]
    if not periods:
        raise ValueError("There are no results to export yet.")
    wb = Workbook()
    summary = wb.active; summary.title = "Summary"
    used = {"summary", "movements"}
    names = [_sheet_name(p["label"], used) for p in periods]

    summary["A1"] = study["title"]; summary["A1"].font = Font(bold=True, size=16)
    rows = [("Study", f"TM-{study['id']}"), ("Status", study["status"]), ("Count date", study.get("study_date") or "")]
    if include_internal:
        rows.insert(1, ("Client", f"{study.get('owner_name', '')} {study.get('owner_email', '')}".strip()))
        rows.append(("Estimation method", periods[0]["result"].get("method", "")))
    rows.append(("Exported", exported_at))
    for i, (k, v) in enumerate(rows):
        summary.cell(row=3 + i, column=1, value=k).font = Font(bold=True)
        summary.cell(row=3 + i, column=2, value=v)
    r = 4 + len(rows)
    summary.cell(row=r, column=1, value="Legs, counter-clockwise").font = Font(bold=True)
    for i in range(4):
        summary.cell(row=r + 1 + i, column=1, value=f"Leg {i + 1}, {SIDE[i]}")
        summary.cell(row=r + 1 + i, column=2, value=study["legs"][i])
    r += 6
    _head(summary, r, ["Period", "Total entering", "Total leaving"])
    for i, (p, name) in enumerate(zip(periods, names)):
        summary.cell(row=r + 1 + i, column=1, value=p["label"])
        for col in (2, 3):
            letter = "BC"[col - 2]
            summary.cell(row=r + 1 + i, column=col, value=f"='{name}'!{letter}9").number_format = THOUSANDS
    _widths(summary, [28, 34, 16])
    _fit_to_page(summary, landscape=False)

    flat = wb.create_sheet("Movements")
    flat.sheet_properties.tabColor = "2F6FD0"
    _head(flat, 1, ["Period", "From leg", "To leg", "Turn", "Vehicles", "Share of leg's entering"])
    row = 2
    for p in periods:
        for f in range(4):
            for t in range(4):
                kind = STEP_KIND[(t - f) % 4]
                flat.cell(row=row, column=1, value=p["label"])
                flat.cell(row=row, column=2, value=study["legs"][f])
                flat.cell(row=row, column=3, value=study["legs"][t])
                flat.cell(row=row, column=4, value=LABEL[kind])
                flat.cell(row=row, column=5, value=p["result"]["movements"][f][kind]).number_format = THOUSANDS
                tot = f"SUMIFS($E:$E,$A:$A,A{row},$B:$B,B{row})"
                flat.cell(row=row, column=6, value=f'=IF({tot}=0,"",E{row}/{tot})').number_format = PERCENT
                row += 1
    flat.freeze_panes = "A2"; flat.auto_filter.ref = f"A1:F{row - 1}"
    _widths(flat, [22, 14, 14, 12, 12, 24])
    _fit_to_page(flat, landscape=False)

    for p, name in zip(periods, names):
        _period_sheet(wb, name, study, p)
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()
