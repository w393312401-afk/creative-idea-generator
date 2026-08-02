# -*- coding: utf-8 -*-
"""号池配置导出与导入功能的单元测试。
"""

import json
import pytest

from integrations.google_fx.utils import account_credentials as creds
from integrations.google_fx.utils import account_pool


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """隔离 account_pool.json 和 account_credentials.json 的数据存储。"""
    pool_file = tmp_path / "account_pool.json"
    cred_file = tmp_path / "account_credentials.json"
    monkeypatch.setattr(account_pool, "_STATE_FILE", pool_file)
    monkeypatch.setattr(creds, "_STATE_FILE", cred_file)
    return tmp_path


def test_export_config_without_credentials():
    pool = account_pool.AccountPool()
    pool.add_account("u1", name="Acc1", note="Note1", serial_number="10")
    creds.save("u1", email="acc1@example.com", password="pass1", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    exported = pool.export_config(include_credentials=False)
    assert exported["include_credentials"] is False
    assert exported["total"] == 1
    acc = exported["accounts"][0]
    assert acc["user_id"] == "u1"
    assert acc["name"] == "Acc1"
    assert "email" not in acc
    assert "password" not in acc
    assert "totp_secret" not in acc


def test_export_config_with_credentials():
    pool = account_pool.AccountPool()
    pool.add_account("u1", name="Acc1", note="Note1", serial_number="10")
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    creds.save("u1", email="acc1@example.com", password="pass1", totp_secret=secret)

    exported = pool.export_config(include_credentials=True)
    assert exported["include_credentials"] is True
    acc = exported["accounts"][0]
    assert acc["email"] == "acc1@example.com"
    assert acc["password"] == "pass1"
    assert acc["totp_secret"] == secret


def test_import_config_basic_and_merge():
    pool = account_pool.AccountPool()
    pool.add_account("u1", name="Original Name", note="Original Note", serial_number="1")

    import_data = {
        "accounts": [
            {
                "user_id": "u1",
                "name": "Updated Name",
                "note": "Updated Note",
                "serial_number": "1",
                "disabled": True,
            },
            {
                "user_id": "u2",
                "name": "New Account",
                "note": "New Note",
                "serial_number": "2",
                "email": "newacc@example.com",
                "password": "pass2",
                "totp_secret": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            },
        ]
    }

    res = pool.import_config(import_data, overwrite=False)
    assert res["added"] == 1
    assert res["updated"] == 1
    assert res["credentials_saved"] == 1

    accounts = pool.list_accounts(heal=False)
    acc_map = {a["user_id"]: a for a in accounts}

    assert acc_map["u1"]["name"] == "Updated Name"
    assert acc_map["u1"]["disabled"] is True

    assert acc_map["u2"]["name"] == "New Account"
    cred_u2 = creds.get("u2")
    assert cred_u2["email"] == "newacc@example.com"
    assert cred_u2["password"] == "pass2"
    assert cred_u2["totp_secret"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_import_config_invalid_data_structure():
    pool = account_pool.AccountPool()
    with pytest.raises(ValueError):
        pool.import_config({"invalid": "data"})
