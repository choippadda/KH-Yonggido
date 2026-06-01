from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from core.cad_exporter import export_cad_dxf
from core.earthwork_integration import (
    apply_earthwork_to_facilities,
    collect_earthwork_output_files,
    resolve_earthwork_context,
)
from core.exporter import export_results
from core.reader import inspect_table_fields, inspect_vector_fields, read_table, read_vector
from core.rule_engine import build_rule_engine
from core.spatial_ops import (
    build_line_groups,
    build_register_order_from_temporary_area,
    compute_facility_segments,
    compute_permanent_intersection_from_facilities,
    compute_temporary_intersection_from_facilities,
)
from core.temporary_occupancy import (
    TemporaryOccupancySelection,
    apply_temporary_occupancy,
    get_default_selection,
    validate_selection,
)
from core.transformer import (
    aggregate_permanent_results,
    aggregate_temporary_area_results,
    aggregate_temporary_results,
    build_parcel_reference,
    build_output_tables,
    merge_results,
    standardize_boundary,
    standardize_code_df,
    standardize_facilities,
    standardize_parcels,
    standardize_sheets,
)
from core.validator import choose_analysis_crs, repair_geometries, reproject_layers, validate_field_mapping
from utils.helpers import load_json, now_timestamp, resolve_app_path


DEFAULT_CONFIG_PATH = resolve_app_path("config", "default_config.json", anchor_file=__file__, anchor_levels_up=2)


@dataclass
class AnalysisInputs:
    cadastral_path: str
    boundary_path: str | None
    facility_path: str
    code_table_path: str
    sheet_path: str | None
    output_dir: str
    field_mapping: dict[str, dict[str, str | None]]
    temporary_selection: TemporaryOccupancySelection
    main_line_reverse: bool = False
    earthwork_workbook_path: str | None = None
    pipe_workbook_path: str | None = None
    earthwork_final_input_path: str | None = None
    earthwork_line_registry_path: str | None = None


@dataclass
class AnalysisResult:
    source_table: pd.DataFrame
    data_table: pd.DataFrame
    error_table: pd.DataFrame
    output_files: dict[str, str]
    analysis_crs: str
    cad_crs: str


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_legacy_config(config: dict[str, Any]) -> dict[str, Any]:
    field_mapping_definitions = config.get("field_mapping_definitions", {})
    if "code_csv" in field_mapping_definitions and "code_table" not in field_mapping_definitions:
        field_mapping_definitions["code_table"] = field_mapping_definitions.pop("code_csv")

    saved_field_mapping = config.get("saved_field_mapping", {})
    if "code_csv" in saved_field_mapping and "code_table" not in saved_field_mapping:
        saved_field_mapping["code_table"] = saved_field_mapping.pop("code_csv")
    return config


def load_config(config_path: str | None = None) -> dict[str, Any]:
    config = load_json(DEFAULT_CONFIG_PATH)
    if config_path:
        config = _deep_merge_dict(config, load_json(config_path))
    return _normalize_legacy_config(config)


def build_initial_field_mapping(
    config: dict[str, Any],
    columns_by_source: dict[str, list[str]],
) -> dict[str, dict[str, str | None]]:
    from utils.helpers import guess_field_name

    mapping: dict[str, dict[str, str | None]] = {}
    for source_name, definitions in config["field_mapping_definitions"].items():
        source_columns = columns_by_source.get(source_name, [])
        mapping[source_name] = {}
        for field_key, meta in definitions.items():
            mapping[source_name][field_key] = guess_field_name(
                source_columns,
                meta.get("aliases", []),
            )
    return mapping


def inspect_input_columns(paths: dict[str, str], config: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "cadastral": inspect_vector_fields(paths["cadastral_path"]),
        "facility": inspect_vector_fields(paths["facility_path"]),
        "code_table": inspect_table_fields(paths["code_table_path"], config["csv"]["encodings"]),
    }


def _selected_columns(mapping: dict[str, str | None]) -> list[str]:
    columns = [column for column in mapping.values() if column]
    return list(dict.fromkeys(columns))


def run_pipeline(inputs: AnalysisInputs, config: dict[str, Any], logger) -> AnalysisResult:
    build_rule_engine(config, logger)
    validate_selection(inputs.temporary_selection, config)
    error_records: list[dict[str, Any]] = []
    earthwork_context = resolve_earthwork_context(inputs, config, logger)

    columns_by_source = inspect_input_columns(
        {
            "cadastral_path": inputs.cadastral_path,
            "facility_path": inputs.facility_path,
            "code_table_path": inputs.code_table_path,
        },
        config,
    )
    validated_mapping = validate_field_mapping(
        inputs.field_mapping,
        columns_by_source,
        config["field_mapping_definitions"],
    )

    raw_parcels = read_vector(
        inputs.cadastral_path,
        "지적도",
        logger,
        columns=_selected_columns(validated_mapping["cadastral"]),
    )
    if inputs.boundary_path:
        raw_boundary = read_vector(inputs.boundary_path, "도로/점용 경계", logger)
    else:
        logger.info("도로/점용 경계 SHP가 선택되지 않아 해당 분석은 생략합니다.")
        raw_boundary = gpd.GeoDataFrame(geometry=[], crs=raw_parcels.crs)
    raw_facilities = read_vector(
        inputs.facility_path,
        "관로",
        logger,
        columns=_selected_columns(validated_mapping["facility"]),
    )
    code_table = read_table(
        inputs.code_table_path,
        logger,
        config["csv"]["encodings"],
        usecols=_selected_columns(validated_mapping["code_table"]),
    )
    if inputs.sheet_path:
        raw_sheets = read_vector(inputs.sheet_path, "도곽", logger)
        if raw_sheets.crs is None and raw_facilities.crs is not None:
            raw_sheets = raw_sheets.set_crs(raw_facilities.crs, allow_override=True)
            logger.info("도곽 DXF에 좌표계 정보가 없어 상수관로 좌표계 %s를 적용합니다.", raw_facilities.crs.to_string())
    else:
        logger.info("도곽이 선택되지 않아 CAD 배치 보조와 도곽 출력은 생략합니다.")
        raw_sheets = gpd.GeoDataFrame(geometry=[], crs=raw_facilities.crs or raw_parcels.crs)
    parcels = standardize_parcels(raw_parcels, validated_mapping["cadastral"])
    boundary = standardize_boundary(raw_boundary)
    facilities = standardize_facilities(raw_facilities, validated_mapping["facility"])
    facilities = apply_earthwork_to_facilities(
        facilities,
        earthwork_context,
        inputs.temporary_selection,
        logger,
    )
    sheets = standardize_sheets(raw_sheets, raw_facilities.crs or raw_parcels.crs, logger)
    codes = standardize_code_df(code_table, validated_mapping["code_table"])
    logger.info("지적도는 PNU와 지번/JIBUN 기준으로 행정구역, 지번, 지목 정리를 진행합니다.")

    missing_pnu_mask = parcels["pnu"].isna()
    if missing_pnu_mask.any():
        missing_count = int(missing_pnu_mask.sum())
        logger.warning("PNU가 없는 지적도 객체 %s건을 제외합니다.", missing_count)
        for index_value in parcels.loc[missing_pnu_mask].index:
            error_records.append(
                {
                    "레이어": "지적도",
                    "원본ID": str(parcels.at[index_value, "source_row_id"]),
                    "오류내용": "PNU 누락",
                }
            )
        parcels = parcels.loc[~missing_pnu_mask].copy()

    duplicate_pnu_count = int(parcels["pnu"].duplicated().sum())
    if duplicate_pnu_count:
        logger.warning("지적도에서 중복 PNU %s건을 확인했습니다. 집계 단계에서 합산 처리합니다.", duplicate_pnu_count)

    layers = {
        "지적도": parcels,
        "도로/점용 경계": boundary,
        "관로": facilities,
        "도곽": sheets,
    }

    repaired_layers = {}
    for layer_name, layer in layers.items():
        repaired, layer_errors = repair_geometries(layer, layer_name, logger)
        repaired_layers[layer_name] = repaired
        error_records.extend(layer_errors)

    analysis_crs = choose_analysis_crs(
        repaired_layers,
        fallback_epsg=int(config["analysis"]["fallback_epsg"]),
        warn_on_geographic_crs=bool(config["analysis"]["warn_on_geographic_crs"]),
        logger=logger,
    )
    logger.info("분석 좌표계는 %s입니다.", analysis_crs.to_string())
    projected_layers = reproject_layers(repaired_layers, analysis_crs, logger)
    cad_output_crs = raw_facilities.crs if raw_facilities.crs is not None else analysis_crs
    if raw_facilities.crs is None:
        logger.warning("상수관로 좌표계를 확인할 수 없어 CAD DXF는 분석 좌표계로 출력합니다.")
    else:
        logger.info("CAD DXF 출력 좌표계는 상수관로 좌표계 %s입니다.", cad_output_crs.to_string())
    projected_layers["지적도"] = projected_layers["지적도"].copy()
    projected_layers["지적도"]["ledger_area_m2"] = projected_layers["지적도"].geometry.area
    parcel_reference_df = build_parcel_reference(
        projected_layers["지적도"],
        round_digits=int(config["analysis"]["round_digits"]),
        logger=logger,
    )

    facility_segments = compute_facility_segments(
        projected_layers["관로"],
        projected_layers["도로/점용 경계"],
        projected_layers["지적도"],
        logger,
    )

    permanent_intersection = compute_permanent_intersection_from_facilities(
        projected_layers["관로"],
        projected_layers["지적도"],
        projected_layers["도로/점용 경계"],
        config,
        logger,
    )
    permanent_df = aggregate_permanent_results(
        permanent_intersection,
        round_digits=int(config["analysis"]["round_digits"]),
        logger=logger,
    )

    temp_segments = apply_temporary_occupancy(
        facility_segments,
        inputs.temporary_selection,
        config,
        logger,
    )
    temporary_df = aggregate_temporary_results(
        temp_segments,
        round_digits=int(config["analysis"]["round_digits"]),
        logger=logger,
    )
    temporary_intersection = compute_temporary_intersection_from_facilities(
        projected_layers["관로"],
        projected_layers["지적도"],
        projected_layers["도로/점용 경계"],
        inputs.temporary_selection,
        config,
        logger,
    )
    temporary_area_df = aggregate_temporary_area_results(
        temporary_intersection,
        round_digits=int(config["analysis"]["round_digits"]),
        logger=logger,
    )
    temporary_df = temporary_df.merge(temporary_area_df, on="pnu", how="outer", suffixes=("", "_overlay"))
    if "temporary_area_m2_overlay" in temporary_df.columns:
        temporary_df["temporary_area_m2"] = temporary_df["temporary_area_m2_overlay"].combine_first(
            temporary_df.get("temporary_area_m2")
        )
        temporary_df.drop(columns=["temporary_area_m2_overlay"], inplace=True)

    line_groups = build_line_groups(projected_layers["관로"], inputs.main_line_reverse, logger)
    register_order_df = build_register_order_from_temporary_area(
        temporary_intersection,
        line_groups,
        projected_layers["도곽"],
        logger,
    )

    combined = merge_results(parcel_reference_df, permanent_df, temporary_df, codes, config)
    if not register_order_df.empty:
        combined = combined.merge(register_order_df, on="pnu", how="left")
    source_table, data_table = build_output_tables(combined, config)

    if source_table.empty:
        logger.warning("교차 결과가 없어 조서 결과가 비어 있습니다.")
    else:
        logger.info(
            "최종 산출 완료: 편입 필지 %s건, 영구점용면적 합계 %.2f㎡, 임시점용면적 합계 %.2f㎡",
            len(source_table),
            float(source_table["영구점용면적"].fillna(0).sum()),
            float(source_table["임시점용면적"].fillna(0).sum()),
        )

    error_table = pd.DataFrame(error_records)
    if error_table.empty:
        error_table = pd.DataFrame(columns=config["output"]["error_columns"])
    else:
        error_table = error_table.reindex(columns=config["output"]["error_columns"])

    base_name = f"{config['output']['file_prefix']}_{now_timestamp()}"
    output_files = export_results(
        inputs.output_dir,
        source_table,
        data_table,
        error_table,
        config,
        logger,
        base_name=base_name,
    )
    cad_permanent = permanent_intersection.to_crs(cad_output_crs) if not permanent_intersection.empty else permanent_intersection
    cad_temporary = temporary_intersection.to_crs(cad_output_crs) if not temporary_intersection.empty else temporary_intersection
    cad_sheets = projected_layers["도곽"].to_crs(cad_output_crs) if not projected_layers["도곽"].empty else projected_layers["도곽"]
    cad_parcels = projected_layers["지적도"].to_crs(cad_output_crs) if not projected_layers["지적도"].empty else projected_layers["지적도"]
    output_files.update(
        export_cad_dxf(
            inputs.output_dir,
            base_name,
            cad_permanent,
            cad_temporary,
            cad_sheets,
            cad_parcels,
            source_table,
            cad_output_crs,
            config,
            logger,
        )
    )
    output_files.update(collect_earthwork_output_files(earthwork_context))

    return AnalysisResult(
        source_table=source_table,
        data_table=data_table,
        error_table=error_table,
        output_files=output_files,
        analysis_crs=analysis_crs.to_string(),
        cad_crs=cad_output_crs.to_string(),
    )


def build_inputs_from_config(
    paths: dict[str, str],
    output_dir: str,
    field_mapping: dict[str, dict[str, str | None]],
    config: dict[str, Any],
) -> AnalysisInputs:
    defaults = get_default_selection(config)
    return AnalysisInputs(
        cadastral_path=paths["cadastral_path"],
        boundary_path=paths["boundary_path"],
        facility_path=paths["facility_path"],
        code_table_path=paths["code_table_path"],
        sheet_path=paths.get("sheet_path"),
        output_dir=output_dir,
        field_mapping=field_mapping,
        temporary_selection=defaults,
        main_line_reverse=False,
        earthwork_workbook_path=None,
        pipe_workbook_path=None,
        earthwork_final_input_path=None,
        earthwork_line_registry_path=None,
    )
