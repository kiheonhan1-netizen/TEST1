"""쇼핑 리스트 데이터 계층.

Streamlit에 의존하지 않는 순수 Python 모듈입니다. 아이템 목록을 JSON 파일에
저장·로드하고, 추가/수정/삭제/체크 함수를 제공합니다. 각 변경 함수는 입력
리스트를 직접 바꾸지 않고 새 리스트를 반환합니다.

앱은 변경할 때 mutate()를 사용합니다. mutate는 잠금 안에서 최신 파일을 읽고,
변경 함수를 적용하고, 저장합니다. 그래서 여러 세션이 동시에 써도 서로의 변경을
덮어쓰지 않습니다. (잠금은 프로세스 안에서만 유효합니다. Streamlit은 모든
세션을 한 프로세스의 스레드로 실행하므로 충분합니다.)

나중에 저장소를 외부 DB로 바꿀 때는 load_items_with_status / mutate만 교체하면 됩니다.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "shopping_list.json"

_LOCK = threading.RLock()

NAME_MAX_LEN = 50
MEMO_MAX_LEN = 100
QTY_MAX = 999

Item = dict[str, Any]


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------

def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ValueError("이름은 문자열이어야 합니다.")
    name = name.strip()
    if not name:
        raise ValueError("이름을 입력해 주세요.")
    if len(name) > NAME_MAX_LEN:
        raise ValueError(f"이름은 {NAME_MAX_LEN}자 이하로 입력해 주세요.")
    return name


def _validate_quantity(quantity: Any) -> int:
    if isinstance(quantity, bool):
        raise ValueError("수량은 정수여야 합니다.")
    try:
        value = int(quantity)
    except (TypeError, ValueError):
        raise ValueError("수량은 정수여야 합니다.") from None
    if value != quantity and not isinstance(quantity, str):
        # 2.5 같은 실수 입력 거부
        raise ValueError("수량은 정수여야 합니다.")
    if value < 1:
        raise ValueError("수량은 1 이상이어야 합니다.")
    if value > QTY_MAX:
        raise ValueError(f"수량은 {QTY_MAX} 이하로 입력해 주세요.")
    return value


def _validate_memo(memo: Any) -> str:
    if memo is None:
        return ""
    if not isinstance(memo, str):
        raise ValueError("메모는 문자열이어야 합니다.")
    memo = memo.strip()
    if len(memo) > MEMO_MAX_LEN:
        raise ValueError(f"메모는 {MEMO_MAX_LEN}자 이하로 입력해 주세요.")
    return memo


def _is_valid_item(obj: Any) -> bool:
    """로드한 데이터의 각 항목이 최소한의 형태를 갖췄는지 확인합니다."""
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("id"), str)
        and isinstance(obj.get("name"), str)
        and obj["name"].strip() != ""
    )


def _normalize(obj: dict) -> Item:
    """누락된 필드는 기본값으로 채웁니다."""
    quantity = obj.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        quantity = 1
    memo = obj.get("memo", "")
    return {
        "id": obj["id"],
        "name": obj["name"],
        "quantity": quantity,
        "memo": memo if isinstance(memo, str) else "",
        "checked": obj.get("checked") is True,
        "created_at": obj.get("created_at") or _now(),
    }


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _find_index(items: list[Item], item_id: str) -> int:
    for i, item in enumerate(items):
        if item["id"] == item_id:
            return i
    raise KeyError(f"아이템을 찾을 수 없습니다: {item_id}")


# ---------------------------------------------------------------------------
# 저장 / 로드
# ---------------------------------------------------------------------------

def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")


def load_items_with_status(path: Path = DATA_FILE) -> tuple[list[Item], Path | None]:
    """JSON 파일에서 아이템 목록을 읽고, 손상 시 백업 파일 경로를 함께 반환합니다.

    - 파일이 없으면 ([], None)
    - JSON 형식이 아니거나 최상위가 리스트가 아니면, 원본을 백업 파일로 옮기고 ([], 백업경로)
    - 일부 항목만 잘못됐으면, 원본을 백업 파일로 복사하고 (올바른 항목, 백업경로)

    다음 저장에서 원본이 덮어써지기 전에 백업을 남겨 데이터를 잃지 않게 합니다.
    """
    with _LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return [], None
        except OSError:
            return [], None
        except (json.JSONDecodeError, UnicodeDecodeError):
            backup = _backup_path(path)
            try:
                os.replace(path, backup)
            except OSError:
                return [], None
            return [], backup

        if not isinstance(data, list):
            backup = _backup_path(path)
            try:
                os.replace(path, backup)
            except OSError:
                return [], None
            return [], backup

        items = [_normalize(obj) for obj in data if _is_valid_item(obj)]
        if len(items) != len(data):
            backup = _backup_path(path)
            try:
                shutil.copy2(path, backup)
            except OSError:
                backup = None
            # 잘못된 항목을 뺀 상태로 바로 저장해 같은 백업이 반복 생성되지 않게 함
            _save_unlocked(items, path)
            return items, backup
        return items, None


def load_items(path: Path = DATA_FILE) -> list[Item]:
    """JSON 파일에서 아이템 목록을 읽습니다.

    파일이 없거나 손상된 경우에도 예외 없이 리스트를 반환합니다.
    손상된 파일은 load_items_with_status 규칙에 따라 백업됩니다.
    """
    return load_items_with_status(path)[0]


def save_items(items: list[Item], path: Path = DATA_FILE) -> None:
    """아이템 목록을 JSON 파일로 저장합니다.

    같은 폴더의 임시 파일에 먼저 쓴 뒤 교체하므로, 저장 도중 오류가 나도
    기존 파일이 손상되지 않습니다.
    """
    with _LOCK:
        _save_unlocked(items, path)


def mutate(
    fn: Callable[[list[Item]], list[Item]], path: Path = DATA_FILE
) -> tuple[list[Item], Path | None]:
    """파일의 최신 목록을 읽어 fn을 적용하고 저장합니다 (읽기-변경-저장을 한 번에).

    여러 세션(탭·기기)이 동시에 변경해도 서로의 변경을 덮어쓰지 않도록
    잠금 안에서 항상 최신 파일을 기준으로 변경합니다.
    반환값: (저장된 새 목록, 손상 백업 경로 또는 None)
    """
    with _LOCK:
        items, backup = load_items_with_status(path)
        new_items = fn(items)
        _save_unlocked(new_items, path)
        return new_items, backup


def _save_unlocked(items: list[Item], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_item(items: list[Item], name: str, quantity: int = 1, memo: str = "") -> list[Item]:
    """검증 후 새 아이템을 리스트 끝에 추가한 새 리스트를 반환합니다."""
    item: Item = {
        "id": str(uuid.uuid4()),
        "name": _validate_name(name),
        "quantity": _validate_quantity(quantity),
        "memo": _validate_memo(memo),
        "checked": False,
        "created_at": _now(),
    }
    return [*items, item]


def update_item(items: list[Item], item_id: str, **fields: Any) -> list[Item]:
    """name / quantity / memo / checked 필드를 수정한 새 리스트를 반환합니다."""
    allowed = {"name", "quantity", "memo", "checked"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"수정할 수 없는 필드입니다: {', '.join(sorted(unknown))}")

    idx = _find_index(items, item_id)
    updated = dict(items[idx])
    if "name" in fields:
        updated["name"] = _validate_name(fields["name"])
    if "quantity" in fields:
        updated["quantity"] = _validate_quantity(fields["quantity"])
    if "memo" in fields:
        updated["memo"] = _validate_memo(fields["memo"])
    if "checked" in fields:
        if not isinstance(fields["checked"], bool):
            raise ValueError("checked는 True 또는 False여야 합니다.")
        updated["checked"] = fields["checked"]

    new_items = list(items)
    new_items[idx] = updated
    return new_items


def delete_item(items: list[Item], item_id: str) -> list[Item]:
    """해당 id의 아이템을 뺀 새 리스트를 반환합니다."""
    _find_index(items, item_id)
    return [item for item in items if item["id"] != item_id]


def toggle_item(items: list[Item], item_id: str) -> list[Item]:
    """해당 아이템의 체크 상태를 뒤집은 새 리스트를 반환합니다."""
    idx = _find_index(items, item_id)
    return update_item(items, item_id, checked=not items[idx]["checked"])


def clear_checked(items: list[Item]) -> list[Item]:
    """체크된 아이템을 모두 뺀 새 리스트를 반환합니다."""
    return [item for item in items if not item["checked"]]
