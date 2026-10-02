"""
Each test pins down a behaviour that was found or confirmed against the live Wise API.
The comments say what went wrong (or could go wrong) without the test.
"""
import os
import sqlite3
import uuid


# ---------- setup ----------

def test_init_picks_personal_profile_case_insensitively(env):
    # Wise returns "PERSONAL" (uppercase). An earlier version filtered on "personal",
    # silently fell back to the first profile and could pick a BUSINESS one.
    result = env.run("init", "--wise-key", "FAKE")
    assert result.exit_code == 0
    assert env.n.load_config()["wise_profile_id"] == 111


def test_config_file_is_owner_only(env):
    # The API token lives in this file.
    env.run("init", "--wise-key", "FAKE")
    assert oct(os.stat(env.n.CONFIG_PATH).st_mode)[-3:] == "600"


# ---------- recipients ----------

def test_opay_recipient_uses_wise_nigeria_type_and_bank_code(ready):
    # Verified live: NGN recipients are type "nigeria"; OPay is bankCode 100004.
    # There is no phone/wallet recipient type for NGN on Wise.
    result = ready.run("add-recipient", input="opay\n1\nTest User\n8000000000\ny\n")
    assert result.exit_code == 0, result.output

    payload = ready.wise.find("POST", "/accounts")[0]["json"]
    assert payload["type"] == "nigeria"
    assert payload["details"] == {"legalType": "PRIVATE", "bankCode": "100004",
                                  "accountNumber": "8000000000"}
    assert ready.n.load_config()["recipient"]["id"] == 40000000


def test_recipient_rejects_single_name_before_calling_wise(ready):
    result = ready.run("add-recipient", input="opay\n1\nTest\n")
    assert result.exit_code != 0
    assert "first and last name" in result.output
    assert not ready.wise.find("POST", "/accounts")


def test_recipient_rejects_bad_account_number(ready):
    result = ready.run("add-recipient", input="opay\n1\nTest User\n12345\n")
    assert result.exit_code != 0
    assert not ready.wise.find("POST", "/accounts")


# ---------- transfers ----------

def test_transfer_sends_uuid_customer_transaction_id(ready):
    # Wise requires a UUID here for idempotency. An earlier version sent "naira-cli-<timestamp>".
    result = ready.run("transfer", "2", input="y\n")
    assert result.exit_code == 0, result.output

    body = ready.wise.find("POST", "/transfers")[0]["json"]
    uuid.UUID(body["customerTransactionId"])  # raises if not a valid UUID
    assert body["targetAccount"] == 40000000


def test_transfer_refuses_when_balance_not_offered(ready):
    # Seen live: without enough balance, Wise offers GOOGLE_PAY instead of BALANCE.
    # An earlier version silently used it. Now nothing should be created.
    ready.wise.balance_available = False
    result = ready.run("transfer", "2", input="y\n")

    assert result.exit_code != 0
    assert "BALANCE" in result.output
    assert "Not enough money" in result.output
    assert not ready.wise.find("POST", "/transfers")


def test_transfer_cancelled_at_prompt_creates_nothing(ready):
    result = ready.run("transfer", "2", input="n\n")
    assert result.exit_code == 0
    assert not ready.wise.find("POST", "/transfers")


def test_funding_403_is_handled_and_transfer_still_logged(ready):
    # Confirmed live: UK personal tokens get HTTP 403 when funding (PSD2 / SCA).
    result = ready.run("transfer", "2", "--try-fund", input="y\n")
    assert result.exit_code == 0, result.output
    assert "403" in result.output
    assert "Wise app" in result.output

    fund = ready.wise.find("POST", "/profiles/111/transfers/16521632/payments")[0]
    assert fund["json"] == {"type": "BALANCE"}

    with sqlite3.connect(ready.n.DB_PATH) as conn:
        rows = conn.execute("SELECT wise_transfer_id, status FROM wise_transfers").fetchall()
    assert rows == [("16521632", "incoming_payment_waiting")]


def test_existing_legacy_transactions_table_is_left_alone(env):
    # Found live: an older version left a 'transactions' table with different columns,
    # which crashed logging after the transfer had already been created in Wise.
    env.n.DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(env.n.DB_PATH) as conn:
        conn.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, ts TEXT, amount REAL)")
        conn.execute("INSERT INTO transactions VALUES (1, 'old', 5)")

    env.run("init", "--wise-key", "FAKE")
    cfg = env.n.load_config()
    cfg["recipient"] = {"id": 1, "name": "A B", "bank": "Opay", "bank_code": "100004",
                        "account": "8000000000"}
    env.n.save_config(cfg)

    assert env.run("transfer", "2", input="y\n").exit_code == 0
    with sqlite3.connect(env.n.DB_PATH) as conn:
        assert conn.execute("SELECT * FROM transactions").fetchall() == [(1, "old", 5.0)]


def test_pending_and_cancel(ready):
    assert "16521632" in ready.run("pending").output
    result = ready.run("cancel", "16521632", input="y\n")
    assert result.exit_code == 0
    assert ready.wise.find("PUT", "/transfers/16521632/cancel")


# ---------- rate alerts ----------

def _watch_at(env, rate):
    env.wise.rate = rate
    result = env.run("watch")
    assert result.exit_code == 0, result.output


def test_alert_fires_only_when_rate_crosses(ready):
    ready.run("telegram-setup", input="123:ABC\n\n")
    ready.telegram.clear()
    ready.run("alert-add", "above", "1800")

    for rate in [1790, 1805, 1810, 1795, 1802]:
        _watch_at(ready, rate)

    # Fires at 1805 (crossed up), silent at 1810 (still above),
    # resets at 1795, fires again at 1802.
    assert len(ready.telegram) == 2
    assert "1,805.00" in ready.telegram[0]
    assert "1,802.00" in ready.telegram[1]


def test_below_alert(ready):
    ready.run("telegram-setup", input="123:ABC\n\n")
    ready.telegram.clear()
    ready.run("alert-add", "below", "1750")

    for rate in [1765, 1745, 1740]:
        _watch_at(ready, rate)

    assert len(ready.telegram) == 1
    assert "below" in ready.telegram[0]


def test_watch_logs_every_reading(ready):
    for rate in [1765.46, 1764.69]:
        _watch_at(ready, rate)
    with sqlite3.connect(ready.n.DB_PATH) as conn:
        rates = [r for (r,) in conn.execute(
            "SELECT rate FROM rate_history WHERE origin = 'live' ORDER BY rate_time")]
    assert sorted(rates) == [1764.69, 1765.46]


def test_backfill_chunks_by_week_and_skips_duplicates(ready):
    # Wise's max range per /rates call isn't documented, so backfill asks in 7-day chunks.
    first = ready.run("backfill", "--days", "15")
    assert first.exit_code == 0, first.output

    chunks = [c["params"] for c in ready.wise.find("GET", "/rates") if c["params"].get("group")]
    assert len(chunks) == 3
    assert all(c["group"] == "hour" for c in chunks)

    again = ready.run("backfill", "--days", "15")
    assert "Added 0" in again.output


def test_export_rates_csv(ready, tmp_path):
    _watch_at(ready, 1765.46)
    out = tmp_path / "rates.csv"
    result = ready.run("export-rates", str(out))
    assert result.exit_code == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "rate_time,source,target,rate,origin"
    assert len(lines) == 2
