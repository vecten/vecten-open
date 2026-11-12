import tomllib

import pytest
import tomli_w

from vecten_shared.config import VectenConfig, load_config, save_config
from vecten_shared.output import ExitCode, Failure


def _config(**extra):
    return VectenConfig.model_validate(
        {"users": 3, "project": {"name": "acme", "scaffold_version": "0.1.0"}, **extra}
    )


def _load(tmp_path, **raw_extra):
    save_config(tmp_path, _config())
    raw = tomllib.loads((tmp_path / "vecten.toml").read_text())
    raw.update(raw_extra)
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    return load_config(tmp_path)


def test_a_project_declares_no_area_by_default(tmp_path):
    assert _config().areas == {}
    save_config(tmp_path, _config())
    assert "areas" not in (tmp_path / "vecten.toml").read_text()


def test_an_area_round_trips_with_and_without_its_access_answer(tmp_path):
    loaded = _load(tmp_path, areas={"finance": {"access": {"groups": ["finance"]}}, "payroll": {}})
    assert loaded.areas["finance"].access.groups == ["finance"]
    assert loaded.areas["payroll"].access is None
    save_config(tmp_path, loaded)
    raw = tomllib.loads((tmp_path / "vecten.toml").read_text())
    assert raw["areas"]["finance"] == {"access": {"groups": ["finance"]}}
    assert load_config(tmp_path).areas == loaded.areas


@pytest.mark.parametrize("name", ["Finance", "fin_ops", "2024", "fin-ops", "", "a" * 58])
def test_an_area_name_is_lowercase_letters_and_digits(tmp_path, name):
    with pytest.raises(Failure) as exc:
        _load(tmp_path, areas={name: {}})
    assert exc.value.code == ExitCode.CONFIG
    assert "areas" in exc.value.message and "[a-z][a-z0-9]*" in exc.value.message


def test_an_area_name_may_be_57_characters(tmp_path):
    assert list(_load(tmp_path, areas={"a" * 57: {}}).areas) == ["a" * 57]


@pytest.mark.parametrize("groups", [[], ["sales-ops"]])
def test_an_area_access_answer_names_groups(tmp_path, groups):
    with pytest.raises(Failure) as exc:
        _load(tmp_path, areas={"finance": {"access": {"groups": groups}}})
    assert exc.value.code == ExitCode.CONFIG and "areas.finance.access" in exc.value.message


def test_an_area_is_refused_under_a_connectors_name(tmp_path):
    with pytest.raises(Failure) as exc:
        _load(
            tmp_path,
            connectors=[{"name": "hubspot", "source": "dlt"}],
            areas={"hubspot": {}},
        )
    assert exc.value.code == ExitCode.CONFIG
    assert "area hubspot" in exc.value.message and "raw_hubspot" in exc.value.message
