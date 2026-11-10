"""过渡期转发层：页面知识卡的唯一实现在 jev-kb（jevkb.kb.cards，jev-qa docs/kb-design-v2.md 4.3）。"""

from __future__ import annotations

from jevkb.kb.cards import (  # noqa: F401 - 对外接口
    SOURCE,
    CardAction,
    CardAssertion,
    CardElement,
    CardError,
    Confusable,
    PageCard,
    build_card,
    build_cards,
    label_text,
)
