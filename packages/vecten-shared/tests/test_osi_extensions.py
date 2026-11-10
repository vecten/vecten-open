import json

import pytest

from vecten_shared.osi.extensions import VECTEN_VENDOR, read_vecten_extension, vecten_extension


def test_extension_is_common_vendor_with_json_string():
    ext = vecten_extension({"dataset": "orders"})
    assert ext == {"vendor_name": "COMMON", "data": '{"vecten": {"dataset": "orders"}}'}
    assert VECTEN_VENDOR == "COMMON"


def test_read_round_trip_and_ignores_other_entries():
    obj = {
        "custom_extensions": [
            {"vendor_name": "SNOWFLAKE", "data": '{"x": 1}'},
            vecten_extension({"additivity": "semi_additive"}),
            {"vendor_name": "COMMON", "data": '{"other": true}'},
        ]
    }
    assert read_vecten_extension(obj) == {"additivity": "semi_additive"}
    assert read_vecten_extension({}) is None
    assert read_vecten_extension({"custom_extensions": []}) is None


def test_read_refuses_two_vecten_entries_and_bad_data():
    two = {"custom_extensions": [vecten_extension({"a": 1}), vecten_extension({"b": 2})]}
    with pytest.raises(ValueError, match="one Vecten extension"):
        read_vecten_extension(two)
    bad = {"custom_extensions": [{"vendor_name": "COMMON", "data": json.dumps({"vecten": [1]})}]}
    with pytest.raises(ValueError, match="object"):
        read_vecten_extension(bad)
    unparsable = {"custom_extensions": [{"vendor_name": "COMMON", "data": "{not json"}]}
    assert read_vecten_extension(unparsable) is None
