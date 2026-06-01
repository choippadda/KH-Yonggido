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

# ── 커스텀 CSS (macOS Tahoe 스타일) ──────────────────────────────

def _build_css(dark: bool) -> str:
    """테마에 따른 CSS를 생성."""
    if dark:
        v = {
            "bg": "#1c1c1e", "bg2": "#2c2c2e", "bg_input": "#3a3a3c",
            "sidebar_bg": "#1c1c1e", "card_bg": "#2c2c2e",
            "text": "#f5f5f7", "text2": "#98989d", "text3": "#636366",
            "border": "#48484a", "border_light": "#3a3a3c",
            "accent": "#0a84ff", "accent_hover": "#409cff",
            "accent_light": "rgba(10,132,255,0.12)",
            "shadow": "0 1px 3px rgba(0,0,0,0.3)",
            "tab_bg": "#3a3a3c", "tab_active_bg": "#48484a",
            "upload_bg": "#2c2c2e",
        }
    else:
        v = {
            "bg": "#f5f5f7", "bg2": "#ffffff", "bg_input": "#ffffff",
            "sidebar_bg": "#fafafa", "card_bg": "#ffffff",
            "text": "#1d1d1f", "text2": "#6e6e73", "text3": "#aeaeb2",
            "border": "#d2d2d7", "border_light": "#e5e5ea",
            "accent": "#0071e3", "accent_hover": "#0077ed",
            "accent_light": "rgba(0,113,227,0.08)",
            "shadow": "0 1px 3px rgba(0,0,0,0.06)",
            "tab_bg": "#f5f5f7", "tab_active_bg": "#ffffff",
            "upload_bg": "#fafafa",
        }

    return f"""
<style>
/* ── 전역 폰트 & 배경 ─────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"] {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

.stApp {{
    background: {v['bg']} !important;
}}

/* ── 메인 컨테이너 ─────────────────────────────── */
.block-container {{
    padding-top: 2rem !important;
    max-width: 960px;
}}

/* ── 헤더 영역 ─────────────────────────────────── */
.app-header-custom {{
    background: linear-gradient(135deg, {v['accent']} 0%, #005bb5 100%);
    border-radius: 14px;
    padding: 28px 32px;
    margin-bottom: 24px;
    color: white;
    box-shadow: 0 4px 16px rgba(0, 113, 227, 0.25);
    display: flex;
    align-items: center;
    justify-content: space-between;
}}

.app-header-left h1 {{
    font-size: 24px;
    font-weight: 700;
    margin: 0 0 4px 0;
    color: white !important;
}}

.app-header-left p {{
    font-size: 14px;
    opacity: 0.85;
    margin: 0;
}}

.app-version {{
    display: inline-block;
    background: rgba(255,255,255,0.2);
    border-radius: 12px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
    margin-left: 8px;
    vertical-align: middle;
}}

/* ── 테마 토글 버튼 ────────────────────────────── */
.theme-toggle-btn {{
    background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 10px;
    padding: 8px 14px;
    cursor: pointer;
    font-size: 18px;
    line-height: 1;
    transition: all 0.2s;
    color: white;
    display: flex;
    align-items: center;
    gap: 6px;
}}

.theme-toggle-btn:hover {{
    background: rgba(255,255,255,0.25);
}}

.theme-toggle-btn .toggle-label {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}

/* ── 로그인 카드 ───────────────────────────────── */
.login-card {{
    background: {v['card_bg']};
    border: 1px solid {v['border']};
    border-radius: 14px;
    padding: 40px;
    max-width: 400px;
    margin: 60px auto;
    box-shadow: {v['shadow']};
    text-align: center;
}}

.login-card h2 {{
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 8px;
    color: {v['text']};
}}

.login-card p {{
    font-size: 13px;
    color: {v['text2']};
    margin-bottom: 24px;
}}

.login-icon {{
    font-size: 48px;
    margin-bottom: 16px;
}}

/* ── 섹션 카드 ─────────────────────────────────── */
.section-card {{
    background: {v['card_bg']};
    border: 1px solid {v['border_light']};
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 16px;
    box-shadow: {v['shadow']};
}}

.section-card h3 {{
    font-size: 15px;
    font-weight: 600;
    color: {v['text']};
    margin: 0 0 4px 0;
    display: flex;
    align-items: center;
    gap: 8px;
}}

.section-card .section-desc {{
    font-size: 12px;
    color: {v['text2']};
    margin-bottom: 16px;
}}

/* ── 위저드 스텝 바 ────────────────────────────── */
.wizard-bar {{
    display: flex;
    align-items: center;
    gap: 0;
    padding: 16px 0;
    margin-bottom: 20px;
}}

.wizard-step-item {{
    display: flex;
    align-items: center;
    gap: 8px;
}}

.step-circle {{
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 700;
    border: 2px solid {v['border']};
    color: {v['text2']};
    background: {v['bg']};
    flex-shrink: 0;
}}

.step-circle.active {{
    background: {v['accent']};
    border-color: {v['accent']};
    color: white;
}}

.step-circle.done {{
    background: #34c759;
    border-color: #34c759;
    color: white;
}}

.step-label {{
    font-size: 12px;
    font-weight: 500;
    color: {v['text2']};
    white-space: nowrap;
}}

.step-label.active {{
    color: {v['text']};
    font-weight: 600;
}}

.step-connector {{
    flex: 1;
    height: 2px;
    background: {v['border']};
    margin: 0 8px;
    min-width: 24px;
}}

.step-connector.done {{
    background: #34c759;
}}

/* ── 파일 업로드 존 ────────────────────────────── */
[data-testid="stFileUploader"] {{
    border: 2px dashed {v['border']} !important;
    border-radius: 10px !important;
    background: {v['upload_bg']} !important;
    transition: all 0.2s;
}}

[data-testid="stFileUploader"]:hover {{
    border-color: {v['accent']} !important;
    background: {v['accent_light']} !important;
}}

/* ── 상태 뱃지 ─────────────────────────────────── */
.status-badge {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
}}

.badge-success {{
    background: rgba(52, 199, 89, 0.1);
    color: #34c759;
}}

.badge-warning {{
    background: rgba(255, 149, 0, 0.1);
    color: #ff9500;
}}

.badge-error {{
    background: rgba(255, 59, 48, 0.1);
    color: #ff3b30;
}}

.badge-info {{
    background: {v['accent_light']};
    color: {v['accent']};
}}

/* ── 결과 메트릭 카드 ──────────────────────────── */
.metric-card {{
    background: {v['card_bg']};
    border: 1px solid {v['border_light']};
    border-radius: 12px;
    padding: 20px;
    text-align: center;
    box-shadow: {v['shadow']};
}}

.metric-card .metric-value {{
    font-size: 28px;
    font-weight: 700;
    color: {v['accent']};
    margin: 4px 0;
}}

.metric-card .metric-label {{
    font-size: 12px;
    color: {v['text2']};
    font-weight: 500;
}}

.metric-card .metric-unit {{
    font-size: 13px;
    color: {v['text3']};
    font-weight: 400;
}}

/* ── 사이드바 스타일 ───────────────────────────── */
[data-testid="stSidebar"] {{
    background: {v['sidebar_bg']} !important;
}}

[data-testid="stSidebar"] .block-container {{
    padding-top: 1.5rem !important;
}}

.sidebar-section {{
    background: {v['card_bg']};
    border: 1px solid {v['border_light']};
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 12px;
}}

.sidebar-section h4 {{
    font-size: 12px;
    font-weight: 600;
    color: {v['text2']};
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 12px 0;
}}

/* ── 버튼 스타일 ───────────────────────────────── */
.stButton > button[kind="primary"] {{
    background: linear-gradient(135deg, {v['accent']} 0%, #005bb5 100%) !important;
    border: none !important;
    border-radius: 8px !important;
    padding: 8px 24px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    box-shadow: 0 2px 8px rgba(0, 113, 227, 0.3) !important;
    transition: all 0.2s !important;
}}

.stButton > button[kind="primary"]:hover {{
    box-shadow: 0 4px 16px rgba(0, 113, 227, 0.4) !important;
    transform: translateY(-1px);
}}

.stButton > button[kind="secondary"] {{
    border-radius: 8px !important;
    font-weight: 500 !important;
    background: {v['card_bg']} !important;
    color: {v['text']} !important;
    border-color: {v['border']} !important;
}}

/* ── 다운로드 버튼 ─────────────────────────────── */
.stDownloadButton > button {{
    border-radius: 8px !important;
    font-weight: 500 !important;
    border: 1px solid {v['border']} !important;
    background: {v['card_bg']} !important;
    color: {v['text']} !important;
    transition: all 0.15s !important;
}}

.stDownloadButton > button:hover {{
    border-color: {v['accent']} !important;
    background: {v['accent_light']} !important;
}}

/* ── 탭 스타일 ─────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {{
    gap: 4px;
    background: {v['tab_bg']};
    border-radius: 10px;
    padding: 4px;
}}

.stTabs [data-baseweb="tab"] {{
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 13px !important;
    padding: 8px 16px !important;
    color: {v['text2']} !important;
}}

.stTabs [aria-selected="true"] {{
    background: {v['tab_active_bg']} !important;
    box-shadow: {v['shadow']} !important;
    color: {v['text']} !important;
}}

/* ── 데이터프레임 ──────────────────────────────── */
[data-testid="stDataFrame"] {{
    border: 1px solid {v['border_light']};
    border-radius: 10px;
    overflow: hidden;
}}

/* ── selectbox ─────────────────────────────────── */
[data-baseweb="select"] {{
    border-radius: 8px !important;
}}

/* ── 알림 스타일 ───────────────────────────────── */
.stAlert {{
    border-radius: 10px !important;
}}

/* ── 구분선 ────────────────────────────────────── */
hr {{
    border: none;
    border-top: 1px solid {v['border_light']};
    margin: 20px 0;
}}

/* ── 진행바 ────────────────────────────────────── */
.stProgress > div > div {{
    border-radius: 4px !important;
    background: {v['border_light']} !important;
}}

.stProgress > div > div > div {{
    background: linear-gradient(90deg, {v['accent']}, #34c759) !important;
    border-radius: 4px !important;
}}

/* ── 로그 영역 ─────────────────────────────────── */
.log-container {{
    background: #1c1c1e;
    border-radius: 10px;
    padding: 16px;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    font-size: 11px;
    line-height: 1.6;
    color: #f5f5f7;
    max-height: 300px;
    overflow-y: auto;
}}

/* ── 푸터 ──────────────────────────────────────── */
.app-footer {{
    text-align: center;
    padding: 24px 0;
    margin-top: 40px;
    border-top: 1px solid {v['border_light']};
    font-size: 12px;
    color: {v['text3']};
}}

/* ── Streamlit 기본 요소 테마 오버라이드 ───────── */
.stMarkdown, .stMarkdown p, .stMarkdown li {{
    color: {v['text']} !important;
}}

label, .stTextInput label, .stSelectbox label, .stNumberInput label {{
    color: {v['text']} !important;
}}

[data-testid="stExpander"] {{
    background: {v['card_bg']} !important;
    border: 1px solid {v['border_light']} !important;
    border-radius: 10px !important;
}}

[data-testid="stExpanderDetails"] {{
    background: {v['card_bg']} !important;
}}

/* 사이드바 텍스트 */
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] .stMarkdown p {{
    color: {v['text']} !important;
}}
</style>
"""


# ── 비밀번호 인증 ──────────────────────────────────────────────

def check_password() -> bool:
    """세션 비밀번호 확인. secrets에 APP_PASSWORD가 없으면 인증 없이 통과."""
    app_password = st.secrets.get("APP_PASSWORD", "")
    if not app_password:
        return True

    if st.session_state.get("authenticated"):
        return True

    if "dark_mode" not in st.session_state:
        st.session_state["dark_mode"] = False
    st.markdown(_build_css(st.session_state["dark_mode"]), unsafe_allow_html=True)

    # 로그인 UI
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        st.markdown("""
        <div class="login-card">
            <div class="login-icon">🔐</div>
            <h2>용지도 조서 산출</h2>
            <p>접근이 제한되어 있습니다.<br>비밀번호를 입력하세요.</p>
        </div>
        """, unsafe_allow_html=True)

        pwd = st.text_input("비밀번호", type="password", key="pwd_input",
                            label_visibility="collapsed",
                            placeholder="비밀번호를 입력하세요")
        if st.button("로그인", type="primary", use_container_width=True):
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


# ── 위저드 스텝 표시 ───────────────────────────────────────────

def _render_wizard_steps(current: int, steps: list[str]):
    """위저드 스텝 바를 렌더링."""
    html_parts = ['<div class="wizard-bar">']
    for i, label in enumerate(steps):
        if i > 0:
            connector_cls = "step-connector done" if i <= current else "step-connector"
            html_parts.append(f'<div class="{connector_cls}"></div>')

        if i < current:
            circle_cls = "step-circle done"
            label_cls = "step-label"
            number = "&#10003;"
        elif i == current:
            circle_cls = "step-circle active"
            label_cls = "step-label active"
            number = str(i + 1)
        else:
            circle_cls = "step-circle"
            label_cls = "step-label"
            number = str(i + 1)

        html_parts.append(f"""
        <div class="wizard-step-item">
            <div class="{circle_cls}">{number}</div>
            <span class="{label_cls}">{label}</span>
        </div>
        """)
    html_parts.append('</div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


# ── 메인 앱 ───────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="용지도 조서 산출",
        page_icon="📐",
        layout="wide",
    )

    if not check_password():
        return

    # ── 다크모드 상태 초기화 ──────────────────────────────────
    if "dark_mode" not in st.session_state:
        st.session_state["dark_mode"] = False

    is_dark = st.session_state["dark_mode"]

    # 커스텀 CSS 삽입 (테마 반영)
    st.markdown(_build_css(is_dark), unsafe_allow_html=True)

    # ── 헤더 + 다크모드 토글 ──────────────────────────────────
    hdr_col1, hdr_col2 = st.columns([6, 1])
    with hdr_col1:
        st.markdown("""
        <div class="app-header-custom">
            <div class="app-header-left">
                <h1>용지도 조서 산출 <span class="app-version">v2.0</span></h1>
                <p>상수도 관로 편입/임시점용 면적을 자동 산출합니다</p>
            </div>
        </div>
        """, unsafe_allow_html=True)
    with hdr_col2:
        toggle_icon = "☀️" if is_dark else "🌙"
        toggle_label = "라이트" if is_dark else "다크"
        if st.button(f"{toggle_icon} {toggle_label}", key="theme_toggle",
                     use_container_width=True):
            st.session_state["dark_mode"] = not is_dark
            st.rerun()

    config = load_config()

    # ── 사이드바: 분석 옵션 ─────────────────────────────────────
    with st.sidebar:
        st.markdown("""
        <div style="padding: 8px 0 16px 0;">
            <span style="font-size: 15px; font-weight: 700; color: #1d1d1f;">설정 패널</span>
        </div>
        """, unsafe_allow_html=True)

        # 임시점용 옵션
        st.markdown('<div class="sidebar-section"><h4>임시점용 옵션</h4></div>',
                    unsafe_allow_html=True)

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

        st.markdown("---")

        # 출력 설정
        st.markdown('<div class="sidebar-section"><h4>출력 설정</h4></div>',
                    unsafe_allow_html=True)

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

    # ── 위저드 스텝 바 ─────────────────────────────────────────
    step_names = ["파일 업로드", "필드 매핑", "실행 및 결과"]

    # 현재 스텝 결정
    required_ok = bool(
        st.session_state.get("_cadastral_files")
        and st.session_state.get("_facility_files")
        and st.session_state.get("_code_table_file")
    )
    has_mapping = "field_mapping" in st.session_state
    current_step = 0
    if required_ok:
        current_step = 1
    if required_ok and has_mapping:
        current_step = 2

    # ── 탭 UI ──────────────────────────────────────────────────
    tab_upload, tab_mapping, tab_run = st.tabs([
        "📁  파일 업로드",
        "🔗  필드 매핑",
        "🚀  실행 및 결과",
    ])

    # ── 탭 1: 파일 업로드 ──────────────────────────────────────
    with tab_upload:
        _render_wizard_steps(0 if not required_ok else 1, step_names)

        st.markdown("""
        <div class="section-card">
            <h3>📂 입력 파일 업로드</h3>
            <div class="section-desc">SHP 파일은 .shp, .shx, .dbf, .prj, .cpg를 함께 업로드하세요.</div>
        </div>
        """, unsafe_allow_html=True)

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**필수 파일**")
            cadastral_files = st.file_uploader(
                "지적도 SHP",
                accept_multiple_files=True,
                key="cadastral",
                type=["shp", "shx", "dbf", "prj", "cpg"],
                help="지적도 Shapefile 세트를 업로드하세요",
            )
            facility_files = st.file_uploader(
                "관로 SHP",
                accept_multiple_files=True,
                key="facility",
                type=["shp", "shx", "dbf", "prj", "cpg"],
                help="관로 Shapefile 세트를 업로드하세요",
            )
            code_table_file = st.file_uploader(
                "법정동코드/행정구역 파일",
                accept_multiple_files=False,
                key="code_table",
                type=["xlsx", "xlsm", "csv"],
                help="법정동코드 또는 행정구역 테이블",
            )

        with col2:
            st.markdown("**선택 파일**")
            boundary_files = st.file_uploader(
                "도로/점용 경계 SHP",
                accept_multiple_files=True,
                key="boundary",
                type=["shp", "shx", "dbf", "prj", "cpg"],
                help="도로 또는 점용 경계 Shapefile (선택)",
            )
            sheet_files = st.file_uploader(
                "도곽 SHP/DXF",
                accept_multiple_files=True,
                key="sheet",
                type=["shp", "shx", "dbf", "prj", "cpg", "dxf"],
                help="도곽 Shapefile 또는 DXF (선택)",
            )

        # 업로드 상태 표시
        required_ok = bool(cadastral_files and facility_files and code_table_file)

        # session_state에 저장 (위저드 스텝용)
        st.session_state["_cadastral_files"] = cadastral_files
        st.session_state["_facility_files"] = facility_files
        st.session_state["_code_table_file"] = code_table_file

        if required_ok:
            st.markdown("""
            <div class="status-badge badge-success">&#10003; 필수 파일 모두 업로드 완료</div>
            """, unsafe_allow_html=True)
        else:
            missing = []
            if not cadastral_files:
                missing.append("지적도 SHP")
            if not facility_files:
                missing.append("관로 SHP")
            if not code_table_file:
                missing.append("법정동코드 파일")
            items = ", ".join(missing)
            st.markdown(f"""
            <div class="status-badge badge-warning">⚠ 필수 파일 누락: {items}</div>
            """, unsafe_allow_html=True)

    # ── 탭 2: 필드 매핑 ────────────────────────────────────────
    with tab_mapping:
        _render_wizard_steps(1 if required_ok else 0, step_names)

        if not required_ok:
            st.markdown("""
            <div class="section-card" style="text-align: center; padding: 40px;">
                <div style="font-size: 40px; margin-bottom: 12px; opacity: 0.3;">📋</div>
                <h3 style="justify-content: center;">먼저 필수 파일을 업로드하세요</h3>
                <div class="section-desc">'파일 업로드' 탭에서 필수 파일을 모두 업로드한 후 이 단계를 진행할 수 있습니다.</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div class="section-card">
                <h3>🔗 필드 매핑 설정</h3>
                <div class="section-desc">각 입력 파일의 컬럼을 분석에 필요한 필드에 매핑합니다. 자동 추정된 값을 확인하고 필요 시 수정하세요.</div>
            </div>
            """, unsafe_allow_html=True)

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
                    "cadastral": ("지적도", "🗺️"),
                    "facility": ("관로", "🔧"),
                    "code_table": ("행정구역 파일", "📊"),
                }

                for col_idx, source_name in enumerate(["cadastral", "facility", "code_table"]):
                    with cols[col_idx]:
                        label, icon = source_labels[source_name]
                        st.markdown(f"##### {icon} {label}")
                        mapping_result[source_name] = {}
                        source_cols = [""] + columns_by_source.get(source_name, [])
                        for field_key, meta in field_defs[source_name].items():
                            if not meta.get("show_in_ui", True):
                                mapping_result[source_name][field_key] = auto_mapping.get(source_name, {}).get(field_key)
                                continue
                            default_val = auto_mapping.get(source_name, {}).get(field_key, "")
                            default_idx = source_cols.index(default_val) if default_val in source_cols else 0
                            req_mark = " *" if meta.get("required") else ""
                            selected = st.selectbox(
                                f"{meta['label']}{req_mark}",
                                options=source_cols,
                                index=default_idx,
                                key=f"map_{source_name}_{field_key}",
                            )
                            mapping_result[source_name][field_key] = selected or None

                st.session_state["field_mapping"] = mapping_result

    # ── 탭 3: 실행 및 결과 ─────────────────────────────────────
    with tab_run:
        _render_wizard_steps(2 if (required_ok and has_mapping) else (1 if required_ok else 0), step_names)

        if not required_ok:
            st.markdown("""
            <div class="section-card" style="text-align: center; padding: 40px;">
                <div style="font-size: 40px; margin-bottom: 12px; opacity: 0.3;">🚀</div>
                <h3 style="justify-content: center;">먼저 파일을 업로드하세요</h3>
                <div class="section-desc">파일 업로드와 필드 매핑을 완료한 후 분석을 실행할 수 있습니다.</div>
            </div>
            """, unsafe_allow_html=True)
        elif "field_mapping" not in st.session_state:
            st.markdown("""
            <div class="section-card" style="text-align: center; padding: 40px;">
                <div style="font-size: 40px; margin-bottom: 12px; opacity: 0.3;">🔗</div>
                <h3 style="justify-content: center;">필드 매핑을 확인하세요</h3>
                <div class="section-desc">'필드 매핑' 탭에서 매핑을 확인한 후 분석을 실행할 수 있습니다.</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div class="section-card">
                <h3>🚀 분석 실행</h3>
                <div class="section-desc">설정을 확인하고 아래 버튼을 클릭하여 분석을 시작합니다.</div>
            </div>
            """, unsafe_allow_html=True)

            # 설정 요약
            with st.expander("현재 설정 확인", expanded=False):
                s1, s2, s3 = st.columns(3)
                with s1:
                    st.markdown(f"**토피:** {soil_cover}m")
                    st.markdown(f"**포장상태:** {pavement}")
                with s2:
                    st.markdown(f"**터파기 기울기:** {excavation_slope}")
                    st.markdown(f"**메인관로 방향:** {main_line_dir}")
                with s3:
                    st.markdown(f"**기준구분:** {criterion_label}")
                    st.markdown(f"**반올림:** {round_digits}자리")

            st.markdown("")

            if st.button("분석 실행", type="primary",
                         use_container_width=True,
                         disabled=st.session_state.get("running", False)):
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

    # ── 푸터 ──────────────────────────────────────────────────
    st.markdown("""
    <div class="app-footer">
        용지도 조서 산출 v2.0 &middot; Powered by Streamlit
    </div>
    """, unsafe_allow_html=True)


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

    st.markdown("---")

    st.markdown("""
    <div class="section-card">
        <h3>📊 분석 결과</h3>
    </div>
    """, unsafe_allow_html=True)

    # 결과 메트릭 카드
    col1, col2, col3 = st.columns(3)
    with col1:
        cnt = len(source_table)
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">편입 필지 수</div>
            <div class="metric-value">{cnt}</div>
            <div class="metric-unit">건</div>
        </div>
        """, unsafe_allow_html=True)
    with col2:
        perm_sum = float(source_table["영구점용면적"].fillna(0).sum()) if not source_table.empty else 0
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">영구점용면적 합계</div>
            <div class="metric-value">{perm_sum:,.2f}</div>
            <div class="metric-unit">m&sup2;</div>
        </div>
        """, unsafe_allow_html=True)
    with col3:
        temp_sum = float(source_table["임시점용면적"].fillna(0).sum()) if not source_table.empty else 0
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">임시점용면적 합계</div>
            <div class="metric-value">{temp_sum:,.2f}</div>
            <div class="metric-unit">m&sup2;</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("")
    st.markdown(f"""
    <div style="text-align: center;">
        <span class="status-badge badge-info">분석 좌표계: {result['analysis_crs']}</span>
        &nbsp;
        <span class="status-badge badge-info">CAD 좌표계: {result['cad_crs']}</span>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("")

    # 테이블 미리보기
    tab_src, tab_data, tab_err, tab_log = st.tabs([
        "📋 조서원본",
        "📊 DATA",
        "⚠️ 오류목록",
        "📝 처리 로그",
    ])
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
            st.markdown("""
            <div style="text-align: center; padding: 24px; color: #34c759;">
                <div style="font-size: 32px; margin-bottom: 8px;">&#10003;</div>
                <div style="font-weight: 600;">오류 없음</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.dataframe(error_table, use_container_width=True, height=300)
    with tab_log:
        log_text = result.get("log", "")
        if log_text:
            st.markdown(f'<div class="log-container"><pre>{log_text}</pre></div>',
                        unsafe_allow_html=True)
        else:
            st.info("로그가 없습니다.")

    # 다운로드
    st.markdown("---")
    st.markdown("""
    <div class="section-card">
        <h3>📥 결과 다운로드</h3>
        <div class="section-desc">개별 파일 또는 전체 ZIP으로 다운로드할 수 있습니다.</div>
    </div>
    """, unsafe_allow_html=True)

    download_cols = st.columns(3)

    for idx, (file_key, file_path) in enumerate(output_files.items()):
        fpath = Path(file_path)
        if fpath.exists():
            with download_cols[idx % 3]:
                with open(fpath, "rb") as f:
                    st.download_button(
                        label=f"📄 {fpath.name}",
                        data=f.read(),
                        file_name=fpath.name,
                        key=f"dl_{file_key}",
                        use_container_width=True,
                    )

    # ZIP 전체 다운로드
    st.markdown("")
    existing_files = [Path(fp) for fp in output_files.values() if Path(fp).exists()]
    if existing_files:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fpath in existing_files:
                zf.write(fpath, fpath.name)
        zip_buffer.seek(0)

        st.download_button(
            label="📦 전체 결과 ZIP 다운로드",
            data=zip_buffer,
            file_name="조서산출_전체결과.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
