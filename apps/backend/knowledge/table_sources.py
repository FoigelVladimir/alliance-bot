"""Immutable table choices: snapshot-local keys and semantic header remapping."""

import unicodedata
from collections import Counter


class SourceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def header(value):
    return unicodedata.normalize("NFC", value).strip()


def selected_text(data, selection, *, max_bytes=64 * 1024):
    if not isinstance(selection, list) or not selection or len(selection) > len(data["sheets"]):
        raise SourceError("invalid_table_selection")
    sheets = {sheet["key"]: sheet for sheet in data["sheets"]}
    seen = set()
    result = []
    formula_count = 0
    value_count = 0
    for choice in selection:
        if not isinstance(choice, dict) or set(choice) != {"sheet", "columns"}:
            raise SourceError("invalid_table_selection")
        key, columns = choice["sheet"], choice["columns"]
        if not isinstance(key, str) or key not in sheets or key in seen:
            raise SourceError("invalid_table_selection")
        seen.add(key)
        sheet = sheets[key]
        if sheet["hidden"] or sheet.get("blocked_reason"):
            raise SourceError(sheet.get("blocked_reason") or "hidden_table_selection")
        available = {col["key"]: col for col in sheet["columns"] if not col["hidden"]}
        if (not isinstance(columns, list) or not columns or any(not isinstance(col, str) for col in columns)
                or len(set(columns)) != len(columns) or any(col not in available for col in columns)):
            raise SourceError("invalid_table_selection")
        result.append("Лист: " + sheet["title"])
        for row in sheet["rows"]:
            values = []
            for key in columns:
                column = available[key]
                cell = row[column["index"]] if column["index"] < len(row) else {"text": "", "formula": False}
                if cell["formula"]:
                    formula_count += 1
                    continue
                if cell["text"]:
                    label = column["header"] or f"Столбец {column['index'] + 1}"
                    values.append(label + ": " + cell["text"])
                    value_count += 1
            if values:
                result.append(" | ".join(values))
    if not value_count:
        raise SourceError("empty_table_selection")
    text = "\n".join(result)
    if len(text.encode("utf-8")) > max_bytes:
        raise SourceError("text_too_large")
    return text, formula_count


def remap_selection(old, new, selection):
    """Never infer a column by position, or carry a choice to a different Google file."""
    if old["source_type"] != new["source_type"] or (
            old["source_type"] == "google" and old["source_id"] != new["source_id"]):
        return [], ["source_changed"], []
    old_sheets = {sheet["key"]: sheet for sheet in old["sheets"]}
    result, hints, issues = [], [], []
    for choice in selection:
        previous = old_sheets.get(choice["sheet"])
        if previous is None:
            issues.append("sheet_missing")
            continue
        matches = [sheet for sheet in new["sheets"] if sheet["identity"] == previous["identity"]]
        if len(matches) != 1 or matches[0]["hidden"] or matches[0].get("blocked_reason"):
            issues.append("sheet_missing_or_hidden")
            continue
        current = matches[0]
        old_counts = Counter(header(col["header"]) for col in previous["columns"])
        new_counts = Counter(header(col["header"]) for col in current["columns"])
        old_columns = {col["key"]: col for col in previous["columns"]}
        mapped = []
        for key in choice["columns"]:
            column = old_columns.get(key)
            name = header(column["header"]) if column else ""
            match = next((col for col in current["columns"] if header(col["header"]) == name), None)
            if not name or old_counts[name] != 1 or new_counts[name] != 1 or match is None or match["hidden"]:
                issues.append("column_missing_ambiguous_or_hidden")
                continue
            mapped.append(match["key"])
        if mapped:
            hints.append({"sheet": current["key"], "columns": mapped})
        if len(mapped) == len(choice["columns"]):
            result.append({"sheet": current["key"], "columns": mapped})
    if issues or len(result) != len(selection):
        return [], sorted(set(issues)), hints
    return result, [], hints


def selection_catalog(snapshot, selection):
    data = snapshot.data
    return {"source_snapshot_id": str(snapshot.pk), "sheets": [
        {"key": sheet["key"], "title": sheet["title"], "blocked_reason": sheet.get("blocked_reason", ""),
         "columns": [{"key": col["key"], "header": col["header"], "position": col["index"] + 1}
                     for col in sheet["columns"] if not col["hidden"]],
         "sample_rows": [["Формула — значение исключено" if cell["formula"] else cell["text"]
                          for index, cell in enumerate(row) if index < len(sheet["columns"])
                          and not sheet["columns"][index]["hidden"]] for row in sheet["rows"][:10]]}
        for sheet in data["sheets"] if not sheet["hidden"]],
        "selection": selection, "issues": snapshot.mapping_issues,
        "suggestions": snapshot.mapping_suggestions}
