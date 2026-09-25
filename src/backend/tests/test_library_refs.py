"""Tests for library_item_name | alternates and optional skip."""

from app.services.library_refs import OPTIONAL_LIBRARY_REF, split_library_item_names


def test_split_single_name():
    names, optional = split_library_item_names("Fedora Cloud 43")
    assert names == ["Fedora Cloud 43"]
    assert optional is False


def test_split_ordered_alternates():
    names, optional = split_library_item_names(
        "Prebuilt RHEL 10.2 Bastion|Fedora Cloud 43"
    )
    assert names == ["Prebuilt RHEL 10.2 Bastion", "Fedora Cloud 43"]
    assert optional is False


def test_split_optional_trailing_empty():
    names, optional = split_library_item_names("RHEL 10.2 Binary DVD|")
    assert names == ["RHEL 10.2 Binary DVD"]
    assert optional is True


def test_split_none():
    assert split_library_item_names(None) == ([], False)


def test_optional_sentinel_is_unique():
    assert OPTIONAL_LIBRARY_REF is not None
    assert type(OPTIONAL_LIBRARY_REF).__name__ == "OptionalLibraryRef"
