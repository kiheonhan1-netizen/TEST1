"""쇼핑 리스트 Streamlit 앱.

규칙
- 데이터의 기준은 항상 파일입니다. 화면을 그릴 때마다 파일을 읽고, 변경은
  storage.mutate()로 "최신 파일 읽기 → 변경 → 저장"을 잠금 안에서 한 번에 합니다.
  (여러 탭·기기가 동시에 써도 서로의 변경을 덮어쓰지 않게 하기 위함)
- 상태 변경은 on_click / on_change 콜백에서만 합니다.
- 위젯 key는 아이템 id로 만들고, 체크 상태의 기준은 item["checked"]입니다.
- 사용자 입력을 HTML로 출력할 때는 html.escape, 마크다운으로 해석되는 곳
  (토스트, 경고, 위젯 라벨)에서는 _md_escape를 반드시 거칩니다.
- 세션 상태 키를 "items"로 쓰지 않습니다 (st.session_state.items는 dict의 .items 메서드).
"""

from __future__ import annotations

import html
import threading
import time
from collections.abc import Callable

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

import storage
from storage import Item

st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")

FILTERS = ["전체", "남은 항목", "완료 항목"]
PAGE_SIZE = 50  # 한 번에 그리는 최대 행 수 (많을 때 화면 반응 속도 유지)
DOUBLE_SUBMIT_SECONDS = 2.0  # 같은 내용을 이 시간 안에 다시 추가하면 중복 제출로 보고 무시
ITEM_KEY_PREFIXES = ("chk_", "edit_btn_", "del_", "edit_name_", "edit_qty_", "edit_memo_")


# ---------------------------------------------------------------------------
# 상태 / 공통 도우미
# ---------------------------------------------------------------------------

def _init_state() -> None:
    defaults = {
        "shopping_items": [],
        "editing_id": None,
        "add_error": None,
        "add_warning": None,
        "edit_error": None,
        "data_warning": None,
        "toasts": [],
        "add_name": "",
        "add_qty": 1,
        "add_memo": "",
        "confirm_clear_all": False,
        "view_filter": FILTERS[0],
        "show_limit": PAGE_SIZE,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _md_escape(text: str) -> str:
    """사용자 입력이 마크다운 서식·링크·Streamlit 지시어로 해석되지 않게 합니다.

    CommonMark는 모든 ASCII 문장부호의 백슬래시 이스케이프를 지원합니다.
    다만 GFM 자동 링크(http://…, www.…, 이메일)는 이스케이프 후에도 링크가 되므로
    보이지 않는 문자(U+2060 WORD JOINER)를 끼워 넣어 링크로 인식되지 않게 합니다.
    """
    wj = "⁠"
    text = text.replace("://", ":" + wj + "//").replace("www.", "www" + wj + ".").replace("@", "@" + wj)
    return "".join("\\" + ch if ch.isascii() and not ch.isalnum() and not ch.isspace() else ch for ch in text)


def _same_name(a: str, b: str) -> bool:
    """중복 판단: 대소문자와 공백 차이는 무시합니다."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _toast(message: str, icon: str = "✅") -> None:
    st.session_state.toasts.append((message, icon))


def _note_backup(backup) -> None:
    if backup is not None:
        st.session_state.data_warning = (
            "데이터 파일이 손상되어 읽을 수 없는 부분이 있었어요. "
            f"원본은 data 폴더의 {_md_escape(backup.name)} 파일로 백업해 두었어요."
        )


def _load() -> list[Item]:
    items, backup = storage.load_items_with_status()
    _note_backup(backup)
    st.session_state.shopping_items = items
    return items


def _mutate(fn: Callable[[list[Item]], list[Item]]) -> list[Item] | None:
    """최신 파일 기준으로 변경하고 저장합니다. 실패하면 None.

    ValueError(입력 검증 실패)는 호출한 쪽에서 처리하도록 그대로 전달합니다.
    """
    try:
        new_items, backup = storage.mutate(fn)
    except KeyError:
        _toast("다른 곳에서 이미 삭제된 아이템이에요.", "ℹ️")
        return None
    except OSError as e:
        _toast("저장에 실패했어요: " + _md_escape(str(e)), "⚠️")
        return None
    _note_backup(backup)
    st.session_state.shopping_items = new_items
    return new_items


def _reset_transient() -> None:
    """다른 작업을 하면 일회성 상태를 초기화합니다.

    - 전체 삭제 확인 체크 (B-7: 체크가 남아 한 번에 전체 삭제되는 것 방지)
    - 추가 오류 메시지 (B-6)
    """
    st.session_state.confirm_clear_all = False
    st.session_state.add_error = None


def _forget_widgets(item_id: str) -> None:
    """삭제된 아이템의 위젯 상태를 지워 다른 아이템에 영향을 주지 않게 합니다."""
    for prefix in ITEM_KEY_PREFIXES:
        st.session_state.pop(f"{prefix}{item_id}", None)


def _forget_edit_widgets(item_id: str) -> None:
    for prefix in ("edit_name_", "edit_qty_", "edit_memo_"):
        st.session_state.pop(f"{prefix}{item_id}", None)


def _end_edit() -> None:
    if st.session_state.editing_id:
        _forget_edit_widgets(st.session_state.editing_id)
    st.session_state.editing_id = None
    st.session_state.edit_error = None


# ---------------------------------------------------------------------------
# 콜백
# ---------------------------------------------------------------------------

def _clear_add_inputs() -> None:
    st.session_state.add_name = ""
    st.session_state.add_qty = 1
    st.session_state.add_memo = ""


class _RecentAdds:
    """세션별 최근 추가 기록 (서버 메모리, 모든 재실행에서 공유).

    B-2: 버튼을 빠르게 두 번 누르면 첫 번째 실행이 중단되면서 그 실행에서 쓴
    st.session_state 값이 사라질 수 있어, 중복 제출 판단에 session_state를 쓸 수 없습니다.
    그래서 서버 메모리에 잠금과 함께 기록합니다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, tuple[tuple, float]] = {}

    def claim(self, session_id: str, signature: tuple) -> bool:
        """같은 세션에서 같은 내용이 방금 추가됐으면 False, 아니면 기록하고 True."""
        now = time.monotonic()
        with self._lock:
            # 오래된 기록 정리
            for sid in [k for k, (_, t) in self._last.items() if now - t > 60]:
                del self._last[sid]
            last = self._last.get(session_id)
            if last and last[0] == signature and now - last[1] < DOUBLE_SUBMIT_SECONDS:
                return False
            self._last[session_id] = (signature, now)
            return True

    def release(self, session_id: str, signature: tuple) -> None:
        """추가에 실패했으면 기록을 지워 바로 다시 시도할 수 있게 합니다."""
        with self._lock:
            last = self._last.get(session_id)
            if last and last[0] == signature:
                del self._last[session_id]


@st.cache_resource
def _recent_adds() -> _RecentAdds:
    return _RecentAdds()


def _session_id() -> str:
    ctx = get_script_run_ctx()
    return ctx.session_id if ctx else "default"


def on_add() -> None:
    ss = st.session_state
    _reset_transient()
    ss.add_warning = None

    name, qty, memo = ss.add_name, ss.add_qty, ss.add_memo
    signature = ((name or "").strip(), qty, (memo or "").strip())
    sid = _session_id()
    # B-2: 버튼을 빠르게 두 번 눌러 같은 내용이 연달아 제출되면 두 번째는 무시
    if not _recent_adds().claim(sid, signature):
        _clear_add_inputs()
        return

    result: dict = {}

    def fn(items: list[Item]) -> list[Item]:
        new_items = storage.add_item(items, name, qty, memo)
        added = new_items[-1]
        result["name"] = added["name"]
        result["duplicate"] = any(
            _same_name(i["name"], added["name"]) and not i["checked"] for i in items
        )
        return new_items

    try:
        new_items = _mutate(fn)
    except ValueError as e:
        _recent_adds().release(sid, signature)
        ss.add_error = str(e)
        return  # 입력값은 그대로 두어 사용자가 고칠 수 있게 함
    if new_items is None:
        _recent_adds().release(sid, signature)
        return

    shown = _md_escape(result["name"])
    _toast(f"'{shown}' 추가되었습니다")
    if result["duplicate"]:
        ss.add_warning = f"'{shown}'은(는) 이미 리스트에 있어요. 별도 항목으로 추가했어요."
    _clear_add_inputs()


def on_toggle(item_id: str) -> None:
    _reset_transient()
    checked = st.session_state.get(f"chk_{item_id}")
    if not isinstance(checked, bool):
        return
    _mutate(lambda items: storage.update_item(items, item_id, checked=checked))


def on_start_edit(item_id: str) -> None:
    _reset_transient()
    item = next((i for i in storage.load_items() if i["id"] == item_id), None)
    if item is None:
        _toast("다른 곳에서 이미 삭제된 아이템이에요.", "ℹ️")
        return
    _end_edit()  # 이전에 편집하던 아이템의 입력값은 버림
    st.session_state.editing_id = item_id
    st.session_state[f"edit_name_{item_id}"] = item["name"]
    st.session_state[f"edit_qty_{item_id}"] = item["quantity"]
    st.session_state[f"edit_memo_{item_id}"] = item["memo"]


def on_save_edit(item_id: str) -> None:
    ss = st.session_state
    _reset_transient()
    fields = {
        "name": ss.get(f"edit_name_{item_id}", ""),
        "quantity": ss.get(f"edit_qty_{item_id}", 1),
        "memo": ss.get(f"edit_memo_{item_id}", ""),
    }
    try:
        new_items = _mutate(lambda items: storage.update_item(items, item_id, **fields))
    except ValueError as e:
        ss.edit_error = str(e)
        return
    if new_items is None:  # 다른 곳에서 삭제됨
        _end_edit()
        return
    _toast("수정되었습니다")
    _end_edit()


def on_cancel_edit(item_id: str) -> None:
    _reset_transient()
    _end_edit()


def on_delete(item_id: str) -> None:
    _reset_transient()
    result: dict = {}

    def fn(items: list[Item]) -> list[Item]:
        match = next((i for i in items if i["id"] == item_id), None)
        result["name"] = match["name"] if match else ""
        return storage.delete_item(items, item_id)  # 없으면 KeyError → _mutate가 안내

    if _mutate(fn) is None:
        return
    _toast(f"'{_md_escape(result['name'])}' 삭제되었습니다")
    if st.session_state.editing_id == item_id:
        _end_edit()
    _forget_widgets(item_id)


def on_clear_checked() -> None:
    _reset_transient()
    removed: list[Item] = []

    def fn(items: list[Item]) -> list[Item]:
        removed.extend(i for i in items if i["checked"])
        return storage.clear_checked(items)

    if _mutate(fn) is None:
        return
    if not removed:
        _toast("삭제할 완료 항목이 없어요.", "ℹ️")
        return
    _toast(f"완료 항목 {len(removed)}개 삭제되었습니다")
    for item in removed:
        if st.session_state.editing_id == item["id"]:
            _end_edit()
        _forget_widgets(item["id"])


def on_clear_all() -> None:
    if not st.session_state.confirm_clear_all:
        return
    removed: list[Item] = []

    def fn(items: list[Item]) -> list[Item]:
        removed.extend(items)
        return []

    if _mutate(fn) is None:
        return
    _toast("전체 삭제되었습니다")
    _end_edit()
    st.session_state.confirm_clear_all = False
    st.session_state.show_limit = PAGE_SIZE
    for item in removed:
        _forget_widgets(item["id"])


def on_filter_change() -> None:
    _reset_transient()
    # B-9: 선택된 버튼을 다시 눌러 해제하면 None이 되므로 "전체"로 되돌림
    if st.session_state.view_filter is None:
        st.session_state.view_filter = FILTERS[0]
    st.session_state.show_limit = PAGE_SIZE


def on_show_more() -> None:
    _reset_transient()
    st.session_state.show_limit += PAGE_SIZE


def on_dismiss_data_warning() -> None:
    st.session_state.data_warning = None


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------

# 색은 반투명(rgba)과 currentColor 기반으로 지정해 라이트/다크 모드 모두에서 읽히게 합니다.
CSS = """
<style>
:root {
  --sl-green: #2e9e5b;
  --sl-green-soft: rgba(46, 158, 91, 0.14);
  --sl-red: #d64545;
}

/* 본문 폭과 여백 */
.block-container { padding-top: 2.5rem; padding-bottom: 4rem; max-width: 720px; }

/* 헤더 */
.sl-header h1 { font-size: 1.9rem; font-weight: 800; margin: 0; padding: 0; line-height: 1.25; }
.sl-header p { margin: .2rem 0 0; opacity: .65; font-size: .95rem; }

/* 요약 */
.sl-stats { display: flex; gap: .5rem; margin-bottom: .25rem; }
.sl-stat { flex: 1; text-align: center; padding: .55rem .25rem; border-radius: .75rem; background: rgba(128, 128, 128, 0.08); }
.sl-stat b { display: block; font-size: 1.5rem; line-height: 1.2; }
.sl-stat span { font-size: .8rem; opacity: .7; }
.sl-stat.left b { color: var(--sl-green); }

/* 섹션 제목 */
.sl-section { font-weight: 700; font-size: 1rem; margin: .75rem 0 .25rem; }

/* 아이템 행: 모바일에서도 체크박스·이름·버튼이 한 줄에 있도록 열 줄바꿈을 막음 */
[class*="st-key-row_"] [data-testid="stHorizontalBlock"],
[class*="st-key-editrow_"] [data-testid="stHorizontalBlock"],
.st-key-add_box [data-testid="stHorizontalBlock"] {
  flex-wrap: nowrap !important;
  gap: .25rem !important;
}
[class*="st-key-row_"] [data-testid="stColumn"],
[class*="st-key-editrow_"] [data-testid="stColumn"],
.st-key-add_box [data-testid="stColumn"] {
  min-width: 0 !important;
}
[class*="st-key-row_"] [data-testid="stColumn"]:not(:nth-child(2)) {
  flex: 0 0 auto !important;
  width: auto !important;
}
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(2) {
  flex: 1 1 auto !important;
  width: auto !important;
}
[class*="st-key-row_"] { padding: .45rem .5rem !important; }

/* 아이콘 버튼: 44px 이상 터치 영역 (B-12) */
[class*="st-key-row_"] button {
  min-height: 2.75rem; min-width: 2.75rem; padding: 0 .5rem;
  border-color: transparent; background: transparent;
}
[class*="st-key-row_"] button:hover { background: rgba(128, 128, 128, 0.12); }
/* 버튼 라벨은 화면 낭독기용 텍스트("사과 수정")이고, 화면에는 아이콘만 보이게 함 (B-13) */
[class*="st-key-edit_btn_"] button p,
[class*="st-key-del_"] button p { font-size: 0 !important; line-height: 0; }
[class*="st-key-edit_btn_"] button p::before { content: "✏️"; font-size: 1.05rem; line-height: 1; }
[class*="st-key-del_"] button p::before { content: "🗑️"; font-size: 1.05rem; line-height: 1; }
/* 체크박스: 칸 전체를 눌러도 체크되게 터치 영역 확대 (B-12) */
[class*="st-key-row_"] [data-testid="stCheckbox"] label {
  min-height: 2.75rem; min-width: 2.75rem; justify-content: center; align-items: center; margin: 0;
}

/* 아이템 텍스트 */
.sl-item { line-height: 1.35; overflow-wrap: anywhere; }
.sl-name { font-weight: 600; font-size: 1.02rem; }
.sl-qty {
  display: inline-block; margin-left: .4rem; padding: .05rem .5rem; border-radius: 999px;
  font-size: .78rem; font-weight: 700; color: var(--sl-green); background: var(--sl-green-soft);
  vertical-align: middle;
}
.sl-memo { font-size: .82rem; opacity: .6; margin-top: .1rem; }
.sl-item.done .sl-name { text-decoration: line-through; opacity: .5; font-weight: 400; }
.sl-item.done .sl-qty { opacity: .5; }
.sl-item.done .sl-memo { opacity: .35; }

/* 편집 중인 행 */
[class*="st-key-editrow_"] {
  border-left: 4px solid var(--sl-green) !important;
  background: var(--sl-green-soft);
}
.sl-edit-title { font-size: .85rem; font-weight: 700; color: var(--sl-green); margin-bottom: .1rem; }

/* 빈 상태 */
.sl-empty { text-align: center; padding: 2.5rem 1rem; border: 2px dashed rgba(128, 128, 128, 0.25); border-radius: 1rem; }
.sl-empty .icon { font-size: 2.6rem; line-height: 1; }
.sl-empty .title { font-weight: 700; margin-top: .6rem; font-size: 1.05rem; }
.sl-empty .desc { opacity: .6; font-size: .9rem; margin-top: .2rem; }

/* 위험 동작 버튼 */
.st-key-clear_all_btn button { background: var(--sl-red) !important; border-color: var(--sl-red) !important; color: #fff !important; }
.st-key-clear_all_btn button:disabled { opacity: .4; }

/* 버튼 공통 */
.stButton button, .stFormSubmitButton button { min-height: 2.75rem; border-radius: .6rem; }
.st-key-add_box .stFormSubmitButton button { width: 100%; }

@media (max-width: 640px) {
  .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.5rem; }
  .sl-header h1 { font-size: 1.6rem; }
  .sl-stat b { font-size: 1.3rem; }
}
</style>
"""


def render_header() -> None:
    st.html(CSS)
    st.html(
        '<div class="sl-header"><h1>🛒 쇼핑 리스트</h1>'
        "<p>살 것을 적고, 담으면 체크하세요.</p></div>"
    )
    if st.session_state.data_warning:
        with st.container(border=False):
            st.warning(st.session_state.data_warning, icon="🛟")
            st.button("확인", key="dismiss_data_warning", on_click=on_dismiss_data_warning)


def render_summary(items: list[Item]) -> None:
    total = len(items)
    done = sum(i["checked"] for i in items)
    st.html(
        '<div class="sl-stats">'
        f'<div class="sl-stat"><b>{total}</b><span>전체</span></div>'
        f'<div class="sl-stat left"><b>{total - done}</b><span>남은 항목</span></div>'
        f'<div class="sl-stat"><b>{done}</b><span>완료</span></div>'
        "</div>"
    )
    ratio = done / total if total else 0.0
    st.progress(ratio, text=f"진행률 {ratio:.0%}")


# B-10, B-11: 길이·수량 제한은 위젯이 아니라 storage 검증(한국어 메시지)으로 처리합니다.
# (max_chars는 초과 붙여넣기를 통째로 버리고, min_value는 영어 오류를 보여주기 때문)
NAME_HELP = f"최대 {storage.NAME_MAX_LEN}자"
QTY_HELP = f"1 ~ {storage.QTY_MAX} 사이의 정수 (소수점은 버려져요)"
MEMO_HELP = f"최대 {storage.MEMO_MAX_LEN}자"


def render_add_form() -> None:
    with st.container(key="add_box"), st.form("add_form", clear_on_submit=False, enter_to_submit=True, border=True):
        c1, c2 = st.columns([2.4, 1])
        c1.text_input("아이템 이름", key="add_name", placeholder="예: 우유", help=NAME_HELP)
        c2.number_input("수량", key="add_qty", step=1, help=QTY_HELP)
        st.text_input("메모 (선택)", key="add_memo", placeholder="예: 서울우유 1L", help=MEMO_HELP)
        st.form_submit_button("➕ 추가", on_click=on_add, type="primary")
    if st.session_state.add_error:
        st.error(st.session_state.add_error, icon="⚠️")
        st.session_state.add_error = None  # B-6: 한 번만 표시
    if st.session_state.add_warning:
        st.warning(st.session_state.add_warning, icon="🔁")
        st.session_state.add_warning = None  # 한 번만 표시


def _item_html(item: Item) -> str:
    """사용자 입력은 html.escape로 이스케이프해 HTML로 해석되지 않게 합니다."""
    cls = "sl-item done" if item["checked"] else "sl-item"
    parts = [
        f'<div class="{cls}">',
        f'<span class="sl-name">{html.escape(item["name"])}</span>',
        f'<span class="sl-qty">x{int(item["quantity"])}</span>',
    ]
    if item["memo"]:
        parts.append(f'<div class="sl-memo">{html.escape(item["memo"])}</div>')
    parts.append("</div>")
    return "".join(parts)


def render_item(item: Item) -> None:
    item_id = item["id"]
    if st.session_state.editing_id == item_id:
        render_edit_row(item)
        return

    label_name = _md_escape(item["name"])
    # 체크박스 값의 기준은 파일의 item["checked"] (다른 탭의 변경도 반영됨)
    st.session_state[f"chk_{item_id}"] = item["checked"]
    with st.container(border=True, key=f"row_{item_id}"):
        c_chk, c_text, c_edit, c_del = st.columns([0.1, 0.7, 0.1, 0.1], vertical_alignment="center", gap="small")
        c_chk.checkbox(
            f"{label_name} 구매 완료",
            key=f"chk_{item_id}",
            on_change=on_toggle,
            args=(item_id,),
            label_visibility="collapsed",
        )
        c_text.html(_item_html(item))
        c_edit.button(f"{label_name} 수정", key=f"edit_btn_{item_id}", help="수정", on_click=on_start_edit, args=(item_id,))
        c_del.button(f"{label_name} 삭제", key=f"del_{item_id}", help="삭제", on_click=on_delete, args=(item_id,))


def render_edit_row(item: Item) -> None:
    item_id = item["id"]
    with st.container(border=True, key=f"editrow_{item_id}"):
        st.html('<div class="sl-edit-title">✏️ 아이템 수정 중</div>')
        with st.form(f"edit_form_{item_id}", enter_to_submit=True, border=False):
            c1, c2 = st.columns([2.4, 1])
            c1.text_input("아이템 이름", key=f"edit_name_{item_id}", help=NAME_HELP)
            c2.number_input("수량", key=f"edit_qty_{item_id}", step=1, help=QTY_HELP)
            st.text_input("메모 (선택)", key=f"edit_memo_{item_id}", help=MEMO_HELP)
            if st.session_state.edit_error:
                st.error(st.session_state.edit_error, icon="⚠️")
            b1, b2 = st.columns(2)
            b1.form_submit_button("저장", type="primary", on_click=on_save_edit, args=(item_id,), width="stretch")
            b2.form_submit_button("취소", on_click=on_cancel_edit, args=(item_id,), width="stretch")


def render_empty(icon: str, title: str, desc: str = "") -> None:
    desc_html = f'<div class="desc">{desc}</div>' if desc else ""
    st.html(f'<div class="sl-empty"><div class="icon">{icon}</div><div class="title">{title}</div>{desc_html}</div>')


def render_list(items: list[Item]) -> None:
    if not items:
        render_empty("🧺", "리스트가 비어 있어요", "위에서 살 물건을 추가해 보세요!")
        return

    st.html('<div class="sl-section">📋 목록</div>')
    view = st.segmented_control(
        "보기", FILTERS, key="view_filter", label_visibility="collapsed", on_change=on_filter_change
    ) or FILTERS[0]
    # 미완료가 위, 완료가 아래 (같은 그룹 안에서는 추가한 순서)
    ordered = sorted(items, key=lambda i: i["checked"])
    if view == "남은 항목":
        ordered = [i for i in ordered if not i["checked"]]
    elif view == "완료 항목":
        ordered = [i for i in ordered if i["checked"]]

    if not ordered:
        if view == "남은 항목":
            render_empty("🎉", "모두 담았어요!", "남은 항목이 없어요.")
        else:
            render_empty("📝", "완료한 항목이 없어요")
        return

    # B-4: 행이 많으면 일부만 그리고 "더 보기"로 이어서 표시
    limit = st.session_state.show_limit
    for item in ordered[:limit]:
        render_item(item)
    hidden = len(ordered) - limit
    if hidden > 0:
        st.button(
            f"더 보기 ({hidden}개 남음)",
            key="show_more",
            on_click=on_show_more,
            width="stretch",
        )


def render_bulk_actions(items: list[Item]) -> None:
    if not items:
        return
    st.divider()
    done = sum(i["checked"] for i in items)
    st.button(
        f"🧹 완료 항목 삭제 ({done})",
        on_click=on_clear_checked,
        disabled=done == 0,
        width="stretch",
        help="체크한 항목을 모두 지웁니다",
    )
    with st.expander("⚠️ 전체 삭제"):
        st.caption(f"리스트의 아이템 {len(items)}개를 모두 지웁니다. 되돌릴 수 없어요.")
        st.checkbox("네, 전체 삭제할게요", key="confirm_clear_all")
        st.button(
            "전체 삭제",
            key="clear_all_btn",
            on_click=on_clear_all,
            disabled=not st.session_state.confirm_clear_all,
            width="stretch",
        )


def main() -> None:
    _init_state()
    items = _load()  # 매 실행마다 파일 기준으로 그림 (다른 탭·기기의 변경 반영)
    if st.session_state.editing_id and not any(i["id"] == st.session_state.editing_id for i in items):
        _end_edit()  # 편집 중이던 아이템이 다른 곳에서 삭제됨

    render_header()
    render_summary(items)
    render_add_form()
    render_list(items)
    render_bulk_actions(items)

    for message, icon in st.session_state.toasts:
        st.toast(message, icon=icon)
    st.session_state.toasts = []


main()
