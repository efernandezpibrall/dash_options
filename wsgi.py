"""Initialize Dash callbacks before a threaded worker accepts requests."""

from index_options import app, server

# Dash's first-request setup is not synchronized across request threads.
# Existing browser intervals can arrive together immediately after a restart.
app._setup_server()

__all__ = ["server"]
