from __vecten_package__ import skill


def test_the_skill_ships_inside_the_package():
    assert (skill / "SKILL.md").is_file()
