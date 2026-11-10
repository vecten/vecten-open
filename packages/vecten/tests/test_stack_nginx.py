import re

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import generate_nginx_conf, write_nginx
from vecten_shared.output import ExitCode, Failure

HOSTS = {"airflow": "airflow.localhost", "mcp": "mcp.localhost"}


def servers(conf: str) -> list[str]:
    return re.findall(r"server\s*{(.*?)\n}", conf, re.S)


def test_both_components_local():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "local")
    blocks = servers(conf)
    assert any("default_server" in b and "return 444" in b for b in blocks)
    airflow = next(b for b in blocks if "server_name airflow.localhost" in b and "listen 443" in b)
    mcp = next(b for b in blocks if "server_name mcp.localhost" in b and "listen 443" in b)
    assert "auth_basic" not in airflow
    assert "set $upstream http://airflow-api-server:8080;" in airflow
    assert "auth_basic" not in mcp and "set $upstream http://mcp-server:8001;" in mcp
    assert (
        "proxy_buffering off" in mcp and "ssl_certificate /etc/nginx/certs/mcp.localhost.crt" in mcp
    )
    assert "proxy_set_header Upgrade $http_upgrade" in airflow
    assert "proxy_set_header Host $http_host;" in airflow
    assert "proxy_set_header Connection $connection_upgrade;" in airflow
    assert "chunked_transfer_encoding" not in conf


def test_default_server_rejects_unknown_sni_without_a_certificate():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "local")
    default = next(b for b in servers(conf) if "default_server" in b)
    assert "ssl_reject_handshake on;" in default
    assert "ssl_certificate" not in default


def test_connection_upgrade_map_is_emitted_once_at_the_top():
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, "local")
    assert conf.startswith(
        "map $http_upgrade $connection_upgrade {\n    default upgrade;\n    '' close;\n}\n"
    )
    assert conf.count("map $http_upgrade") == 1


def test_unknown_component_is_an_internal_failure():
    with pytest.raises(Failure) as exc:
        generate_nginx_conf(["platform", "warehouse"], HOSTS, "local")
    assert exc.value.code == ExitCode.INTERNAL


def test_mcp_only_has_no_airflow_upstream():
    conf = generate_nginx_conf(["mcp"], HOSTS, "local")
    assert "airflow-api-server" not in conf and "mcp-server:8001" in conf


def test_vm_mode_uses_letsencrypt_and_acme_challenge():
    conf = generate_nginx_conf(
        ["platform"], {"airflow": "airflow.data.acme.com", "mcp": "mcp.data.acme.com"}, "vm"
    )
    assert "/etc/letsencrypt/live/airflow.data.acme.com/fullchain.pem" in conf
    assert "location /.well-known/acme-challenge/" in conf
    assert "return 301 https://$host$request_uri" in conf
    assert "mcp.data.acme.com" not in conf.split("listen 443")[1]


def test_local_hosts_with_port_yield_bare_server_name_and_cert_path():
    conf = generate_nginx_conf(
        ["platform"], {"airflow": "airflow.localhost:8443", "mcp": "mcp.localhost:8443"}, "local"
    )
    assert "server_name airflow.localhost;" in conf
    assert "/etc/nginx/certs/airflow.localhost.crt" in conf
    assert "server_name airflow.localhost:8443" not in conf
    assert "airflow.localhost:8443.crt" not in conf


def test_local_redirect_keeps_https_port():
    conf = generate_nginx_conf(
        ["platform"], {"airflow": "airflow.localhost:8443", "mcp": "mcp.localhost:8443"}, "local"
    )
    assert "return 301 https://airflow.localhost:8443$request_uri;" in conf
    assert "https://$host$request_uri" not in conf


def _locations(block: str) -> dict[str, str]:
    return dict(re.findall(r"location ([^{]+?) {\n(.*?)\n    }", block, re.S))


@pytest.mark.parametrize("mode", ["local", "vm"])
def test_airflow_is_proxied_whole_and_its_own_login_guards_it(mode):
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, mode)
    airflow = next(b for b in servers(conf) if "server_name airflow.localhost" in b and "443" in b)
    locations = _locations(airflow)
    # Airflow's UI sends its token as `Authorization: Bearer`, which NGINX basic auth would
    # take for missing credentials and challenge again; the health path needs no exception.
    assert set(locations) == {"/"}
    assert "auth_basic" not in airflow
    assert "set $upstream http://airflow-api-server:8080;" in locations["/"]
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in locations["/"]
    assert "proxy_set_header Host $http_host;" in locations["/"]


@pytest.mark.parametrize("mode", ["local", "vm"])
def test_every_upstream_gets_only_the_address_nginx_saw(mode):
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, mode)
    for location in re.findall(r"location [^{]+? {\n(.*?)\n    }", conf, re.S):
        if "proxy_pass" in location:
            assert "proxy_set_header X-Forwarded-For $remote_addr;" in location
    assert "$proxy_add_x_forwarded_for" not in conf


@pytest.mark.parametrize("mode", ["local", "vm"])
def test_upstreams_are_resolved_per_request_through_docker_dns(mode):
    # A recreated container comes back on a new address; NGINX must look the name up again.
    conf = generate_nginx_conf(["platform", "mcp"], HOSTS, mode)
    assert conf.count("resolver 127.0.0.11 valid=10s ipv6=off;") == 1
    assert conf.index("resolver 127.0.0.11") < conf.index("server {")
    assert not re.search(r"proxy_pass\s+http://", conf)
    for location in re.findall(r"location [^{]+? {\n(.*?)\n    }", conf, re.S):
        if "proxy_pass" in location:
            assert re.search(r"set \$upstream http://[a-z-]+:\d+;", location)
            assert "proxy_pass $upstream;" in location


def test_no_served_component_is_an_internal_failure():
    with pytest.raises(Failure) as exc:
        generate_nginx_conf([], HOSTS, "local")
    assert exc.value.code == ExitCode.INTERNAL


def test_write_nginx_writes_the_conf_and_no_password_file(tmp_path):
    write_nginx(tmp_path, ["platform", "mcp"], HOSTS, "local")
    assert (tmp_path / "nginx" / "conf.d" / "default.conf").is_file()
    assert not list((tmp_path / "nginx" / "conf.d").glob(".*.tmp"))
    assert not (tmp_path / "nginx" / "htpasswd").exists()


def test_self_signed_is_idempotent_with_san(tmp_path):
    from cryptography import x509

    first = ensure_self_signed(tmp_path, ["airflow.localhost", "mcp.localhost"])
    crt = (tmp_path / "certs" / "mcp.localhost.crt").read_bytes()
    cert = x509.load_pem_x509_certificate(crt)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "mcp.localhost" in san.get_values_for_type(x509.DNSName)
    ensure_self_signed(tmp_path, ["airflow.localhost", "mcp.localhost"])
    assert (tmp_path / "certs" / "mcp.localhost.crt").read_bytes() == crt
    assert first == tmp_path / "certs"


def test_self_signed_strips_port_from_hostname(tmp_path):
    from cryptography import x509

    ensure_self_signed(tmp_path, ["airflow.localhost:8443"])
    crt = tmp_path / "certs" / "airflow.localhost.crt"
    assert crt.is_file() and (tmp_path / "certs" / "airflow.localhost.key").is_file()
    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["airflow.localhost"]


def test_self_signed_key_is_written_atomically_at_0600(tmp_path, monkeypatch):
    import vecten.stack.certs as certs

    calls = []
    real = certs.write_text_atomic

    def spy(path, text, mode=None):
        calls.append((path.name, mode))
        real(path, text, mode=mode)

    monkeypatch.setattr(certs, "write_text_atomic", spy)
    ensure_self_signed(tmp_path, ["mcp.localhost"])
    assert ("mcp.localhost.key", 0o600) in calls
    assert (tmp_path / "certs" / "mcp.localhost.key").stat().st_mode & 0o777 == 0o600
