from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .models import Matrix3, OptimizationHistoryRecord, OptimizationResult
from .settings import application_data_dir, existing_application_data_file


HISTORY_VERSION = 1


def record_from_result(
    result: OptimizationResult,
    *,
    dataset_name: str,
    region_label: str,
    xml_diff: str = "",
) -> OptimizationHistoryRecord:
    rates = result.pass_rates
    index = rates.thresholds.index(3.0)
    return OptimizationHistoryRecord(
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        dataset_name=dataset_name,
        region_label=region_label,
        strategy=result.strategy,
        mean_before=result.mean_before,
        mean_after=result.mean_after,
        pass_rate_before_3=rates.before_rate(index),
        pass_rate_after_3=rates.after_rate(index),
        matrix_status=result.matrix_health.status,
        optimized_matrix=result.optimized_matrix,
        search_method=result.search_method,
        xml_diff=xml_diff,
    )


def _record_to_dict(record: OptimizationHistoryRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp,
        "dataset_name": record.dataset_name,
        "region_label": record.region_label,
        "strategy": record.strategy,
        "mean_before": record.mean_before,
        "mean_after": record.mean_after,
        "pass_rate_before_3": record.pass_rate_before_3,
        "pass_rate_after_3": record.pass_rate_after_3,
        "matrix_status": record.matrix_status,
        "optimized_matrix": [list(row) for row in record.optimized_matrix],
        "search_method": record.search_method,
        "xml_diff": record.xml_diff,
    }


def _matrix(payload: Any) -> Matrix3:
    if not isinstance(payload, list) or len(payload) != 3:
        raise ValueError("invalid history matrix")
    rows = [tuple(float(value) for value in row) for row in payload]
    if any(len(row) != 3 for row in rows):
        raise ValueError("invalid history matrix")
    return tuple(rows)  # type: ignore[return-value]


def _record_from_dict(payload: dict[str, Any]) -> OptimizationHistoryRecord:
    return OptimizationHistoryRecord(
        timestamp=str(payload["timestamp"]),
        dataset_name=str(payload["dataset_name"]),
        region_label=str(payload.get("region_label", "")),
        strategy=str(payload.get("strategy", "balanced")),
        mean_before=float(payload["mean_before"]),
        mean_after=float(payload["mean_after"]),
        pass_rate_before_3=float(payload["pass_rate_before_3"]),
        pass_rate_after_3=float(payload["pass_rate_after_3"]),
        matrix_status=str(payload["matrix_status"]),
        optimized_matrix=_matrix(payload["optimized_matrix"]),
        search_method=str(payload.get("search_method", "")),
        xml_diff=str(payload.get("xml_diff", "")),
    )


def load_history(path: Optional[Union[str, Path]] = None) -> list[OptimizationHistoryRecord]:
    history_path = Path(path) if path is not None else existing_application_data_file("history.json")
    if not history_path.exists():
        return []
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        rows = payload.get("records", []) if isinstance(payload, dict) else []
        return [_record_from_dict(row) for row in rows if isinstance(row, dict)]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def save_history(
    records: list[OptimizationHistoryRecord],
    path: Optional[Union[str, Path]] = None,
    *,
    limit: int = 200,
) -> Path:
    history_path = Path(path) if path is not None else application_data_dir() / "history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": HISTORY_VERSION, "records": [_record_to_dict(row) for row in records[-limit:]]}
    temporary = history_path.with_suffix(history_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(history_path)
    return history_path
