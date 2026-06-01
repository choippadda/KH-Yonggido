from __future__ import annotations

import re
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from utils.helpers import first_non_empty, round_or_none


SOURCE_TO_OUTPUT_COLUMN = {
    "register_no": "조서번호",
    "pnu": "PNU",
    "region_code": "법정동코드",
    "sido": "시도",
    "sigungu": "시군구",
    "eupmyeondong": "읍면동",
    "ri": "리",
    "lot_number": "지번",
    "land_category": "지목",
    "ledger_area_m2": "공부면적",
    "permanent_area_m2": "영구점용면적",
    "temporary_area_m2": "임시점용면적",
    "soil_cover_m": "토피(m)",
    "pavement_status": "포장상태",
    "excavation_slope": "터파기기울기",
    "temporary_width_m": "임시점용폭(m)",
}

OUTPUT_TO_SOURCE_COLUMN = {value: key for key, value in SOURCE_TO_OUTPUT_COLUMN.items()}
OUTPUT_TO_SOURCE_COLUMN.update(
    {
        "행정구역명": "admin_name",
        "소유구분": "ownership_type",
        "편입면적(㎡)": "permanent_area_m2",
        "영구점용면적(㎡)": "permanent_area_m2",
        "임시점용면적(㎡)": "temporary_area_m2",
    }
)

JIBUN_PATTERN = re.compile(r"^(?:(?:산)?\s*[0-9]+(?:-[0-9]+)?|0-[0-9]+)")


def _empty_admin_info() -> dict[str, str | None]:
    return {
        "admin_name": None,
        "sido": None,
        "sigungu": None,
        "eupmyeondong": None,
        "ri": None,
    }


def _derive_region_code_from_pnu(value: Any) -> str | None:
    text = _clean_text(value)
    if text and len(text) >= 10:
        return text[:10]
    return None


def _split_admin_name(value: Any) -> dict[str, str | None]:
    text = _clean_text(value)
    if not text:
        return _empty_admin_info()

    parts = text.split()
    sido = parts[0] if parts else None
    remainder = parts[1:]

    sigungu = None
    if remainder and remainder[0].endswith(("시", "군", "구")):
        sigungu = remainder.pop(0)

    ri = None
    if remainder and remainder[-1].endswith("리"):
        ri = remainder.pop()

    eupmyeondong = " ".join(remainder) if remainder else None
    return {
        "admin_name": text,
        "sido": sido,
        "sigungu": sigungu,
        "eupmyeondong": eupmyeondong,
        "ri": ri,
    }


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\ufeff", "").strip()
    if not text or text.lower() == "nan":
        return None
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        return text[:-2]
    return text


def _join_admin_name_parts(*parts: Any) -> str | None:
    values = [_clean_text(part) for part in parts]
    joined = " ".join(value for value in values if value)
    return joined or None


def _parse_jibun_components(value: Any) -> dict[str, str | None]:
    raw_text = _clean_text(value)
    if not raw_text:
        return {
            "jibun_raw": None,
            "lot_number": None,
            "land_category": None,
        }

    normalized = re.sub(r"\s+", " ", raw_text).strip()
    matched = JIBUN_PATTERN.search(normalized)
    if matched:
        lot_number = matched.group(0).strip()
        land_category = normalized[matched.end() :].strip() or None
    else:
        lot_number = normalized
        land_category = None

    return {
        "jibun_raw": normalized,
        "lot_number": lot_number,
        "land_category": land_category,
    }


def _series_or_none(raw, column_name: str | None):
    if not column_name:
        return None
    return raw[column_name]


def standardize_parcels(raw: gpd.GeoDataFrame, mapping: dict[str, str | None]) -> gpd.GeoDataFrame:
    parcel = gpd.GeoDataFrame(geometry=raw.geometry.copy(), crs=raw.crs)
    parcel["source_row_id"] = raw.index.astype(str)
    parcel["pnu"] = raw[mapping["pnu"]].map(_clean_text)
    jibun_series = raw[mapping["lot_number"]].map(_clean_text)
    parsed_jibun = jibun_series.apply(_parse_jibun_components).apply(pd.Series)

    land_category = _series_or_none(raw, mapping.get("land_category"))
    ownership_type = _series_or_none(raw, mapping.get("ownership_type"))
    region_code = _series_or_none(raw, mapping.get("region_code"))

    parcel["jibun_raw"] = parsed_jibun["jibun_raw"]
    parcel["lot_number"] = parsed_jibun["lot_number"].combine_first(jibun_series)
    parsed_land_category = parsed_jibun["land_category"]
    if land_category is not None:
        parcel["land_category"] = land_category.map(_clean_text).combine_first(parsed_land_category)
    else:
        parcel["land_category"] = parsed_land_category
    parcel["ownership_type"] = ownership_type.map(_clean_text) if ownership_type is not None else None
    parcel["region_code"] = region_code.map(_clean_text) if region_code is not None else None
    parcel["region_code"] = parcel["region_code"].combine_first(parcel["pnu"].apply(_derive_region_code_from_pnu))
    return parcel


def standardize_boundary(raw: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    boundary = gpd.GeoDataFrame(geometry=raw.geometry.copy(), crs=raw.crs)
    boundary["source_row_id"] = raw.index.astype(str)
    return boundary


def _collect_polygon_geometries(geometry) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [polygon for polygon in geometry.geoms if not polygon.is_empty]
    if isinstance(geometry, GeometryCollection):
        values: list[Polygon] = []
        for part in geometry.geoms:
            values.extend(_collect_polygon_geometries(part))
        return values
    return []


def _collect_linear_geometries(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    geometry_type = geometry.geom_type
    if geometry_type in {"LineString", "LinearRing"}:
        return [geometry]
    if geometry_type == "MultiLineString":
        return [part for part in geometry.geoms if not part.is_empty]
    if geometry_type == "GeometryCollection":
        values: list = []
        for part in geometry.geoms:
            values.extend(_collect_linear_geometries(part))
        return values
    return []


def standardize_sheets(
    raw: gpd.GeoDataFrame,
    fallback_crs,
    logger,
) -> gpd.GeoDataFrame:
    crs = raw.crs or fallback_crs
    polygons: list[Polygon] = []
    lines: list = []

    for geometry in raw.geometry:
        polygons.extend(_collect_polygon_geometries(geometry))
        lines.extend(_collect_linear_geometries(geometry))

    if lines:
        try:
            polygonized = list(polygonize(unary_union(lines)))
            polygons.extend([polygon for polygon in polygonized if not polygon.is_empty])
        except Exception as error:
            logger.warning("도곽 선형을 폴리곤으로 복원하는 중 일부를 건너뜁니다: %s", error)

    if not polygons:
        return gpd.GeoDataFrame({"source_row_id": []}, geometry=[], crs=crs)

    sheet = gpd.GeoDataFrame(
        {"source_row_id": [str(index) for index in range(len(polygons))]},
        geometry=polygons,
        crs=crs,
    )
    sheet = sheet.loc[~(sheet.geometry.isna() | sheet.geometry.is_empty)].copy()
    sheet = sheet.explode(ignore_index=True)
    sheet = sheet.loc[sheet.geometry.area > 0].copy()
    logger.info("도곽 geometry %s건을 폴리곤으로 정리했습니다.", len(sheet))
    return sheet


def standardize_facilities(raw: gpd.GeoDataFrame, mapping: dict[str, str | None]) -> gpd.GeoDataFrame:
    facility = gpd.GeoDataFrame(geometry=raw.geometry.copy(), crs=raw.crs)
    facility["source_row_id"] = raw.index.astype(str)

    diameter = _series_or_none(raw, mapping.get("diameter"))
    facility_length = _series_or_none(raw, mapping.get("length"))
    facility_type = _series_or_none(raw, mapping.get("facility_type"))

    facility["diameter_value"] = diameter if diameter is not None else None
    facility["facility_length_attr"] = facility_length if facility_length is not None else None
    facility["facility_type"] = facility_type.map(_clean_text) if facility_type is not None else None
    return facility


def standardize_code_df(raw: pd.DataFrame, mapping: dict[str, str | None]) -> pd.DataFrame:
    if not mapping.get("code"):
        return pd.DataFrame(columns=["region_code", "admin_name", "sido", "sigungu", "eupmyeondong", "ri"])

    code_df = pd.DataFrame()
    code_df["region_code"] = raw[mapping["code"]].map(_clean_text)
    code_df["admin_name"] = raw[mapping["name"]].map(_clean_text) if mapping.get("name") else None

    admin_columns: dict[str, pd.Series] = {}
    for field_key in ["sido", "sigungu", "eupmyeondong", "ri"]:
        column_name = mapping.get(field_key)
        if column_name:
            admin_columns[field_key] = raw[column_name].map(_clean_text)
        else:
            admin_columns[field_key] = pd.Series([None] * len(raw), index=raw.index, dtype="object")

    explicit_admin = pd.DataFrame(admin_columns)
    parsed_admin = code_df["admin_name"].apply(_split_admin_name).apply(pd.Series)

    for column in ["sido", "sigungu", "eupmyeondong", "ri"]:
        code_df[column] = explicit_admin[column].combine_first(parsed_admin[column])

    code_df["admin_name"] = code_df["admin_name"].combine_first(
        code_df.apply(
            lambda row: _join_admin_name_parts(
                row.get("sido"),
                row.get("sigungu"),
                row.get("eupmyeondong"),
                row.get("ri"),
            ),
            axis=1,
        )
    )
    return code_df.dropna(subset=["region_code"]).drop_duplicates("region_code")


def build_parcel_reference(
    parcels: gpd.GeoDataFrame,
    round_digits: int,
    logger,
) -> pd.DataFrame:
    if parcels.empty:
        logger.warning("필지 기준정보를 생성할 지적도 데이터가 없습니다.")
        return pd.DataFrame(
            columns=[
                "pnu",
                "lot_number",
                "land_category",
                "ownership_type",
                "region_code",
                "ledger_area_m2",
            ]
        )

    grouped = (
        parcels.groupby("pnu", dropna=False)
        .agg(
            lot_number=("lot_number", first_non_empty),
            land_category=("land_category", first_non_empty),
            ownership_type=("ownership_type", first_non_empty),
            region_code=("region_code", first_non_empty),
            ledger_area_m2=("ledger_area_m2", "sum"),
        )
        .reset_index()
    )
    grouped["ledger_area_m2"] = grouped["ledger_area_m2"].apply(lambda value: round_or_none(value, round_digits))
    return grouped


def aggregate_permanent_results(
    intersection: gpd.GeoDataFrame,
    round_digits: int,
    logger,
) -> pd.DataFrame:
    if intersection.empty:
        logger.warning("편입면적 산정 결과가 없습니다.")
        return pd.DataFrame(columns=["pnu", "permanent_area_m2"])

    duplicate_pnu_count = int(intersection["pnu"].duplicated().sum())
    if duplicate_pnu_count:
        logger.info("동일 PNU %s건을 필지 기준으로 집계합니다.", duplicate_pnu_count)

    grouped = (
        intersection.groupby("pnu", dropna=False)
        .agg(permanent_area_m2=("permanent_area_m2", "sum"))
        .reset_index()
    )
    return grouped


def aggregate_temporary_results(temp_segments: gpd.GeoDataFrame, round_digits: int, logger) -> pd.DataFrame:
    if temp_segments.empty:
        logger.warning("임시 점용 산정 결과가 없습니다.")
        return pd.DataFrame(
            columns=[
                "pnu",
                "temporary_width_m",
                "temporary_area_m2",
                "soil_cover_m",
                "pavement_status",
                "excavation_slope",
            ]
        )

    duplicate_pnu_count = int(temp_segments["pnu"].duplicated().sum())
    if duplicate_pnu_count:
        logger.info("동일 PNU %s건의 임시 점용 값을 합산합니다.", duplicate_pnu_count)

    grouped = (
        temp_segments.groupby("pnu", dropna=False)
        .agg(
            temporary_width_m=("temporary_width_m", "max"),
            temporary_area_m2=("temporary_area_m2", "sum"),
            soil_cover_m=("soil_cover_m", first_non_empty),
            pavement_status=("pavement_status", first_non_empty),
            excavation_slope=("excavation_slope", first_non_empty),
        )
        .reset_index()
    )

    grouped["temporary_width_m"] = grouped["temporary_width_m"].apply(
        lambda value: round_or_none(value, round_digits)
    )
    return grouped


def aggregate_temporary_area_results(
    intersection: gpd.GeoDataFrame,
    round_digits: int,
    logger,
) -> pd.DataFrame:
    if intersection.empty:
        logger.warning("임시 점용 면적 산정 결과가 없습니다.")
        return pd.DataFrame(columns=["pnu", "temporary_area_m2"])

    duplicate_pnu_count = int(intersection["pnu"].duplicated().sum())
    if duplicate_pnu_count:
        logger.info("동일 PNU %s건의 임시 점용 면적을 합산합니다.", duplicate_pnu_count)

    grouped = (
        intersection.groupby("pnu", dropna=False)
        .agg(temporary_area_m2=("temporary_area_m2", "sum"))
        .reset_index()
    )
    return grouped


def _resolve_region_rows_by_pnu_prefix(frame: pd.DataFrame, code_df: pd.DataFrame) -> pd.DataFrame:
    code_rows = sorted(
        (
            {
                "region_code": str(row.region_code),
                "admin_name": _clean_text(row.admin_name),
                "sido": _clean_text(row.sido),
                "sigungu": _clean_text(row.sigungu),
                "eupmyeondong": _clean_text(row.eupmyeondong),
                "ri": _clean_text(row.ri),
            }
            for row in code_df.itertuples()
        ),
        key=lambda item: len(item["region_code"]),
        reverse=True,
    )

    def resolve_row(pnu_value: Any) -> dict[str, str | None]:
        pnu_text = _clean_text(pnu_value)
        if not pnu_text:
            return {"region_code": None, **_empty_admin_info()}
        for row in code_rows:
            if pnu_text.startswith(row["region_code"]):
                return row
        return {"region_code": _derive_region_code_from_pnu(pnu_text), **_empty_admin_info()}

    return frame["pnu"].apply(resolve_row).apply(pd.Series)


def merge_results(
    parcel_reference_df: pd.DataFrame,
    permanent_df: pd.DataFrame,
    temporary_df: pd.DataFrame,
    code_df: pd.DataFrame,
    config: dict,
) -> pd.DataFrame:
    combined = pd.merge(parcel_reference_df, permanent_df, on="pnu", how="outer")
    combined = pd.merge(combined, temporary_df, on="pnu", how="outer")
    combined["criterion_type"] = config["analysis"]["default_criterion_label"]

    for column in ["lot_number", "land_category", "ownership_type", "region_code", "ledger_area_m2"]:
        if column not in combined.columns:
            combined[column] = None
    combined["region_code"] = combined["region_code"].combine_first(combined["pnu"].apply(_derive_region_code_from_pnu))

    if code_df.empty:
        combined["admin_name"] = None
        combined["sido"] = None
        combined["sigungu"] = None
        combined["eupmyeondong"] = None
        combined["ri"] = None
    else:
        combined = combined.merge(
            code_df[["region_code", "admin_name", "sido", "sigungu", "eupmyeondong", "ri"]],
            on="region_code",
            how="left",
        )
        missing_name_mask = combined["admin_name"].isna()
        if missing_name_mask.any():
            fallback = _resolve_region_rows_by_pnu_prefix(combined.loc[missing_name_mask, ["pnu"]].copy(), code_df)
            for column in ["region_code", "admin_name", "sido", "sigungu", "eupmyeondong", "ri"]:
                combined.loc[missing_name_mask, column] = combined.loc[missing_name_mask, column].combine_first(
                    fallback[column]
                )

    for column in ["admin_name", "sido", "sigungu", "eupmyeondong", "ri"]:
        combined[column] = combined[column].fillna("")
    return combined


def apply_corrections(combined: pd.DataFrame, corrections_df: pd.DataFrame | None, logger) -> pd.DataFrame:
    if corrections_df is None or corrections_df.empty:
        return combined

    correction = corrections_df.copy()
    pnu_key_candidates = ["PNU", "pnu", "고유번호"]
    matched_key = next((column for column in pnu_key_candidates if column in correction.columns), None)
    if not matched_key:
        logger.warning("보정 CSV에서 PNU 컬럼을 찾지 못해 보정 처리를 생략합니다.")
        return combined

    rename_map = {
        column: OUTPUT_TO_SOURCE_COLUMN[column]
        for column in correction.columns
        if column in OUTPUT_TO_SOURCE_COLUMN
    }
    correction = correction.rename(columns=rename_map)
    correction["pnu"] = correction[matched_key].map(_clean_text)
    correction = correction.dropna(subset=["pnu"])

    merged = combined.merge(correction, on="pnu", how="left", suffixes=("", "_보정"))
    correction_columns = [column for column in merged.columns if column.endswith("_보정")]
    for correction_column in correction_columns:
        base_column = correction_column[:-3]
        if base_column in merged.columns:
            merged[base_column] = merged[correction_column].combine_first(merged[base_column])
        else:
            merged.rename(columns={correction_column: base_column}, inplace=True)
        if correction_column in merged.columns:
            merged.drop(columns=[correction_column], inplace=True)
    return merged


def build_output_tables(combined: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    digits = int(config["analysis"]["round_digits"])
    output = combined.copy()
    raw_positive_area_mask = (
        pd.to_numeric(output.get("permanent_area_m2"), errors="coerce").fillna(0)
        + pd.to_numeric(output.get("temporary_area_m2"), errors="coerce").fillna(0)
    ) > 0

    for column in [
        "pnu",
        "region_code",
        "admin_name",
        "sido",
        "sigungu",
        "eupmyeondong",
        "ri",
        "lot_number",
        "land_category",
        "ownership_type",
        "pavement_status",
        "excavation_slope",
    ]:
        if column not in output.columns:
            output[column] = ""
        output[column] = output[column].fillna("")

    if "route_chainage_m" not in output.columns:
        output["route_chainage_m"] = None
    if "register_priority" not in output.columns:
        output["register_priority"] = None

    for column in [
        "ledger_area_m2",
        "permanent_area_m2",
        "temporary_area_m2",
        "temporary_width_m",
        "soil_cover_m",
        "route_chainage_m",
    ]:
        if column not in output.columns:
            output[column] = None
        output[column] = output[column].apply(lambda value: round_or_none(value, digits))

    output = output.loc[raw_positive_area_mask].copy()
    output["register_group"] = output["register_priority"].apply(lambda value: 0 if pd.notna(value) else 1)
    sort_columns = [
        column
        for column in [
            "register_group",
            "register_priority",
            "route_chainage_m",
            "sido",
            "sigungu",
            "eupmyeondong",
            "ri",
            "lot_number",
            "pnu",
        ]
        if column in output.columns
    ]
    if sort_columns:
        output = output.sort_values(by=sort_columns, kind="stable", na_position="last").reset_index(drop=True)
    else:
        output = output.reset_index(drop=True)
    output["register_no"] = output.index + 1
    output.drop(columns=["register_group"], inplace=True, errors="ignore")

    source_table = pd.DataFrame(
        {
            "조서번호": output.get("register_no"),
            "PNU": output.get("pnu"),
            "시도": output.get("sido"),
            "시군구": output.get("sigungu"),
            "읍면동": output.get("eupmyeondong"),
            "리": output.get("ri"),
            "지번": output.get("lot_number"),
            "지목": output.get("land_category"),
            "공부면적": output.get("ledger_area_m2"),
            "영구점용면적": output.get("permanent_area_m2"),
            "임시점용면적": output.get("temporary_area_m2"),
        }
    )

    data_table = pd.DataFrame(
        {
            "조서번호": output.get("register_no"),
            "PNU": output.get("pnu"),
            "법정동코드": output.get("region_code"),
            "시도": output.get("sido"),
            "시군구": output.get("sigungu"),
            "읍면동": output.get("eupmyeondong"),
            "리": output.get("ri"),
            "지번": output.get("lot_number"),
            "지목": output.get("land_category"),
            "공부면적": output.get("ledger_area_m2"),
            "영구점용면적": output.get("permanent_area_m2"),
            "임시점용면적": output.get("temporary_area_m2"),
            "토피(m)": output.get("soil_cover_m"),
            "포장상태": output.get("pavement_status"),
            "터파기기울기": output.get("excavation_slope"),
            "임시점용폭(m)": output.get("temporary_width_m"),
        }
    )

    source_table = source_table.reindex(columns=config["output"]["source_columns"])
    data_table = data_table.reindex(columns=config["output"]["data_sheet_columns"]).copy()
    return source_table, data_table
