"""过渡期转发层：导航图的唯一实现在 jev-kb（jevkb.kb.navgraph，jev-qa docs/kb-design-v2.md 4.3）。"""

from __future__ import annotations

from jevkb.kb.navgraph import (  # noqa: F401 - 对外接口
    DYNAMIC,
    EDGE_KINDS,
    NavEdge,
    NavGraph,
    NavGraphError,
    NavNode,
    build_navgraph,
    count_dynamic,
    is_dynamic,
    node_id,
    snake_case,
)
