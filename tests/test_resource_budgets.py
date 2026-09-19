import json

import pytest
from tools.check_resources import measure_page, check


def test_assets_are_resolved_with_queries_and_external_assets_skipped(tmp_path):
    (tmp_path / 'index.html').write_text('<script src="app.js?v=2"></script><link rel="stylesheet" href="https://example.invalid/style.css">')
    (tmp_path / 'app.js').write_text('const a = 1;\n' * 1000)
    measured = measure_page(tmp_path, 'index.html')
    assert measured['scripts'] == 1
    assert measured['gzip_bytes'] < measured['raw_bytes']
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'resource_budgets.json').write_text(json.dumps({'pages': {'index.html': {'raw_bytes': 100}}}))
    assert 'exceeds 100' in check(tmp_path)['errors'][0]


@pytest.mark.parametrize('url', ['missing.js', '../escape.js', '/%2e%2e/escape.js'])
def test_missing_and_escaping_assets_fail(tmp_path, url):
    (tmp_path / 'index.html').write_text(f'<script src="{url}"></script>')
    with pytest.raises(ValueError, match='missing or out-of-project'):
        measure_page(tmp_path, 'index.html')
