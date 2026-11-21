"""Reference data for the MT5 report import (#96): a report file in, what `parse_report` makes of it out.

Each case in ``tests/golden/reports/<case>/`` has ``report.html``, the file as the trader would
upload it (raw bytes: UTF-16 as the terminal saves it, or UTF-8), and ``expected.json``: either
``{"ok": {"account": ..., "trades": ..., "cash": ...}}`` or ``{"error": "<the ReportError message>"}``
(a file the parser chokes on otherwise gives ``imports.UNREADABLE``). ``wed-server/tests/it/report_golden.rs`` runs the Rust
``parse_report`` on the same files and wants the same output, key order and float bits included.

Rewrite the files after a deliberate change to the import, and review the diff:

    UPDATE_GOLDEN=1 uv run pytest tests/test_report_golden.py

The Rust side also carries tables copied from Python (the HTML entities, and from Python 3.11's
Unicode 14.0 which characters are printable and which are decimal digits); the last tests check
them against this Python.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from html.entities import html5
from pathlib import Path

import pytest

from wednesday.journal.imports import ReportError, parse_report

GOLDEN = Path(__file__).with_name("golden") / "reports"
RUST = Path(__file__).parents[1] / "wed-server" / "src" / "journal" / "report.rs"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


# ---- building reports the way the terminal writes them ----

HEAD = """<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">
<html>
  <head>
    <title>{title}</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-16">
    <style type="text/css" media="screen">
    <!--
    td {{ font: 8pt Tahoma,Arial; }}
    .hidden {{ display: none; }}
    //-->
    </style>
    <script type="text/javascript">
    var rows = "<tr><td>not a row</td></tr>";
    if (1 < 2 && rows) {{ }}
    </script>
  </head>
<body topmargin="1" marginheight="1">
<div align="center">
<table cellspacing="1" cellpadding="3" border="0">
"""
TAIL = """</table>
</div></body>
</html>
"""


def th_row(text: str, span: int = 14) -> str:
    return f'<tr align="center">\n<th colspan="{span}" style="height: 25px"><div style="font: 10pt Tahoma"><b>{text}</b></div></th>\n</tr>\n'


def info(label: str, value: str) -> str:
    return f'<tr align="left">\n<th colspan="3" nowrap align="right" style="width: 220px; height: 20px">{label}</th>\n<th colspan="11" nowrap align="left" style="width: 220px; height: 20px"><b>{value}</b></th>\n</tr>\n'


def row(cells: list, header: bool = False) -> str:
    """A table row; a cell is a text or ``(text, attributes)``."""
    out = ['<tr bgcolor="#E5F0FC" align="right">' if header else '<tr bgcolor="#FFFFFF" align="right">']
    for c in cells:
        text, attrs = (c, "") if isinstance(c, str) else c
        text = f"<b>{text}</b>" if header else text
        out.append(f"<td nowrap{(' ' + attrs) if attrs else ''}>{text}</td>")
    return "\n".join(out) + "\n</tr>\n"


HIDDEN8 = ("", 'class="hidden" colspan="8"')
POSITIONS_HEAD = row(["Time", "Position", "Symbol", "Type", HIDDEN8, "Volume", "Price", "S / L", "T / P", "Time", "Price",
                      "Commission", "Swap", "Profit"], header=True)
DEALS_HEAD = row(["Time", "Deal", "Symbol", "Type", "Direction", "Volume", "Price", "Order", "Commission", "Fee", "Swap",
                  "Profit", "Balance", "Comment"], header=True)
ORDERS_HEAD = row(["Open Time", "Order", "Symbol", "Type", "Volume", "Price", "S / L", "T / P", "Time", "State",
                   ("Comment", 'colspan="4"')], header=True)


def position(t, pos, sym, side, vol, price, sl, tp, t2, price2, comm, swap, profit, extra=""):
    return row([t, pos, sym, side, (extra, 'class="hidden" colspan="8"'), vol, price, sl, tp, t2, price2, comm, swap,
                profit])


def deal(t, num, sym, kind, direction, vol, price, order, comm, fee, swap, profit, balance, comment):
    return row([t, num, sym, kind, direction, vol, price, order, comm, fee, swap, profit, balance, comment])


def header(name="Sample Trader", account="51234567 (USD, Broker-Live, real, Hedge)", company="Broker Ltd"):
    return (th_row("<b>Trade History Report</b>") + info("Name:", name) + info("Account:", account)
            + info("Company:", company) + info("Date:", "2026.03.06 23:59"))


def full_report() -> str:
    """A week of gold trades as the terminal saves it."""
    positions = [
        position("2026.03.02 01:15:20", "70011", "XAUUSD.m", "buy", "0.10", "2 905.12", "2 895.00", "2 925.00",
                 "2026.03.02 03:40:01", "2 925.00", "-0.70", "0.00", "198.80"),
        position("2026.03.02 01:15:20", "70009", "XAUUSD.m", "sell", "0.05 / 0.05", "2 905.10", "", "",
                 "2026.03.02 02:00:00", "2 899.60", "-0.35", "-1.12", "27.50", extra="0.05"),
        position("2026.03.03 14:30:00", "70120", "XAUUSD.m", "sell", "1.00", "2 931.45", "2 941.45", "0.00",
                 "2026.03.04 09:05:33", "2 941.45", "-7.00", "-24.36", "-1 000.00"),
        position("2026.03.04 16:00:00", "70200", "EURUSD", "buy", "0.01", "1.08512", "1.08000", "1.09000",
                 "2026.03.04 16:20:00", "1.08700", "0.00", "0.00", "1.88"),
        position("2026.03.05 08:00:00", "70300", "XAUUSD.m", "buy limit", "0.10", "2 900.00", "", "",
                 "2026.03.05 08:01:00", "2 900.00", "0.00", "0.00", "0.00"),
    ]
    totals = row([("", 'colspan="10" style="height: 30px"'), "-8.05", "-25.48", "-771.82"])
    orders = [
        row(["2026.03.02 01:15:20", "80011", "XAUUSD.m", "buy", "0.10 / 0.10", "2 905.12", "2 895.00", "2 925.00",
             "2026.03.02 01:15:20", "filled", ("", 'colspan="4"')]),
    ]
    deals = [
        deal("2026.03.01 20:00:00", "1001", "", "balance", "", "", "", "", "0.00", "0.00", "0.00", "10 000.00",
             "10 000.00", "Deposit &amp; welcome"),
        deal("2026.03.02 01:15:20", "1002", "XAUUSD.m", "buy", "in", "0.10", "2 905.12", "80011", "-0.35", "0.00",
             "0.00", "0.00", "9 999.65", ""),
        deal("2026.03.03 00:00:00", "1003", "", "credit", "", "", "", "", "0.00", "0.00", "0.00", "500.00",
             "10 499.65", "Credit in"),
        deal("2026.03.03 00:00:00", "1004", "", "bonus", "", "", "", "", "0.00", "0.00", "0.00", "50.00", "10 549.65",
             "Bonus 5%"),
        deal("2026.03.04 12:00:00", "1005", "", "balance", "", "", "", "", "0.00", "0.00", "0.00", "-2 500.00",
             "8 049.65", "Withdrawal"),
        deal("2026.03.05 00:00:00", "1006", "", "charge", "", "", "", "", "-1.50", "-0.25", "0.00", "-10.00",
             "8 037.90", "Platform fee"),
        deal("2026.03.05 00:00:00", "", "", "correction", "", "", "", "", "0.00", "0.00", "0.00", "0.1", "8 038.00",
             ""),
        deal("2026.03.05 00:00:00", "", "", "Interest", "", "", "", "", "0.2", "0.00", "0.00", "0.1", "8 038.30",
             "interest"),
        deal("2026.03.06 00:00:00", "", "", "balance", "", "", "", "", "-0", "-0", "-0", "-0.00", "8 038.30", ""),
        deal("2026.03.06 01:00:00", "", "", "credit", "", "", "", "", "0.00", "0.00", "0.00", "12 345 678 901 234 567",
             "", "big"),
    ]
    deal_totals = row([("", 'colspan="8"'), "-2.10", "-0.25", "0.00", "7 040.00", "8 038.30", ""])
    open_positions = [
        row(["Time", "Position", "Symbol", "Type", "Volume", "Price", "S / L", "T / P", "Market Price", "Swap",
             "Profit", ("Comment", 'colspan="3"')], header=True),
        row(["2026.03.06 10:00:00", "70400", "XAUUSD.m", "buy", "0.10", "2 950.00", "", "", "2 951.00", "0.00", "10.00",
             ("", 'colspan="3"')]),
    ]
    results = (th_row("Results") + row([("Total Net Profit:", 'colspan="3"'), "-771.82",
                                         ("Gross Profit:", 'colspan="3"'), "228.18", ("Gross Loss:", 'colspan="3"'),
                                         "-1 000.00", ("", 'colspan="3"')]))
    return (HEAD.format(title="51234567: Sample Trader - Trade History Report") + header(name="Sample Trader")
            + th_row("Positions") + POSITIONS_HEAD + "".join(positions) + totals
            + th_row("Orders") + ORDERS_HEAD + "".join(orders)
            + th_row("Deals") + DEALS_HEAD + "".join(deals) + deal_totals
            + th_row("Open Positions") + "".join(open_positions)
            + th_row("Working Orders") + ORDERS_HEAD
            + results + TAIL)


def small_report(**header_args) -> str:
    return (HEAD.format(title="Report") + header(**header_args) + th_row("Positions") + POSITIONS_HEAD
            + position("2026.03.02 00:01:00", "7", "XAUUSD", "buy", "1", "2 000.00", "", "2 020.00",
                       "2026.03.02 00:10:00", "2 010.00", "-7.00", "0.00", "1 000.00")
            + th_row("Deals") + DEALS_HEAD
            + deal("2026.03.01 20:00:00", "1", "", "balance", "", "", "", "", "0.00", "0.00", "0.00", "10 000.00",
                   "10 000.00", "Deposit")
            + TAIL)


def utf16(text: str) -> bytes:
    return text.encode("utf-16")  # little endian with a BOM, as the terminal saves it


CASES = {
    # the terminal's own files
    "mt5-full-utf16": lambda: utf16(full_report()),
    "utf16-le-no-bom": lambda: small_report().encode("utf-16-le"),
    "utf16-be-bom": lambda: b"\xfe\xff" + small_report().encode("utf-16-be"),
    "utf8-bom": lambda: b"\xef\xbb\xbf" + small_report(name="Zoë Müller", company="Brökér&nbsp;Ltd").replace(
        "2 000.00", "2\xa0000.00").replace("1 000.00", "1&nbsp;000.00").encode("utf-8"),
    "utf8-bad-bytes": lambda: small_report().encode("utf-8").replace(b"Deposit", b"Dep\xc3osit \xed\xa0\x80 \xff\xfe!"),
    # the account line
    "account-variants": lambda: utf16(small_report(account="  77001234  ( usd , ICMarkets-Demo02 )", name="A, B",
                                                   company="Some &quot;Broker&quot; Inc.")),
    "account-no-brackets": lambda: utf16(small_report(account="51234567")),
    "account-no-currency": lambda: utf16(small_report(account="51234567 (, Server-X, demo)")),
    "account-not-a-number": lambda: utf16(small_report(account="demo (USD, Server)")),
    # HTML the parser has to get right
    "entities": lambda: utf16(entities_report()),
    "hidden-and-colspans": lambda: utf16(colspans_report()),
    "uppercase-tags": lambda: uppercase_report().encode("utf-8"),
    "comments-scripts": lambda: utf16(comments_report()),
    "unclosed-quote-stops-parsing": lambda: utf16(full_report().replace(">big<", "><span title='it>big</span><")),
    "totals-and-blank-rows": lambda: utf16(totals_report()),
    # what's in the tables
    "time-formats": lambda: utf16(times_report()),
    "only-cash": lambda: utf16(HEAD.format(title="x") + header() + th_row("Deals") + DEALS_HEAD + deal(
        "2026.03.01 20:00:00", "1", "", "balance", "", "", "", "", "0.00", "0.00", "0.00", "250.00", "250.00", "")
        + TAIL),
    "only-positions": lambda: utf16(HEAD.format(title="x") + header() + th_row("Positions") + POSITIONS_HEAD + position(
        "2026.03.02 00:01:00", "7", "XAUUSD", "sell", "0.5", "2000", "2010", "1990", "2026.03.02 00:10:00", "1990",
        "", "", "500") + TAIL),
    "missing-positions-section": lambda: utf16(HEAD.format(title="x") + header() + th_row("Orders") + ORDERS_HEAD
                                               + th_row("Deals") + DEALS_HEAD + TAIL),
    # errors the trader sees
    "error-not-html": lambda: b"Ticket;Time;Type\n1;2026.03.02;buy\n",
    "error-no-sections": lambda: utf16(HEAD.format(title="x") + header() + row(["a", "b", "c", "d"]) + TAIL),
    "error-no-trades-or-cash": lambda: utf16(HEAD.format(title="x") + header() + th_row("Positions") + POSITIONS_HEAD
                                             + position("2026.03.02 00:01:00", "7", "XAUUSD", "buy", "1", "2000", "",
                                                        "", "", "", "", "", "") + th_row("Deals") + DEALS_HEAD
                                             + deal("2026.03.02 00:01:00", "2", "XAUUSD", "buy", "in", "1", "2000", "7",
                                                    "", "", "", "", "", "") + TAIL),
    "error-bad-number": lambda: utf16(small_report().replace("1 000.00", "1'000.00")),
    "error-bad-number-repr": lambda: utf16(small_report().replace("-7.00", "-7.00\u200b\U0001f600&#x7f;\\é")),
    "error-bad-time": lambda: utf16(small_report().replace("2026.03.02 00:10:00", "2026/03/02 00:10")),
    "error-bad-time-feb-30": lambda: utf16(small_report().replace("2026.03.02 00:10:00", "2026.02.30 00:10:00")),
    # Python fails with something other than ReportError
    "unreadable-utf16-cut": lambda: utf16(small_report())[:-1],
    "unreadable-marked-section": lambda: utf16(small_report().replace("<body", "<![foo[ x ]]><body")),
    "unreadable-no-time-column": lambda: utf16(small_report().replace(
        "<b>Time</b>", "<b>Opened</b>", 1).replace("<b>Time</b>", "<b>Close Time</b>", 1)),
}


def entities_report() -> str:
    return (HEAD.format(title="x") + header(name="J&eacute;r&ocirc;me &amp O&#39;Neil &notit; &#128;&#x20AC;&#0;&#65;",
                                            company="&lt;Broker&gt; &copy2026 &#xD800;&#1114112;&#xFFFE;")
            + th_row("Positions") + POSITIONS_HEAD
            + row(["2026.03.02 00:01:00", "7", "XAU&#85;SD", "buy", ("x", 'class="hid&#100;en" colspan="8"'),
                   "1&#46;5", "2&#x30;00", "", "", "2026&#46;03.02 00:10:00", "2010", "", "", "1&nbsp;500"])
            + th_row("Deals") + DEALS_HEAD
            + deal("2026.03.01 20:00:00", "1", "", "bal&#97;nce", "", "", "", "", "", "", "", "100", "100",
                   "&quot;Deposit&quot; &amp;&amp; more &unknown; &am")
            + TAIL)


def colspans_report() -> str:
    """The columns only line up when every colspan and hidden cell counts as Python counts it."""
    head = row(["Time", "Position", "Symbol", "Type", ("", 'class="hidden" colspan="8"'),
                ("Volume", 'colspan=" 2 "'), "Price", ("S / L", 'colspan="0"'), ("T / P", 'colspan="-1"'),
                ("Time", 'colspan="abc"'), ("Price", 'colspan=""'), ("Commission", 'colspan="1_0"'),
                ("Swap", 'colspan="\u0663"'), ("Profit", "colspan")], header=True)
    body = row(["2026.03.02 00:01:00", "7", "XAUUSD", "buy", ("ignored", 'class="x hidden" colspan="8"'),
                ("1", 'colspan=" 2 "'), "2000", "1990", "2020", "2026.03.02 00:10:00", "2010",
                ("-1", 'colspan="1_0"'), ("-2", 'colspan="\u0663"'), "100"])
    shown = row(["2026.03.02 01:01:00", "8", "XAUUSD", "sell", ("0.5", 'class="Hidden" colspan="2"'), "2000", "", "",
                 "2026.03.02 01:10:00", "1990", ("", 'colspan="10"'), ("", 'colspan="3"'), "50"])
    return HEAD.format(title="x") + header() + th_row("Positions") + head + body + shown + TAIL


def uppercase_report() -> str:
    """Upper case tags and attributes, unquoted values, a cell left open (Python drops what it held)."""
    return """<HTML><BODY><TABLE BORDER=0>
<TR><TH COLSPAN=3>Account:</TH><TH COLSPAN=11>9001 (JPY, Tokyo-1)</TH></TR>
<TR ALIGN=center><TH COLSPAN=14><B>DEALS</B></TH></TR>
<TR><TD>Time</TD><TD>Deal</TD><TD>Symbol</TD><TD>Type</TD><TD>Direction</TD><TD>Volume</TD><TD>Price</TD><TD>Order</TD>
<TD>Commission</TD><TD>Fee</TD><TD>Swap</TD><TD>Profit</TD><TD>Balance</TD><TD>Comment</TD></TR>
<TR><TD NOWRAP>2026.03.01 20:00:00</TD ><TD CLASS=hidden>x</TD><TD>5</TD><TD></TD><TD>BALANCE</TD><TD></TD><TD></TD><TD></TD><TD></TD>
<TD>0</TD><TD>0</TD><TD>0</TD><TD>1 234 567</TD><TD>1 234 567</TD><TD Class = "a  hidden b">gone</TD><TD>Funding</TD></TR>
<TR><TD>2026.03.01 21:00:00<TD>6<TD><TD>Credit<TD><TD><TD><TD><TD><TD><TD><TD>25<TD><TD>Credit</TR>
<TR><TD>2026.03.01 21:00:00</td><td>4</td><td></td><td>balance</td><td></td><td></td><td></td><td></td><td>0</td><td>0</td><td>0</td><td>-100</td><td></td><td/>
</TR><TR><TD>2026.03.01 22:00:00</TD><TD>7</TD><TD></TD><TD>BONUS</TD><TD></TD><TD></TD><TD></TD><TD></TD><TD>0</TD><TD>0</TD><TD>0</TD><TD>10</TD><TD></TD><TD>b</TD></TR>
</TABLE></BODY></HTML>"""


def comments_report() -> str:
    return (HEAD.format(title="x") + header()
            + "<!-- <tr><td>Positions</td></tr> -- >\n<tr><td>fake</td><td>1</td></tr> --\n>"
            + th_row("Deals") + DEALS_HEAD
            + "<?xml version='1.0'?><!DOCTYPE x><![CDATA[ <tr><td>cdata</td></tr> ]]>"
            + deal("2026.03.01 20:00:00", "1", "", "balance", "", "", "", "", "", "", "",
                   "1<!-- 9 -->00", "", "a <b>bold</b><script>var a = '<td>5</td>';</script >c<style>p{}</style>")
            + "<![if !IE]><tr><td>ie</td></tr><![endif]>"
            + deal("2026.03.01 21:00:00", "2", "", "balance", "", "", "", "", "", "", "",
                   "5", "", "< 1 & 2 > <SCRIPT>x</ſcript>y</Script>z")
            + TAIL)


def totals_report() -> str:
    return (HEAD.format(title="x") + header() + th_row("Positions") + "<tr></tr>\n<tr><td></td><td> </td></tr>\n"
            + POSITIONS_HEAD
            + position("2026.03.02 00:01:00", "7", "XAUUSD", "buy", "1", "2000", "", "", "2026.03.02 00:10:00", "2010",
                       "", "", "1000")
            + row(["", "", "", "", "", "", "-7.00", "0.00", "1 000.00"])
            + row(["Total:", "", "", "1 000.00"])
            + position("2026.03.02 00:01:00", "6", "XAUUSD", "SELL", "1", "2000", "", "", "2026.03.02 00:20:00", "1990",
                       "", "", "1000")
            + th_row("Working Orders") + ORDERS_HEAD
            + row(["2026.03.02 00:01:00", "90", "XAUUSD", "buy limit", "1", "1900", "", "", "", "placed",
                   ("", 'colspan="4"')])
            + th_row("Summary") + row(["Balance:", "1", "Equity:", "2", "Margin:", "3"])
            + th_row("Deals") + DEALS_HEAD
            + deal("2026.03.01 20:00:00", "1", "", "balance", "", "", "", "", "", "", "", "100", "", "")
            + row(["", "", "", "100"])
            + TAIL)


def times_report() -> str:
    times = [("2026.03.02 10:00", "2026-03-02 10:05:05"), ("2026.3.2 1:2:3", "2026.12.31 23:59:59"),
             ("2024.02.29 00:00:00", "2026.03.2  1:02"), ("1970.01.01 00:00:00", "2026-3-2 0:0:0"),
             ("2026.03.02 10:00:60", "\u0662\u0660\u0662\u0666.03.02 10:00:00")]
    rows = [position(a, str(10 + i), "XAUUSD", "buy", "1", "2000", "", "", b, "2000", "", "", "0")
            for i, (a, b) in enumerate(times[:4])]
    return (HEAD.format(title="x") + header() + th_row("Positions") + POSITIONS_HEAD + "".join(rows)
            + th_row("Deals") + DEALS_HEAD
            + deal("1999-12-31 23:59:59", "1", "", "balance", "", "", "", "", "", "", "", "1", "", "")
            + deal(times[4][1], "2", "", "balance", "", "", "", "", "", "", "", "1", "", "")
            + TAIL)


# ---- the golden files ----

def outcome(data: bytes) -> dict:
    try:
        return {"ok": parse_report(data)}
    except ReportError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # what the route answered with a 500
        return {"exception": type(exc).__name__}


@pytest.mark.parametrize("name", sorted(CASES))
def test_report_golden(name):
    folder = GOLDEN / name
    data = CASES[name]()
    got = outcome(data)
    text = json.dumps(got, indent=1) + "\n"
    if UPDATE or not (folder / "expected.json").exists():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report.html").write_bytes(data)
        (folder / "expected.json").write_text(text)
    assert (folder / "report.html").read_bytes() == data, "the case changed: UPDATE_GOLDEN=1 rewrites its files"
    assert json.loads((folder / "expected.json").read_text()) == got
    assert (folder / "expected.json").read_text() == text


def test_no_stray_cases():
    assert sorted(p.name for p in GOLDEN.iterdir() if p.is_dir()) == sorted(CASES)


def test_cases_cover_every_outcome():
    kinds = {next(iter(json.loads((GOLDEN / n / "expected.json").read_text()))) for n in CASES}
    assert kinds == {"ok", "error"}


# ---- the tables report.rs copies from Python ----

def rust_table(name: str) -> str:
    m = re.search(rf"const {name}: [^=]+= &\[\n(.*?)\n\];", RUST.read_text(), re.S)
    assert m, f"{name} not found in report.rs"
    return m.group(1)


def unescape_rust(s: str) -> str:
    return re.sub(r"\\u\{([0-9a-f]+)\}", lambda m: chr(int(m.group(1), 16)), s)


def code_ranges(pred) -> list[tuple[int, int]]:
    out, start = [], None
    for c in range(0x110001):
        hit = c < 0x110000 and pred(chr(c))
        if hit and start is None:
            start = c
        if not hit and start is not None:
            out.append((start, c - 1))
            start = None
    return out


def test_rust_entities_match_python():
    pairs = re.findall(r'\("((?:[^"\\]|\\u\{[0-9a-f]+\})*)", "((?:[^"\\]|\\u\{[0-9a-f]+\})*)"\)', rust_table("ENTITIES"))
    assert [(unescape_rust(k), unescape_rust(v)) for k, v in pairs] == sorted(html5.items())


@pytest.mark.skipif(unicodedata.unidata_version != "14.0.0", reason="the tables are Python 3.11's Unicode 14.0")
@pytest.mark.parametrize("name,pred", [("NON_PRINTABLE", lambda c: not c.isprintable()), ("DECIMAL", str.isdecimal)])
def test_rust_unicode_tables_match_python(name, pred):
    pairs = [(int(a, 16), int(b, 16)) for a, b in re.findall(r"\(0x([0-9a-f]+), 0x([0-9a-f]+)\)", rust_table(name))]
    assert pairs == code_ranges(pred)
