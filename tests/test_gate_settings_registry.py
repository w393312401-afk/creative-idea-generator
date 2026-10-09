"""Retired quality rules cannot be revived by any legacy configuration source."""
import os
from unittest.mock import patch
import pytest
import server_common as common

@pytest.mark.parametrize('managed', [True, False])
@pytest.mark.parametrize('raw', [False, 'false', None])
def test_all_quality_rules_stay_retired(managed, raw):
    legacy = {row['key']: (True if row['type']=='bool' else 3 if row['type']=='int' else 'strict')
              for row in common.GATE_SETTINGS}
    legacy['reviewsDisabled'] = raw
    with patch.dict(common.SERVER_CONFIG, legacy, clear=True), \
         patch.dict(os.environ, {'SPARK_REVIEWS_DISABLED':'false','SPARK_QA_GATE_LEVEL':'standard','SPARK_STRICT_GATES':'true'}), \
         patch.object(common, 'SERVER_MANAGED', managed):
        normalized = common.effective_config(dict(legacy, strictPromptPipelineV2=True))
        assert common.reviews_disabled(legacy)
        assert common.qa_gate_level(legacy)=='off'
        assert not common.strict_gates_enabled(legacy)
        for spec in common.GATE_SETTINGS:
            assert common.gate_setting(spec['key'], legacy)==spec['default']
            assert normalized[spec['key']]==spec['default']
        assert normalized['reviewsRetired']
        assert normalized['strictPromptPipelineV2'] is False
        report=common.gate_settings_report()
        assert all(row['retired'] and not row['editable'] for row in report)
        assert all(row['server_value']==row['default'] for row in report)

def test_compatibility_keys_and_non_review_configuration():
    keys={row['key'] for row in common.GATE_SETTINGS}
    assert len(keys)==12
    assert common.QA_GATE_LEVELS==('standard','lenient','off')
    with pytest.raises(KeyError):
        common.gate_setting('misspelledGate')
    with patch.object(common, 'SERVER_MANAGED', False):
        result=common.effective_config({'imageQuality':'1K','imageAspectRatio':'9:16','customField':'keep'})
    assert result['imageQuality']=='1K'
    assert result['imageAspectRatio']=='9:16'
    assert result['customField']=='keep'
