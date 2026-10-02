#!/usr/bin/env python3
"""
Naira CLI: send GBP (or USD) from your Wise balance to a Nigerian bank or OPay.

Verified against Wise API 2026Q4 docs and a live read-only check (2 Oct 2026):
  - NGN recipient type is "nigeria" with bankCode + accountNumber + legalType
  - OPay is a bank in Wise's list (bankCode 100004)
  - UK personal tokens can create quotes, recipients and transfers, but cannot
    fund them (confirmed live: HTTP 403). You approve payment in the Wise app.
  - Rate alerts use GET /rates (supports hourly history) and Telegram's Bot API.
"""

import csv
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import requests

APP_DIR = Path.home() / ".naira_cli"
DB_PATH = APP_DIR / "accounts.db"
CONFIG_PATH = APP_DIR / "config.json"
APP_DIR.mkdir(exist_ok=True)

WISE_API = "https://api.wise.com/2026Q4"
TIMEOUT = 15


# ---------- storage ----------

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)  # token lives here, keep it owner-only
    except OSError:
        pass


def db():
    # Own table name so we never clash with the 'transactions' table
    # left by the earlier non-Wise version of this CLI.
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wise_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            type TEXT,
            from_account TEXT,
            to_account TEXT,
            amount REAL,
            currency TEXT,
            rate REAL,
            wise_transfer_id TEXT,
            status TEXT,
            notes TEXT
        )
    """)
    return conn


def log_transfer(source_ccy, amount, recipient_label, rate, transfer_id, status, notes):
    with db() as conn:
        conn.execute(
            """INSERT INTO wise_transfers
               (timestamp, type, from_account, to_account, amount, currency, rate,
                wise_transfer_id, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(timespec="seconds"), "TRANSFER_WISE",
             f"Wise ({source_ccy})", recipient_label, amount, source_ccy, rate,
             str(transfer_id), status, notes),
        )


def update_status(transfer_id, status):
    with db() as conn:
        conn.execute("UPDATE wise_transfers SET status = ? WHERE wise_transfer_id = ?",
                     (status, str(transfer_id)))


# ---------- Wise API ----------

class WiseError(Exception):
    pass


def headers(token=None):
    token = token or load_config().get("wise_token") or os.environ.get("WISE_TOKEN")
    if not token:
        raise click.ClickException("No Wise token. Run: python3 naira_cli_wise.py init")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept-Minor-Version": "1",
    }


def call(method, path, token=None, **kwargs):
    """Make a Wise call. On failure raise WiseError carrying the status and Wise's message."""
    r = requests.request(method, f"{WISE_API}{path}", headers=headers(token),
                         timeout=TIMEOUT, **kwargs)
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        err = WiseError(f"HTTP {r.status_code} on {method} {path}: {body}")
        err.status = r.status_code
        raise err
    return r.json() if r.content else {}


def profile_id():
    pid = load_config().get("wise_profile_id")
    if not pid:
        raise click.ClickException("No profile saved. Run: python3 naira_cli_wise.py init")
    return pid


def create_quote(amount, source_ccy, target_account=None):
    body = {"sourceCurrency": source_ccy, "targetCurrency": "NGN", "sourceAmount": amount}
    if target_account:
        body["targetAccount"] = int(target_account)
    quote = call("POST", f"/profiles/{profile_id()}/quotes", json=body)

    # The NGN amount lives inside paymentOptions. Only accept paying from balance:
    # never silently fall back to card / Google Pay.
    all_opts = quote.get("paymentOptions", [])
    option = next((o for o in all_opts
                   if o.get("payIn") == "BALANCE" and not o.get("disabled")), None)
    if option is None:
        lines = []
        for o in all_opts:
            state = "enabled"
            if o.get("disabled"):
                reason = (o.get("disabledReason") or {}).get("message") or "no reason given"
                state = f"DISABLED: {reason}"
            lines.append(f"  {o.get('payIn')}: {state}")
        raise click.ClickException(
            "Wise is not offering BALANCE for this quote, so nothing was created.\n"
            "Usually this means not enough " + source_ccy + " in your Wise balance.\n"
            "Payment options Wise returned:\n" + "\n".join(lines or ["  (none)"]))

    fee = (option.get("price") or {}).get("total", {}).get("value", {}).get("amount")
    if fee is None:
        fee = (option.get("fee") or {}).get("total")
    return {
        "id": quote["id"],
        "rate": quote["rate"],
        "target": option.get("targetAmount"),
        "fee": fee,
        "pay_in": option.get("payIn"),
        "expires": quote.get("expirationTime"),
    }


def ngn_bank_list():
    """Wise's own NGN bank list (name + code), from account requirements."""
    q = call("POST", f"/profiles/{profile_id()}/quotes",
             json={"sourceCurrency": "GBP", "targetCurrency": "NGN", "sourceAmount": 10})
    reqs = call("GET", f"/quotes/{q['id']}/account-requirements")
    for t in reqs:
        if t.get("type") == "nigeria":
            for g in t.get("fields", []):
                for f in g.get("group", []):
                    if f.get("key") == "bankCode":
                        return [(v["key"], v["name"]) for v in f.get("valuesAllowed") or []]
    raise click.ClickException("Wise did not return an NGN 'nigeria' bank list.")


# ---------- CLI ----------

@click.group()
def cli():
    """Naira CLI: Wise balance (GBP/USD) to Nigerian bank or OPay."""


@cli.command()
@click.option("--wise-key", prompt="Wise API token", hide_input=True,
              help="Your Wise personal API token")
def init(wise_key):
    """Save your token and personal profile ID."""
    try:
        profiles = call("GET", "/profiles", token=wise_key)
    except WiseError as e:
        raise click.ClickException(f"Token check failed. {e}")

    personal = [p for p in profiles if str(p.get("type", "")).upper() == "PERSONAL"]
    if not personal:
        raise click.ClickException(f"No PERSONAL profile found. Got: {[p.get('type') for p in profiles]}")

    config = load_config()
    config["wise_token"] = wise_key
    config["wise_profile_id"] = personal[0]["id"]
    save_config(config)
    click.echo(f"✓ Token works. Personal profile ID: {personal[0]['id']}")
    click.echo("Next: python3 naira_cli_wise.py add-recipient")


@cli.command()
@click.argument("search", required=False)
def banks(search):
    """List Wise's NGN banks (optionally filter, e.g. 'banks opay')."""
    rows = ngn_bank_list()
    if search:
        rows = [r for r in rows if search.lower() in r[1].lower()]
    for code, name in rows:
        click.echo(f"  {code:>8}  {name}")
    click.echo(f"\n{len(rows)} bank(s)")


@cli.command("add-recipient")
def add_recipient():
    """Create a Nigerian bank or OPay recipient in Wise (done once, then reused)."""
    all_banks = ngn_bank_list()

    term = click.prompt("Bank name to search (e.g. opay, gtbank, access)").strip().lower()
    matches = [b for b in all_banks if term in b[1].lower()]
    if not matches:
        raise click.ClickException("No match. Run 'banks' to see the full list.")
    for i, (code, name) in enumerate(matches, 1):
        click.echo(f"  {i}. {name} ({code})")
    pick = click.prompt("Pick a number", type=click.IntRange(1, len(matches)), default=1)
    bank_code, bank_name = matches[pick - 1]

    name = click.prompt("Account holder full name (as on the account)").strip()
    if len(name.split()) < 2:
        raise click.ClickException("Wise needs first and last name.")

    hint = " (for OPay, usually your phone number without the leading 0)" if "opay" in bank_name.lower() else ""
    acct = click.prompt(f"10-digit account number{hint}").strip().replace(" ", "")
    if not (acct.isdigit() and len(acct) == 10):
        raise click.ClickException("Nigerian account numbers are 10 digits.")

    own = click.confirm("Is this your own account?", default=True)

    payload = {
        "profile": profile_id(),
        "currency": "NGN",
        "type": "nigeria",
        "accountHolderName": name,
        "ownedByCustomer": own,
        "details": {"legalType": "PRIVATE", "bankCode": bank_code, "accountNumber": acct},
    }
    try:
        rec = call("POST", "/accounts", json=payload)
    except WiseError as e:
        raise click.ClickException(f"Wise rejected the recipient. {e}")

    config = load_config()
    config["recipient"] = {
        "id": rec["id"], "name": name, "bank": bank_name,
        "bank_code": bank_code, "account": acct,
    }
    save_config(config)
    click.echo(f"\n✓ Recipient saved in Wise (ID {rec['id']}): {name}, {bank_name} {acct}")
    click.echo("Next: python3 naira_cli_wise.py transfer 10")


@cli.command()
@click.option("--from", "source", default="GBP", show_default=True,
              type=click.Choice(["GBP", "USD"]), help="Source currency")
def rate(source):
    """Show the current Wise rate to NGN."""
    q = call("POST", f"/profiles/{profile_id()}/quotes",
             json={"sourceCurrency": source, "targetCurrency": "NGN", "sourceAmount": 1})
    click.echo(f"1 {source} = ₦{q['rate']:,.2f}  (quote rate, before fees)")


@cli.command()
@click.argument("amount", type=click.FloatRange(min=1))
@click.option("--from", "source", default="GBP", show_default=True,
              type=click.Choice(["GBP", "USD"]), help="Source currency")
@click.option("--reference", default="Naira CLI", show_default=True, help="Payment reference")
@click.option("--try-fund", is_flag=True,
              help="Also try to fund via API. Wise docs say this fails for UK personal accounts.")
def transfer(amount, source, reference, try_fund):
    """Create a transfer to your saved recipient. You approve payment in the Wise app."""
    rec = load_config().get("recipient")
    if not rec:
        raise click.ClickException("No recipient. Run: python3 naira_cli_wise.py add-recipient")
    label = f"{rec['bank']} {rec['account']} ({rec['name']})"

    q = create_quote(amount, source, target_account=rec["id"])
    click.echo(f"\nSend:     {amount:,.2f} {source}")
    click.echo(f"Rate:     1 {source} = ₦{q['rate']:,.2f}")
    if q["fee"] is not None:
        click.echo(f"Fee:      {q['fee']} {source}")
    click.echo(f"Receives: ₦{q['target']:,.2f}  (paying from {q['pay_in']})")
    click.echo(f"To:       {label}")
    if q["fee"] is not None and q["fee"] / amount > 0.05:
        click.echo(f"⚠ Fee is {q['fee'] / amount:.0%} of the amount. Larger amounts are proportionally cheaper.")
    if q["expires"]:
        click.echo(f"Quote expires: {q['expires']} (UTC)")

    if not click.confirm("\nCreate this transfer?"):
        click.echo("Cancelled. Nothing created.")
        return

    try:
        t = call("POST", "/transfers", json={
            "targetAccount": int(rec["id"]),
            "quoteUuid": q["id"],
            "customerTransactionId": str(uuid.uuid4()),
            "details": {"reference": reference},
        })
    except WiseError as e:
        raise click.ClickException(f"Wise rejected the transfer. {e}")

    tid = t["id"]
    status = t.get("status", "unknown")
    click.echo(f"\n✓ Transfer created. ID {tid}, status: {status}")
    try:
        log_transfer(source, amount, label, q["rate"], tid, status,
                     f"{amount:,.2f} {source} -> ₦{q['target']:,.2f}")
    except sqlite3.Error as e:
        # The transfer already exists in Wise, so a local logging problem must not hide that.
        click.echo(f"⚠ Transfer exists in Wise but local history save failed: {e}")

    if try_fund:
        try:
            res = call("POST", f"/profiles/{profile_id()}/transfers/{tid}/payments",
                       json={"type": "BALANCE"})
            click.echo(f"Funding result: {res.get('status')} {res.get('errorCode') or ''}")
            if res.get("status") == "COMPLETED":
                update_status(tid, "funded")
                return
        except WiseError as e:
            click.echo(f"Funding via API failed, as Wise docs predict for UK accounts:\n  {e}")

    click.echo("\nNow open the Wise app, find this transfer under pending payments, and pay it")
    click.echo("from your balance. Wise cancels unpaid transfers after about 14 days.")
    click.echo(f"Check progress: python3 naira_cli_wise.py status {tid}")


@cli.command()
@click.argument("transfer_id")
def status(transfer_id):
    """Check a transfer's status in Wise."""
    t = call("GET", f"/transfers/{transfer_id}")
    update_status(transfer_id, t.get("status"))
    click.echo(f"Transfer {transfer_id}: {t.get('status')}")
    click.echo(f"  {t.get('sourceValue')} {t.get('sourceCurrency')} -> "
               f"{t.get('targetValue')} {t.get('targetCurrency')}")


@cli.command()
def pending():
    """List transfers in Wise that are waiting for payment."""
    rows = call("GET", "/transfers", params={
        "profile": profile_id(), "status": "incoming_payment_waiting", "limit": 20})
    if not rows:
        click.echo("No transfers waiting for payment.")
        return
    for t in rows:
        click.echo(f"ID {t.get('id')}  {t.get('created')}  "
                   f"{t.get('sourceValue')} {t.get('sourceCurrency')} -> "
                   f"{t.get('targetValue')} {t.get('targetCurrency')}")
    click.echo("\nPay these in the Wise app, or cancel with: python3 naira_cli_wise.py cancel ID")


@cli.command()
@click.argument("transfer_id")
def cancel(transfer_id):
    """Cancel an unpaid transfer (only works while waiting for payment)."""
    if not click.confirm(f"Cancel transfer {transfer_id}?"):
        return
    try:
        t = call("PUT", f"/transfers/{transfer_id}/cancel")
    except WiseError as e:
        if getattr(e, "status", None) == 409:
            raise click.ClickException("Wise says it can't be cancelled now (already paid or being processed).")
        raise click.ClickException(str(e))
    update_status(transfer_id, t.get("status", "cancelled"))
    click.echo(f"✓ Transfer {transfer_id}: {t.get('status')}")


@cli.command()
@click.option("--limit", default=10, show_default=True, type=int)
def history(limit):
    """Show transfers this CLI has created."""
    with db() as conn:
        rows = conn.execute(
            """SELECT timestamp, amount, currency, to_account, wise_transfer_id, status, notes
               FROM wise_transfers ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        click.echo("No transfers yet.")
        return
    for ts, amt, ccy, to, tid, st, notes in rows:
        click.echo(f"{ts}  {amt:,.2f} {ccy}  ->  {to}")
        click.echo(f"    ID {tid}  |  {st}  |  {notes}")


# ---------- rate history + alerts ----------

def rates_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_history (
            rate_time TEXT,
            source TEXT,
            target TEXT,
            rate REAL,
            origin TEXT,          -- 'live' from watch, 'backfill' from history import
            PRIMARY KEY (rate_time, source, target)
        )
    """)
    return conn


def fetch_rates(source, **params):
    """GET /rates. Returns list of {rate, source, target, time}."""
    rows = call("GET", "/rates", params={"source": source, "target": "NGN", **params})
    if isinstance(rows, dict):  # be tolerant if Wise ever wraps it
        rows = [rows]
    return rows


def save_rates(rows, origin):
    with rates_db() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO rate_history VALUES (?, ?, ?, ?, ?)",
            [(r["time"], r["source"], r["target"], float(r["rate"]), origin) for r in rows])
        return conn.total_changes - before


def notify(text):
    """Send to Telegram if set up, plus a Mac desktop notification. Always print."""
    click.echo(text)
    tg = load_config().get("telegram")
    if tg:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tg['token']}/sendMessage",
                              json={"chat_id": tg["chat_id"], "text": text}, timeout=TIMEOUT)
            body = r.json()
            if not body.get("ok"):
                click.echo(f"⚠ Telegram refused: {body.get('description')}", err=True)
        except (requests.RequestException, ValueError) as e:
            click.echo(f"⚠ Telegram failed: {e}", err=True)
    if sys.platform == "darwin":
        safe = text.replace('"', "'").replace("\n", " ")
        subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Naira CLI"'],
                       check=False, capture_output=True)


@cli.command("telegram-setup")
def telegram_setup():
    """Connect a Telegram bot for alerts."""
    click.echo("1. In Telegram, message @BotFather, send /newbot, follow the prompts.")
    click.echo("2. Copy the bot token it gives you.")
    token = click.prompt("Bot token", hide_input=True).strip()
    click.echo("3. Now open your new bot in Telegram and send it any message (e.g. 'hi').")
    click.pause("   Press any key once you've sent it...")

    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=TIMEOUT).json()
    if not r.get("ok"):
        raise click.ClickException(f"Telegram rejected the token: {r.get('description')}")
    chats = [u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u]
    if not chats:
        raise click.ClickException("No message found. Send your bot a message, then rerun this.")

    config = load_config()
    config["telegram"] = {"token": token, "chat_id": chats[-1]}
    save_config(config)
    notify("✅ Naira CLI connected. Rate alerts will arrive here.")


@cli.command("alert-add")
@click.argument("direction", type=click.Choice(["above", "below"]))
@click.argument("threshold", type=click.FloatRange(min=0, min_open=True))
@click.option("--from", "source", default="GBP", show_default=True, type=click.Choice(["GBP", "USD"]))
def alert_add(direction, threshold, source):
    """Alert when the rate goes above/below a number, e.g. 'alert-add above 1800'."""
    config = load_config()
    alerts = config.setdefault("alerts", [])
    new_id = max([a["id"] for a in alerts], default=0) + 1
    alerts.append({"id": new_id, "direction": direction, "threshold": threshold,
                   "source": source, "last_side": None})
    save_config(config)
    click.echo(f"✓ Alert {new_id}: 1 {source} {direction} ₦{threshold:,.2f}")


@cli.command()
def alerts():
    """List your alerts."""
    rows = load_config().get("alerts", [])
    if not rows:
        click.echo("No alerts. Add one: python3 naira_cli_wise.py alert-add above 1800")
    for a in rows:
        click.echo(f"  {a['id']}. 1 {a['source']} {a['direction']} ₦{a['threshold']:,.2f}"
                   f"   (currently: {a['last_side'] or 'not checked yet'})")


@cli.command("alert-remove")
@click.argument("alert_id", type=int)
def alert_remove(alert_id):
    """Delete an alert by its number."""
    config = load_config()
    before = len(config.get("alerts", []))
    config["alerts"] = [a for a in config.get("alerts", []) if a["id"] != alert_id]
    save_config(config)
    click.echo("✓ Removed." if len(config["alerts"]) < before else "No alert with that number.")


@cli.command()
def watch():
    """Check the rate once, log it, fire any alerts. Run this hourly via the schedule."""
    config = load_config()
    sources = sorted({a["source"] for a in config.get("alerts", [])} | {"GBP"})
    current = {}
    for src in sources:
        rows = fetch_rates(src)
        if not rows:
            click.echo(f"⚠ Wise returned no rate for {src}", err=True)
            continue
        save_rates(rows, "live")
        current[src] = float(rows[-1]["rate"])
        click.echo(f"{datetime.now():%Y-%m-%d %H:%M}  1 {src} = ₦{current[src]:,.2f}")

    # Fire only when the rate CROSSES the line, not every hour it stays past it.
    for a in config.get("alerts", []):
        rate = current.get(a["source"])
        if rate is None:
            continue
        side = "above" if rate >= a["threshold"] else "below"
        if side == a["direction"] and a["last_side"] != side:
            notify(f"💱 1 {a['source']} = ₦{rate:,.2f}, now {side} your ₦{a['threshold']:,.2f} alert")
        a["last_side"] = side
    save_config(config)


@cli.command()
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 365))
@click.option("--from", "source", default="GBP", show_default=True, type=click.Choice(["GBP", "USD"]))
def backfill(days, source):
    """Import past hourly rates from Wise so you have history from day one."""
    end = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    added, chunk = 0, timedelta(days=7)  # Wise's max range per call isn't documented, so go in weeks
    cursor = start
    while cursor < end:
        nxt = min(cursor + chunk, end)
        rows = fetch_rates(source, **{"from": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "to": nxt.strftime("%Y-%m-%dT%H:%M:%S"), "group": "hour"})
        added += save_rates(rows, "backfill")
        cursor = nxt
    click.echo(f"✓ Added {added} hourly {source}→NGN rates covering the last {days} days.")


@cli.command("rate-history")
@click.option("--hours", default=24, show_default=True, type=int)
@click.option("--from", "source", default="GBP", show_default=True, type=click.Choice(["GBP", "USD"]))
def rate_history(hours, source):
    """Show recent logged rates with high, low and change."""
    with rates_db() as conn:
        rows = conn.execute(
            """SELECT rate_time, rate FROM rate_history WHERE source = ?
               ORDER BY rate_time DESC LIMIT ?""", (source, hours)).fetchall()
    if not rows:
        click.echo("No history yet. Run 'backfill' or 'watch'.")
        return
    rates = [r for _, r in rows]
    click.echo(f"Last {len(rows)} readings, 1 {source} → NGN")
    click.echo(f"  latest ₦{rates[0]:,.2f}   high ₦{max(rates):,.2f}   low ₦{min(rates):,.2f}"
               f"   change {rates[0] - rates[-1]:+,.2f}")
    for t, r in rows[:10]:
        click.echo(f"  {t}  ₦{r:,.2f}")


@cli.command("export-rates")
@click.argument("out", default="rate_history.csv")
def export_rates(out):
    """Export all logged rates to CSV (for Excel, Power BI, pandas)."""
    with rates_db() as conn:
        rows = conn.execute("SELECT * FROM rate_history ORDER BY rate_time").fetchall()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rate_time", "source", "target", "rate", "origin"])
        w.writerows(rows)
    click.echo(f"✓ {len(rows)} rows written to {out}")


LAUNCH_LABEL = "com.nairacli.watch"


@cli.command("schedule-install")
def schedule_install():
    """Mac only: run 'watch' every hour in the background (also catches up after sleep)."""
    if sys.platform != "darwin":
        raise click.ClickException("This sets up a Mac launchd job. On Linux, use cron instead.")
    script = Path(__file__).resolve()
    if "Downloads" in script.parts or "Desktop" in script.parts or "Documents" in script.parts:
        click.echo("⚠ macOS blocks background jobs from reading Downloads/Desktop/Documents.")
        click.echo("  Move the project first, e.g.:  mv ~/Downloads/Naira_GBP_CLI ~/Naira_GBP_CLI")
        click.echo("  then run this command again from the new folder.")
        return
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    log = APP_DIR / "watch.log"
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>watch</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise click.ClickException(f"launchctl failed: {res.stderr.strip()}")
    click.echo(f"✓ Hourly check installed. It ran once just now. Log: {log}")
    click.echo("  Stop it any time with: python3 naira_cli_wise.py schedule-remove")


@cli.command("schedule-remove")
def schedule_remove():
    """Mac only: stop the hourly background check."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    plist.unlink(missing_ok=True)
    click.echo("✓ Hourly check removed.")


if __name__ == "__main__":
    try:
        cli()
    except WiseError as e:
        raise SystemExit(f"❌ {e}")
