from pathlib import Path

from jev_convert.jsonio import dump, dumps, load


def test_dumps_sorts_keys_and_ends_with_newline() -> None:
    text = dumps({"b": 1, "a": 2})
    assert text == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_dumps_keeps_chinese_readable() -> None:
    assert "开始处理" in dumps({"name": "开始处理"})


def test_dumps_is_stable_for_same_object() -> None:
    first = {"z": [3, 1, 2], "a": {"y": 1, "x": 2}}
    second = {"a": {"x": 2, "y": 1}, "z": [3, 1, 2]}
    assert dumps(first) == dumps(second)


def test_dump_writes_identical_bytes_twice(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    payload = {"k": "值", "n": [1, 2, 3]}
    dump(payload, target)
    first = target.read_bytes()
    dump(payload, target)
    assert target.read_bytes() == first
    assert load(target) == payload


def test_dump_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "out.json"
    dump({"k": 1}, target)
    assert load(target) == {"k": 1}


def test_dump_leaves_no_temporary_files(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    dump({"k": 1}, target)
    assert [path.name for path in tmp_path.iterdir()] == ["out.json"]
