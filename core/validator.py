from __future__ import annotations

from typing import Any

import geopandas as gpd
from pyproj import CRS

try:
    from shapely import make_valid as shapely_make_valid
except ImportError:  # pragma: no cover
    try:
        from shapely.validation import make_valid as shapely_make_valid
    except ImportError:  # pragma: no cover
        shapely_make_valid = None


class ValidationError(Exception):
    pass


def validate_field_mapping(
    field_mapping: dict[str, dict[str, str | None]],
    columns_by_source: dict[str, list[str]],
    mapping_definitions: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, str | None]]:
    validated: dict[str, dict[str, str | None]] = {}
    for source_name, definition in mapping_definitions.items():
        source_mapping = field_mapping.get(source_name, {})
        source_columns = set(columns_by_source.get(source_name, []))
        validated[source_name] = {}
        for field_key, meta in definition.items():
            selected_column = source_mapping.get(field_key) or None
            if meta.get("required") and not selected_column:
                raise ValidationError(
                    f"{source_name} 데이터의 '{meta['label']}' 필드 매핑이 필요합니다."
                )
            if selected_column and selected_column not in source_columns:
                raise ValidationError(
                    f"{source_name} 데이터에서 '{selected_column}' 컬럼을 찾을 수 없습니다."
                )
            validated[source_name][field_key] = selected_column
    return validated


def repair_geometries(
    gdf: gpd.GeoDataFrame,
    layer_name: str,
    logger,
) -> tuple[gpd.GeoDataFrame, list[dict[str, Any]]]:
    working = gdf.copy()
    error_records: list[dict[str, Any]] = []
    geometry_name = working.geometry.name

    def source_id_for(index_value: Any) -> str:
        if "source_row_id" in working.columns:
            return str(working.at[index_value, "source_row_id"])
        return str(index_value)

    invalid_mask = working.geometry.notna() & ~working.geometry.is_valid
    if invalid_mask.any():
        invalid_count = int(invalid_mask.sum())
        logger.warning(
            "%s 레이어에서 geometry invalid 객체 %s건을 보정 시도합니다.",
            layer_name,
            invalid_count,
        )
        for index_value in working.loc[invalid_mask].index:
            error_records.append(
                {
                    "레이어": layer_name,
                    "원본ID": source_id_for(index_value),
                    "오류내용": "geometry invalid -> 보정 시도",
                }
            )

        def repair(geometry):
            if geometry is None or geometry.is_empty or geometry.is_valid:
                return geometry
            if shapely_make_valid is not None:
                try:
                    return shapely_make_valid(geometry)
                except Exception:
                    return geometry.buffer(0)
            return geometry.buffer(0)

        working.loc[invalid_mask, geometry_name] = working.loc[invalid_mask, geometry_name].apply(repair)

    empty_mask = working.geometry.isna() | working.geometry.is_empty
    for index_value in working.loc[empty_mask].index:
        error_records.append(
            {
                "레이어": layer_name,
                "원본ID": source_id_for(index_value),
                "오류내용": "빈 geometry",
            }
        )

    still_invalid_mask = working.geometry.notna() & ~working.geometry.is_valid
    for index_value in working.loc[still_invalid_mask].index:
        error_records.append(
            {
                "레이어": layer_name,
                "원본ID": source_id_for(index_value),
                "오류내용": "geometry 보정 실패",
            }
        )

    keep_mask = ~(empty_mask | still_invalid_mask)
    dropped_count = int((~keep_mask).sum())
    if dropped_count:
        logger.warning("%s 레이어에서 %s건을 분석 대상에서 제외했습니다.", layer_name, dropped_count)

    return working.loc[keep_mask].copy(), error_records


def choose_analysis_crs(
    layers: dict[str, gpd.GeoDataFrame],
    fallback_epsg: int,
    warn_on_geographic_crs: bool,
    logger,
) -> CRS:
    for layer_name, layer in layers.items():
        if layer.crs is None:
            raise ValidationError(f"{layer_name} 레이어에 좌표계 정보가 없습니다.")

    # 관로 중심선 기준 버퍼와 CAD 중첩 정합을 위해 관로 좌표계를 우선 사용한다.
    preferred_order = ["관로", "지적도", "도로/점용 경계"]
    for layer_name in preferred_order:
        layer = layers.get(layer_name)
        if layer is None:
            continue
        crs = CRS.from_user_input(layer.crs)
        if crs.is_projected:
            return crs

    for layer in layers.values():
        crs = CRS.from_user_input(layer.crs)
        if crs.is_projected:
            return crs

    reference = next(iter(layers.values()))
    if warn_on_geographic_crs:
        logger.warning(
            "입력 레이어가 모두 경위도 좌표계입니다. 면적 계산을 위해 투영 좌표계로 변환합니다."
        )

    try:
        estimated = reference.estimate_utm_crs()
        if estimated:
            logger.info("자동 추정된 투영 좌표계를 사용합니다: %s", estimated)
            return CRS.from_user_input(estimated)
    except Exception:
        logger.warning(
            "자동 UTM 추정에 실패하여 기본 분석 좌표계 EPSG:%s를 사용합니다.",
            fallback_epsg,
        )

    return CRS.from_epsg(fallback_epsg)


def reproject_layers(
    layers: dict[str, gpd.GeoDataFrame],
    analysis_crs: CRS,
    logger,
) -> dict[str, gpd.GeoDataFrame]:
    projected: dict[str, gpd.GeoDataFrame] = {}
    for layer_name, layer in layers.items():
        layer_crs = CRS.from_user_input(layer.crs)
        if layer_crs != analysis_crs:
            logger.info(
                "%s 레이어 좌표계를 %s로 변환합니다.",
                layer_name,
                analysis_crs.to_string(),
            )
            projected[layer_name] = layer.to_crs(analysis_crs)
        else:
            projected[layer_name] = layer.copy()
    return projected
