"""过渡期转发层：知识库构建的唯一实现在 jev-kb（jevkb.kb.build，jev-qa docs/kb-design-v2.md 4.3）。

构建入口是 `jev-kb kb build`；抽取脚本与 node_modules 只在 jev-kb 仓库里维护。
"""

from __future__ import annotations

from jevkb.kb.build import (  # noqa: F401 - 对外接口
    DEFAULT_MODULES_EXPORT,
    DEFAULT_MODULES_FILE,
    KbError,
    build_kb,
    collect_components,
    extract_documents,
    git_version,
    write_kb,
)
