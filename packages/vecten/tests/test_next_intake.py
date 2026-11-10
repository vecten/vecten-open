import yaml

from vecten.intake.answers import ANSWER_KEYS, ANSWERS_FILE
from vecten.next.intake import intake_items
from vecten.next.items import Item


def answer(directory, **values):
    path = directory / ANSWERS_FILE
    current = yaml.safe_load(path.read_text()) if path.is_file() else {}
    path.write_text(yaml.safe_dump({**(current or {}), **values}))


def asked(directory):
    items = intake_items(directory)
    assert len(items) == 1
    return items[0]


def test_empty_directory_asks_has_warehouse_first(tmp_path):
    item = asked(tmp_path)
    question = ANSWER_KEYS["has_warehouse"].question
    assert item == Item(
        "ask", question, question, answer_keys=("has_warehouse",), allowed=("yes", "no")
    )


def test_new_warehouse_branch_order(tmp_path):
    answer(tmp_path, has_warehouse="no")
    item = asked(tmp_path)
    assert item.answer_keys == ("sources",)
    assert item.title == item.resolution == ANSWER_KEYS["sources"].question
    assert item.allowed == ("start empty",)
    answer(tmp_path, sources=["hubspot"])
    item = asked(tmp_path)
    assert item.answer_keys == ("warehouse",) and item.allowed == ("postgres", "snowflake")
    answer(tmp_path, warehouse="postgres")
    item = asked(tmp_path)
    assert item.answer_keys == ("name",) and item.allowed == ()
    assert item.title == ANSWER_KEYS["name"].question
    answer(tmp_path, name="acme")
    assert asked(tmp_path).answer_keys == ("users",)
    answer(tmp_path, users=3)
    assert intake_items(tmp_path) == [Item("command", "Create the project", "vecten init")]


def test_existing_warehouse_branch_skips_sources_and_warehouse(tmp_path):
    answer(tmp_path, has_warehouse="yes")
    seen = []
    for key, value in (
        ("warehouse_url", "postgres://dw.internal:5432/analytics"),
        ("name", "acme"),
        ("users", 3),
    ):
        item = asked(tmp_path)
        seen.append(item.answer_keys[0])
        assert item.title == ANSWER_KEYS[key].question
        answer(tmp_path, **{key: value})
    assert seen == ["warehouse_url", "name", "users"]
    assert intake_items(tmp_path) == [Item("command", "Create the project", "vecten init")]


def test_start_empty_counts_as_answered(tmp_path):
    answer(tmp_path, has_warehouse="no", sources=[])
    assert asked(tmp_path).answer_keys == ("warehouse",)


def test_unknown_key_is_ignored(tmp_path):
    answer(tmp_path, colour="blue", has_warehouse="no")
    assert asked(tmp_path).answer_keys == ("sources",)


def test_yaml_boolean_has_warehouse_takes_the_existing_branch(tmp_path):
    answer(tmp_path, has_warehouse=True)
    assert asked(tmp_path).answer_keys == ("warehouse_url",)
    answer(tmp_path, has_warehouse=False)
    assert asked(tmp_path).answer_keys == ("sources",)
