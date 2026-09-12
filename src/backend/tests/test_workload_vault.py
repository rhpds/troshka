import pytest

from app.services.workloads.vault import VaultError, decrypt_vault, is_vault

# Generated with: ansible-vault encrypt_string --vault-password-file <(printf testpass) 'hello-vault-value'
REAL_VECTOR = """$ANSIBLE_VAULT;1.1;AES256
35383362393935383966663839366536386264396234333331643136343566626361366435383132
6661383762623234643463653766616338313437323964300a316132666265373563616335326264
34303362373165663362323231636439376637393133386338356435643230353438363630613165
3734643063373934310a373464353964613634363930303061366633303132346333376264323633
37326565666639663332323739376630306461343039383735623730326130343362"""


def test_is_vault_detects_header():
    assert is_vault(REAL_VECTOR) is True
    assert is_vault("plain string") is False


def test_decrypt_real_ansible_vector():
    assert decrypt_vault(REAL_VECTOR, "testpass") == "hello-vault-value"


def test_wrong_password_raises():
    with pytest.raises(VaultError):
        decrypt_vault(REAL_VECTOR, "wrong-password")
