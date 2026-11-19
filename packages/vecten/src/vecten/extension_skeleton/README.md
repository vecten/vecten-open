# __vecten_title__

A Vecten Open extension kept in this project under `extensions/__vecten_name__/`. Its code is
the project's own: change it in pull requests like any other file.

- Add a part by declaring its entry point in `pyproject.toml` and returning a directory inside
  the package: `vecten.dags`, `vecten.dbt_packages`, `vecten.osi` or `vecten.skills`; or an object
  for `vecten.connectors` or `vecten.mcp.tools`. Resources live inside `src/__vecten_package__/`.
- Add a dependency to `dependencies`, or to the `platform`, `mcp` or `cli` extra when only one
  component needs it, then run `vecten lock`. A code change needs no lock.
- Run the tests with `uv run --with pytest --with ./extensions/__vecten_name__ pytest
  extensions/__vecten_name__/tests`.
