import gzip

import brotli

import index_options


def test_large_frontend_bundles_support_gzip_and_brotli_without_body_changes():
    client = index_options.app.server.test_client()
    client.get('/')
    paths = [
        '/_dash-component-suites/plotly/package_data/plotly.min.js',
        '/_dash-component-suites/dash_ag_grid/async-community.js',
    ]

    for path in paths:
        raw = client.get(path)
        gzip_response = client.get(path, headers={'Accept-Encoding': 'gzip'})
        brotli_response = client.get(path, headers={'Accept-Encoding': 'br'})

        assert raw.status_code == 200
        assert gzip_response.headers['Content-Encoding'] == 'gzip'
        assert brotli_response.headers['Content-Encoding'] == 'br'
        assert gzip_response.headers['Vary'] == 'Accept-Encoding'
        assert gzip.decompress(gzip_response.data) == raw.data
        assert brotli.decompress(brotli_response.data) == raw.data
        assert len(gzip_response.data) < len(raw.data)
        assert len(brotli_response.data) < len(raw.data)
        assert 'public' in gzip_response.headers['Cache-Control']


def test_versioned_assets_receive_immutable_cache_headers():
    client = index_options.app.server.test_client()
    response = client.get('/assets/styles.css?m=1')

    assert response.status_code == 200
    assert 'max-age=31536000' in response.headers['Cache-Control']
    assert 'immutable' in response.headers['Cache-Control']


def test_missing_assets_are_not_cached():
    client = index_options.app.server.test_client()
    response = client.get('/assets/not-present.css?m=1')

    assert response.status_code == 404
    assert 'immutable' not in response.headers.get('Cache-Control', '')
