from pathlib import Path

import pytest

from jev_convert import jsonio
from jev_convert.kb.build import build_kb, write_kb
from jev_convert.kb.cards import PageCard, build_cards
from jev_convert.kb.navgraph import NavGraph, build_navgraph

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "webui"
EXTRACTED = FIXTURES / "extracted"
KB_VERSION = "webui@testfixture"
POOL = "data_platform_new_annotation_task_pool_filter"
REQUIRED_FIELDS = ("node", "structure_version", "elements", "actions", "confusables", "assertions")


@pytest.fixture(scope="module")
def documents() -> tuple[dict, dict]:
    return jsonio.load(EXTRACTED / "routes.json"), jsonio.load(EXTRACTED / "vue.json")


@pytest.fixture(scope="module")
def graph(documents: tuple[dict, dict]) -> NavGraph:
    return build_navgraph(documents[0], documents[1], KB_VERSION)


@pytest.fixture(scope="module")
def cards(graph: NavGraph, documents: tuple[dict, dict]) -> tuple[PageCard, ...]:
    return build_cards(graph, documents[1], KB_VERSION)


def card_of(cards: tuple[PageCard, ...], node: str) -> PageCard:
    for card in cards:
        if card.node == node:
            return card
    raise AssertionError(f"没有节点 {node} 的知识卡")


def test_every_node_has_a_card(graph: NavGraph, cards: tuple[PageCard, ...]) -> None:
    assert tuple(card.node for card in cards) == tuple(node.id for node in graph.nodes)


def test_required_fields_and_provenance(cards: tuple[PageCard, ...]) -> None:
    for card in cards:
        data = card.to_dict()
        for field in REQUIRED_FIELDS:
            assert field in data
        assert data["structure_version"] == KB_VERSION
        for element in data["elements"]:
            assert element["source"] == "source"
            assert element["verified"] == 0
        for action in data["actions"]:
            assert action["source"] == "source"
            assert action["verified"] == 0


def test_pool_card_elements(cards: tuple[PageCard, ...]) -> None:
    card = card_of(cards, POOL)
    elements = {(element.role, element.label) for element in card.elements if isinstance(element.label, str)}
    assert ("button", "开始处理") in elements
    assert ("form-item", "处理任务ID") in elements
    assert ("column", "任务ID") in elements
    assert ("tab", "待处理") in elements
    assert ("dialog", "确认退回") in elements


def test_dynamic_labels_are_kept_as_snippets(cards: tuple[PageCard, ...]) -> None:
    card = card_of(cards, POOL)
    dynamic = [element.label for element in card.elements if not isinstance(element.label, str)]
    assert {"dynamic": "collectorLabel"} in dynamic
    assert {"dynamic": "columnLabel"} in dynamic
    assert card.dynamic_labels == 6
    # 绝不把动态值猜成源码里出现过的字面量
    assert "查看详情" not in [element.label for element in card.elements]


def test_actions_cover_clicks_tabs_dialogs_and_messages(cards: tuple[PageCard, ...]) -> None:
    card = card_of(cards, POOL)
    actions = {(action.kind, action.target, action.level) for action in card.actions if isinstance(action.target, str)}
    dynamic_targets = [action.target for action in card.actions if not isinstance(action.target, str)]
    assert {"dynamic": "dynamicTabLabel"} in dynamic_targets
    assert ("click", "开始处理", "") in actions
    assert ("tab", "待处理", "") in actions
    assert ("dialog", "确认退回", "") in actions
    assert ("message", "请先选择流程", "warning") in actions
    assert ("message", "开始处理成功", "success") in actions
    # 注释里的 ElMessage 与对象写法都不抽取
    targets = {action.target for action in card.actions if isinstance(action.target, str)}
    assert "这句在注释里，不应被抽到" not in targets
    assert "对象写法不是字面量，不抽取" not in targets


def test_assertions_describe_the_page(cards: tuple[PageCard, ...]) -> None:
    card = card_of(cards, POOL)
    assertions = [(assertion.kind, assertion.value) for assertion in card.assertions]
    assert assertions[0] == ("url", "/new/data-platform/workbench/annotation/task-pool-filter")
    # 第一个元素断言取源码顺序里的首个按钮
    assert ("element", "开始处理") in assertions
    # 标题是 dynamic，不做文字断言
    assert all(kind != "text" for kind, _ in assertions)

    other = card_of(cards, "data_platform_new_batch_list")
    assert ("text", "批次列表") in [(assertion.kind, assertion.value) for assertion in other.assertions]


def test_confusables_come_from_similar_labels(cards: tuple[PageCard, ...]) -> None:
    card = card_of(cards, POOL)
    pairs = {(item.a, item.b) for item in card.confusables}
    assert ("任务ID", "处理任务ID") in pairs
    assert len(card.confusables) <= 8


def test_cards_are_deterministic(graph: NavGraph, documents: tuple[dict, dict], cards: tuple[PageCard, ...]) -> None:
    again = build_cards(graph, documents[1], KB_VERSION)
    assert jsonio.dumps([card.to_dict() for card in cards]) == jsonio.dumps([card.to_dict() for card in again])


def test_card_roundtrip(cards: tuple[PageCard, ...]) -> None:
    for card in cards:
        assert PageCard.from_dict(card.to_dict()) == card


def test_card_filename() -> None:
    card = PageCard(node="demo", structure_version=KB_VERSION, path="/demo")
    assert card.filename == "demo__webui@testfixture.json"


def test_write_kb_is_reproducible(tmp_path: Path, documents: tuple[dict, dict]) -> None:
    routes, vue = documents
    first = tmp_path / "first"
    meta = write_kb(routes, vue, KB_VERSION, first)
    assert meta["counts"]["nodes"] == meta["counts"]["cards"]
    assert (first / "navgraph.json").is_file()
    assert (first / "meta.json").is_file()
    snapshot = {path.relative_to(first).as_posix(): path.read_bytes() for path in sorted(first.rglob("*.json"))}

    second = tmp_path / "second"
    write_kb(routes, vue, KB_VERSION, second)
    again = {path.relative_to(second).as_posix(): path.read_bytes() for path in sorted(second.rglob("*.json"))}
    assert snapshot == again
    assert str(tmp_path) not in snapshot["navgraph.json"].decode("utf-8")


def test_write_kb_removes_stale_cards(tmp_path: Path, documents: tuple[dict, dict]) -> None:
    routes, vue = documents
    write_kb(routes, vue, KB_VERSION, tmp_path)
    stale = tmp_path / "cards" / "obsolete__webui@testfixture.json"
    stale.write_text("{}", encoding="utf-8")
    write_kb(routes, vue, KB_VERSION, tmp_path)
    assert not stale.exists()


def test_build_kb_with_documents(tmp_path: Path, documents: tuple[dict, dict]) -> None:
    meta = build_kb(FIXTURES, tmp_path, kb_version=KB_VERSION, documents=documents)
    assert meta["kb_version"] == KB_VERSION
    assert meta["counts"]["dynamic"] > 0
    assert meta["sources"]["routes"] == "src/router/routes.ts"
    graph = jsonio.load(tmp_path / "navgraph.json")
    assert graph["kb_version"] == KB_VERSION
    assert POOL in graph["nodes"]
