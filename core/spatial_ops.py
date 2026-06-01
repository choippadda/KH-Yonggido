from __future__ import annotations

from collections import defaultdict
import heapq
import math

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import linemerge, nearest_points, unary_union

from core.temporary_occupancy import TemporaryOccupancySelection, calculate_temporary_width
from utils.helpers import meters_from_diameter


LINE_GEOMETRY_TYPES = {
    "LineString",
    "MultiLineString",
    "LinearRing",
}
NODE_PRECISION = 6
NETWORK_TOLERANCE = 1e-8


def _explode_line_parts(facilities: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    line_parts = facilities.explode(ignore_index=True)
    return line_parts.loc[
        line_parts.geometry.notna()
        & ~line_parts.geometry.is_empty
        & line_parts.geometry.geom_type.isin(LINE_GEOMETRY_TYPES)
    ].copy()


def _collect_line_geometries(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return [geom for geom in geometry.geoms if not geom.is_empty]
    if geometry.geom_type == "GeometryCollection":
        values: list = []
        for part in geometry.geoms:
            values.extend(_collect_line_geometries(part))
        return values
    return []


def _extract_points_from_geometry(geometry) -> list[Point]:
    if geometry is None or geometry.is_empty:
        return []

    geometry_type = geometry.geom_type
    if geometry_type == "Point":
        return [geometry]
    if geometry_type == "MultiPoint":
        return [point for point in geometry.geoms if not point.is_empty]
    if geometry_type in {"LineString", "LinearRing"}:
        coordinates = list(geometry.coords)
        if not coordinates:
            return []
        if len(coordinates) == 1:
            return [Point(coordinates[0])]
        return [Point(coordinates[0]), Point(coordinates[-1])]
    if geometry_type == "MultiLineString":
        values: list[Point] = []
        for part in geometry.geoms:
            values.extend(_extract_points_from_geometry(part))
        return values
    if geometry_type == "GeometryCollection":
        values: list[Point] = []
        for part in geometry.geoms:
            values.extend(_extract_points_from_geometry(part))
        return values
    return [geometry.representative_point()]


def _node_key(value) -> tuple[float, float]:
    if isinstance(value, Point):
        x_value = value.x
        y_value = value.y
    else:
        if len(value) < 2:
            raise ValueError("좌표값에서 x, y를 확인할 수 없습니다.")
        x_value = value[0]
        y_value = value[1]
    return (round(float(x_value), NODE_PRECISION), round(float(y_value), NODE_PRECISION))


def _point_from_node(node: tuple[float, float]) -> Point:
    return Point(node[0], node[1])


def _edge_sort_key(edge_id: str) -> tuple[int, str]:
    digits = "".join(character for character in edge_id if character.isdigit())
    return (int(digits) if digits else 0, edge_id)


def _build_network_edges(
    facilities: gpd.GeoDataFrame,
) -> tuple[dict[str, dict], dict[tuple[float, float], list[tuple[str, tuple[float, float]]]]]:
    line_parts = _explode_line_parts(facilities)
    if line_parts.empty:
        return {}, defaultdict(list)

    unioned = unary_union(line_parts.geometry.tolist())
    if unioned is None or unioned.is_empty:
        return {}, defaultdict(list)

    try:
        merged = unioned if unioned.geom_type in LINE_GEOMETRY_TYPES else linemerge(unioned)
    except ValueError:
        merged = unioned

    line_geometries = [
        LineString(list(geometry.coords))
        for geometry in _collect_line_geometries(merged)
        if geometry.length > 0
    ]
    if not line_geometries:
        return {}, defaultdict(list)

    edges: dict[str, dict] = {}
    adjacency: dict[tuple[float, float], list[tuple[str, tuple[float, float]]]] = defaultdict(list)
    for index, geometry in enumerate(line_geometries, start=1):
        coordinates = list(geometry.coords)
        start_node = _node_key(coordinates[0])
        end_node = _node_key(coordinates[-1])
        edge_id = f"E{index:04d}"
        edge_record = {
            "edge_id": edge_id,
            "geometry": geometry,
            "start_node": start_node,
            "end_node": end_node,
            "length": float(geometry.length),
        }
        edges[edge_id] = edge_record
        adjacency[start_node].append((edge_id, end_node))
        adjacency[end_node].append((edge_id, start_node))
    return edges, adjacency


def _dijkstra(
    adjacency: dict[tuple[float, float], list[tuple[str, tuple[float, float]]]],
    edges: dict[str, dict],
    source: tuple[float, float],
    allowed_edges: set[str] | None = None,
) -> tuple[dict[tuple[float, float], float], dict[tuple[float, float], tuple[tuple[float, float], str]]]:
    distances = {source: 0.0}
    previous: dict[tuple[float, float], tuple[tuple[float, float], str]] = {}
    heap: list[tuple[float, tuple[float, float]]] = [(0.0, source)]

    while heap:
        current_distance, node = heapq.heappop(heap)
        if current_distance > distances.get(node, math.inf) + NETWORK_TOLERANCE:
            continue

        for edge_id, neighbor in adjacency.get(node, []):
            if allowed_edges is not None and edge_id not in allowed_edges:
                continue
            next_distance = current_distance + float(edges[edge_id]["length"])
            if next_distance + NETWORK_TOLERANCE < distances.get(neighbor, math.inf):
                distances[neighbor] = next_distance
                previous[neighbor] = (node, edge_id)
                heapq.heappush(heap, (next_distance, neighbor))

    return distances, previous


def _reconstruct_path(
    previous: dict[tuple[float, float], tuple[tuple[float, float], str]],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[list[tuple[float, float]], list[str]]:
    nodes = [end]
    edge_ids: list[str] = []
    current = end

    while current != start:
        prior = previous.get(current)
        if prior is None:
            return [], []
        previous_node, edge_id = prior
        nodes.append(previous_node)
        edge_ids.append(edge_id)
        current = previous_node

    nodes.reverse()
    edge_ids.reverse()
    return nodes, edge_ids


def _build_path_geometry(
    path_nodes: list[tuple[float, float]],
    path_edge_ids: list[str],
    edges: dict[str, dict],
):
    if not path_edge_ids:
        return None

    coordinates: list[tuple[float, float]] = []
    for index, edge_id in enumerate(path_edge_ids):
        edge = edges[edge_id]
        edge_coordinates = list(edge["geometry"].coords)
        start_node = path_nodes[index]
        end_node = path_nodes[index + 1]
        start_key = _node_key(edge_coordinates[0])
        end_key = _node_key(edge_coordinates[-1])

        if start_key == start_node and end_key == end_node:
            oriented_coordinates = edge_coordinates
        elif start_key == end_node and end_key == start_node:
            oriented_coordinates = list(reversed(edge_coordinates))
        else:
            start_distance = Point(edge_coordinates[0]).distance(_point_from_node(start_node))
            end_distance = Point(edge_coordinates[-1]).distance(_point_from_node(start_node))
            oriented_coordinates = edge_coordinates if start_distance <= end_distance else list(reversed(edge_coordinates))

        if not coordinates:
            coordinates.extend(oriented_coordinates)
        elif coordinates[-1] == oriented_coordinates[0]:
            coordinates.extend(oriented_coordinates[1:])
        else:
            coordinates.extend(oriented_coordinates)

    if len(coordinates) < 2:
        return edges[path_edge_ids[0]]["geometry"]
    return LineString(coordinates)


def _path_turn_angle_sum(path_nodes: list[tuple[float, float]]) -> float:
    if len(path_nodes) < 3:
        return 0.0

    total_angle = 0.0
    for index in range(1, len(path_nodes) - 1):
        prev_node = path_nodes[index - 1]
        current_node = path_nodes[index]
        next_node = path_nodes[index + 1]

        vector_a = (current_node[0] - prev_node[0], current_node[1] - prev_node[1])
        vector_b = (next_node[0] - current_node[0], next_node[1] - current_node[1])
        norm_a = math.hypot(*vector_a)
        norm_b = math.hypot(*vector_b)
        if norm_a <= NETWORK_TOLERANCE or norm_b <= NETWORK_TOLERANCE:
            continue

        cosine = ((vector_a[0] * vector_b[0]) + (vector_a[1] * vector_b[1])) / (norm_a * norm_b)
        cosine = max(-1.0, min(1.0, cosine))
        total_angle += math.degrees(math.acos(cosine))
    return total_angle


def _find_main_path(
    edges: dict[str, dict],
    adjacency: dict[tuple[float, float], list[tuple[str, tuple[float, float]]]],
) -> tuple[list[tuple[float, float]], list[str], float]:
    if not edges:
        return [], [], 0.0

    candidate_nodes = [node for node, neighbors in adjacency.items() if len(neighbors) == 1]
    if len(candidate_nodes) < 2:
        candidate_nodes = list(adjacency.keys())

    best_score = -math.inf
    best_distance = -1.0
    best_pair: tuple[tuple[float, float], tuple[float, float]] | None = None
    best_previous: dict[tuple[float, float], tuple[tuple[float, float], str]] = {}

    for start in candidate_nodes:
        distances, previous = _dijkstra(adjacency, edges, start)
        for end in candidate_nodes:
            if end == start or end not in distances:
                continue
            current_distance = float(distances[end])
            path_nodes, path_edge_ids = _reconstruct_path(previous, start, end)
            if not path_edge_ids:
                continue
            turn_angle_sum = _path_turn_angle_sum(path_nodes)
            current_score = current_distance - (0.1 * turn_angle_sum)
            if (
                current_score > best_score + NETWORK_TOLERANCE
                or (
                    abs(current_score - best_score) <= NETWORK_TOLERANCE
                    and current_distance > best_distance + NETWORK_TOLERANCE
                )
            ):
                best_score = current_score
                best_distance = current_distance
                best_pair = (start, end)
                best_previous = previous

    if best_pair is None:
        longest_edge = max(edges.values(), key=lambda item: (float(item["length"]), item["edge_id"]))
        return [longest_edge["start_node"], longest_edge["end_node"]], [longest_edge["edge_id"]], float(
            longest_edge["length"]
        )

    path_nodes, path_edge_ids = _reconstruct_path(best_previous, best_pair[0], best_pair[1])
    if path_edge_ids:
        return path_nodes, path_edge_ids, max(best_distance, 0.0)

    longest_edge = max(edges.values(), key=lambda item: (float(item["length"]), item["edge_id"]))
    return [longest_edge["start_node"], longest_edge["end_node"]], [longest_edge["edge_id"]], float(
        longest_edge["length"]
    )


def _collect_connected_components(
    edge_ids: set[str],
    edges: dict[str, dict],
    adjacency: dict[tuple[float, float], list[tuple[str, tuple[float, float]]]],
) -> list[dict]:
    remaining = set(edge_ids)
    components: list[dict] = []

    while remaining:
        seed_edge = next(iter(remaining))
        queue = [seed_edge]
        component_edges: set[str] = set()
        component_nodes: set[tuple[float, float]] = set()

        while queue:
            edge_id = queue.pop()
            if edge_id not in remaining:
                continue
            remaining.remove(edge_id)
            component_edges.add(edge_id)

            edge = edges[edge_id]
            for node in [edge["start_node"], edge["end_node"]]:
                component_nodes.add(node)
                for neighbor_edge_id, _neighbor_node in adjacency.get(node, []):
                    if neighbor_edge_id in remaining:
                        queue.append(neighbor_edge_id)

        components.append({"edge_ids": component_edges, "nodes": component_nodes})

    return components


def _transform_chainages_for_order(
    raw_chainages: list[float],
    line_length: float,
    reverse_for_order: bool,
) -> list[float]:
    if reverse_for_order:
        return [max(float(line_length) - float(value), 0.0) for value in raw_chainages]
    return [max(float(value), 0.0) for value in raw_chainages]


def _candidate_sort_key(candidate: dict) -> tuple:
    return (
        float(candidate.get("_sort_anchor", math.inf)),
        int(candidate.get("line_order", 999999)),
        float(candidate.get("_sort_local", math.inf)),
        str(candidate.get("pnu", "")),
    )


def _resolve_component_local_distances(
    points: list[Point],
    edge_records: tuple[dict, ...],
    node_distances: dict[tuple[float, float], float],
) -> list[float]:
    local_distances: list[float] = []
    for point in points:
        best_distance = math.inf
        for edge in edge_records:
            geometry = edge["geometry"]
            if point.distance(geometry) > NETWORK_TOLERANCE:
                continue
            projection = float(geometry.project(point))
            start_distance = node_distances.get(edge["start_node"], math.inf) + projection
            end_distance = node_distances.get(edge["end_node"], math.inf) + (float(edge["length"]) - projection)
            best_distance = min(best_distance, start_distance, end_distance)
        if math.isfinite(best_distance):
            local_distances.append(best_distance)
    return local_distances


def build_route_geometry(facilities: gpd.GeoDataFrame, logger):
    line_groups = build_line_groups(facilities, main_line_reverse=False, logger=logger)
    if line_groups.empty:
        return None
    main_rows = line_groups.loc[line_groups["line_role"] == "메인"]
    if main_rows.empty:
        return None
    return main_rows.iloc[0].geometry


def build_line_groups(
    facilities: gpd.GeoDataFrame,
    main_line_reverse: bool,
    logger,
) -> gpd.GeoDataFrame:
    columns = [
        "line_group_id",
        "line_role",
        "line_order",
        "group_length_m",
        "anchor_chainage_m",
        "direction_label",
        "reverse_for_order",
        "edge_records",
        "anchor_node",
        "node_distances",
    ]
    if facilities.empty:
        logger.warning("관로 라인 그룹을 생성할 데이터가 없습니다.")
        return gpd.GeoDataFrame(columns=columns, geometry=[], crs=facilities.crs)

    edges, adjacency = _build_network_edges(facilities)
    if not edges:
        logger.warning("관로 라인 그룹으로 해석할 선형 geometry가 없습니다.")
        return gpd.GeoDataFrame(columns=columns, geometry=[], crs=facilities.crs)

    main_path_nodes, main_path_edge_ids, main_length = _find_main_path(edges, adjacency)
    if not main_path_edge_ids:
        logger.warning("메인관로 기준선을 생성하지 못했습니다.")
        return gpd.GeoDataFrame(columns=columns, geometry=[], crs=facilities.crs)

    main_geometry = _build_path_geometry(main_path_nodes, main_path_edge_ids, edges)
    if main_geometry is None or main_geometry.is_empty:
        logger.warning("메인관로 기준선을 생성하지 못했습니다.")
        return gpd.GeoDataFrame(columns=columns, geometry=[], crs=facilities.crs)

    main_node_raw_chainages: dict[tuple[float, float], float] = {main_path_nodes[0]: 0.0}
    cumulative = 0.0
    for edge_id, node in zip(main_path_edge_ids, main_path_nodes[1:]):
        cumulative += float(edges[edge_id]["length"])
        main_node_raw_chainages[node] = cumulative

    main_node_oriented_chainages = {
        node: _transform_chainages_for_order([chainage], main_length, main_line_reverse)[0]
        for node, chainage in main_node_raw_chainages.items()
    }

    rows: list[dict] = [
        {
            "line_group_id": None,
            "line_role": "메인",
            "line_order": 1,
            "group_length_m": float(main_length),
            "anchor_chainage_m": 0.0,
            "direction_label": "종점→시점" if main_line_reverse else "시점→종점",
            "reverse_for_order": bool(main_line_reverse),
            "edge_records": tuple(edges[edge_id] for edge_id in main_path_edge_ids),
            "anchor_node": main_path_nodes[0],
            "node_distances": main_node_raw_chainages,
            "geometry": main_geometry,
        }
    ]

    remaining_edge_ids = set(edges.keys()) - set(main_path_edge_ids)
    branch_components = _collect_connected_components(remaining_edge_ids, edges, adjacency)
    branch_rows: list[dict] = []
    for component in branch_components:
        edge_records = tuple(
            edges[edge_id]
            for edge_id in sorted(component["edge_ids"], key=_edge_sort_key)
        )
        component_geometry = unary_union([edge["geometry"] for edge in edge_records])
        if component_geometry is None or component_geometry.is_empty:
            continue

        anchor_candidates = [
            node for node in component["nodes"] if node in main_node_oriented_chainages
        ]
        if anchor_candidates:
            anchor_node = min(
                anchor_candidates,
                key=lambda node: (float(main_node_oriented_chainages[node]), node),
            )
            anchor_chainage = float(main_node_oriented_chainages[anchor_node])
        else:
            component_point, main_point = nearest_points(component_geometry, main_geometry)
            raw_anchor_chainage = float(main_geometry.project(main_point))
            anchor_chainage = _transform_chainages_for_order(
                [raw_anchor_chainage],
                main_length,
                main_line_reverse,
            )[0]
            anchor_node = min(
                component["nodes"],
                key=lambda node: _point_from_node(node).distance(component_point),
            )

        node_distances, _previous = _dijkstra(
            adjacency,
            edges,
            anchor_node,
            allowed_edges=component["edge_ids"],
        )
        branch_rows.append(
            {
                "line_group_id": None,
                "line_role": "지선",
                "line_order": None,
                "group_length_m": float(sum(edge["length"] for edge in edge_records)),
                "anchor_chainage_m": float(anchor_chainage),
                "direction_label": "분기점→말단",
                "reverse_for_order": False,
                "edge_records": edge_records,
                "anchor_node": anchor_node,
                "node_distances": node_distances,
                "geometry": component_geometry,
            }
        )

    branch_rows.sort(
        key=lambda row: (
            0 if math.isfinite(float(row["anchor_chainage_m"])) else 1,
            float(row["anchor_chainage_m"]),
            float(row["group_length_m"]),
        )
    )
    for line_order, row in enumerate(branch_rows, start=2):
        row["line_order"] = line_order

    all_rows = rows + branch_rows
    for index, row in enumerate(all_rows, start=1):
        row["line_group_id"] = f"L{index:02d}"

    line_groups = gpd.GeoDataFrame(all_rows, geometry="geometry", crs=facilities.crs)
    line_groups = line_groups.sort_values(by=["line_order"], kind="stable").reset_index(drop=True)

    for row in line_groups.itertuples():
        anchor_text = (
            f"{float(row.anchor_chainage_m):.2f}m"
            if math.isfinite(float(row.anchor_chainage_m))
            else "연결없음"
        )
        logger.info(
            "라인그룹 %s | 구분=%s | 순서=%s | 길이=%.2fm | 기준점=%s | 방향=%s",
            row.line_group_id,
            row.line_role,
            row.line_order,
            row.group_length_m,
            anchor_text,
            row.direction_label,
        )
    return line_groups


def build_register_order_from_temporary_area(
    temporary_intersection: gpd.GeoDataFrame,
    line_groups: gpd.GeoDataFrame,
    sheet_frame: gpd.GeoDataFrame | None,
    logger,
) -> pd.DataFrame:
    columns = ["pnu", "route_chainage_m", "register_priority", "line_group_id", "line_order"]
    if line_groups.empty:
        logger.warning("라인 그룹이 없어 조서번호를 기본 정렬 순서로 산정합니다.")
        return pd.DataFrame(columns=columns)

    if temporary_intersection.empty or "pnu" not in temporary_intersection.columns:
        logger.warning("임시점용 geometry가 없어 조서번호를 기본 정렬 순서로 산정합니다.")
        return pd.DataFrame(columns=columns)

    working = temporary_intersection[["pnu", temporary_intersection.geometry.name]].copy()
    working = working.loc[
        working["pnu"].notna()
        & working.geometry.notna()
        & ~working.geometry.is_empty
    ].copy()
    if working.empty:
        logger.warning("조서번호 산정을 위한 임시점용 geometry가 없습니다.")
        return pd.DataFrame(columns=columns)

    rows: list[dict] = []
    main_row = line_groups.sort_values(by=["line_order"], kind="stable").iloc[0]
    for pnu, group in working.groupby("pnu", dropna=False):
        merged = unary_union(group.geometry.tolist())
        if merged is None or merged.is_empty:
            continue

        best_candidate: dict | None = None
        for line_group in line_groups.itertuples():
            intersection_geometry = line_group.geometry.intersection(merged)
            if intersection_geometry is None or intersection_geometry.is_empty:
                continue

            if line_group.line_role == "메인":
                raw_chainages = _extract_chainages_from_geometry(line_group.geometry, intersection_geometry)
                local_chainages = _transform_chainages_for_order(
                    raw_chainages,
                    float(line_group.group_length_m),
                    bool(line_group.reverse_for_order),
                )
                if not local_chainages:
                    continue
                local_chainage = min(local_chainages)
                candidate = {
                    "pnu": str(pnu),
                    "route_chainage_m": local_chainage,
                    "line_group_id": line_group.line_group_id,
                    "line_order": int(line_group.line_order),
                    "_sort_anchor": local_chainage,
                    "_sort_local": 0.0,
                }
            else:
                points = _extract_points_from_geometry(intersection_geometry)
                local_distances = _resolve_component_local_distances(
                    points,
                    line_group.edge_records,
                    line_group.node_distances,
                )
                if not local_distances:
                    fallback_point = merged.representative_point()
                    closest_point = nearest_points(fallback_point, line_group.geometry)[1]
                    local_distances = _resolve_component_local_distances(
                        [closest_point],
                        line_group.edge_records,
                        line_group.node_distances,
                    )
                if not local_distances:
                    continue
                local_chainage = min(local_distances)
                candidate = {
                    "pnu": str(pnu),
                    "route_chainage_m": local_chainage,
                    "line_group_id": line_group.line_group_id,
                    "line_order": int(line_group.line_order),
                    "_sort_anchor": float(line_group.anchor_chainage_m),
                    "_sort_local": local_chainage,
                }

            if best_candidate is None or _candidate_sort_key(candidate) < _candidate_sort_key(best_candidate):
                best_candidate = candidate

        if best_candidate is None:
            representative_point = merged.representative_point()
            raw_chainage = float(main_row.geometry.project(representative_point))
            local_chainage = _transform_chainages_for_order(
                [raw_chainage],
                float(main_row.group_length_m),
                bool(main_row.reverse_for_order),
            )[0]
            best_candidate = {
                "pnu": str(pnu),
                "route_chainage_m": local_chainage,
                "line_group_id": main_row.line_group_id,
                "line_order": int(main_row.line_order),
                "_sort_anchor": local_chainage,
                "_sort_local": 0.0,
            }

        rows.append(best_candidate)

    if not rows:
        logger.warning("임시점용 geometry에서 조서번호 산정 기준점을 생성하지 못했습니다.")
        return pd.DataFrame(columns=columns)

    order_df = pd.DataFrame(rows)
    order_df = order_df.sort_values(
        by=["_sort_anchor", "line_order", "_sort_local", "pnu"],
        kind="stable",
    ).reset_index(drop=True)
    order_df["register_priority"] = order_df.index + 1
    logger.info("임시점용 기준 메인관로/지선관로 조서번호 순서 %s건을 산정했습니다.", len(order_df))
    return order_df[columns]


def _extract_chainages_from_geometry(route_geometry, geometry) -> list[float]:
    if geometry is None or geometry.is_empty:
        return []

    geometry_type = geometry.geom_type
    if geometry_type == "Point":
        return [float(route_geometry.project(geometry))]
    if geometry_type == "MultiPoint":
        return [float(route_geometry.project(point)) for point in geometry.geoms if not point.is_empty]
    if geometry_type in {"LineString", "LinearRing"}:
        return [float(route_geometry.project(Point(coord))) for coord in geometry.coords]
    if geometry_type == "MultiLineString":
        values: list[float] = []
        for part in geometry.geoms:
            values.extend(_extract_chainages_from_geometry(route_geometry, part))
        return values
    if geometry_type == "GeometryCollection":
        values: list[float] = []
        for part in geometry.geoms:
            values.extend(_extract_chainages_from_geometry(route_geometry, part))
        return values
    return [float(route_geometry.project(geometry.representative_point()))]


def build_permanent_buffer_polygons(
    facilities: gpd.GeoDataFrame,
    config: dict,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty:
        logger.warning("영구점용 버퍼를 생성할 관로 데이터가 없습니다.")
        return facilities.iloc[0:0].copy()

    permanent_config = config["permanent_occupancy"]
    default_width_m = float(permanent_config["default_width_m"])
    use_pipe_diameter = bool(permanent_config.get("use_pipe_diameter_as_minimum", True))
    minimum_pipe_diameter = float(permanent_config.get("minimum_pipe_diameter_m", 0.0))

    working = facilities.copy()
    if "permanent_allowed" in working.columns:
        allowed_mask = working["permanent_allowed"].fillna(True).astype(bool)
        if (~allowed_mask).any():
            logger.info("영구점용 제외 구간 %s건은 버퍼 생성에서 제외합니다.", int((~allowed_mask).sum()))
        working = working.loc[allowed_mask].copy()
        if working.empty:
            logger.warning("영구점용 적용 대상 구간이 없습니다.")
            return facilities.iloc[0:0].copy()

    def resolve_width(row) -> float:
        diameter_m = meters_from_diameter(row.get("diameter_value"), default=minimum_pipe_diameter)
        if use_pipe_diameter:
            return max(default_width_m, diameter_m)
        return default_width_m

    working["permanent_width_m"] = working.apply(resolve_width, axis=1)
    working = working.loc[working["permanent_width_m"] > 0].copy()
    if working.empty:
        logger.warning("영구점용 폭을 계산할 수 있는 관로가 없습니다.")
        return facilities.iloc[0:0].copy()

    logger.info("관로 영구점용 버퍼를 생성합니다.")
    working[working.geometry.name] = working.apply(
        lambda row: row.geometry.buffer(row["permanent_width_m"] / 2, cap_style="flat"),
        axis=1,
    )
    buffers = gpd.GeoDataFrame(working.copy(), geometry=working.geometry.name, crs=facilities.crs)
    buffers = buffers.loc[~(buffers.geometry.isna() | buffers.geometry.is_empty)].copy()
    if buffers.empty:
        logger.warning("영구점용 버퍼 생성 결과가 없습니다.")
        return facilities.iloc[0:0].copy()
    return buffers


def build_temporary_buffer_polygons(
    facilities: gpd.GeoDataFrame,
    selection: TemporaryOccupancySelection,
    config: dict,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty:
        logger.warning("임시점용 버퍼를 생성할 관로 데이터가 없습니다.")
        return facilities.iloc[0:0].copy()

    working = facilities.copy()
    if "temporary_allowed" in working.columns:
        allowed_mask = working["temporary_allowed"].fillna(True).astype(bool)
        if (~allowed_mask).any():
            logger.info("임시점용 제외 구간 %s건은 버퍼 생성에서 제외합니다.", int((~allowed_mask).sum()))
        working = working.loc[allowed_mask].copy()
        if working.empty:
            logger.warning("임시점용 적용 대상 구간이 없습니다.")
            return facilities.iloc[0:0].copy()

    if "temporary_width_m" in working.columns:
        working["temporary_width_m"] = pd.to_numeric(working["temporary_width_m"], errors="coerce")
    else:
        working["temporary_width_m"] = working["diameter_value"].apply(
            lambda value: calculate_temporary_width(value, selection, config)
        )
    missing_width_mask = working["temporary_width_m"].isna()
    if missing_width_mask.any():
        working.loc[missing_width_mask, "temporary_width_m"] = working.loc[missing_width_mask, "diameter_value"].apply(
            lambda value: calculate_temporary_width(value, selection, config)
        )
    working = working.loc[working["temporary_width_m"] > 0].copy()
    if working.empty:
        logger.warning("임시점용 폭을 계산할 수 있는 관로가 없습니다.")
        return facilities.iloc[0:0].copy()

    logger.info("관로 임시 점용 버퍼를 생성합니다.")
    working[working.geometry.name] = working.apply(
        lambda row: row.geometry.buffer(row["temporary_width_m"] / 2, cap_style="flat"),
        axis=1,
    )
    buffers = gpd.GeoDataFrame(working.copy(), geometry=working.geometry.name, crs=facilities.crs)
    buffers = buffers.loc[~(buffers.geometry.isna() | buffers.geometry.is_empty)].copy()
    if buffers.empty:
        logger.warning("임시 점용 버퍼 생성 결과가 없습니다.")
        return facilities.iloc[0:0].copy()
    return buffers


def _clip_buffers_to_boundary(
    buffers: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    logger,
    log_prefix: str,
) -> gpd.GeoDataFrame:
    if buffers.empty or boundary.empty:
        return buffers

    logger.info("%s를 도로/점용 경계로 제한합니다.", log_prefix)
    boundary_subset = boundary[[boundary.geometry.name]].copy()
    clipped = gpd.overlay(buffers, boundary_subset, how="intersection", keep_geom_type=False)
    if clipped.empty:
        logger.warning("도로/점용 경계와 교차하는 %s가 없습니다.", log_prefix)
        return clipped
    return clipped


def compute_permanent_intersection_from_facilities(
    facilities: gpd.GeoDataFrame,
    parcels: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    config: dict,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty or parcels.empty:
        logger.warning("영구점용 산정을 위한 레이어에 데이터가 없습니다.")
        result = parcels.iloc[0:0].copy()
        result["permanent_area_m2"] = []
        return result

    buffers = build_permanent_buffer_polygons(facilities, config, logger)
    if buffers.empty:
        result = parcels.iloc[0:0].copy()
        result["permanent_area_m2"] = []
        return result

    if not boundary.empty:
        logger.info("영구점용 버퍼를 도로/점용 경계로 제한합니다.")
        boundary_subset = boundary[[boundary.geometry.name]].copy()
        buffers = gpd.overlay(buffers, boundary_subset, how="intersection", keep_geom_type=False)
        if buffers.empty:
            logger.warning("도로/점용 경계와 교차하는 영구점용 버퍼가 없습니다.")
            result = parcels.iloc[0:0].copy()
            result["permanent_area_m2"] = []
            return result

    dissolved = buffers[[buffers.geometry.name]].dissolve().reset_index(drop=True)
    logger.info("영구점용 버퍼와 지적도의 교차 분석을 시작합니다.")
    intersection = gpd.overlay(parcels, dissolved, how="intersection", keep_geom_type=False)
    if intersection.empty:
        logger.warning("영구점용 대상 필지가 없습니다.")
        intersection["permanent_area_m2"] = []
        return intersection

    intersection = intersection.explode(ignore_index=True)
    intersection = intersection.loc[intersection.geometry.area > 0].copy()
    intersection["permanent_area_m2"] = intersection.geometry.area
    logger.info("영구점용 면적 산정 대상 %s건을 확인했습니다.", len(intersection))
    return intersection


def compute_temporary_intersection_from_facilities(
    facilities: gpd.GeoDataFrame,
    parcels: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    selection: TemporaryOccupancySelection,
    config: dict,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty or parcels.empty:
        logger.warning("임시 점용 산정을 위한 레이어에 데이터가 없습니다.")
        result = parcels.iloc[0:0].copy()
        result["temporary_area_m2"] = []
        return result

    buffers = build_temporary_buffer_polygons(facilities, selection, config, logger)
    if buffers.empty:
        result = parcels.iloc[0:0].copy()
        result["temporary_area_m2"] = []
        return result

    permanent_buffers = build_permanent_buffer_polygons(facilities, config, logger)

    buffers = _clip_buffers_to_boundary(buffers, boundary, logger, "임시 점용 버퍼")
    if buffers.empty:
        result = parcels.iloc[0:0].copy()
        result["temporary_area_m2"] = []
        return result
    permanent_buffers = _clip_buffers_to_boundary(permanent_buffers, boundary, logger, "영구점용 버퍼")

    dissolved = buffers[[buffers.geometry.name]].dissolve().reset_index(drop=True)
    if not permanent_buffers.empty:
        logger.info("임시 점용 버퍼에서 영구점용 버퍼를 제외합니다.")
        permanent_dissolved = permanent_buffers[[permanent_buffers.geometry.name]].dissolve().reset_index(drop=True)
        dissolved = gpd.overlay(dissolved, permanent_dissolved, how="difference", keep_geom_type=False)
        if dissolved.empty:
            logger.warning("영구점용을 제외한 임시 점용 형상이 없습니다.")
            result = parcels.iloc[0:0].copy()
            result["temporary_area_m2"] = []
            return result

    logger.info("임시 점용 버퍼와 지적도의 교차 분석을 시작합니다.")
    intersection = gpd.overlay(parcels, dissolved, how="intersection", keep_geom_type=False)
    if intersection.empty:
        logger.warning("임시 점용 대상 필지가 없습니다.")
        intersection["temporary_area_m2"] = []
        return intersection

    intersection = intersection.explode(ignore_index=True)
    intersection = intersection.loc[intersection.geometry.area > 0].copy()
    intersection["temporary_area_m2"] = intersection.geometry.area
    logger.info("임시 점용 면적 산정 대상 %s건을 확인했습니다.", len(intersection))
    return intersection


def compute_facility_segments(
    facilities: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    parcels: gpd.GeoDataFrame,
    logger,
) -> gpd.GeoDataFrame:
    if facilities.empty:
        logger.warning("관로 레이어에 데이터가 없습니다.")
        return facilities.iloc[0:0].copy()

    logger.info("관로를 도로/점용 경계로 1차 절단합니다.")
    if boundary.empty:
        clipped = facilities.copy()
    else:
        boundary_subset = boundary[[boundary.geometry.name]].copy()
        clipped = gpd.overlay(facilities, boundary_subset, how="intersection", keep_geom_type=False)

    if clipped.empty:
        logger.warning("도로/점용 경계 내부에 관로가 없습니다.")
        return clipped

    clipped = clipped.explode(ignore_index=True)
    clipped = clipped.loc[~(clipped.geometry.isna() | clipped.geometry.is_empty)].copy()

    logger.info("절단된 관로를 필지 단위로 다시 교차 분석합니다.")
    segments = gpd.overlay(clipped, parcels, how="intersection", keep_geom_type=False)
    if segments.empty:
        logger.warning("필지와 교차하는 관로 구간이 없습니다.")
        return segments

    segments = segments.explode(ignore_index=True)
    segments = segments.loc[~(segments.geometry.isna() | segments.geometry.is_empty)].copy()
    logger.info("임시 점용 산정 대상 관로 구간 %s건을 확인했습니다.", len(segments))
    return segments
