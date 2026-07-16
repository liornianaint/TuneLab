from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .models import GammaOptimizationResult
from ..ccm.settings import application_data_dir, existing_application_data_file


GAMMA_HISTORY_VERSION = 1


@dataclass(frozen=True)
class GammaHistoryRecord:
    timestamp: str
    dataset_name: str
    xml_name: str
    region_label: str
    target_gamma_factor: float
    target_steps: int
    before_steps: int
    after_steps: int
    rmse_before: float
    rmse_after: float
    curve_status: str
    lut_length: int
    maximum_value: int
    applied_strength: float
    xml_diff: str = ""


def record_gamma_result(
    result: GammaOptimizationResult,
    *,
    dataset_name: str,
    xml_name: str,
    region_label: str,
    xml_diff: str = "",
) -> GammaHistoryRecord:
    metrics = result.metrics
    return GammaHistoryRecord(
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        dataset_name=dataset_name,
        xml_name=xml_name,
        region_label=region_label,
        target_gamma_factor=result.target_gamma_factor,
        target_steps=result.requested_step_count,
        before_steps=metrics.distinguishable_before,
        after_steps=metrics.distinguishable_after,
        rmse_before=metrics.rmse_before,
        rmse_after=metrics.rmse_after,
        curve_status=result.health.status,
        lut_length=result.lut_length,
        maximum_value=result.maximum_value,
        applied_strength=result.applied_strength,
        xml_diff=xml_diff,
    )


def _record_from_dict(payload: dict[str, Any]) -> GammaHistoryRecord:
    return GammaHistoryRecord(
        timestamp=str(payload["timestamp"]),
        dataset_name=str(payload["dataset_name"]),
        xml_name=str(payload.get("xml_name", "")),
        region_label=str(payload.get("region_label", "")),
        target_gamma_factor=float(payload.get("target_gamma_factor", 1.0)),
        target_steps=int(payload.get("target_steps", 0)),
        before_steps=int(payload.get("before_steps", 0)),
        after_steps=int(payload.get("after_steps", 0)),
        rmse_before=float(payload.get("rmse_before", 0.0)),
        rmse_after=float(payload.get("rmse_after", 0.0)),
        curve_status=str(payload.get("curve_status", "WARNING")),
        lut_length=int(payload.get("lut_length", 0)),
        maximum_value=int(payload.get("maximum_value", 0)),
        applied_strength=float(payload.get("applied_strength", 0.0)),
        xml_diff=str(payload.get("xml_diff", "")),
    )


def load_gamma_history(path: Optional[Union[str, Path]] = None) -> list[GammaHistoryRecord]:
    history_path = Path(path) if path is not None else existing_application_data_file("gamma_history.json")
    if not history_path.exists():
        return []
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        rows = payload.get("records", []) if isinstance(payload, dict) else []
        return [_record_from_dict(row) for row in rows if isinstance(row, dict)]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def save_gamma_history(
    records: list[GammaHistoryRecord],
    path: Optional[Union[str, Path]] = None,
    *,
    limit: int = 200,
) -> Path:
    history_path = Path(path) if path is not None else application_data_dir() / "gamma_history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": GAMMA_HISTORY_VERSION,
        "records": [asdict(record) for record in records[-limit:]],
    }
    temporary = history_path.with_suffix(history_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(history_path)
    return history_path
