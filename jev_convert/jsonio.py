"""可复现的 JSON 写出与读取。

同一对象多次写出的字节完全一致：key 排序、固定缩进、末尾换行。写入先落临时文件再替换，
中途中断不会留下半个文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

INTENT = 2


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=INTENT) + "\n"


def dump(obj: Any, path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(dumps(obj))
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
