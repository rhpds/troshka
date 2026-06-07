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
