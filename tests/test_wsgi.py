"""A fresh deployment must register callbacks before any browser request."""

from pathlib import Path
import subprocess
import sys


def test_wsgi_registers_callbacks_before_first_request():
    result = subprocess.run(
        [sys.executable, "-c", """
import wsgi
from index_options import app
assert wsgi.server is app.server
assert app._got_first_request['setup_server']
assert '..ice-chat-quote-snapshot.data...ice-chat-service-snapshot.data..' in app.callback_map
assert any('brent-single-candidate.data' in key for key in app.callback_map)
"""],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
