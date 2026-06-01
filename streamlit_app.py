"""
Streamlit Cloud 배포용 용지도 조서 산출 앱.
기존 core/ 분석 엔진을 그대로 호출합니다.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

# ── 비밀번호 인증 ──────────────────────────────────────────────

def check_password() -> bool:
    """세션 비밀번호 확인. secrets에 APP_PASSWORD가 없으면 인증 없이 통과."""
    app_password = st.secrets.get("APP_PASSWORD", "")
    if not app_password:
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title("용지도 조서 산출 프로그램")
    st.markdown("접근이 제한되어 있습니다. 비밀번호를 입력하세요.")
    pwd = st.text_input("비밀번호", type="password", key="pwd_input")
    if st.button("로그인"):
        if pwd == app_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    return False


# ── 파일 저장 유틸 ─────────────────────────────────────────────

def _get_work_dir() -> Path:
    """세션별 임시 작업 디렉토리."""
    if "work_dir" not in st.session_state:
        st.session_state["work_dir"] = tempfile.mkdtemp(prefix="yonggido_")
    return Path(st.session_state["work_dir"])


def _save_uploaded_files(uploaded_files, sub_dir: str) -> Path:
    """업로드된 파일들을 작업 디렉토리 하위에 저장하고 디렉토리 경로를 반환."""
    target = _get_work_dir() / sub_dir
    target.mkdir(parents=True, exist_ok=True)
    for uf in uploaded_files:
        (target / uf.name).write_bytes(uf.getvalue())
    return target


def _find_shp(directory: Path) -> str | None:
    for f in directory.iterdir():
        if f.suffix.lower() == ".shp":
            return str(f)
    return None


def _find_table(directory: Path) -> str | None:
    for f in directory.iterdir():
        if f.suffix.lower() in {".xlsx", ".xlsm", ".csv"}:
            return str(f)
    return None


def _cleanup_work_dir():
    work_dir = st.session_state.pop("work_dir", None)
    if work_dir and Path(work_dir).exists():
        shutil.rmtree(work_dir, ignore_errors=True)


# ── 설정 로드 ──────────────────────────────────────────────────

@st.cache_data
def load_config():
    from core.pipeline import load_config as _load
    return _load()


# ── 메인 앱 ───────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="용지도 조서 산출",
        page_icon="📐",
        layout="wide",
    )

    if not check_password():
        return

    st.title("용지도 조서 산출 프로그램")
    st.caption("상수도 관로 편입/임시점용 면적을 자동 산출합니다.")

    config = load_config()

    # ── 사이드바: 분석 옵션 ─────────────────────────────────────
    with st.sidebar:
        st.header("분석 옵션")

        temp_config = config["temporary_occupancy"]
        defaults = temp_config["defaults"]

        soil_cover = st.selectbox(
            "토피 (m)",
            options=temp_config["soil_cover_options_m"],
            index=temp_config["soil_cover_options_m"].index(defaults["soil_cover_m"]),
        )
        pavement = st.selectbox(
            "포장상태",
            options=temp_config["pavement_options"],
            index=temp_config["pavement_options"].index(defaults["pavement_status"]),
        )
        excavation_slope = st.selectbox(
            "터파기 기울기",
            options=temp_config["excavation_slope_options"],
            index=temp_config["excavation_slope_options"].index(defaults["excavation_slope"]),
        )

        st.divider()
        st.header("출력 설정")

        criterion_label = st.text_input(
            "기준구분",
            value=config["analysis"]["default_criterion_label"],
        )
        file_prefix = st.text_input(
            "파일명 접두어",
            value=config["output"]["file_prefix"],
        )
        round_digits = st.number_input(
            "반올림 자릿수",
            min_value=0, max_value=6,
            value=int(config["analysis"]["round_digits"]),
        )
        main_line_dir = st.selectbox(
            "메인관로 방향",
            options=config["numbering"]["main_line_direction_options"],
            index=0,
        )

    # ── 탭 UI ──────────────────────────────────────────────────
    tab_upload, tab_mapping, tab_run = st.tabs([
        "1. 파일 업로드",
        "2. 필드 매핑",
        "3. 실행 및 결과",
    ])

    # ── 탭 1: 파일 업로드 ──────────────────────────────────────
    with tab_upload:
        st.subheader("입력 파일 업로드")
        st.info("SHP 파일은 .shp, .shx, .dbf, .prj, .cpg를 함께 업로드하세요.")

        col1, col2 = st.columns(2)
        with col1:
            cadastral_files = st.file_uploader(
                "지적도 SHP (필수)",
                accept_multiple_files=True,
                key="cadastral",
                type=["shp", "shx", "dbf", "prj", "cpg"],
            )
            facility_files = st.file_uploader(
                "관로 SHP (필수)",
                accept_multiple_files=True,
                key="facility",
                type=["shp", "shx", "dbf", "prj", "cpg"],
            )
            code_table_file = st.file_uploader(
                "법정동코드/행정구역 파일 (필수)",
                accept_multiple_files=False,
                key="code_table",
                type=["xlsx", "xlsm", "csv"],
            )

        with col2:
            boundary_files = st.file_uploader(
                "도로/점용 경계 SHP (선택)",
                accept_multiple_files=True,
                key="boundary",
                type=["shp", "shx", "dbf", "prj", "cpg"],
            )
            sheet_files = st.file_uploader(
                "도곽 SHP/DXF (선택)",
                accept_multiple_files=True,
                key="sheet",
                type=["shp", "shx", "dbf", "prj", "cpg", "dxf"],
            )

        # 업로드 상태 표시
        required_ok = bool(cadastral_files and facility_files and code_table_file)
        if required_ok:
            st.success("필수 파일이 모두 업로드되었습니다.")
        else:
            missing = []
            if not cadastral_files:
                missing.append("지적도 SHP")
            if not facility_files:
                missing.append("관로 SHP")
            if not code_table_file:
                missing.append("법정동코드 파일")
            st.warning(f"필수 파일 누락: {', '.join(missing)}")

    # ── 탭 2: 필드 매핑 ────────────────────────────────────────
    with tab_mapping:
        st.subheader("필드 매핑")

        if not required_ok:
            st.warning("먼저 필수 파일을 모두 업로드하세요.")
        else:
            # 파일 저장
            cad_dir = _save_uploaded_files(cadastral_files, "cadastral")
            fac_dir = _save_uploaded_files(facility_files, "facility")
            code_dir = _save_uploaded_files([code_table_file], "code_table")

            cad_path = _find_shp(cad_dir)
            fac_path = _find_shp(fac_dir)
            code_path = _find_table(code_dir)

            if not cad_path or not fac_path or not code_path:
                st.error("SHP 또는 테이블 파일을 찾을 수 없습니다. 업로드를 확인하세요.")
            else:
                try:
                    from core.pipeline import build_initial_field_mapping, inspect_input_columns

                    columns_by_source = inspect_input_columns(
                        {
                            "cadastral_path": cad_path,
                            "facility_path": fac_path,
                            "code_table_path": code_path,
                        },
                        config,
                    )
                    auto_mapping = build_initial_field_mapping(config, columns_by_source)
                except Exception as e:
                    st.error(f"필드 자동 추정 실패: {e}")
                    columns_by_source = {}
                    auto_mapping = {}

                # 필드 매핑 UI
                mapping_result: dict[str, dict[str, str | None]] = {}
                field_defs = config["field_mapping_definitions"]

                cols = st.columns(3)
                source_labels = {
                    "cadastral": "지적도",
                    "facility": "관로",
                    "code_table": "행정구역 파일",
                }

                for col_idx, source_name in enumerate(["cadastral", "facility", "code_table"]):
                    with cols[col_idx]:
                        st.markdown(f"**{source_labels[source_name]}**")
                        mapping_result[source_name] = {}
                        source_cols = [""] + columns_by_source.get(source_name, [])
                        for field_key, meta in field_defs[source_name].items():
                            if not meta.get("show_in_ui", True):
                                # 숨김 필드는 자동 추정값 사용
                                mapping_result[source_name][field_key] = auto_mapping.get(source_name, {}).get(field_key)
                                continue
                            default_val = auto_mapping.get(source_name, {}).get(field_key, "")
                            default_idx = source_cols.index(default_val) if default_val in source_cols else 0
                            selected = st.selectbox(
                                f"{meta['label']} {'(필수)' if meta.get('required') else ''}",
                                options=source_cols,
                                index=default_idx,
                                key=f"map_{source_name}_{field_key}",
                            )
                            mapping_result[source_name][field_key] = selected or None

                st.session_state["field_mapping"] = mapping_result

    # ── 탭 3: 실행 및 결과 ─────────────────────────────────────
    with tab_run:
        st.subheader("분석 실행")

        if not required_ok:
            st.warning("먼저 필수 파일을 모두 업로드하세요.")
        elif "field_mapping" not in st.session_state:
            st.warning("필드 매핑 탭에서 매핑을 확인하세요.")
        else:
            if st.button("분석 실행", type="primary", disabled=st.session_state.get("running", False)):
                _run_analysis(
                    config=config,
                    field_mapping=st.session_state["field_mapping"],
                    soil_cover=float(soil_cover),
                    pavement=pavement,
                    excavation_slope=excavation_slope,
                    criterion_label=criterion_label,
                    file_prefix=file_prefix,
                    round_digits=int(round_digits),
                    main_line_dir=main_line_dir,
                    cadastral_files=cadastral_files,
                    facility_files=facility_files,
                    code_table_file=code_table_file,
                    boundary_files=boundary_files,
                    sheet_files=sheet_files,
                )

            # 결과 표시
            if "analysis_result" in st.session_state:
                _show_results()


def _run_analysis(
    config,
    field_mapping,
    soil_cover,
    pavement,
    excavation_slope,
    criterion_label,
    file_prefix,
    round_digits,
    main_line_dir,
    cadastral_files,
    facility_files,
    code_table_file,
    boundary_files,
    sheet_files,
):
    """분석 파이프라인을 실행."""
    import json

    from core.pipeline import AnalysisInputs, run_pipeline
    from core.temporary_occupancy import TemporaryOccupancySelection
    from utils.logger import build_logger

    st.session_state["running"] = True
    log_area = st.empty()
    progress_bar = st.progress(0, text="분석 준비 중...")
    log_lines: list[str] = []

    def log_callback(message: str):
        log_lines.append(message)
        # 마지막 5줄만 표시
        log_area.code("\n".join(log_lines[-5:]), language=None)

    try:
        progress_bar.progress(5, text="파일 저장 중...")

        # 파일 저장
        cad_dir = _save_uploaded_files(cadastral_files, "cadastral")
        fac_dir = _save_uploaded_files(facility_files, "facility")
        code_dir = _save_uploaded_files([code_table_file], "code_table")

        boundary_path = None
        if boundary_files:
            bnd_dir = _save_uploaded_files(boundary_files, "boundary")
            boundary_path = _find_shp(bnd_dir)

        sheet_path = None
        if sheet_files:
            sht_dir = _save_uploaded_files(sheet_files, "sheet")
            sheet_path = _find_shp(sht_dir)
            if not sheet_path:
                # DXF 파일 확인
                for f in sht_dir.iterdir():
                    if f.suffix.lower() == ".dxf":
                        sheet_path = str(f)
                        break

        cad_path = _find_shp(cad_dir)
        fac_path = _find_shp(fac_dir)
        code_path = _find_table(code_dir)

        if not cad_path or not fac_path or not code_path:
            st.error("필수 파일을 찾을 수 없습니다.")
            st.session_state["running"] = False
            return

        # 출력 디렉토리
        output_dir = _get_work_dir() / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        # 설정 오버라이드
        runtime_config = json.loads(json.dumps(config))
        runtime_config["analysis"]["default_criterion_label"] = criterion_label
        runtime_config["output"]["file_prefix"] = file_prefix
        runtime_config["analysis"]["round_digits"] = round_digits
        runtime_config["numbering"]["main_line_direction"] = main_line_dir

        progress_bar.progress(10, text="분석 엔진 실행 중...")

        logger = build_logger(callback=log_callback)

        inputs = AnalysisInputs(
            cadastral_path=cad_path,
            boundary_path=boundary_path,
            facility_path=fac_path,
            code_table_path=code_path,
            sheet_path=sheet_path,
            output_dir=str(output_dir),
            field_mapping=field_mapping,
            temporary_selection=TemporaryOccupancySelection(
                soil_cover_m=soil_cover,
                pavement_status=pavement,
                excavation_slope=excavation_slope,
            ),
            main_line_reverse=(main_line_dir == "역방향"),
        )

        result = run_pipeline(inputs, runtime_config, logger)

        progress_bar.progress(100, text="완료!")

        st.session_state["analysis_result"] = {
            "source_table": result.source_table,
            "data_table": result.data_table,
            "error_table": result.error_table,
            "output_files": result.output_files,
            "analysis_crs": result.analysis_crs,
            "cad_crs": result.cad_crs,
            "log": "\n".join(log_lines),
        }

        st.session_state["running"] = False
        st.rerun()

    except Exception as e:
        progress_bar.progress(100, text="오류 발생")
        st.error(f"분석 실패: {e}")
        st.session_state["running"] = False


def _show_results():
    """분석 결과를 표시하고 다운로드 버튼을 제공."""
    result = st.session_state["analysis_result"]
    source_table: pd.DataFrame = result["source_table"]
    data_table: pd.DataFrame = result["data_table"]
    error_table: pd.DataFrame = result["error_table"]
    output_files: dict[str, str] = result["output_files"]

    st.divider()
    st.subheader("분석 결과")

    # 요약
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("편입 필지 수", f"{len(source_table)}건")
    with col2:
        perm_sum = float(source_table["영구점용면적"].fillna(0).sum()) if not source_table.empty else 0
        st.metric("영구점용면적 합계", f"{perm_sum:.2f} m2")
    with col3:
        temp_sum = float(source_table["임시점용면적"].fillna(0).sum()) if not source_table.empty else 0
        st.metric("임시점용면적 합계", f"{temp_sum:.2f} m2")

    st.caption(f"분석 좌표계: {result['analysis_crs']} / CAD 좌표계: {result['cad_crs']}")

    # 테이블 미리보기
    tab_src, tab_data, tab_err, tab_log = st.tabs(["조서원본", "DATA", "오류목록", "처리 로그"])
    with tab_src:
        if source_table.empty:
            st.info("조서 결과가 비어 있습니다.")
        else:
            st.dataframe(source_table, use_container_width=True, height=400)
    with tab_data:
        if data_table.empty:
            st.info("DATA 시트가 비어 있습니다.")
        else:
            st.dataframe(data_table, use_container_width=True, height=400)
    with tab_err:
        if error_table.empty:
            st.info("오류가 없습니다.")
        else:
            st.dataframe(error_table, use_container_width=True, height=300)
    with tab_log:
        st.code(result.get("log", ""), language=None)

    # 다운로드
    st.divider()
    st.subheader("결과 다운로드")

    download_cols = st.columns(3)

    # 개별 파일 다운로드
    for idx, (file_key, file_path) in enumerate(output_files.items()):
        fpath = Path(file_path)
        if fpath.exists():
            with download_cols[idx % 3]:
                with open(fpath, "rb") as f:
                    st.download_button(
                        label=f"{fpath.name}",
                        data=f.read(),
                        file_name=fpath.name,
                        key=f"dl_{file_key}",
                    )

    # ZIP 전체 다운로드
    st.divider()
    existing_files = [Path(fp) for fp in output_files.values() if Path(fp).exists()]
    if existing_files:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in existing_files:
                zf.write(fpath, fpath.name)
        zip_buffer.seek(0)

        st.download_button(
            label="전체 결과 ZIP 다운로드",
            data=zip_buffer,
            file_name="조서산출_전체결과.zip",
            mime="application/zip",
            type="primary",
        )


if __name__ == "__main__":
    main()
