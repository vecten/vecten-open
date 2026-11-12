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
