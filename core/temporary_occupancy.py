from __future__ import annotations

from dataclasses import dataclass
import math

import geopandas as gpd

from utils.helpers import coerce_to_float, meters_from_diameter


@dataclass(frozen=True)
class TemporaryOccupancySelection:
    soil_cover_m: float
    pavement_status: str
    excavation_slope: str


def get_default_selection(config: dict) -> TemporaryOccupancySelection:
    defaults = config["temporary_occupancy"]["defaults"]
    return TemporaryOccupancySelection(
        soil_cover_m=float(defaults["soil_cover_m"]),
        pavement_status=str(defaults["pavement_status"]),
        excavation_slope=str(defaults["excavation_slope"]),
    )


def validate_selection(selection: TemporaryOccupancySelection, config: dict) -> None:
    temp_config = config["temporary_occupancy"]
    if float(selection.soil_cover_m) not in [float(value) for value in temp_config["soil_cover_options_m"]]:
        raise ValueError(f"허용되지 않은 토피 값입니다: {selection.soil_cover_m}")
    if selection.pavement_status not in temp_config["pavement_options"]:
        raise ValueError(f"허용되지 않은 포장상태입니다: {selection.pavement_status}")
    if selection.excavation_slope not in temp_config["excavation_slope_options"]:
        raise ValueError(f"허용되지 않은 터파기 기울기입니다: {selection.excavation_slope}")


def parse_excavation_slope_ratio(value: str) -> float:
    text = str(value).strip()
    if ":" not in text:
        raise ValueError(f"터파기 기울기 형식이 올바르지 않습니다: {value}")
    _, right = text.split(":", 1)
    return float(right)


def _round_up_to_tenth(value: float) -> float:
    return math.ceil(value * 10 - 1e-9) / 10


def calculate_temporary_width(
    diameter_value,
    selection: TemporaryOccupancySelection,
    config: dict,
) -> float:
    validate_selection(selection, config)
    temp_config = config["temporary_occupancy"]
    defaults = temp_config["defaults"]
    pipe_side_clearance = float(temp_config.get("pipe_side_clearance_m", 0.4))
    reference_depth = float(temp_config.get("reference_depth_m", 0.9))
    pavement_adjust = float(temp_config["pavement_width_adjustment_m"][selection.pavement_status])
    default_soil_cover = float(defaults["soil_cover_m"])
    diameter_m = meters_from_diameter(
        diameter_value,
        default=float(temp_config.get("minimum_pipe_diameter_m", 0.0)),
    )
    slope_ratio = parse_excavation_slope_ratio(selection.excavation_slope)
    cover_delta = float(selection.soil_cover_m) - default_soil_cover
    effective_depth = max(0.0, reference_depth + diameter_m + cover_delta)
    trench_bottom_width = max(diameter_m, diameter_m + pipe_side_clearance + pavement_adjust)
    width = trench_bottom_width + (2 * slope_ratio * effective_depth)
    if bool(temp_config.get("round_up_to_0_1m", True)):
        return _round_up_to_tenth(width)
    return width


def _resolve_segment_length(row) -> float | None:
    geometry = row.geometry
    if geometry is not None and not geometry.is_empty and geometry.geom_type in {
        "LineString",
        "MultiLineString",
        "LinearRing",
    }:
        return float(geometry.length)

    length_attr = coerce_to_float(row.get("facility_length_attr"))
    if length_attr is not None and length_attr > 0:
        return length_attr
    return None


def _resolve_row_selection(
    row,
    default_selection: TemporaryOccupancySelection,
    config: dict,
) -> TemporaryOccupancySelection:
    temp_config = config["temporary_occupancy"]

    soil_cover_value = coerce_to_float(row.get("soil_cover_m"))
    if soil_cover_value is None:
        soil_cover_value = float(default_selection.soil_cover_m)

    pavement_status = row.get("pavement_status") or default_selection.pavement_status
    if pavement_status not in temp_config["pavement_options"]:
        pavement_status = default_selection.pavement_status

    excavation_slope = row.get("excavation_slope") or default_selection.excavation_slope
    if excavation_slope not in temp_config["excavation_slope_options"]:
        excavation_slope = default_selection.excavation_slope

    return TemporaryOccupancySelection(
        soil_cover_m=float(soil_cover_value),
        pavement_status=str(pavement_status),
        excavation_slope=str(excavation_slope),
    )


def apply_temporary_occupancy(
    segments: gpd.GeoDataFrame,
    selection: TemporaryOccupancySelection,
    config: dict,
    logger,
) -> gpd.GeoDataFrame:
    validate_selection(selection, config)

    if segments.empty:
        logger.warning("임시 점용 산정 대상 관로 구간이 없습니다.")
        result = segments.copy()
        result["facility_segment_length_m"] = []
        result["temporary_width_m"] = []
        result["temporary_area_m2"] = []
        return result

    logger.info(
        "임시 점용 산출 조건을 적용합니다. 토피=%sm, 포장상태=%s, 터파기 기울기=%s (QGIS 기준식 보정)",
        selection.soil_cover_m,
        selection.pavement_status,
        selection.excavation_slope,
    )

    working = segments.copy()
    working["facility_segment_length_m"] = working.apply(_resolve_segment_length, axis=1)
    missing_length_mask = working["facility_segment_length_m"].isna()
    if missing_length_mask.any():
        logger.warning(
            "관로 연장을 계산하지 못한 객체 %s건을 제외합니다.",
            int(missing_length_mask.sum()),
        )
    working = working.loc[~missing_length_mask].copy()

    if working.empty:
        logger.warning("연장을 계산할 수 있는 관로 구간이 없어 임시 점용 산정을 종료합니다.")
        return working

    if "temporary_allowed" in working.columns:
        allowed_mask = working["temporary_allowed"].fillna(True).astype(bool)
        if (~allowed_mask).any():
            logger.info("임시점용 제외 구간 %s건은 산정 대상에서 제외합니다.", int((~allowed_mask).sum()))
        working = working.loc[allowed_mask].copy()

    if working.empty:
        logger.warning("임시 점용 적용 대상 구간이 없어 산정을 종료합니다.")
        return working

    row_selections = working.apply(lambda row: _resolve_row_selection(row, selection, config), axis=1)
    width_override = working.get("temporary_width_m")
    if width_override is not None:
        width_override = width_override.apply(coerce_to_float)

    temporary_widths: list[float] = []
    soil_cover_values: list[float] = []
    pavement_values: list[str] = []
    slope_values: list[str] = []
    for index_value, row_selection in row_selections.items():
        override_value = None if width_override is None else width_override.loc[index_value]
        if override_value is None:
            override_value = calculate_temporary_width(working.at[index_value, "diameter_value"], row_selection, config)
        temporary_widths.append(float(override_value))
        soil_cover_values.append(float(row_selection.soil_cover_m))
        pavement_values.append(str(row_selection.pavement_status))
        slope_values.append(str(row_selection.excavation_slope))

    working["temporary_width_m"] = temporary_widths
    working["temporary_area_m2"] = working["facility_segment_length_m"] * working["temporary_width_m"]
    working["soil_cover_m"] = soil_cover_values
    working["pavement_status"] = pavement_values
    working["excavation_slope"] = slope_values

    logger.info(
        "임시 점용 폭 산정 완료: 최소 %.2fm / 최대 %.2fm, 임시 점용 면적 합계 %.2f㎡",
        float(working["temporary_width_m"].min()),
        float(working["temporary_width_m"].max()),
        float(working["temporary_area_m2"].sum()),
    )
    return working
