from __future__ import annotations

from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd


class ReaderError(Exception):
    pass


def validate_shapefile_components(path: str | Path) -> None:
    target = Path(path)
    if not target.exists():
        raise ReaderError(f"파일을 찾을 수 없습니다: {target}")

    if target.suffix.lower() != ".shp":
        return

    required_extensions = [".shp", ".shx", ".dbf"]
    missing_files = [
        target.with_suffix(ext).name
        for ext in required_extensions
        if not target.with_suffix(ext).exists()
    ]
    if missing_files:
        joined = ", ".join(missing_files)
        raise ReaderError(f"SHP 구성 파일이 누락되었습니다: {joined}")


def inspect_vector_fields(path: str | Path) -> list[str]:
    validate_shapefile_components(path)
    try:
        preview = gpd.read_file(path, rows=1)
    except TypeError:
        preview = gpd.read_file(path).head(1)

    geometry_name = preview.geometry.name if hasattr(preview, "geometry") else "geometry"
    return [column for column in preview.columns if column != geometry_name]


def inspect_csv_fields(path: str | Path, encodings: Iterable[str]) -> list[str]:
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            frame = pd.read_csv(path, encoding=encoding, nrows=0, dtype=str)
            return list(frame.columns)
        except Exception as error:  # pragma: no cover
            last_error = error
    raise ReaderError(f"CSV 헤더를 읽지 못했습니다: {last_error}")


def inspect_table_fields(path: str | Path, encodings: Iterable[str]) -> list[str]:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        try:
            frame = pd.read_excel(target, nrows=0, dtype=str)
        except Exception as error:
            raise ReaderError(f"엑셀 헤더를 읽지 못했습니다: {error}") from error
        return list(frame.columns)
    return inspect_csv_fields(target, encodings)


def read_vector(
    path: str | Path,
    layer_name: str,
    logger,
    columns: list[str] | None = None,
) -> gpd.GeoDataFrame:
    validate_shapefile_components(path)
    logger.info("%s 레이어를 읽는 중입니다: %s", layer_name, path)

    try:
        read_kwargs = {}
        if columns:
            read_kwargs["columns"] = columns
        gdf = gpd.read_file(path, **read_kwargs)
    except TypeError:
        gdf = gpd.read_file(path)
        if columns:
            keep_columns = [column for column in columns if column in gdf.columns]
            geometry_name = gdf.geometry.name
            gdf = gdf[keep_columns + [geometry_name]]
    except Exception as error:
        raise ReaderError(f"{layer_name} 레이어를 읽지 못했습니다: {error}") from error

    if gdf.empty:
        raise ReaderError(f"{layer_name} 레이어에 데이터가 없습니다.")
    if gdf.geometry is None:
        raise ReaderError(f"{layer_name} 레이어에서 geometry 컬럼을 찾지 못했습니다.")
    return gdf


def read_csv(
    path: str | Path,
    logger,
    encodings: Iterable[str],
    usecols: list[str] | None = None,
) -> pd.DataFrame:
    target = Path(path)
    if not target.exists():
        raise ReaderError(f"CSV 파일을 찾을 수 없습니다: {target}")

    last_error: Exception | None = None
    for encoding in encodings:
        try:
            logger.info("CSV를 읽는 중입니다. 인코딩=%s, 경로=%s", encoding, path)
            return pd.read_csv(target, encoding=encoding, dtype=str, usecols=usecols)
        except UnicodeDecodeError as error:
            last_error = error
        except ValueError as error:
            last_error = error
            break
        except Exception as error:
            last_error = error

    raise ReaderError(f"CSV 파일을 읽지 못했습니다: {last_error}")


def read_table(
    path: str | Path,
    logger,
    encodings: Iterable[str],
    usecols: list[str] | None = None,
) -> pd.DataFrame:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        if not target.exists():
            raise ReaderError(f"엑셀 파일을 찾을 수 없습니다: {target}")
        try:
            logger.info("엑셀을 읽는 중입니다: %s", path)
            return pd.read_excel(target, dtype=str, usecols=usecols)
        except Exception as error:
            raise ReaderError(f"엑셀 파일을 읽지 못했습니다: {error}") from error
    return read_csv(target, logger, encodings, usecols=usecols)
