"""The CLI's own environment after `install_lock`: what it installed is usable at once."""

import sys

from vecten import extensions


def test_an_editable_extension_imports_in_the_process_that_installed_it(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    source = tmp_path / "project" / "extensions" / "acme-fresh" / "src"
    (source / "acme_fresh_pkg").mkdir(parents=True)
    (source / "acme_fresh_pkg" / "__init__.py").write_text("VALUE = 7\n")

    def uv(args, root, cwd=None):
        # An editable install of a src layout writes a path file, read at interpreter start.
        if "--editable" in args:
            (site / "_editable_impl_acme_fresh_pkg.pth").write_text(f"{source}\n")

    monkeypatch.setattr(extensions, "_uv", uv)
    monkeypatch.setattr(extensions, "site_packages", lambda: site)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "acme_fresh_pkg", raising=False)
    extensions.install_lock("", tmp_path / "project", ["extensions/acme-fresh"])
    import acme_fresh_pkg

    assert acme_fresh_pkg.VALUE == 7
    sys.modules.pop("acme_fresh_pkg", None)


def _dist(monkeypatch, direct_url):
    from types import SimpleNamespace

    from vecten import extensions

    def distribution(name):
        if direct_url is None:
            raise extensions.metadata.PackageNotFoundError(name)
        return SimpleNamespace(read_text=lambda f: direct_url)

    monkeypatch.setattr(extensions.metadata, "distribution", distribution)


def test_installed_from_needs_an_editable_install_of_that_very_path(tmp_path, monkeypatch):
    import json

    from vecten import extensions

    here = tmp_path / "extensions" / "acme"
    here.mkdir(parents=True)
    editable = {"url": here.as_uri(), "dir_info": {"editable": True}}
    _dist(monkeypatch, json.dumps(editable))
    assert extensions.installed_from("acme", here)
    assert not extensions.installed_from("acme", tmp_path / "elsewhere")
    _dist(monkeypatch, json.dumps({**editable, "dir_info": {}}))
    assert not extensions.installed_from("acme", here)
    _dist(monkeypatch, None)
    assert not extensions.installed_from("acme", here)
    _dist(monkeypatch, "not json")
    assert not extensions.installed_from("acme", here)
