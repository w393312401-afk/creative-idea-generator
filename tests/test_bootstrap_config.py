"""首次运行的配置引导（tools/bootstrap_config.py）契约。

run.bat / run.sh 在新机器上调它从模板生成 server_config.json。核心一条：
**不能直接拷模板**——模板里 apiKey / accessCode / codexApiKey 填的是中文说明
文字而不是空值，原样拷过去的话 accessCode 非空即视为已设门禁，整个界面被一个
谁也不知道的口令锁死（实测干净克隆后 /api/library 返回 401）。
"""
import importlib.util
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tools', 'bootstrap_config.py')


def _load(tmp_path, monkeypatch):
    """把脚本的 ROOT 指到临时目录，避免碰到真实仓库里的配置。"""
    spec = importlib.util.spec_from_file_location('bootstrap_config', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'ROOT', str(tmp_path))
    monkeypatch.setattr(mod, 'TARGET', str(tmp_path / 'server_config.json'))
    monkeypatch.setattr(mod, 'TEMPLATE', str(tmp_path / 'server_config.example.json'))
    return mod


@pytest.fixture
def sandbox(tmp_path):
    """用仓库里真实的模板，别自己造一份——造的那份和真模板漂移了测试就白测。"""
    with open(os.path.join(ROOT, 'server_config.example.json'), encoding='utf-8') as f:
        template = f.read()
    (tmp_path / 'server_config.example.json').write_text(template, encoding='utf-8')
    return tmp_path


def _written(tmp_path):
    with open(tmp_path / 'server_config.json', encoding='utf-8') as f:
        return json.load(f)


def test_the_real_template_still_has_placeholder_text(sandbox):
    """这条守的是前提：模板里那三项确实是说明文字。哪天模板改成了真空值，
    净化逻辑就不再必要——那时应该来改这个测试，而不是让它悄悄失去意义。"""
    cfg = json.loads((sandbox / 'server_config.example.json').read_text(encoding='utf-8'))
    assert cfg['accessCode'] and '设一个' in cfg['accessCode']
    assert cfg['apiKey'] and '在这里填' in cfg['apiKey']


def test_placeholders_are_cleared_not_copied(sandbox, monkeypatch):
    mod = _load(sandbox, monkeypatch)
    assert mod.main() == 0
    cfg = _written(sandbox)
    assert cfg['accessCode'] == '', '门禁必须留空，否则界面被未知口令锁死'
    assert cfg['apiKey'] == '', '占位说明文字不能当成真密钥发给上游'
    assert cfg['codexApiKey'] == ''


def test_real_settings_are_preserved(sandbox, monkeypatch):
    """除了被清空的那几项占位符，其余每一项都必须与模板逐字一致——
    包括类型（模板里 adsPowerPort 是字符串 "50325"，别在搬运途中改成 int）。"""
    template = json.loads(
        (sandbox / 'server_config.example.json').read_text(encoding='utf-8'))
    mod = _load(sandbox, monkeypatch)
    mod.main()
    cfg = _written(sandbox)

    assert set(cfg) == set(template), '键集合不能增删'
    cleared = {k for k in template if cfg[k] != template[k]}
    assert cleared == {'apiKey', 'accessCode', 'codexApiKey'}, \
        '只有这三项占位符该被清空，实际被改动的是 %s' % sorted(cleared)
    for key in set(template) - cleared:
        assert cfg[key] == template[key] and type(cfg[key]) is type(template[key]), key


def test_comment_keys_survive(sandbox, monkeypatch):
    """`_xxx_comment` 是给人看的说明，值里也含"在这里填"之类的字样，
    但它们不是配置项，不能被当成占位符清空。"""
    mod = _load(sandbox, monkeypatch)
    mod.main()
    cfg = _written(sandbox)
    comments = [k for k in cfg if k.startswith('_')]
    assert comments, '模板里的注释键应当原样保留'
    assert all(cfg[k] for k in comments), '注释内容不该被清空'


def test_existing_config_is_never_overwritten(sandbox, monkeypatch):
    mine = {'apiKey': 'sk-my-real-key', 'accessCode': 'my-code', 'model': 'x'}
    (sandbox / 'server_config.json').write_text(
        json.dumps(mine), encoding='utf-8')
    mod = _load(sandbox, monkeypatch)
    assert mod.main() == 0
    assert _written(sandbox) == mine, '已有配置必须原样保留，绝不能被模板覆盖'


def test_missing_template_is_not_an_error(sandbox, monkeypatch):
    os.remove(sandbox / 'server_config.example.json')
    mod = _load(sandbox, monkeypatch)
    assert mod.main() == 0, '模板缺失只是跳过生成，不该让启动脚本中断'
    assert not (sandbox / 'server_config.json').exists()


def test_output_is_valid_json_with_trailing_newline(sandbox, monkeypatch):
    mod = _load(sandbox, monkeypatch)
    mod.main()
    raw = (sandbox / 'server_config.json').read_text(encoding='utf-8')
    assert raw.endswith('\n')
    json.loads(raw)


def test_generated_config_leaves_the_access_gate_open(sandbox, monkeypatch):
    """服务端读到空 accessCode 就不设门禁——这是"拉下来能直接用"的关键一环。"""
    import server_common
    mod = _load(sandbox, monkeypatch)
    mod.main()
    cfg = _written(sandbox)
    assert (cfg.get('accessCode') or '').strip() == '', (
        'ACCESS_CODE 由 server_common 按 accessCode.strip() 取值，'
        '非空即开启门禁（server_common.py 的 ACCESS_CODE）')
    assert hasattr(server_common, 'ACCESS_CODE')
