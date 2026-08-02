"""运行时能力印章（server_common.runtime_capability_report / stamp_manifest_capabilities）。

存在的唯一理由是把「悄悄劣化」变成「清单上写着」。两处已知的静默失效：

· numpy 缺失 → 本地视觉探针（防串片的首尾帧锚点比对、i2v 帧对契约、冻结检测、换族
  惯性检测）全部走 except 分支静默返回 'skipped'/False。设计如此（探针不该拖垮主
  流程），后果是整套内容级校验消失而日志上看不出异常。
· 技能契约文件缺失 → load_reference_file 返回空串，合成按空契约跑完，创意维度变窄、
  一致性约束消失。启动日志与 /api/mode 会喊，但那是服务级信号，翻不到具体某一单上。

所以除了服务级告警，能力状态还要盖进每一单的 manifest：三天后看着一单成片，必须能
看出它当初是不是在缺 numpy 的状态下渲的。
"""
import pytest

import server_common
from server_common import runtime_capability_report, stamp_manifest_capabilities


@pytest.fixture
def all_capable(monkeypatch):
    """一个"什么都齐全"的环境。"""
    monkeypatch.setattr(server_common, '_module_available', lambda name: True)
    monkeypatch.setattr(server_common.shutil, 'which', lambda name: '/usr/bin/' + name)
    # 签名带 profile：能力印章要按本单实际用的技能包查契约
    monkeypatch.setattr(server_common, 'missing_skill_contract_files', lambda profile=None: [])


class TestRuntimeCapabilityReport:
    def test_healthy_environment_reports_nothing(self, all_capable):
        report = runtime_capability_report()
        assert report['degraded'] == []
        assert report['numpy'] is True and report['ffmpeg'] is True

    def test_missing_numpy_names_the_probes_that_die_with_it(self, all_capable, monkeypatch):
        """报文必须点名"哪些校验没跑了"。只说 "numpy 缺失" 等于让人自己去翻代码，
        而这条信息的全部价值就在于"本单没做内容级校验"。"""
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        report = runtime_capability_report()
        assert report['numpy'] is False
        assert len(report['degraded']) == 1
        text = report['degraded'][0]
        assert '内容级校验' in text
        assert '串片' in text

    def test_missing_ffmpeg_is_reported(self, all_capable, monkeypatch):
        monkeypatch.setattr(server_common.shutil, 'which', lambda name: None)
        assert any('ffmpeg' in t for t in runtime_capability_report()['degraded'])

    def test_missing_skill_contract_lists_the_files(self, all_capable, monkeypatch):
        monkeypatch.setattr(server_common, 'missing_skill_contract_files',
                            lambda profile=None: ['SKILL.md', 'references/idea-engine.md'])
        report = runtime_capability_report()
        assert report['skill_contract_missing'] == ['SKILL.md', 'references/idea-engine.md']
        text = next(t for t in report['degraded'] if '技能契约' in t)
        assert 'references/idea-engine.md' in text
        assert '空契约' in text

    def test_multiple_degradations_are_all_reported(self, all_capable, monkeypatch):
        monkeypatch.setattr(server_common, '_module_available', lambda name: False)
        monkeypatch.setattr(server_common.shutil, 'which', lambda name: None)
        monkeypatch.setattr(server_common, 'missing_skill_contract_files',
                            lambda profile=None: ['SKILL.md'])
        assert len(runtime_capability_report()['degraded']) == 4

    def test_report_is_not_cached(self, all_capable, monkeypatch):
        """换 venv / 改 skillDir 都可能在服务运行期间发生；缓存只会让清单记录当年
        那一刻的假象。"""
        assert runtime_capability_report()['degraded'] == []
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        assert runtime_capability_report()['degraded'] != []


class TestStampManifestCapabilities:
    def test_degraded_stage_is_stamped_with_issues(self, all_capable, monkeypatch):
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        manifest = {'frames': []}
        stamp_manifest_capabilities(manifest, 'frames')
        stamp = manifest['capability_degraded']['frames']
        assert stamp['issues'] and stamp['at']

    def test_healthy_stage_leaves_no_key(self, all_capable):
        manifest = {'frames': []}
        stamp_manifest_capabilities(manifest, 'frames')
        assert 'capability_degraded' not in manifest

    def test_restamping_a_fixed_environment_clears_the_flag(self, all_capable, monkeypatch):
        """环境补好后重渲，旗标必须消失——否则清单会永久挂着一条早已修好的告警，
        下次真出问题时没人再当回事。"""
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        manifest = {}
        stamp_manifest_capabilities(manifest, 'frames')
        assert 'capability_degraded' in manifest

        monkeypatch.setattr(server_common, '_module_available', lambda name: True)
        stamp_manifest_capabilities(manifest, 'frames')
        assert 'capability_degraded' not in manifest

    def test_stages_are_recorded_independently(self, all_capable, monkeypatch):
        """帧阶段与视频阶段可能跨越环境变化，且劣化后果不同（帧阶段丢换族惯性检测，
        视频阶段丢防串片与冻结检测）——修好一个不该把另一个的记录抹掉。"""
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        manifest = {}
        stamp_manifest_capabilities(manifest, 'frames')

        monkeypatch.setattr(server_common, '_module_available', lambda name: True)
        stamp_manifest_capabilities(manifest, 'videos')
        assert set(manifest['capability_degraded']) == {'frames'}

        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        stamp_manifest_capabilities(manifest, 'videos')
        assert set(manifest['capability_degraded']) == {'frames', 'videos'}

    def test_non_dict_manifest_is_ignored(self, all_capable):
        stamp_manifest_capabilities(None, 'frames')      # 不该抛

    def test_corrupt_existing_stamp_is_replaced(self, all_capable, monkeypatch):
        """老清单里这个键可能是别的形状（或被手改过）——不能因此抛异常拖垮收尾。"""
        monkeypatch.setattr(server_common, '_module_available',
                            lambda name: name != 'numpy')
        manifest = {'capability_degraded': 'garbage'}
        stamp_manifest_capabilities(manifest, 'frames')
        assert isinstance(manifest['capability_degraded'], dict)


def test_frame_finalize_stamps_the_manifest(all_capable, monkeypatch, tmp_path):
    """update_manifest_stale_status(finalize=True) 是所有渲染路径的共同收尾点：
    单帧重试/定向修复/整单重渲都必须留下印章，不能只有整单那条路径记得。"""
    import frame_generator
    monkeypatch.setattr(server_common, '_module_available',
                        lambda name: name != 'numpy')
    monkeypatch.setattr(frame_generator, 'drop_stale_review_verdicts', lambda m, d: [])
    manifest = {'frames': [{'sequence': 1}, {'sequence': 2}]}
    frame_generator.update_manifest_stale_status(
        manifest, str(tmp_path), regenerated_sequences=[1], finalize=True)
    assert manifest['capability_degraded']['frames']['issues']


def test_non_finalize_write_does_not_stamp(all_capable, monkeypatch, tmp_path):
    """逐帧落盘（finalize=False）走的是同一个函数，但那不是收尾——每帧盖一次章
    只是白写盘。"""
    import frame_generator
    monkeypatch.setattr(server_common, '_module_available',
                        lambda name: name != 'numpy')
    manifest = {'frames': [{'sequence': 1}]}
    frame_generator.update_manifest_stale_status(manifest, str(tmp_path))
    assert 'capability_degraded' not in manifest
