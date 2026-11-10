# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

A Korean-language shopping list web app (add / edit / delete / check items) built with Python + Streamlit. Target: Streamlit Community Cloud, but it is not deployed yet, the folder is not a git repo yet, and AC-8 is unverified. Stages 1–4 are done, and all 13 bugs from the stage 4 review (`review.md`) are fixed. Planning documents:

- `prd.md` — the source of truth for requirements (features F-1–F-7, data model, UI layout, acceptance criteria AC-1–AC-8, open questions in §7).
- `prompts.md` — the staged build plan: (1) skeleton, (2) core features, (3) UI design, (4) PRD compliance + bug review with Playwright, written to `review.md`. Follow the stage the user asks for and don't pull in work from later stages (e.g. no CSS/theming during stage 2).

Keep this file in sync as later stages change the code.

## Commands

```bash
pip install -r requirements.txt
streamlit run app.py
streamlit run app.py --server.headless true --server.port 8501   # for Playwright testing
```

There is no test suite or linter configured. Quick checks that work without a browser:

```bash
# Run the app script once headlessly and print any exception (reads/writes data/shopping_list.json)
python -c "from streamlit.testing.v1 import AppTest; at=AppTest.from_file('app.py').run(); print(at.exception)"
```

All `storage` functions take a `path` argument. Throwaway scripts should pass a temp path, because the default (`DATA_FILE`) is the real data.

## Playwright testing pitfalls

Browser tests run against `streamlit run app.py --server.headless true --server.port 8501` through the Playwright MCP tools. Most false failures in stage 4 came from the following:

- `browser_run_code_unsafe` with `filename` only accepts files inside the project. Put scripts in `.playwright-mcp/` (gitignored) and delete the folder afterwards.
- During a rerun, Streamlit briefly keeps the old element next to the new one, so a locator can match two elements. After every action, wait until `[data-testid="stStatusWidget"]` no longer shows "Running...".
- After a reload, rows render progressively over about 1.5 s. Counting rows too early makes "persists after reload" fail falsely.
- Streamlit scrolls inside its own container, so `fullPage` screenshots only capture the viewport. Use `scrollIntoViewIfNeeded()` before element screenshots or coordinate clicks; clicks at coordinates outside the viewport do nothing.
- Tests write to the real `data/shopping_list.json`. Back it up before testing and restore it afterwards.
- `ConnectionResetError` (WinError 10054) in the server log comes from closing tabs on Windows and is harmless.

## Architecture

- `storage.py` is a pure-Python data layer with no Streamlit import. Pure list functions: `add_item`, `update_item`, `delete_item`, `toggle_item`, `clear_checked`. I/O: `load_items_with_status`, `load_items`, `save_items`, `mutate`. Input validation lives here and raises `ValueError` with Korean messages: name trimmed, 1–50 chars; quantity 1–`QTY_MAX` (999); memo ≤ 100 chars. Unknown ids raise `KeyError`. It is designed so the persistence backend can be swapped (e.g. Google Sheets or Supabase in v2) by replacing `load_items_with_status` / `mutate`.
- **The file is the source of truth, not session state.** `app.py` re-reads the file on every run, and every change goes through `storage.mutate(fn)`, which reads the latest file, applies `fn`, and saves, all under a module-level `threading.RLock`. This is what keeps two tabs or devices from overwriting each other. All Streamlit sessions are threads in one process, so the in-process lock is enough. Never save a session's cached list back to disk.
- `app.py` wraps `mutate` in `_mutate`, which turns `KeyError` (item deleted elsewhere) and `OSError` into toasts and lets `ValueError` reach the callback. `st.session_state.shopping_items` is only a copy of the last load. Do not name the key `items`: `st.session_state.items` resolves to the dict-like `.items()` method. `editing_id` allows one row in edit mode and is cleared if that item disappears.
- Persistence: `data/shopping_list.json` (gitignored), written UTF-8 with `ensure_ascii=False` via temp-file-then-replace. A missing file gives `[]`. A corrupt file is moved to `shopping_list.corrupt-<timestamp>.json`; if only some entries are invalid, the file is copied instead. The backup path is returned so the UI shows a dismissible warning.
- Item model: `id` (uuid4 str), `name`, `quantity` (default 1), `memo`, `checked` (default False), `created_at` (ISO 8601).

## Streamlit rules that matter here

- Mutate data only inside `on_click` / `on_change` callbacks, via `_mutate`. Never mutate the list while rendering. Callbacks call `_reset_transient()` first, which clears the clear-all confirmation checkbox and the add error.
- Derive widget keys from item ids (`chk_{id}`, `edit_btn_{id}`, `del_{id}`, `edit_name_{id}`…). Before rendering each checkbox, set `st.session_state[f"chk_{id}"] = item["checked"]` so the file, not leftover widget state, decides the value.
- Escaping user text:
  - Use `html.escape` for HTML output (`st.html` rows).
  - Use `_md_escape` for anything Streamlit parses as markdown: `st.toast`, `st.warning`, widget labels.
  - `_md_escape` also inserts U+2060 into `://`, `www.` and `@`, because GFM autolinks survive backslash escaping.
- **Session-state writes can be lost when a rerun is interrupted** (e.g. a fast double-click). Anything that must survive that, such as the add double-submit guard, lives server-side. The add guard is `_RecentAdds`, held in `@st.cache_resource` and keyed by session id from `get_script_run_ctx()`. Same name, quantity and memo within 2 s is ignored.
- Do not use `max_chars` / `min_value` on inputs:
  - `max_chars` silently drops an over-long paste.
  - `min_value` shows an English error.
  - `storage` validation gives Korean messages instead, and the limits are explained in `help=`.

## Defaults for PRD open questions (§7) unless the user decides otherwise

JSON-file storage. A duplicate name shows a warning but is still added as a separate item. New items go at the bottom. Unchecked items sort above checked ones. No access control.

Known limitation to keep documented in the README: on Streamlit Community Cloud the file system is ephemeral, so the JSON can reset on restart or redeploy, and all visitors share one list.

The README's "알려진 한계" section documents three behaviors, and it must change whenever they change:
- Very fast clicks on different buttons can drop the earlier click.
- The same name, quantity and memo submitted again within 2 s is ignored (`DOUBLE_SUBMIT_SECONDS`).
- Rows are paged 50 at a time (`PAGE_SIZE`).

## Conventions

- All UI text is in Korean. The user communicates in Korean.
- Pin the Streamlit version in `requirements.txt`. The app must work on mobile at 375px width and in both light and dark themes.

## UI implementation notes

- Styling lives in the `CSS` string in `app.py`, injected with `st.html`. Rows are targeted via keyed containers (`st.container(key=...)` → class `st-key-<key>`): `row_<id>`, `editrow_<id>`, `add_box`, `clear_all_btn`. `st.form` does not get an `st-key-` class, which is why the add form is wrapped in the `add_box` container.
- Streamlit stacks `st.columns` below 640px; the CSS forces `flex-wrap: nowrap` inside those keyed containers so rows stay on one line at 375px.
- Theme colors go in `[theme.light]` / `[theme.dark]` in `.streamlit/config.toml`. Setting colors under plain `[theme]` creates a fixed custom theme that ignores the system dark mode. Config changes need a server restart.
- The view filter is `st.segmented_control`. It returns `None` when the user deselects, so `on_filter_change` resets it to "전체". In Playwright it is exposed as `radiogroup "보기"` with radio children.
- Row icon buttons:
  - The labels are `"<name> 수정"` / `"<name> 삭제"`, which gives screen readers the item name.
  - CSS hides the label text (`font-size: 0`) and draws the emoji with `::before`.
  - Playwright selects them with `getByRole('button', { name: / 수정$/ })`.
  - Text inputs have `help=` tooltips, so use `getByLabel(..., { exact: true })`.
- Only `PAGE_SIZE` (50) rows render at once, followed by a "더 보기" button. Rendering hundreds of rows is frontend-bound: about 2 s for 200 rows, against 0.3 s of Python.
- Touch targets are ≥ 44px: icon buttons and the checkbox label are 2.75rem.
