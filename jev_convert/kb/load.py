"""过渡期转发层：知识库加载的唯一实现在 jev-kb（jevkb.kb.load，jev-qa docs/kb-design-v2.md 4.3）。

调用方继续 `from jev_convert.kb.load import ...`；测试的 monkeypatch 打在本模块属性上同样生效。
一个迭代后调用方统一改成 `from jevkb...`，本目录删除。
"""

from __future__ import annotations

from jevkb.kb.load import (  # noqa: F401 - 对外接口
    CONFIRMED_FILE,
    LEARNED_FILE,
    OVERRIDES_FILE,
    KbLoadError,
    load_cards,
    load_confirmed,
    load_graph,
    load_learned,
    load_overrides,
    load_raw_cards,
)
