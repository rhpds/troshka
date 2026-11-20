"""CR-spec digests must ignore whole-number float vs int JSON differences."""

from app.services.kubevirt_storage_enrollment import (
    canonicalize_json_numbers,
    digest,
    digest_cr_spec,
)


def test_canonicalize_coerces_whole_number_floats_recursively():
    assert canonicalize_json_numbers(
        {"position": {"x": 300.0, "y": 1.5}, "ram": 32.0, "tags": [1.0, 2.5]}
    ) == {"position": {"x": 300, "y": 1.5}, "ram": 32, "tags": [1, 2.5]}


def test_digest_cr_spec_equates_whole_number_float_and_int():
    assert digest_cr_spec({"x": 300.0}) == digest_cr_spec({"x": 300})
    assert digest_cr_spec({"x": 300.0}) == digest({"x": 300})


def test_digest_cr_spec_keeps_non_integer_floats_distinct():
    assert digest_cr_spec({"ram": 28.5}) != digest_cr_spec({"ram": 28})
    assert digest_cr_spec({"ram": 28.5}) != digest({"ram": 28})


def test_raw_digest_still_distinguishes_float_and_int():
    assert digest({"x": 300.0}) != digest({"x": 300})
