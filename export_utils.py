#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSV and XLSX export helpers."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

from db import (
    list_content_comments,
    list_content_items,
    list_crawl_runs,
    list_dedupe_groups,
    list_discovered_keywords,
    list_site_profiles,
)

EXPORT_TABLES = ("content", "comments", "keywords", "crawl_runs", "site_profiles", "dedupe_groups")


def export_dataset(
    *,
    table: str,
    output_format: str,
    output_path: str | Path | None,
    limit: int,
    db_path: str | Path | None = None,
) -> Dict[str, object]:
    table = table or "content"
    output_format = output_format or "csv"
    if output_format == "xlsx":
        default_name = "all_export.xlsx" if table == "all" else f"{table}_export.xlsx"
        path = Path(output_path or Path("outputs/csv") / default_name)
        return {"paths": [str(export_xlsx(table=table, output_path=path, limit=limit, db_path=db_path))]}
    return export_csv(table=table, output_path=output_path, limit=limit, db_path=db_path)


def export_csv(
    *,
    table: str,
    output_path: str | Path | None,
    limit: int,
    db_path: str | Path | None = None,
) -> Dict[str, object]:
    if table == "all":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        directory = Path(output_path) if output_path else Path("outputs/csv") / f"all_export_{timestamp}"
        if directory.suffix:
            directory = directory.with_suffix("")
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for table_name in EXPORT_TABLES:
            rows = load_export_rows(table_name, limit=limit, db_path=db_path)
            path = directory / f"{table_name}.csv"
            write_csv_rows(path, rows)
            paths.append(str(path))
        return {"paths": paths}

    rows = load_export_rows(table, limit=limit, db_path=db_path)
    default_name = f"{table}_export.csv"
    path = Path(output_path or Path("outputs/csv") / default_name)
    write_csv_rows(path, rows)
    return {"paths": [str(path)]}


def export_xlsx(
    *,
    table: str,
    output_path: str | Path,
    limit: int,
    db_path: str | Path | None = None,
) -> Path:
    from openpyxl import Workbook

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    table_names = EXPORT_TABLES if table == "all" else (table,)
    for table_name in table_names:
        rows = load_export_rows(table_name, limit=limit, db_path=db_path)
        worksheet = workbook.create_sheet(_sheet_title(table_name))
        write_worksheet_rows(worksheet, rows)
    workbook.save(path)
    return path


def load_export_rows(
    table: str,
    *,
    limit: int,
    db_path: str | Path | None = None,
) -> List[Dict[str, object]]:
    if table == "content":
        return list_content_items(limit=limit, db_path=db_path)
    if table == "comments":
        return list_content_comments(limit=limit, db_path=db_path)
    if table == "keywords":
        return list_discovered_keywords(limit=limit, db_path=db_path)
    if table == "crawl_runs":
        return list_crawl_runs(limit=limit, db_path=db_path)
    if table == "site_profiles":
        return list_site_profiles(db_path=db_path)
    if table == "dedupe_groups":
        return list_dedupe_groups(limit=limit, db_path=db_path)
    raise ValueError(f"unknown export table: {table}")


def write_csv_rows(path: Path, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _fieldnames(rows)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_worksheet_rows(worksheet, rows: Iterable[Dict[str, object]]) -> None:
    rows = list(rows)
    fieldnames = _fieldnames(rows)
    worksheet.append(fieldnames)
    for row in rows:
        worksheet.append([row.get(field, "") for field in fieldnames])
    worksheet.freeze_panes = "A2"


def _fieldnames(rows: List[Dict[str, object]]) -> List[str]:
    if not rows:
        return ["empty"]
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key in seen:
                continue
            seen.add(key)
            fields.append(key)
    return fields


def _sheet_title(table_name: str) -> str:
    title = table_name[:31]
    return title or "sheet"
