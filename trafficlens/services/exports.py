"""Helpers for Excel/CSV result exports."""
import json
from openpyxl.styles import Alignment, Font, PatternFill


def _csv_safe_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif value is None:
        value = ""
    else:
        value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _result_records(value):
    if isinstance(value, list):
        for item in value:
            yield from _result_records(item)
    elif isinstance(value, dict):
        yield value
    elif value is not None:
        yield {"value": value}


def _redact_client_directions_context(result_request):
    for service_data in result_request["data"].values():
        if not isinstance(service_data, dict):
            continue
        service_data.pop("outscraper_job_id", None)
        direction_result = service_data.get("directions_result")
        if isinstance(direction_result, dict):
            for key in ("source_request_id", "outscraper_job_id", "point_a", "point_b", "point_c"):
                direction_result.pop(key, None)


def _excel_safe_text(value):
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _style_excel_sheet(sheet):
    header_fill = PatternFill("solid", fgColor="5632A8")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        column_letter = column[0].column_letter
        width = max((len(str(cell.value or "")) for cell in column), default=10)
        sheet.column_dimensions[column_letter].width = min(max(width + 2, 14), 48)
