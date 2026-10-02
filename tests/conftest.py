"""
Test fixtures. No test ever talks to the real Wise or Telegram APIs.

Each test gets a fresh HOME directory (so config, token and database are isolated)
and a fake Wise API whose responses are shaped like the examples in Wise's docs.
"""
import importlib
import sys
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


class FakeWise:
    """Routes requests by path and records every call for assertions."""

    def __init__(self, base):
        self.base = base
        self.calls = []
        self.rate = 1765.46
        self.balance_available = True
        self.fund_status = 403

    def request(self, method, url, headers=None, timeout=None, json=None, params=None, **kw):
        path = url.replace(self.base, "")
        self.calls.append({"method": method, "path": path, "json": json, "params": params})

        if path == "/profiles":
            return FakeResponse(200, [{"type": "BUSINESS", "id": 999},
                                      {"type": "PERSONAL", "id": 111}])
        if path.endswith("/quotes"):
            return FakeResponse(200, {
                "id": str(uuid.uuid4()),
                "rate": self.rate,
                "expirationTime": "2026-10-02T12:00:00Z",
                "paymentOptions": [
                    {"payIn": "GOOGLE_PAY", "disabled": False, "targetAmount": 2452.65},
                    {"payIn": "BALANCE", "disabled": not self.balance_available,
                     "targetAmount": 2533.47,
                     "price": {"total": {"value": {"amount": 0.57}}},
                     "disabledReason": {"message": "Not enough money in your balance"}},
                ],
            })
        if path.endswith("/account-requirements"):
            return FakeResponse(200, [{"type": "nigeria", "fields": [{"group": [
                {"key": "bankCode", "valuesAllowed": [
                    {"key": "100004", "name": "Opay"},
                    {"key": "058", "name": "Guaranty Trust Bank"}]}]}]}])
        if path == "/accounts":
            return FakeResponse(200, {"id": 40000000})
        if path == "/transfers" and method == "POST":
            return FakeResponse(200, {"id": 16521632, "status": "incoming_payment_waiting"})
        if path == "/transfers" and method == "GET":
            return FakeResponse(200, [{"id": 16521632, "created": "2026-10-02 11:00:00",
                                       "sourceValue": 2, "sourceCurrency": "GBP",
                                       "targetValue": 2533.47, "targetCurrency": "NGN"}])
        if path.endswith("/cancel"):
            return FakeResponse(200, {"id": 16521632, "status": "cancelled"})
        if path.endswith("/payments"):
            if self.fund_status == 403:
                return FakeResponse(403, {"error": "forbidden"})
            return FakeResponse(201, {"type": "BALANCE", "status": "COMPLETED"})
        if path == "/rates":
            if params and "group" in params:
                day = params["from"][:10]
                return FakeResponse(200, [{"rate": 1800 + h, "source": "GBP", "target": "NGN",
                                           "time": f"{day}T{h:02d}:00:00+0000"} for h in range(24)])
            return FakeResponse(200, [{"rate": self.rate, "source": params["source"],
                                       "target": "NGN", "time": f"live-{len(self.calls)}"}])
        if path.startswith("/transfers/"):
            return FakeResponse(200, {"status": "outgoing_payment_sent", "sourceValue": 2,
                                      "sourceCurrency": "GBP", "targetValue": 2533.47,
                                      "targetCurrency": "NGN"})
        return FakeResponse(404, {"error": "not mocked"})

    def find(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh module + isolated HOME + fake Wise + captured Telegram messages."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WISE_TOKEN", raising=False)
    if "naira_cli_wise" in sys.modules:
        del sys.modules["naira_cli_wise"]
    cli_module = importlib.import_module("naira_cli_wise")

    wise = FakeWise(cli_module.WISE_API)
    telegram = []

    def fake_post(url, json=None, timeout=None, **kw):
        telegram.append(json["text"])
        return FakeResponse(200, {"ok": True})

    def fake_get(url, timeout=None, **kw):
        return FakeResponse(200, {"ok": True, "result": [{"message": {"chat": {"id": 4242}}}]})

    monkeypatch.setattr("requests.request", wise.request)
    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)

    runner = CliRunner()

    def run(*args, input=None):
        return runner.invoke(cli_module.cli, list(args), input=input)

    class Env:
        pass

    e = Env()
    e.n, e.wise, e.telegram, e.run = cli_module, wise, telegram, run
    return e


@pytest.fixture
def ready(env):
    """An environment that has already run init and has a saved OPay recipient."""
    assert env.run("init", "--wise-key", "FAKE").exit_code == 0
    cfg = env.n.load_config()
    cfg["recipient"] = {"id": 40000000, "name": "Test User", "bank": "Opay",
                        "bank_code": "100004", "account": "8000000000"}
    env.n.save_config(cfg)
    return env
