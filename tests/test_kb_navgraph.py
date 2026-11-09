import json
from pathlib import Path

import pytest

from jev_convert import jsonio
from jev_convert.kb.navgraph import (
    NavGraph,
    NavGraphError,
    NavNode,
    build_navgraph,
    count_dynamic,
    node_id,
    snake_case,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "webui"
EXTRACTED = FIXTURES / "extracted"
KB_VERSION = "webui@testfixture"
POOL = "data_platform_new_annotation_task_pool_filter"


@pytest.fixture(scope="module")
def documents() -> tuple[dict, dict]:
    return jsonio.load(EXTRACTED / "routes.json"), jsonio.load(EXTRACTED / "vue.json")


@pytest.fixture(scope="module")
def graph(documents: tuple[dict, dict]) -> NavGraph:
    routes, vue = documents
    return build_navgraph(routes, vue, KB_VERSION)


def test_extraction_fixtures_match_sources(documents: tuple[dict, dict]) -> None:
    routes, vue = documents
    routes_source = (FIXTURES / "src/router/routes.ts").read_text(encoding="utf-8")
    vue_source = (FIXTURES / "src/views/demo/Page.vue").read_text(encoding="utf-8")

    def walk(items: list) -> list[dict]:
        found = []
        for item in items:
            found.append(item)
            found.extend(walk(item.get("children") or []))
        return found

    for route in walk(routes["routes"]):
        if isinstance(route["path"], str):
            assert '"' + route["path"] + '"' in routes_source
    for bucket, field in (("buttons", "text"), ("tabs", "label"), ("form_items", "label"), ("dialogs", "title")):
        for item in vue["files"][0][bucket]:
            if isinstance(item[field], str):
                assert item[field] in vue_source
    assert vue["files"][0]["query_keys"] == ["flow", "graph_definition_id", "node_id"]


def test_node_ids_and_full_paths(graph: NavGraph) -> None:
    nodes = {node.id: node for node in graph.nodes}
    assert POOL in nodes
    assert nodes[POOL].path == "/new/data-platform/workbench/annotation/task-pool-filter"
    assert nodes[POOL].route_name == "DataPlatformNewAnnotationTaskPoolFilter"
    # 没有 name 的路由用完整 path 生成 id
    assert nodes["login"].path == "/login"
    assert nodes["login"].route_name == ""
    assert [node.id for node in graph.nodes] == sorted(node.id for node in graph.nodes)


def test_nested_paths_are_joined(graph: NavGraph) -> None:
    paths = {node.path for node in graph.nodes}
    assert "/new/data-platform" in paths
    assert "/new/data-platform/task" in paths
    assert "/new/data-platform/task/process" in paths
    assert "/new/data-platform/workbench/annotation" in paths


def test_menu_paths_follow_modules(graph: NavGraph) -> None:
    nodes = {node.id: node for node in graph.nodes}
    assert nodes[POOL].menu == ("数据平台-新版", "工作台", "标注工作台")
    assert nodes["data_platform_new_batch_list"].menu == ("数据平台-新版", "批次列表")
    # 容器标题来自 fromBackendKey 指向的路由 meta.title
    assert nodes["data_platform_new_process_task"].menu == ("数据平台-新版", "任务管理", "处理任务")
    # 模块名不是字面量：该模块下的叶子不生成菜单路径
    assert nodes["data_platform_new_task"].menu == ()


def test_dynamic_route_values_are_not_guessed(graph: NavGraph) -> None:
    nodes = {node.id: node for node in graph.nodes}
    assert nodes[POOL].title == ""
    assert nodes[POOL].backend_key == "data_platform_new_annotation_workbench"


def test_required_query_and_direct(graph: NavGraph) -> None:
    nodes = {node.id: node for node in graph.nodes}
    assert nodes[POOL].required_query == ("flow", "graph_definition_id", "node_id")
    assert nodes[POOL].direct == "with_params"
    assert nodes["data_platform_new_batch_list"].direct == "yes"
    # 守卫特殊处理的路径（根路径、403、登录页）记为 menu
    assert nodes["403"].direct == "menu"
    assert nodes["internal_login"].direct == "menu"


def test_landmarks_come_from_source_order_buttons(graph: NavGraph) -> None:
    nodes = {node.id: node for node in graph.nodes}
    assert nodes[POOL].landmarks == ("element=开始处理", "element=批量退回", "element=取消", "element=确定")
    assert nodes["data_platform_new_batch_list"].landmarks == ()


def test_redirect_edges(graph: NavGraph) -> None:
    edges = {(edge.from_, edge.to, edge.kind) for edge in graph.edges}
    assert ("login", "internal_login", "url") in edges
    assert ("data_platform_new_workbench", "data_platform_new_annotation_workbench", "url") in edges
    labels = {(edge.from_, edge.to): edge.label for edge in graph.edges}
    assert labels[("data_platform_new_workbench", "data_platform_new_annotation_workbench")] == "标注工作台"


def test_build_is_deterministic(documents: tuple[dict, dict], graph: NavGraph) -> None:
    routes, vue = documents
    first = jsonio.dumps(graph.to_dict())
    second = jsonio.dumps(build_navgraph(routes, vue, KB_VERSION).to_dict())
    assert first == second
    assert json.loads(first)["kb_version"] == KB_VERSION


def test_roundtrip(graph: NavGraph) -> None:
    data = graph.to_dict()
    assert NavGraph.from_dict(data) == graph
    assert data["nodes"][POOL]["menu"] == ["数据平台-新版", "工作台", "标注工作台"]


def test_from_dict_rejects_bad_data(graph: NavGraph) -> None:
    unknown = graph.to_dict()
    unknown["nodes"][POOL]["component_sha"] = "abc"
    with pytest.raises(NavGraphError):
        NavGraph.from_dict(unknown)

    missing = graph.to_dict()
    del missing["nodes"][POOL]["direct"]
    with pytest.raises(NavGraphError):
        NavGraph.from_dict(missing)

    invalid_direct = graph.to_dict()
    invalid_direct["nodes"][POOL]["direct"] = "maybe"
    with pytest.raises(NavGraphError):
        NavGraph.from_dict(invalid_direct)

    invalid_edge = graph.to_dict()
    invalid_edge["edges"][0]["kind"] = "hover"
    with pytest.raises(NavGraphError):
        NavGraph.from_dict(invalid_edge)


def test_bad_kb_version(documents: tuple[dict, dict]) -> None:
    routes, vue = documents
    with pytest.raises(NavGraphError):
        build_navgraph(routes, vue, "0ebeee8")


def test_node_id_and_snake_case() -> None:
    assert snake_case("DataPlatformNewAnnotationTaskPoolFilter") == "data_platform_new_annotation_task_pool_filter"
    assert snake_case("/new/data-platform/task-pool-filter") == "new_data_platform_task_pool_filter"
    assert node_id("403", "/403") == "403"
    assert node_id(None, "/new/data-platform/task") == "new_data_platform_task"
    assert NavNode(id="x", path="/x").to_dict()["direct"] == "yes"


def test_count_dynamic() -> None:
    payload = {"a": {"dynamic": "POOL_TITLE"}, "b": [{"dynamic": "x"}, "y"], "c": "z"}
    assert count_dynamic(payload) == 2
    assert count_dynamic("plain") == 0
