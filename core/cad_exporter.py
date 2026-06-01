from __future__ import annotations

from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union

from utils.helpers import ensure_directory


class CadExportError(Exception):
    pass


def _format_number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = f"{float(value):.6f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _pair(code: int, value) -> list[str]:
    return [str(code), str(value)]


def _iter_polygon_parts(geometry) -> Iterable[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return [polygon for polygon in geometry.geoms if not polygon.is_empty]
    if isinstance(geometry, GeometryCollection):
        values: list[Polygon] = []
        for part in geometry.geoms:
            values.extend(_iter_polygon_parts(part))
        return values
    return []


def _normalize_ring_coordinates(coords) -> list[tuple[float, float]]:
    ring = [(float(x), float(y)) for x, y, *_ in coords]
    if len(ring) >= 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def _iter_ring_coordinates(geometry) -> Iterable[list[tuple[float, float]]]:
    for polygon in _iter_polygon_parts(geometry):
        exterior = _normalize_ring_coordinates(polygon.exterior.coords)
        if len(exterior) >= 3:
            yield exterior
        for interior in polygon.interiors:
            ring = _normalize_ring_coordinates(interior.coords)
            if len(ring) >= 3:
                yield ring


def _dissolve_by_pnu(frame: gpd.GeoDataFrame, register_lookup: pd.DataFrame) -> gpd.GeoDataFrame:
    if frame.empty:
        return gpd.GeoDataFrame(columns=["pnu", "register_no"], geometry=[], crs=frame.crs)

    grouped = (
        frame.loc[frame["pnu"].notna()]
        .dissolve(by="pnu", as_index=False)
        .merge(register_lookup, on="pnu", how="left")
        .sort_values(by=["register_no", "pnu"], kind="stable", na_position="last")
        .reset_index(drop=True)
    )
    return gpd.GeoDataFrame(grouped, geometry=frame.geometry.name, crs=frame.crs)


def _clip_frame_to_geometry(frame: gpd.GeoDataFrame, clip_geometry) -> gpd.GeoDataFrame:
    if frame.empty or clip_geometry is None or clip_geometry.is_empty:
        return frame

    working = frame.copy()
    geometry_name = working.geometry.name
    working[geometry_name] = working.geometry.intersection(clip_geometry)
    working = working.loc[working.geometry.notna() & ~working.geometry.is_empty].copy()
    return gpd.GeoDataFrame(working, geometry=geometry_name, crs=frame.crs)


def _prepare_sheet_frame(sheet_frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if sheet_frame.empty:
        return gpd.GeoDataFrame(columns=["sheet_id"], geometry=[], crs=sheet_frame.crs)

    working = sheet_frame[[sheet_frame.geometry.name]].copy()
    working = working.loc[working.geometry.notna() & ~working.geometry.is_empty].copy()
    working = working.explode(ignore_index=True)
    working = working.loc[working.geometry.area > 0].copy()
    working["sheet_id"] = [f"S{index + 1:03d}" for index in range(len(working))]
    return gpd.GeoDataFrame(working, geometry=sheet_frame.geometry.name, crs=sheet_frame.crs)


def _resolve_clip_geometry(
    sheet_frame: gpd.GeoDataFrame,
    permanent_frame: gpd.GeoDataFrame,
    temporary_frame: gpd.GeoDataFrame,
    config: dict,
):
    if not sheet_frame.empty:
        return unary_union(sheet_frame.geometry.tolist())

    bounds_values: list[tuple[float, float, float, float]] = []
    for frame in [permanent_frame, temporary_frame]:
        if not frame.empty:
            bounds = frame.total_bounds
            if len(bounds) == 4:
                bounds_values.append(tuple(float(value) for value in bounds))

    if not bounds_values:
        return None

    min_x = min(value[0] for value in bounds_values)
    min_y = min(value[1] for value in bounds_values)
    max_x = max(value[2] for value in bounds_values)
    max_y = max(value[3] for value in bounds_values)
    margin = float(config["cad"].get("parcel_clip_buffer_m", 20.0))
    return box(min_x - margin, min_y - margin, max_x + margin, max_y + margin)


def _prepare_parcel_frame(parcel_frame: gpd.GeoDataFrame, clip_geometry) -> gpd.GeoDataFrame:
    if parcel_frame.empty or "pnu" not in parcel_frame.columns:
        return gpd.GeoDataFrame(columns=["pnu", "lot_number"], geometry=[], crs=parcel_frame.crs)

    geometry_name = parcel_frame.geometry.name
    working = parcel_frame[["pnu", "lot_number", geometry_name]].copy()
    working = working.loc[working["pnu"].notna() & working.geometry.notna() & ~working.geometry.is_empty].copy()
    working = _clip_frame_to_geometry(working, clip_geometry)
    if working.empty:
        return gpd.GeoDataFrame(columns=["pnu", "lot_number"], geometry=[], crs=parcel_frame.crs)

    grouped_rows: list[dict] = []
    for pnu, group in working.groupby("pnu", dropna=False):
        merged_geometry = unary_union(group.geometry.tolist())
        if merged_geometry is None or merged_geometry.is_empty:
            continue
        lot_number = next(
            (
                str(value).strip()
                for value in group["lot_number"]
                if value is not None and str(value).strip()
            ),
            None,
        )
        grouped_rows.append(
            {
                "pnu": str(pnu),
                "lot_number": lot_number,
                "geometry": merged_geometry,
            }
        )

    return gpd.GeoDataFrame(grouped_rows, geometry="geometry", crs=parcel_frame.crs)


def _build_lot_label_features(parcel_frame: gpd.GeoDataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    if parcel_frame.empty:
        return pd.DataFrame(columns=["pnu", "lot_number", "label_x", "label_y"])

    for row in parcel_frame.itertuples():
        if row.geometry is None or row.geometry.is_empty or not row.lot_number:
            continue
        representative = row.geometry.representative_point()
        rows.append(
            {
                "pnu": str(row.pnu),
                "lot_number": str(row.lot_number),
                "label_x": float(representative.x),
                "label_y": float(representative.y),
            }
        )
    return pd.DataFrame(rows)


def _select_sheet_geometry(geometry, sheet_frame: gpd.GeoDataFrame):
    if sheet_frame.empty or geometry is None or geometry.is_empty:
        return None

    best_geometry = None
    best_score = -1.0
    for row in sheet_frame.itertuples():
        if row.geometry is None or row.geometry.is_empty:
            continue
        if not row.geometry.intersects(geometry):
            continue
        score = float(row.geometry.intersection(geometry).area)
        if score > best_score:
            best_score = score
            best_geometry = row.geometry

    if best_geometry is not None:
        return best_geometry

    representative = geometry.representative_point()
    distances = [
        (float(row.geometry.distance(representative)), row.geometry)
        for row in sheet_frame.itertuples()
        if row.geometry is not None and not row.geometry.is_empty
    ]
    if not distances:
        return None
    return min(distances, key=lambda item: item[0])[1]


def _build_label_features(
    temporary_frame: gpd.GeoDataFrame,
    permanent_frame: gpd.GeoDataFrame,
    register_lookup: pd.DataFrame,
    sheet_frame: gpd.GeoDataFrame,
    config: dict,
) -> pd.DataFrame:
    label_config = config["cad"]["label"]
    temporary_lookup = {
        str(row.pnu): row.geometry
        for row in temporary_frame.itertuples()
        if row.geometry is not None and not row.geometry.is_empty
    }
    permanent_lookup = {
        str(row.pnu): row.geometry
        for row in permanent_frame.itertuples()
        if row.geometry is not None and not row.geometry.is_empty
    }

    rows: list[dict] = []
    occupied_lookup = {**permanent_lookup, **temporary_lookup}
    placed_centers: list[Point] = []
    for row in register_lookup.itertuples():
        pnu = str(row.pnu)
        base_geometry = temporary_lookup.get(pnu) or permanent_lookup.get(pnu)
        if base_geometry is None or base_geometry.is_empty:
            continue

        if not sheet_frame.empty:
            label_targets: list[tuple[str | None, object, object | None]] = []
            for sheet_row in sheet_frame.itertuples():
                if sheet_row.geometry is None or sheet_row.geometry.is_empty:
                    continue
                if not sheet_row.geometry.intersects(base_geometry):
                    continue
                clipped_geometry = base_geometry.intersection(sheet_row.geometry)
                if clipped_geometry is None or clipped_geometry.is_empty:
                    continue
                label_targets.append((str(getattr(sheet_row, "sheet_id", "")) or None, clipped_geometry, sheet_row.geometry))
            if not label_targets:
                label_targets = [(None, base_geometry, _select_sheet_geometry(base_geometry, sheet_frame))]
        else:
            label_targets = [(None, base_geometry, None)]

        for sheet_id, geometry, containing_sheet in label_targets:
            if geometry is None or geometry.is_empty:
                continue

            representative = geometry.representative_point()
            min_x, min_y, max_x, max_y = geometry.bounds
            span = max(max_x - min_x, max_y - min_y)
            offset = max(float(label_config["offset_m"]), span * float(label_config["offset_scale"]))
            circle_radius = float(label_config["circle_radius_m"])
            minimum_spacing = float(label_config.get("minimum_spacing_m", circle_radius * 3))
            geometry_clearance = float(label_config.get("geometry_clearance_m", circle_radius * 1.25))
            occupied_buffer = geometry.buffer(geometry_clearance)

            candidate_positions: list[tuple[float, float]] = []
            for scale in [0.8, 1.0, 1.2, 1.5]:
                scaled_offset = offset * scale
                candidate_positions.extend(
                    [
                        (max_x + scaled_offset, representative.y),
                        (min_x - scaled_offset, representative.y),
                        (representative.x, max_y + scaled_offset),
                        (representative.x, min_y - scaled_offset),
                        (max_x + scaled_offset, max_y + (scaled_offset * float(label_config["vertical_offset_ratio"]))),
                        (min_x - scaled_offset, max_y + (scaled_offset * float(label_config["vertical_offset_ratio"]))),
                        (max_x + scaled_offset, min_y - (scaled_offset * float(label_config["vertical_offset_ratio"]))),
                        (min_x - scaled_offset, min_y - (scaled_offset * float(label_config["vertical_offset_ratio"]))),
                    ]
                )

            if bool(label_config.get("alternate_sides", True)) and int(row.register_no) % 2 == 0:
                candidate_positions = candidate_positions[1::2] + candidate_positions[::2]

            best_score = float("inf")
            best_position: tuple[float, float] | None = None
            for label_x, label_y in candidate_positions:
                candidate_point = Point(label_x, label_y)
                if containing_sheet is not None and not containing_sheet.buffer(circle_radius * 0.25).covers(candidate_point):
                    continue

                score = representative.distance(candidate_point)
                if occupied_buffer.contains(candidate_point):
                    score += 1000.0
                if placed_centers:
                    min_spacing = min(center.distance(candidate_point) for center in placed_centers)
                    if min_spacing < minimum_spacing:
                        score += (minimum_spacing - min_spacing) * 100.0
                if score < best_score:
                    best_score = score
                    best_position = (float(label_x), float(label_y))

            if best_position is None:
                if containing_sheet is not None:
                    fallback_area = containing_sheet.difference(occupied_buffer)
                    fallback_point = fallback_area.representative_point() if not fallback_area.is_empty else containing_sheet.representative_point()
                else:
                    fallback_point = representative
                label_x = float(fallback_point.x)
                label_y = float(fallback_point.y)
            else:
                label_x, label_y = best_position

            placed_centers.append(Point(label_x, label_y))
            rows.append(
                {
                    "pnu": pnu,
                    "sheet_id": sheet_id,
                    "register_no": int(row.register_no),
                    "anchor_x": float(representative.x),
                    "anchor_y": float(representative.y),
                    "label_x": float(label_x),
                    "label_y": float(label_y),
                }
            )
    return pd.DataFrame(rows)


def _build_dxf_document(
    permanent_frame: gpd.GeoDataFrame,
    temporary_frame: gpd.GeoDataFrame,
    sheet_frame: gpd.GeoDataFrame,
    parcel_frame: gpd.GeoDataFrame,
    lot_label_frame: pd.DataFrame,
    label_frame: pd.DataFrame,
    config: dict,
) -> str:
    layer_names = config["cad"]["layers"]
    colors = config["cad"]["colors"]
    label_config = config["cad"]["label"]
    lines: list[str] = []

    def add_group(code: int, value) -> None:
        lines.extend(_pair(code, value))

    def add_layer_record(layer_name: str, color: int) -> None:
        add_group(0, "LAYER")
        add_group(2, layer_name)
        add_group(70, 0)
        add_group(62, color)
        add_group(6, "CONTINUOUS")

    def add_polyline_entity(layer_name: str, color: int, ring: list[tuple[float, float]]) -> None:
        add_group(0, "POLYLINE")
        add_group(8, layer_name)
        add_group(62, color)
        add_group(66, 1)
        add_group(70, 1)
        add_group(10, 0.0)
        add_group(20, 0.0)
        add_group(30, 0.0)
        for x_coord, y_coord in ring:
            add_group(0, "VERTEX")
            add_group(8, layer_name)
            add_group(10, _format_number(x_coord))
            add_group(20, _format_number(y_coord))
            add_group(30, 0.0)
        add_group(0, "SEQEND")

    def add_line_entity(layer_name: str, color: int, start_x: float, start_y: float, end_x: float, end_y: float) -> None:
        add_group(0, "LINE")
        add_group(8, layer_name)
        add_group(62, color)
        add_group(10, _format_number(start_x))
        add_group(20, _format_number(start_y))
        add_group(30, 0.0)
        add_group(11, _format_number(end_x))
        add_group(21, _format_number(end_y))
        add_group(31, 0.0)

    def add_circle_entity(layer_name: str, color: int, center_x: float, center_y: float, radius: float) -> None:
        add_group(0, "CIRCLE")
        add_group(8, layer_name)
        add_group(62, color)
        add_group(10, _format_number(center_x))
        add_group(20, _format_number(center_y))
        add_group(30, 0.0)
        add_group(40, _format_number(radius))

    def add_text_entity(layer_name: str, color: int, insert_x: float, insert_y: float, text: str, height: float) -> None:
        add_group(0, "TEXT")
        add_group(8, layer_name)
        add_group(62, color)
        add_group(10, _format_number(insert_x))
        add_group(20, _format_number(insert_y))
        add_group(30, 0.0)
        add_group(40, _format_number(height))
        add_group(1, text)
        add_group(7, config["cad"]["text_style"].get("name", "STANDARD"))
        add_group(72, 1)
        add_group(73, 2)
        add_group(11, _format_number(insert_x))
        add_group(21, _format_number(insert_y))
        add_group(31, 0.0)

    bounds_values: list[tuple[float, float, float, float]] = []
    for frame in [sheet_frame, parcel_frame, permanent_frame, temporary_frame]:
        if not frame.empty:
            bounds = frame.total_bounds
            if len(bounds) == 4:
                bounds_values.append(tuple(float(value) for value in bounds))
    if not lot_label_frame.empty:
        lot_min_x = float(lot_label_frame["label_x"].min())
        lot_min_y = float(lot_label_frame["label_y"].min())
        lot_max_x = float(lot_label_frame["label_x"].max())
        lot_max_y = float(lot_label_frame["label_y"].max())
        bounds_values.append((lot_min_x, lot_min_y, lot_max_x, lot_max_y))
    if not label_frame.empty:
        min_x = min(float(label_frame["anchor_x"].min()), float(label_frame["label_x"].min()))
        min_y = min(float(label_frame["anchor_y"].min()), float(label_frame["label_y"].min()))
        max_x = max(float(label_frame["anchor_x"].max()), float(label_frame["label_x"].max()))
        max_y = max(float(label_frame["anchor_y"].max()), float(label_frame["label_y"].max()))
        bounds_values.append((min_x, min_y, max_x, max_y))

    if bounds_values:
        ext_min_x = min(value[0] for value in bounds_values)
        ext_min_y = min(value[1] for value in bounds_values)
        ext_max_x = max(value[2] for value in bounds_values)
        ext_max_y = max(value[3] for value in bounds_values)
    else:
        ext_min_x = ext_min_y = 0.0
        ext_max_x = ext_max_y = 100.0

    add_group(0, "SECTION")
    add_group(2, "HEADER")
    add_group(9, "$ACADVER")
    add_group(1, "AC1009")
    add_group(9, "$DWGCODEPAGE")
    add_group(3, config["cad"].get("dwg_codepage", "ANSI_949"))
    add_group(9, "$INSUNITS")
    add_group(70, int(config["cad"]["insunits"]))
    add_group(9, "$EXTMIN")
    add_group(10, _format_number(ext_min_x))
    add_group(20, _format_number(ext_min_y))
    add_group(30, 0.0)
    add_group(9, "$EXTMAX")
    add_group(10, _format_number(ext_max_x))
    add_group(20, _format_number(ext_max_y))
    add_group(30, 0.0)
    add_group(0, "ENDSEC")

    add_group(0, "SECTION")
    add_group(2, "TABLES")
    add_group(0, "TABLE")
    add_group(2, "LTYPE")
    add_group(70, 1)
    add_group(0, "LTYPE")
    add_group(2, "CONTINUOUS")
    add_group(70, 0)
    add_group(3, "Solid line")
    add_group(72, 65)
    add_group(73, 0)
    add_group(40, 0.0)
    add_group(0, "ENDTAB")
    add_group(0, "TABLE")
    add_group(2, "LAYER")
    add_group(70, 8)
    add_layer_record("0", 7)
    for key in [
        "sheet",
        "parcel",
        "lot_number",
        "permanent",
        "temporary",
        "register",
        "leader",
    ]:
        add_layer_record(layer_names[key], int(colors[key]))
    add_group(0, "ENDTAB")
    add_group(0, "TABLE")
    add_group(2, "STYLE")
    add_group(70, 1)
    add_group(0, "STYLE")
    add_group(2, config["cad"]["text_style"].get("name", "STANDARD"))
    add_group(70, 0)
    add_group(40, 0.0)
    add_group(41, 1.0)
    add_group(50, 0.0)
    add_group(71, 0)
    add_group(42, 2.5)
    add_group(3, config["cad"]["text_style"].get("font_file", "malgun.ttf"))
    add_group(4, "")
    add_group(0, "ENDTAB")
    add_group(0, "ENDSEC")

    add_group(0, "SECTION")
    add_group(2, "ENTITIES")
    for frame, layer_key in [
        (sheet_frame, "sheet"),
        (parcel_frame, "parcel"),
    ]:
        if frame.empty:
            continue
        for row in frame.itertuples():
            for ring in _iter_ring_coordinates(row.geometry):
                add_polyline_entity(layer_names[layer_key], int(colors[layer_key]), ring)

    for frame, layer_key in [
        (permanent_frame, "permanent"),
        (temporary_frame, "temporary"),
    ]:
        if frame.empty:
            continue
        for row in frame.itertuples():
            for ring in _iter_ring_coordinates(row.geometry):
                add_polyline_entity(layer_names[layer_key], int(colors[layer_key]), ring)

    for row in lot_label_frame.itertuples():
        add_text_entity(
            layer_names["lot_number"],
            int(colors["lot_number"]),
            float(row.label_x),
            float(row.label_y),
            str(row.lot_number),
            float(label_config.get("lot_text_height_m", label_config["text_height_m"])),
        )

    for row in label_frame.itertuples():
        add_line_entity(
            layer_names["leader"],
            int(colors["leader"]),
            float(row.anchor_x),
            float(row.anchor_y),
            float(row.label_x),
            float(row.label_y),
        )
        add_circle_entity(
            layer_names["register"],
            int(colors["register"]),
            float(row.label_x),
            float(row.label_y),
            float(label_config["circle_radius_m"]),
        )
        add_text_entity(
            layer_names["register"],
            int(colors["register"]),
            float(row.label_x),
            float(row.label_y),
            str(row.register_no),
            float(label_config["text_height_m"]),
        )

    add_group(0, "ENDSEC")
    add_group(0, "EOF")
    return "\n".join(lines) + "\n"


def export_cad_dxf(
    output_dir: str | Path,
    base_name: str,
    permanent_intersection: gpd.GeoDataFrame,
    temporary_intersection: gpd.GeoDataFrame,
    sheet_frame: gpd.GeoDataFrame,
    parcel_frame: gpd.GeoDataFrame,
    source_table: pd.DataFrame,
    cad_crs,
    config: dict,
    logger,
) -> dict[str, str]:
    if "PNU" not in source_table.columns or "조서번호" not in source_table.columns:
        raise CadExportError("조서번호와 PNU 정보를 찾을 수 없어 CAD DXF를 생성할 수 없습니다.")

    target_dir = ensure_directory(output_dir)
    dxf_path = target_dir / f"{base_name}_{config['cad']['file_suffix']}.dxf"
    crs_info_path = target_dir / f"{base_name}_{config['cad']['crs_info_suffix']}.txt"

    register_lookup = (
        source_table[["PNU", "조서번호"]]
        .dropna(subset=["PNU", "조서번호"])
        .drop_duplicates("PNU")
        .rename(columns={"PNU": "pnu", "조서번호": "register_no"})
        .copy()
    )
    register_lookup["pnu"] = register_lookup["pnu"].astype(str)
    register_lookup["register_no"] = register_lookup["register_no"].astype(int)

    sheet_frame = _prepare_sheet_frame(sheet_frame)
    clip_geometry = _resolve_clip_geometry(sheet_frame, permanent_intersection, temporary_intersection, config)
    parcel_frame = _prepare_parcel_frame(parcel_frame, clip_geometry)
    permanent_frame = _dissolve_by_pnu(permanent_intersection, register_lookup)
    temporary_frame = _dissolve_by_pnu(temporary_intersection, register_lookup)
    label_frame = _build_label_features(temporary_frame, permanent_frame, register_lookup, sheet_frame, config)
    lot_label_frame = _build_lot_label_features(parcel_frame)

    dxf_text = _build_dxf_document(
        permanent_frame,
        temporary_frame,
        sheet_frame,
        parcel_frame,
        lot_label_frame,
        label_frame,
        config,
    )
    try:
        dxf_path.write_text(
            dxf_text,
            encoding=config["cad"].get("dxf_encoding", "cp949"),
        )
        crs_text = "\n".join(
            [
                f"CAD 출력 좌표계: {cad_crs.to_string() if cad_crs is not None else '확인 불가'}",
                "기준: 상수관로 SHP 좌표계",
                f"도곽 레이어: {config['cad']['layers']['sheet']}",
                f"지적도 레이어: {config['cad']['layers']['parcel']}",
                f"지번 레이어: {config['cad']['layers']['lot_number']}",
                f"영구점용 레이어: {config['cad']['layers']['permanent']}",
                f"임시점용 레이어: {config['cad']['layers']['temporary']}",
                f"조서번호 레이어: {config['cad']['layers']['register']}",
                f"조서번호 연결선 레이어: {config['cad']['layers']['leader']}",
                f"문자 스타일: {config['cad']['text_style'].get('name', 'MALGUN')} ({config['cad']['text_style'].get('font_file', 'malgun.ttf')})",
            ]
        )
        crs_info_path.write_text(crs_text, encoding="utf-8")
    except Exception as error:
        raise CadExportError(f"CAD DXF 저장에 실패했습니다: {error}") from error

    logger.info(
        "CAD DXF를 생성했습니다. 도곽 %s건 지적도 %s건 지번 %s건 영구점용 %s건 임시점용 %s건 조서번호 %s건",
        len(sheet_frame),
        len(parcel_frame),
        len(lot_label_frame),
        len(permanent_frame),
        len(temporary_frame),
        len(label_frame),
    )
    return {
        "cad_dxf": str(dxf_path),
        "cad_crs_txt": str(crs_info_path),
    }
