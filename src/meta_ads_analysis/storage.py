"""DuckDB persistence and retrieval."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


AD_DAILY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ad_daily_metrics (
  ingestion_run_date DATE,
  source_run_path VARCHAR,
  report_date DATE,
  account_id VARCHAR,
  account_name VARCHAR,
  campaign_id VARCHAR,
  campaign_name VARCHAR,
  adset_id VARCHAR,
  adset_name VARCHAR,
  ad_id VARCHAR,
  ad_name VARCHAR,
  objective VARCHAR,
  spend DOUBLE,
  impressions BIGINT,
  reach BIGINT,
  frequency DOUBLE,
  clicks BIGINT,
  link_clicks BIGINT,
  outbound_clicks BIGINT,
  ctr DOUBLE,
  cpc DOUBLE,
  cpm DOUBLE,
  results DOUBLE,
  result_label VARCHAR,
  cost_per_result DOUBLE,
  purchase_count DOUBLE,
  purchase_value DOUBLE,
  purchase_roas DOUBLE,
  video_3s_plays DOUBLE,
  thruplays DOUBLE,
  hook_rate DOUBLE,
  hold_rate DOUBLE,
  average_order_value DOUBLE,
  creative_type VARCHAR,
  creative_copy VARCHAR,
  creative_headline VARCHAR,
  launch_date DATE,
  preview_link VARCHAR,
  post_link VARCHAR,
  has_video_metrics BOOLEAN,
  tracking_confidence VARCHAR
)
"""

CREATIVE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS creative_lookup (
  ingestion_run_date DATE,
  ad_id VARCHAR,
  ad_name VARCHAR,
  creative_type VARCHAR,
  creative_copy VARCHAR,
  creative_headline VARCHAR,
  launch_date DATE,
  preview_link VARCHAR,
  post_link VARCHAR
)
"""


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def initialize_database(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(AD_DAILY_TABLE_SQL)
    con.execute(CREATIVE_TABLE_SQL)


def replace_run_rows(
    con: duckdb.DuckDBPyConnection,
    run_date: str,
    normalized_rows: list[dict[str, Any]],
    creative_rows: list[dict[str, Any]],
) -> None:
    initialize_database(con)
    con.execute("DELETE FROM ad_daily_metrics WHERE ingestion_run_date = ?", [run_date])
    con.execute("DELETE FROM creative_lookup WHERE ingestion_run_date = ?", [run_date])

    ad_columns = [
        "ingestion_run_date",
        "source_run_path",
        "report_date",
        "account_id",
        "account_name",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "objective",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "link_clicks",
        "outbound_clicks",
        "ctr",
        "cpc",
        "cpm",
        "results",
        "result_label",
        "cost_per_result",
        "purchase_count",
        "purchase_value",
        "purchase_roas",
        "video_3s_plays",
        "thruplays",
        "hook_rate",
        "hold_rate",
        "average_order_value",
        "creative_type",
        "creative_copy",
        "creative_headline",
        "launch_date",
        "preview_link",
        "post_link",
        "has_video_metrics",
        "tracking_confidence",
    ]
    placeholders = ", ".join(["?"] * len(ad_columns))
    insert_ad_sql = (
        f"INSERT INTO ad_daily_metrics ({', '.join(ad_columns)}) VALUES ({placeholders})"
    )
    con.executemany(insert_ad_sql, [_row_to_tuple(row, ad_columns) for row in normalized_rows])

    creative_columns = [
        "ingestion_run_date",
        "ad_id",
        "ad_name",
        "creative_type",
        "creative_copy",
        "creative_headline",
        "launch_date",
        "preview_link",
        "post_link",
    ]
    insert_creative_sql = (
        f"INSERT INTO creative_lookup ({', '.join(creative_columns)}) "
        f"VALUES ({', '.join(['?'] * len(creative_columns))})"
    )
    creative_insert_rows = [
        {
            "ingestion_run_date": run_date,
            **row,
        }
        for row in creative_rows
    ]
    if creative_insert_rows:
        con.executemany(
            insert_creative_sql, [_row_to_tuple(row, creative_columns) for row in creative_insert_rows]
        )


def fetch_run_rows(con: duckdb.DuckDBPyConnection, run_date: str) -> list[dict[str, Any]]:
    result = con.execute(
        """
        SELECT *
        FROM ad_daily_metrics
        WHERE ingestion_run_date = ?
        ORDER BY report_date, campaign_name, adset_name, ad_name
        """,
        [run_date],
    )
    columns = [item[0] for item in result.description]
    return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]


def _row_to_tuple(row: dict[str, Any], columns: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in columns)
