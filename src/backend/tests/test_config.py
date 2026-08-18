from app.core.config import config


def test_config_loads_defaults():
    assert config.app.name == "troshka"
    assert config.app.port == 8200


def test_config_has_database_section():
    assert hasattr(config, "database")
    assert config.database.url is not None


def test_config_has_auth_section():
    assert hasattr(config, "auth")
    assert config.auth.jwt_algorithm == "HS256"


def test_config_has_ocpvirt_pkg_repo():
    assert hasattr(config, "ocpvirt")
    assert hasattr(config.ocpvirt, "pkg_repo")
    assert "ocpv-infra01" in config.ocpvirt.pkg_repo.url
    assert config.ocpvirt.pkg_repo.iso_library_item_name == "RHEL 10.2 Binary DVD"
