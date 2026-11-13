"""Recording a fact in vecten.toml changes that value only, and keeps everything a person wrote."""

from vecten_shared.config import load_config, save_config

WRITTEN = """# Acme's data platform
users = 5
components = ["platform", "mcp"]
extensions = ["acme"]

[project]
name = "acme"  # the repository's name too
scaffold_version = "0.1.0"

# Caps agreed with finance on 2026-11-01.
[settings.acme]
daily_cap = 500  # per day, not per run

[settings.acme.env.local]
daily_cap = 10
"""


def test_a_recorded_fact_keeps_comments_blank_lines_and_order(tmp_path):
    (tmp_path / "vecten.toml").write_text(WRITTEN)
    config = load_config(tmp_path)
    config.environments["prod"].first_user_added = True
    save_config(tmp_path, config)
    text = (tmp_path / "vecten.toml").read_text()
    assert text.startswith(WRITTEN.rstrip("\n"))
    assert "[environments.prod]" in text and "first_user_added = true" in text
    assert load_config(tmp_path).environments["prod"].first_user_added is True


def test_a_changed_value_keeps_its_line_comment(tmp_path):
    (tmp_path / "vecten.toml").write_text(WRITTEN)
    config = load_config(tmp_path)
    config.users = 7
    save_config(tmp_path, config)
    text = (tmp_path / "vecten.toml").read_text()
    assert "users = 7\n" in text
    assert "# Caps agreed with finance on 2026-11-01." in text
    assert "daily_cap = 500  # per day, not per run" in text


def test_a_value_set_to_nothing_is_removed(tmp_path):
    (tmp_path / "vecten.toml").write_text(WRITTEN + '\n[warehouse]\ndatabase = "analytics"\n')
    config = load_config(tmp_path)
    config.warehouse.database = None
    save_config(tmp_path, config)
    text = (tmp_path / "vecten.toml").read_text()
    assert "database" not in text
    assert "# Caps agreed with finance on 2026-11-01." in text


def test_a_new_file_holds_the_whole_configuration(tmp_path):
    (tmp_path / "vecten.toml").write_text(WRITTEN)
    config = load_config(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    save_config(other, config)
    assert load_config(other) == config


def test_a_save_tomlkit_would_corrupt_falls_back_to_a_valid_file(tmp_path):
    written = WRITTEN + (
        '\n[environments.prod]\ndeployed_commit = "abc"\n\n[warehouse]\nkind = "postgres"\n'
        '\n[environments.prod.cloud_ids]\nproject = "p1"\n'
    )
    (tmp_path / "vecten.toml").write_text(written)
    config = load_config(tmp_path)
    config.environments["prod"].deployed_commit = None
    config.environments["prod"].first_user_added = True
    save_config(tmp_path, config)
    assert load_config(tmp_path) == config
