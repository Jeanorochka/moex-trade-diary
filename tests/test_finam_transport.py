from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from trade_exporter.brokers.finam import FinamBroker
from trade_exporter.brokers.portfolio import _finam_transactions


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def local_api():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            requests.append((url.path, parse_qs(url.query)))
            body = b'{"trades":[],"transactions":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("kind", ["trades", "transactions"])
def test_real_primp_accepts_finam_history_query_parameters(local_api, kind):
    base_url, requests = local_api
    broker = FinamBroker({
        "token": "eyJ.synthetic.signature", "base_url": base_url,
        "request_interval_seconds": 0, "request_timeout_seconds": 2,
    })
    if kind == "trades":
        result = broker._load_window("test-account", START, END)
    else:
        result = _finam_transactions(broker, "test-account", START, END)
    assert result == []
    assert requests == [(f"/v1/accounts/test-account/{kind}", {
        "limit": ["50"], "interval.start_time": ["2026-01-01T00:00:00Z"],
        "interval.end_time": ["2026-01-02T00:00:00Z"],
    })]


def test_query_normalization_preserves_the_callers_numeric_limit(local_api):
    base_url, requests = local_api
    broker = FinamBroker({"token": "eyJ.synthetic.signature", "base_url": base_url, "request_interval_seconds": 0})
    params = {"limit": 17, "account_id": "account with spaces"}
    broker._request("GET", "accounts/test-account/trades", params=params)
    assert params == {"limit": 17, "account_id": "account with spaces"}
    assert requests[0][1] == {"limit": ["17"], "account_id": ["account with spaces"]}
