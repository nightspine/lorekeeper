import pytest
from jsonschema import Draft202012Validator

from lorekeeper import conformance, validate

from .conftest import ROOT

CASES = conformance.cases(ROOT / "conformance")


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case(path):
    ok, expected, actual = conformance.check(path)
    assert actual == expected
    Draft202012Validator(validate.schema("view")).validate(actual)


def test_cases_exist():
    assert len(CASES) >= 10
