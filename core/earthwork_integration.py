from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import linemerge, substring

from core.earthwork_preprocessor_v2 import EarthworkPrepInputs, run_earthwork_preprocessor
from core.reader import read_table
from core.temporary_occupancy import TemporaryOccupancySelection
from utils.helpers import coerce_to_float


EARTHWORK_REQUIRED_FINAL_COLUMNS = {
    "라인명",
    "시점거리(m)",
    "종점거리(m)",
    "시공유형",
    "임시점용적용",
    "영구점용적용",
}
EARTHWORK_REQUIRED_LINE_COLUMNS = {
    "라인명",
    "SHP후보FID",
    "SHP매칭상태",
}
LINE_GEOMETRY_TYPES = {
    "LineString",
    "MultiLineString",
    "LinearRing",
}
GEOMETRY_TOLERANCE = 1e-8


class EarthworkIntegrationError(ValueError):
    pass


@dataclass
class EarthworkContext:
    final_input_table: pd.DataFrame
    line_registry_table: pd.DataFrame
    output_files: dict[str, str]
    source_mode: str


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\ufeff", "").strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _coerce_flag(value: Any, default: bool) -> bool:
    text = _clean_text(value)
    if text is None:
        return default
    if text in {"예", "Y", "YES", "True", "true", "1"}:
        return True
    if text in {"아니오", "N", "NO", "False", "false", "0"}:
        return False
    return default


def _ensure_required_columns(frame: pd.DataFrame, required_columns: set[str], label: str) -> None:
    missing = [column_name for column_name in sorted(required_columns) if column_name not in frame.columns]
    if missing:
        raise EarthworkIntegrationError(f"{label}에 필수 컬럼이 없습니다: {', '.join(missing)}")


def _load_preprocessed_tables(
    final_input_path: str,
    line_registry_path: str,
    config: dict[str, Any],
    logger,
) -> EarthworkContext:
    final_input_table = read_table(final_input_path, logger, config["csv"]["encodings"])
    line_registry_table = read_table(line_registry_path, logger, config["csv"]["encodings"])
    _ensure_required_columns(final_input_table, EARTHWORK_REQUIRED_FINAL_COLUMNS, "산정입력")
    _ensure_required_columns(line_registry_table, EARTHWORK_REQUIRED_LINE_COLUMNS, "관로라인")
    return EarthworkContext(
        final_input_table=final_input_table,
        line_registry_table=line_registry_table,
        output_files={},
        source_mode="preprocessed",
    )


def resolve_earthwork_context(inputs, config: dict[str, Any], logger) -> EarthworkContext | None:
    has_raw_inputs = bool(inputs.earthwork_workbook_path or inputs.pipe_workbook_path)
    has_preprocessed_inputs = bool(inputs.earthwork_final_input_path or inputs.earthwork_line_registry_path)

    if has_raw_inputs and has_preprocessed_inputs:
        raise EarthworkIntegrationError(
            "토공 원본 입력과 전처리 결과 재사용 입력을 동시에 선택할 수 없습니다. 한 가지 방식만 선택해 주세요."
        )

    if has_raw_inputs:
        if not inputs.earthwork_workbook_path or not inputs.pipe_workbook_path:
            raise EarthworkIntegrationError("토공 원본 자동연결을 사용하려면 토공 엑셀과 관로공 엑셀을 모두 선택해 주세요.")

        logger.info("토공 원본 파일을 내부 전처리 후 본 산정기에 연결합니다.")
        result = run_earthwork_preprocessor(
            EarthworkPrepInputs(
                workbook_path=inputs.earthwork_workbook_path,
                output_dir=inputs.output_dir,
                pipe_workbook_path=inputs.pipe_workbook_path,
                pipe_shapefile_path=inputs.facility_path,
            ),
            config,
            logger,
        )
        return EarthworkContext(
            final_input_table=result.final_input_table,
            line_registry_table=result.line_registry_table,
            output_files=result.output_files,
            source_mode="raw",
        )

    if has_preprocessed_inputs:
        if not inputs.earthwork_final_input_path or not inputs.earthwork_line_registry_path:
            raise EarthworkIntegrationError(
                "전처리 결과를 재사용하려면 산정입력 파일과 관로라인 파일을 모두 선택해 주세요."
            )
        logger.info("기존 전처리 결과를 읽어 본 산정기에 연결합니다.")
        return _load_preprocessed_tables(
            inputs.earthwork_final_input_path,
            inputs.earthwork_line_registry_path,
            config,
            logger,
        )

    return None


def _parse_candidate_feature_ids(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [candidate.strip() for candidate in text.split(",") if candidate.strip()]


def _build_feature_line_mapping(line_registry_table: pd.DataFrame, logger) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    duplicate_feature_ids: set[str] = set()
    unresolved_line_names: list[str] = []

    for row in line_registry_table.to_dict("records"):
        line_name = _clean_text(row.get("라인명"))
        match_status = _clean_text(row.get("SHP매칭상태"))
        candidate_ids = _parse_candidate_feature_ids(row.get("SHP후보FID"))
        if not line_name:
            continue
        if match_status != "단일일치" or len(candidate_ids) != 1:
            unresolved_line_names.append(line_name)
            continue

        feature_id = candidate_ids[0]
        if feature_id in mapping:
            duplicate_feature_ids.add(feature_id)
            continue

        mapping[feature_id] = {
            "line_name": line_name,
            "line_type": _clean_text(row.get("라인유형")),
            "parent_line_name": _clean_text(row.get("상위라인명")),
            "matched_feature_id": feature_id,
            "base_length_m": coerce_to_float(row.get("기준연장(m)")) or coerce_to_float(row.get("연장(m)")),
            "diameter_mm": coerce_to_float(row.get("적용관경(mm)")),
        }

    if unresolved_line_names:
        logger.warning(
            "SHP 단일매칭이 아닌 라인 %s건은 토공 조건 자동연결에서 제외하고 기본 산정값으로 처리합니다.",
            len(unresolved_line_names),
        )
    if duplicate_feature_ids:
        logger.warning(
            "하나의 관로 FID에 여러 라인이 대응되어 자동연결에서 제외한 항목 %s건이 있습니다.",
            len(duplicate_feature_ids),
        )
        for feature_id in duplicate_feature_ids:
            mapping.pop(feature_id, None)

    return mapping


def _normalize_linear_geometry(geometry) -> LineString | None:
    if geometry is None or geometry.is_empty:
        return None

    if geometry.geom_type == "LineString":
        return geometry if geometry.length > GEOMETRY_TOLERANCE else None
    if geometry.geom_type == "LinearRing":
        line = LineString(list(geometry.coords))
        return line if line.length > GEOMETRY_TOLERANCE else None
    if geometry.geom_type == "MultiLineString":
        merged = linemerge(geometry)
        if merged.is_empty:
            return None
        if merged.geom_type == "LineString":
            return merged if merged.length > GEOMETRY_TOLERANCE else None
        parts = [part for part in merged.geoms if part.length > GEOMETRY_TOLERANCE]
        if not parts:
            return None
        return max(parts, key=lambda value: value.length)
    return None


def _build_default_segment_meta(
    feature_row: dict[str, Any],
    line_info: dict[str, Any] | None,
    selection: TemporaryOccupancySelection,
) -> dict[str, Any]:
    diameter_mm = coerce_to_float(feature_row.get("diameter_value"))
    if line_info is not None and line_info.get("diameter_mm") is not None:
        diameter_mm = float(line_info["diameter_mm"])

    return {
        "earthwork_line_name": None if line_info is None else line_info.get("line_name"),
        "earthwork_line_type": None if line_info is None else line_info.get("line_type"),
        "earthwork_parent_line_name": None if line_info is None else line_info.get("parent_line_name"),
        "construction_type": "개착",
        "temporary_allowed": True,
        "permanent_allowed": True,
        "soil_cover_m": float(selection.soil_cover_m),
        "pavement_status": selection.pavement_status,
        "excavation_slope": selection.excavation_slope,
        "work_environment": None,
        "earthwork_source": None,
        "earthwork_note": None,
        "diameter_value": diameter_mm,
        "temporary_width_m": None,
    }


def _build_interval_segments(
    line_geometry: LineString,
    feature_row: dict[str, Any],
    line_info: dict[str, Any] | None,
    line_rows: pd.DataFrame,
    selection: TemporaryOccupancySelection,
) -> list[dict[str, Any]]:
    line_length = float(line_geometry.length)
    if line_length <= GEOMETRY_TOLERANCE:
        return []

    default_meta = _build_default_segment_meta(feature_row, line_info, selection)
    interval_records: list[dict[str, Any]] = []
    breakpoints = {0.0, line_length}

    for row in line_rows.to_dict("records"):
        start_value = coerce_to_float(row.get("시점거리(m)"))
        end_value = coerce_to_float(row.get("종점거리(m)"))
        if start_value is None or end_value is None:
            continue
        start_value = max(0.0, min(float(start_value), line_length))
        end_value = max(0.0, min(float(end_value), line_length))
        if end_value <= start_value + GEOMETRY_TOLERANCE:
            continue

        interval_records.append(
            {
                "start_m": start_value,
                "end_m": end_value,
                "construction_type": _clean_text(row.get("시공유형")) or default_meta["construction_type"],
                "temporary_allowed": _coerce_flag(row.get("임시점용적용"), True),
                "permanent_allowed": _coerce_flag(row.get("영구점용적용"), True),
                "soil_cover_m": coerce_to_float(row.get("토피(m)")) or default_meta["soil_cover_m"],
                "pavement_status": _clean_text(row.get("산정포장상태")) or _clean_text(row.get("포장상태")) or default_meta["pavement_status"],
                "excavation_slope": _clean_text(row.get("터파기기울기")) or default_meta["excavation_slope"],
                "work_environment": _clean_text(row.get("시공환경")),
                "temporary_width_m": coerce_to_float(row.get("터파기폭(m)")),
                "diameter_value": coerce_to_float(row.get("적용관경(mm)")) or default_meta["diameter_value"],
                "earthwork_source": _clean_text(row.get("세그먼트출처")),
                "earthwork_note": _clean_text(row.get("비고")),
            }
        )
        breakpoints.add(start_value)
        breakpoints.add(end_value)

    if not interval_records:
        return [{**feature_row, **default_meta, "geometry": line_geometry}]

    ordered_breakpoints = sorted(breakpoints)
    records: list[dict[str, Any]] = []
    for start_value, end_value in zip(ordered_breakpoints[:-1], ordered_breakpoints[1:]):
        if end_value <= start_value + GEOMETRY_TOLERANCE:
            continue

        midpoint = (start_value + end_value) / 2.0
        matched_interval = next(
            (
                interval
                for interval in interval_records
                if interval["start_m"] <= midpoint <= interval["end_m"] + GEOMETRY_TOLERANCE
            ),
            None,
        )
        meta = default_meta if matched_interval is None else {**default_meta, **matched_interval}

        sub_geometry = substring(line_geometry, start_value, end_value)
        normalized_sub_geometry = _normalize_linear_geometry(sub_geometry)
        if normalized_sub_geometry is None:
            continue

        record = {**feature_row, **meta}
        record["geometry"] = normalized_sub_geometry
        record["segment_chainage_from_m"] = round(start_value, 6)
        record["segment_chainage_to_m"] = round(end_value, 6)
        record["facility_length_attr"] = round(float(normalized_sub_geometry.length), 6)
        records.append(record)

    return records


def apply_earthwork_to_facilities(
    facilities: gpd.GeoDataFrame,
    earthwork_context: EarthworkContext | None,
    selection: TemporaryOccupancySelection,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty:
        return facilities.copy()

    working = facilities.copy()
    default_columns = {
        "earthwork_line_name": None,
        "earthwork_line_type": None,
        "earthwork_parent_line_name": None,
        "construction_type": "개착",
        "temporary_allowed": True,
        "permanent_allowed": True,
        "soil_cover_m": float(selection.soil_cover_m),
        "pavement_status": selection.pavement_status,
        "excavation_slope": selection.excavation_slope,
        "work_environment": None,
        "earthwork_source": None,
        "earthwork_note": None,
        "segment_chainage_from_m": None,
        "segment_chainage_to_m": None,
    }
    for column_name, default_value in default_columns.items():
        if column_name not in working.columns:
            working[column_name] = default_value

    if earthwork_context is None:
        logger.info("토공 구간 입력이 없어 기존 전역 임시점용 조건을 그대로 사용합니다.")
        return working

    line_mapping = _build_feature_line_mapping(earthwork_context.line_registry_table, logger)
    if not line_mapping:
        logger.warning("토공 전처리 결과에서 자동연결 가능한 관로 라인이 없어 기존 전역 조건으로 산정합니다.")
        return working

    final_input = earthwork_context.final_input_table.copy()
    final_input["라인명"] = final_input["라인명"].map(_clean_text)
    final_input = final_input.loc[final_input["라인명"].notna()].copy()
    grouped_rows = {line_name: frame.copy() for line_name, frame in final_input.groupby("라인명", dropna=False)}

    records: list[dict[str, Any]] = []
    applied_feature_count = 0
    for feature_row in working.to_dict("records"):
        feature_id = str(feature_row.get("source_row_id"))
        line_info = line_mapping.get(feature_id)
        line_geometry = _normalize_linear_geometry(feature_row.get("geometry"))
        if line_geometry is None:
            continue

        feature_row["geometry"] = line_geometry
        if line_info is None:
            default_record = {**feature_row, **_build_default_segment_meta(feature_row, None, selection)}
            default_record["facility_length_attr"] = round(float(line_geometry.length), 6)
            records.append(default_record)
            continue

        line_rows = grouped_rows.get(str(line_info["line_name"]), pd.DataFrame())
        split_records = _build_interval_segments(line_geometry, feature_row, line_info, line_rows, selection)
        if split_records:
            applied_feature_count += 1
            records.extend(split_records)
        else:
            default_record = {**feature_row, **_build_default_segment_meta(feature_row, line_info, selection)}
            default_record["facility_length_attr"] = round(float(line_geometry.length), 6)
            records.append(default_record)

    result = gpd.GeoDataFrame(records, geometry="geometry", crs=working.crs)
    result = result.loc[result.geometry.notna() & ~result.geometry.is_empty].copy()
    logger.info(
        "토공 전처리 구간을 관로 geometry에 적용했습니다. 원본 관로 %s건 중 %s건에 구간조건을 연결했고, 최종 세그먼트는 %s건입니다.",
        len(working),
        applied_feature_count,
        len(result),
    )
    return result.reset_index(drop=True)


def collect_earthwork_output_files(earthwork_context: EarthworkContext | None) -> dict[str, str]:
    if earthwork_context is None or not earthwork_context.output_files:
        return {}
    return {f"earthwork_{key}": value for key, value in earthwork_context.output_files.items()}
