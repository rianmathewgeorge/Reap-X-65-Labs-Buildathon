from __future__ import annotations

import http.client
import secrets
import threading

import pytest

from scripts.callback_relay import RelayConfig, relay_handler


HOST = "relay.example.test"
ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def relay():
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), relay_handler(RelayConfig(HOST, ORIGIN)))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def request(port: int, path: str, method: str = "GET", host: str = HOST) -> tuple[int, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port)
    connection.request(method, path, headers={"Host": host})
    response = connection.getresponse()
    headers = {key.lower(): value for key, value in response.getheaders()}
    response.read()
    connection.close()
    return response.status, headers


def test_payment_callback_relays_only_one_state_and_discards_other_values(relay):
    state = secrets.token_urlsafe(32)
    status, headers = request(relay, f"/payment/return?status=COMPLETED&state={state}&provider=Reap")
    assert status == 303
    assert headers["location"] == f"{ORIGIN}/payment/return?state={state}"
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", [
    "/payment/return",
    "/payment/return?state=short",
    "/payment/return?state=bad&state=also-bad",
    "/payment/return?state=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&state=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
])
def test_payment_callback_rejects_missing_duplicate_or_malformed_state(relay, path):
    status, _ = request(relay, path)
    assert status == 400


def test_enrollment_callback_discards_provider_parameters(relay):
    status, headers = request(relay, "/enrollment/return?status=REQUIRES_ACTION&state=provider-value")
    assert status == 303
    assert headers["location"] == f"{ORIGIN}/?enrollment_return=1"


def test_relay_rejects_wrong_host_and_non_get_methods(relay):
    state = secrets.token_urlsafe(32)
    wrong_host, _ = request(relay, f"/payment/return?state={state}", host="other.example.test")
    wrong_method, headers = request(relay, f"/payment/return?state={state}", method="OPTIONS")
    assert wrong_host == 403
    assert wrong_method == 405
    assert headers["allow"] == "GET"


@pytest.mark.parametrize("public_host, local_origin", [
    ("https://relay.example.test", ORIGIN),
    ("relay.example.test/path", ORIGIN),
    (HOST, "https://app.example.test"),
    (HOST, f"{ORIGIN}/path"),
])
def test_relay_configuration_rejects_non_exact_hosts_and_non_loopback_origins(public_host, local_origin):
    with pytest.raises(ValueError):
        RelayConfig(public_host, local_origin)
