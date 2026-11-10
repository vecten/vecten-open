from vecten.connectors.inspect_source import secret_params, source_function_name

VERIFIED = """
import dlt

@dlt.source(name="hubspot")
def hubspot(
    api_key: str = dlt.secrets.value, include_history: bool = False, props: list = dlt.config.value
):
    pass

@dlt.source
def hubspot_events(api_key: str = dlt.secrets.value, event_types=None):
    pass
"""


def test_secret_params_of_the_source_named_like_the_connector(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text(VERIFIED)
    assert secret_params(init, "hubspot") == ["api_key"]
    assert source_function_name(init, "hubspot") == "hubspot"


def test_falls_back_to_the_first_dlt_source(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text(VERIFIED.replace("def hubspot(", "def crm("))
    assert source_function_name(init, "hubspot") == "crm"
    assert secret_params(init, "hubspot") == ["api_key"]


def test_keyword_only_secret_params_are_found(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text(
        "import dlt\n@dlt.source\ndef s(*, token: str = dlt.secrets.value):\n    pass\n"
    )
    assert secret_params(init, "s") == ["token"]


def test_no_source_function_is_an_error(tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text("x = 1\n")
    import pytest

    from vecten_shared.output import ExitCode, Failure

    with pytest.raises(Failure) as exc:
        source_function_name(init, "s")
    assert exc.value.code == ExitCode.TOOL_FAILED
