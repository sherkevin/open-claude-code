import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / 'fixtures' / 'industrial-public-v1'


def test_public_corpus_manifest_has_verified_sources_and_hashes():
    manifest = json.loads((ROOT / 'source-catalog.json').read_text(encoding='utf-8'))
    assert manifest['corpus_id'] == 'industrial-public-v1'
    assert manifest['customer_data'] is False
    assert len(manifest['downloaded']) >= 5
    for item in manifest['downloaded']:
        path = ROOT / item['path']
        assert path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == item['sha256']
        assert item['url'].startswith('https://')
        assert item['license']


def test_public_corpus_keeps_catalog_only_sources_explicit():
    manifest = json.loads((ROOT / 'source-catalog.json').read_text(encoding='utf-8'))
    assert manifest['catalog_only']
    assert all('url' in item and item['license'] for item in manifest['catalog_only'])
