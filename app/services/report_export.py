"""Exports van rapporten: CSV, XLSX (opgemaakt) en PDF (met grafieken)."""

import csv
import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..config import APP_NAME
from .report_model import Cell, Report, Section

EUR_FORMAT = "€ #,##0.00;[Red]-€ #,##0.00"


def _slug(title: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")


def filename(report: Report, ext: str) -> str:
    return f"{APP_NAME.lower()}-{_slug(report.title)}.{ext}"


# --- CSV ----------------------------------------------------------------------------------------


def to_csv(report: Report, meta: dict[str, str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow([APP_NAME, report.title])
    for key, value in meta.items():
        writer.writerow([key, value])
    for section in report.sections:
        writer.writerow([])
        writer.writerow([section.title])
        if section.columns:
            writer.writerow(section.columns)
        for row in section.rows:
            writer.writerow([c.csv() for c in row])
        if section.total:
            writer.writerow([c.csv() for c in section.total])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM: Excel leest UTF-8 dan goed


# --- XLSX ---------------------------------------------------------------------------------------


def _xlsx_value(cell: Cell):
    if cell.v in (None, ""):
        return None
    if cell.kind in {"money", "delta"}:
        return int(cell.v) / 100
    if cell.kind == "pct":
        return float(cell.v) / 100
    if cell.kind == "int":
        return int(cell.v)
    return str(cell.v)


def _sheet_name(title: str, used: set[str]) -> str:
    name = "".join(c for c in title if c not in "[]:*?/\\")[:31] or "Blad"
    base, n = name, 2
    while name in used:
        name = f"{base[:28]} {n}"
        n += 1
    used.add(name)
    return name


def to_xlsx(report: Report, meta: dict[str, str]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    header_fill = PatternFill("solid", fgColor="0F766E")
    total_fill = PatternFill("solid", fgColor="E2E8F0")
    sections = [s for s in report.sections if s.columns] or report.sections
    for section in sections:
        ws = wb.create_sheet(_sheet_name(section.title, used))
        ws["A1"] = f"{APP_NAME} — {report.title}: {section.title}"
        ws["A1"].font = Font(bold=True, size=13)
        for i, (key, value) in enumerate(meta.items(), start=2):
            ws.cell(row=i, column=1, value=f"{key}: {value}")
        row_idx = len(meta) + 3
        for col, name in enumerate(section.columns, start=1):
            c = ws.cell(row=row_idx, column=col, value=name)
            c.font, c.fill = Font(bold=True, color="FFFFFF"), header_fill
            c.alignment = Alignment(
                horizontal="right" if col > section.numeric_from else "left", wrap_text=True
            )
        ws.freeze_panes = ws.cell(row=row_idx + 1, column=2)
        for row in section.rows + ([section.total] if section.total else []):
            row_idx += 1
            is_total = row is section.total
            for col, cell in enumerate(row, start=1):
                c = ws.cell(row=row_idx, column=col, value=_xlsx_value(cell))
                if cell.kind in {"money", "delta"}:
                    c.number_format = EUR_FORMAT
                elif cell.kind == "pct":
                    c.number_format = "0.0%"
                if cell.bold or is_total:
                    c.font = Font(bold=True)
                if is_total:
                    c.fill = total_fill
                if col == 1 and cell.indent:
                    c.alignment = Alignment(indent=2 * cell.indent)
        ws.column_dimensions["A"].width = 34
        for col in range(2, max(len(section.columns), 2) + 1):
            ws.column_dimensions[get_column_letter(col)].width = 16
        if section.note:
            ws.cell(row=row_idx + 2, column=1, value=section.note).font = Font(italic=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --- PDF ----------------------------------------------------------------------------------------


def _hex(color: str | None, default="#0f766e"):
    from reportlab.lib.colors import HexColor

    try:
        return HexColor((color or default)[:7])
    except ValueError:
        return HexColor(default)


def _chart_drawing(section: Section, width: float):
    """Eenvoudige vectorgrafiek van de sectiegrafiek (bar/line/doughnut)."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing

    chart = section.chart
    if not chart or not chart.get("datasets"):
        return None
    sets = chart["datasets"]
    labels = [str(label) for label in chart.get("labels", [])]
    height = 180
    d = Drawing(width, height)
    if chart["type"] == "doughnut":
        data = [float(v) for v in sets[0]["data"]]
        if not any(data):
            return None
        pie = Pie()
        pie.x, pie.y, pie.width, pie.height = 20, 15, 150, 150
        pie.data, pie.labels = data, None
        colors = sets[0].get("backgroundColor", [])
        for i in range(len(data)):
            pie.slices[i].fillColor = _hex(colors[i] if i < len(colors) else None)
            pie.slices[i].strokeWidth = 0
        d.add(pie)
        from reportlab.graphics.shapes import Rect, String

        for i, label in enumerate(labels[:10]):
            y = height - 22 - i * 15
            d.add(
                Rect(
                    200,
                    y,
                    8,
                    8,
                    fillColor=_hex(colors[i] if i < len(colors) else None),
                    strokeWidth=0,
                )
            )
            d.add(String(214, y, f"{label[:28]}: {data[i]:,.0f}".replace(",", "."), fontSize=7))
        return d
    if (
        chart["type"] == "line"
        and chart.get("stacked") is not True
        or any(s.get("type") == "line" for s in sets)
    ):
        pass
    if chart["type"] == "line":
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 40, 25, width - 60, height - 45
        lp.data = [[(i, float(v)) for i, v in enumerate(s["data"])] for s in sets]
        for i, s in enumerate(sets):
            lp.lines[i].strokeColor = _hex(s.get("borderColor"))
            lp.lines[i].strokeWidth = 1.5
        lp.xValueAxis.valueSteps = list(range(0, len(labels), max(1, len(labels) // 8)))
        lp.xValueAxis.labelTextFormat = lambda v: (
            labels[int(v)] if 0 <= int(v) < len(labels) else ""
        )
        lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 6
        d.add(lp)
        return d
    bar = VerticalBarChart()
    bar.x, bar.y, bar.width, bar.height = 40, 25, width - 60, height - 45
    bar.data = [[float(v) for v in s["data"]] for s in sets]
    bar.categoryAxis.categoryNames = labels
    bar.categoryAxis.labels.fontSize = bar.valueAxis.labels.fontSize = 6
    bar.categoryAxis.labels.angle, bar.categoryAxis.labels.dy = 30, -8
    bar.groupSpacing, bar.barSpacing = 6, 1
    for i, s in enumerate(sets):
        bar.bars[i].fillColor = _hex(
            s.get("backgroundColor") if isinstance(s.get("backgroundColor"), str) else None
        )
    d.add(bar)
    return d


def to_pdf(report: Report, meta: dict[str, str]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    out = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(
        out,
        pagesize=page,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=22 * mm,
        bottomMargin=14 * mm,
        title=f"{APP_NAME} — {report.title}",
    )
    styles = getSampleStyleSheet()
    usable = page[0] - 28 * mm
    header = " · ".join(f"{k}: {v}" for k, v in meta.items())

    def decorate(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#0f766e"))
        canvas.rect(0, page[1] - 14 * mm, page[0], 14 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(14 * mm, page[1] - 9 * mm, f"{APP_NAME} — {report.title}")
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(page[0] - 14 * mm, page[1] - 9 * mm, header[:150])
        canvas.setFillColor(colors.grey)
        canvas.drawString(14 * mm, 8 * mm, f"Gemaakt op {datetime.now():%d-%m-%Y %H:%M}")
        canvas.drawRightString(page[0] - 14 * mm, 8 * mm, f"Pagina {canvas.getPageNumber()}")
        canvas.restoreState()

    story = []
    for section in report.sections:
        story.append(Paragraph(section.title, styles["Heading3"]))
        drawing = _chart_drawing(section, usable)
        if drawing is not None:
            story += [drawing, Spacer(1, 4)]
        if section.columns:
            rows = [section.columns] + [
                [("   " * c.indent) + c.text() for c in r] for r in section.rows
            ]
            if section.total:
                rows.append([c.text() for c in section.total])
            ncols = len(section.columns)
            first = 38 * mm if ncols > 6 else 60 * mm
            widths = [first] + [(usable - first) / max(1, ncols - 1)] * (ncols - 1)
            table = Table(rows, colWidths=widths, repeatRows=1)
            style = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("ALIGN", (section.numeric_from, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
            ]
            if section.total:
                style += [
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
                ]
            table.setStyle(TableStyle(style))
            story.append(table)
        if section.note:
            story.append(Paragraph(f"<i>{section.note}</i>", styles["BodyText"]))
        story.append(Spacer(1, 10))
    for line in report.insights:
        story.append(Paragraph("• " + line, styles["BodyText"]))
    if not story:
        story.append(Paragraph("Geen gegevens in deze periode.", styles["BodyText"]))
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return out.getvalue()
