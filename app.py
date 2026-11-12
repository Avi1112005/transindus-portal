"""TransIndus Roadways — Customer Self Service Portal (fictional).

A deliberately awkward legacy portal for the Week 3 Team Delta browser-agent
project. Instructors only: do not share this source with the team.

Run locally:  uvicorn app:app --port 8800
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import random
import secrets
import time
from collections import defaultdict, deque
from decimal import Decimal
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import docs
import gen

# ───────────────────────── configuration ─────────────────────────
USER = os.getenv("PORTAL_USER", "meridian.ap")
PASS = os.getenv("PORTAL_PASS", "Tir@2009portal")
OPERATOR_PASS = os.getenv("OPERATOR_PASS", "operator-otp")
TEAM_KEY = os.getenv("TEAM_KEY", "team-delta-key")
ADMIN_KEY = os.getenv("ADMIN_KEY", "admin-key-change-me")
PUBLIC_SEED = int(os.getenv("PUBLIC_SEED", "20261101"))
HIDDEN_SEED = int(os.getenv("HIDDEN_SEED", "0") or 0)
CHAOS = os.getenv("CHAOS", "1") == "1"
SECRET = os.getenv("PORTAL_SECRET", secrets.token_hex(16)).encode()

IDLE_TIMEOUT = 240          # seconds
RANDOM_EXPIRY = 0.015       # per request
SLOW_P, ERROR_P = 0.07, 0.04
LATE_FAIL_VIEW, LATE_FAIL_DISPUTE = 0.03, 0.12
RATE_LIMIT = 3              # requests per second per client
PAGE_SIZE = 15

REASON_LABELS = {
    "weight_over": "Excess weight billed",
    "rate_wrong": "Incorrect rate applied",
    "surcharge_wrong": "Incorrect fuel surcharge",
    "detention_unjustified": "Detention not applicable",
    "duplicate_billing": "Duplicate billing",
    "shipment_unknown": "Consignment not ours",
    "no_pod": "POD not available",
    "math_error": "Calculation error",
}


class State:
    def __init__(self) -> None:
        self.reset("public")

    def reset(self, mode: str) -> None:
        seed = HIDDEN_SEED if mode == "hidden" and HIDDEN_SEED else PUBLIC_SEED
        self.mode = mode if seed == HIDDEN_SEED and mode == "hidden" else "public"
        self.seed = seed
        self.world = gen.build(seed, 30 if self.mode == "hidden" else 20)
        self.disputes: list[gen.Dispute] = list(self.world.disputes)
        self.sessions: dict[str, dict] = {}
        self.devices: set[str] = set()
        self.pending_otp: dict[str, dict] = {}
        self.otp_inbox: deque = deque(maxlen=20)
        self.failed: dict[str, int] = defaultdict(int)
        self.captcha: dict[str, str] = {}
        self.hits: dict[str, deque] = defaultdict(deque)
        self.dispute_seq = 5000
        self.doc_cache: dict[tuple, bytes] = {}
        self.login_count = 0


S = State()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ───────────────────────── helpers ─────────────────────────
def esc(x) -> str:
    return html.escape(str(x))


def client_id(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?"))


def token(sid: str, doc_id: str) -> str:
    return hmac.new(SECRET, f"{sid}:{doc_id}".encode(), hashlib.sha256).hexdigest()[:16]


CSS = """
body{font:11px Verdana,Arial,sans-serif;margin:0;background:#dfe6ee;color:#000}
#hdr{background:linear-gradient(#2c5a8c,#1b3a5e);color:#fff;padding:8px 12px;border-bottom:3px solid #f2a516}
#hdr b{font-size:16px} #hdr span{float:right;font-size:10px;margin-top:4px}
#menu{background:#f2f2f2;border-bottom:1px solid #999;padding:4px 8px}
#menu a{color:#1b3a5e;margin-right:14px;font-weight:bold;text-decoration:none}
#body{padding:12px;background:#fff;margin:10px;border:1px solid #999;min-height:400px}
table.grid{border-collapse:collapse;margin:6px 0}
table.grid th{background:#5b7ea6;color:#fff;padding:3px 6px;border:1px solid #3d5a7c;text-align:left}
table.grid td{padding:3px 6px;border:1px solid #bbb}
table.grid tr.alt td{background:#eef3f8}
.btn{background:#e9e9e9;border:1px solid #777;padding:2px 10px;font:11px Verdana;cursor:pointer}
.err{color:#b00000;font-weight:bold} .ok{color:#006400;font-weight:bold}
.foot{font-size:9px;color:#666;text-align:center;margin:8px}
#ovl{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99}
#ovl div{background:#fffbe6;border:2px solid #c90;width:420px;margin:120px auto;padding:14px}
.lbl{width:160px;display:inline-block}
"""


def shell(title: str, body: str, sess: dict | None = None, notice: bool = False) -> HTMLResponse:
    menu = ""
    if sess:
        menu = ('<div id="menu"><a href="/Default.aspx">Home</a><a href="/Billing/InvoiceList.aspx">Invoices</a>'
                '<a href="/Ops/PODSearch.aspx">POD Search</a><a href="/Masters/RateCard.aspx">Rate Card</a>'
                '<a href="/Masters/FuelSurcharge.aspx">Fuel Surcharge</a><a href="/Disputes/DisputeList.aspx">Disputes</a>'
                f'<a href="/Logout.aspx">Logout</a> <span style="float:right">User: {esc(USER)}</span></div>')
    ovl = ""
    if notice:
        ovl = ('<div id="ovl"><div><b>Portal maintenance notice</b><p>The portal will be unavailable on Sunday between 01:00 and 05:00 IST '
               'for scheduled maintenance. Unsaved work may be lost.</p><form method="post" action="/Notice/Ack.aspx">'
               '<input type="submit" class="btn" id="btnAck" value="I Acknowledge"></form></div></div>')
    page = (f"<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\"><html><head><title>{esc(title)} - TransIndus CSS</title>"
            f"<style>{CSS}</style></head><body><div id=\"hdr\"><b>TransIndus Roadways</b> &nbsp;Customer Self Service Portal "
            f"<span>Ver 2.3.11 &middot; Best viewed in Internet Explorer 8 at 1024x768</span></div>{menu}"
            f"<div id=\"body\">{body}</div><div class=\"foot\">&copy; 2009 TransIndus Roadways Pvt. Ltd. All rights reserved.</div>{ovl}</body></html>")
    return HTMLResponse(page)


def error_page(code: int, title: str, msg: str) -> HTMLResponse:
    r = HTMLResponse(f"<html><head><title>{title}</title></head><body style='font:12px Verdana'><h2>{title}</h2><p>{msg}</p>"
                     f"<hr><i>Microsoft-IIS/6.0</i></body></html>", status_code=code)
    return r


def get_session(req: Request) -> dict | None:
    sid = req.cookies.get("ASP.NET_SessionId")
    s = S.sessions.get(sid or "")
    if not s:
        return None
    now = time.time()
    if now - s["last"] > IDLE_TIMEOUT or (CHAOS and random.random() < RANDOM_EXPIRY):
        S.sessions.pop(sid, None)
        return None
    s["last"] = now
    return s


def need_login(req: Request) -> RedirectResponse:
    target = req.url.path + (("?" + req.url.query) if req.url.query else "")
    return RedirectResponse(f"/Login.aspx?ReturnUrl={quote(target)}&msg=expired", status_code=302)


def new_session(resp: Response) -> dict:
    sid = secrets.token_hex(12)
    sess = {"id": sid, "last": time.time(), "variant": random.choice("AB"), "ack": False}
    S.sessions[sid] = sess
    resp.set_cookie("ASP.NET_SessionId", sid, httponly=True, samesite="lax")
    return sess


# ───────────────────────── chaos middleware ─────────────────────────
PORTAL_PREFIXES = ("/Default", "/Login", "/Billing", "/Ops", "/Masters", "/Disputes", "/Notice", "/Logout")


@app.middleware("http")
async def chaos(req: Request, call_next):
    path = req.url.path
    if CHAOS and path.startswith(PORTAL_PREFIXES):
        key = client_id(req) + "|" + req.cookies.get("ASP.NET_SessionId", "")
        q = S.hits[key]
        now = time.time()
        while q and now - q[0] > 1:
            q.popleft()
        q.append(now)
        if len(q) > RATE_LIMIT:
            return error_page(429, "Too Many Requests", "You are sending requests too quickly. Please wait and try again.")
        await asyncio.sleep(random.uniform(3, 7) if random.random() < SLOW_P else random.uniform(0.2, 1.4))
        if random.random() < ERROR_P and not path.startswith("/Login"):
            return error_page(502, "Service Temporarily Unavailable", "The server is temporarily unable to service your request. Please try again later.")
    return await call_next(req)


# ───────────────────────── login, OTP, captcha ─────────────────────────
def captcha_png(text: str) -> bytes:
    img = Image.new("RGB", (170, 50), (235, 235, 225))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=30)
    for i, ch in enumerate(text):
        d.text((12 + i * 30, random.randint(4, 12)), ch, fill=(random.randint(0, 90),) * 3, font=f)
    for _ in range(8):
        d.line([(random.randint(0, 170), random.randint(0, 50)), (random.randint(0, 170), random.randint(0, 50))], fill=(110, 110, 110), width=2)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


@app.get("/")
async def root():
    return RedirectResponse("/Default.aspx")


@app.get("/Login.aspx")
async def login_form(req: Request, ReturnUrl: str = "/Default.aspx", msg: str = ""):
    ip = client_id(req)
    note = {"expired": '<p class="err">Your session has expired. Please log in again.</p>',
            "bad": '<p class="err">Invalid User ID or Password.</p>',
            "captcha": '<p class="err">Please enter the characters shown in the image.</p>'}.get(msg, "")
    cap = ""
    if S.failed[ip] >= 5:
        S.captcha[ip] = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(5))
        cap = ('<p><span class="lbl">Security check:</span><img id="imgCaptcha" src="/Captcha.ashx?r=' + str(random.random()) +
               '"><br><span class="lbl">Enter characters:</span><input name="txtCaptcha" id="txtCaptcha"></p>')
    body = (f"<h3>Customer Login</h3>{note}<form method='post' action='/Login.aspx'><input type='hidden' name='ReturnUrl' value='{esc(ReturnUrl)}'>"
            "<p><span class='lbl'>User ID:</span><input name='txtUser' id='txtUser'></p>"
            "<p><span class='lbl'>Password:</span><input type='password' name='txtPass' id='txtPass'></p>"
            f"{cap}<p><input type='submit' class='btn' id='btnLogin' value='Login'></p></form>")
    return shell("Login", body)


@app.get("/Captcha.ashx")
async def captcha(req: Request):
    text = S.captcha.get(client_id(req), "XXXXX")
    return Response(captcha_png(text), media_type="image/png")


@app.post("/Login.aspx")
async def login(req: Request, txtUser: str = Form(""), txtPass: str = Form(""), txtCaptcha: str = Form(""), ReturnUrl: str = Form("/Default.aspx")):
    ip = client_id(req)
    back = quote(ReturnUrl)
    if S.failed[ip] >= 5:
        if txtCaptcha.strip().upper() != S.captcha.get(ip, "#"):
            return RedirectResponse(f"/Login.aspx?ReturnUrl={back}&msg=captcha", status_code=302)
    if txtUser != USER or txtPass != PASS:
        S.failed[ip] += 1
        return RedirectResponse(f"/Login.aspx?ReturnUrl={back}&msg=bad", status_code=302)
    S.failed[ip] = 0
    S.login_count += 1
    device = req.cookies.get("TIRDEV", "")
    if device not in S.devices or S.login_count % 12 == 0:
        code = f"{random.randint(0, 999999):06d}"
        pid = secrets.token_hex(8)
        S.pending_otp[pid] = {"code": code, "at": time.time(), "ret": ReturnUrl}
        S.otp_inbox.appendleft((time.strftime("%H:%M:%S"), code))
        r = RedirectResponse("/Login/Otp.aspx", status_code=302)
        r.set_cookie("TIROTP", pid, httponly=True)
        return r
    r = RedirectResponse(ReturnUrl if ReturnUrl.startswith("/") else "/Default.aspx", status_code=302)
    new_session(r)
    return r


@app.get("/Login/Otp.aspx")
async def otp_form(msg: str = ""):
    note = '<p class="err">Invalid or expired OTP.</p>' if msg else ""
    body = (f"<h3>Additional Verification</h3>{note}<p>A One Time Password has been sent to the registered mobile number ending ******4417. "
            "It is valid for 5 minutes.</p><form method='post' action='/Login/Otp.aspx'><span class='lbl'>Enter OTP:</span>"
            "<input name='txtOtp' id='txtOtp' maxlength='6'> <input type='submit' class='btn' id='btnVerify' value='Verify'></form>")
    return shell("Verification", body)


@app.post("/Login/Otp.aspx")
async def otp_verify(req: Request, txtOtp: str = Form("")):
    pid = req.cookies.get("TIROTP", "")
    p = S.pending_otp.get(pid)
    if not p or time.time() - p["at"] > 300 or txtOtp.strip() != p["code"]:
        return RedirectResponse("/Login/Otp.aspx?msg=bad", status_code=302)
    S.pending_otp.pop(pid, None)
    ret = p["ret"] if p["ret"].startswith("/") else "/Default.aspx"
    r = RedirectResponse(ret, status_code=302)
    dev = secrets.token_hex(8)
    S.devices.add(dev)
    r.set_cookie("TIRDEV", dev, max_age=86400, httponly=True)
    r.delete_cookie("TIROTP")
    new_session(r)
    return r


@app.get("/Logout.aspx")
async def logout(req: Request):
    S.sessions.pop(req.cookies.get("ASP.NET_SessionId", ""), None)
    return RedirectResponse("/Login.aspx", status_code=302)


@app.post("/Notice/Ack.aspx")
async def ack(req: Request):
    s = get_session(req)
    if not s:
        return need_login(req)
    s["ack"] = True
    return RedirectResponse(req.headers.get("referer") or "/Default.aspx", status_code=302)


# ───────────────────────── pages ─────────────────────────
def page(req: Request, title: str, body: str):
    s = get_session(req)
    if not s:
        return need_login(req)
    return shell(title, body, s, notice=not s["ack"])


@app.get("/Default.aspx")
async def home(req: Request):
    if not get_session(req):
        return RedirectResponse("/Login.aspx")
    open_n = sum(1 for i in S.world.invoices.values() if i.status == "Open")
    return page(req, "Home", f"<h3>Welcome, Meridian Freight Pvt. Ltd.</h3><p>Customer code: MER-PUN-0042</p>"
                             f"<p>You have <b>{open_n}</b> invoices on record. Use the menu above to view invoices, PODs and raise disputes.</p>")


def parse_month(m: str) -> str:
    return m if m in gen.MONTHS else ""


@app.get("/Billing/InvoiceList.aspx")
async def invoice_list(req: Request, ddlLane: str = "", ddlMonth: str = "", ddlStatus: str = "", pg: int = 1):
    s = get_session(req)
    if not s:
        return need_login(req)
    invs = list(S.world.invoices.values())
    if ddlLane:
        invs = [i for i in invs if i.lane == ddlLane]
    if parse_month(ddlMonth):
        invs = [i for i in invs if gen.month_of(i.invoice_date) == ddlMonth]
    if ddlStatus:
        invs = [i for i in invs if i.status == ddlStatus]
    variant = s["variant"]
    invs.sort(key=lambda i: (i.invoice_date, i.invoice_no), reverse=(variant == "A"))
    total = len(invs)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    pg = max(1, min(pg, pages))
    start = (pg - 1) * PAGE_SIZE - (1 if pg > 1 else 0)  # legacy paging bug: one row repeats
    rows = invs[start:start + PAGE_SIZE] if total else []
    lane_opts = "".join(f"<option {'selected' if l == ddlLane else ''}>{l}</option>" for l in gen.LANES)
    month_opts = "".join(f"<option value='{m}' {'selected' if m == ddlMonth else ''}>{gen.MONTH_NAMES[m]}</option>" for m in gen.MONTHS)
    stat_opts = "".join(f"<option {'selected' if x == ddlStatus else ''}>{x}</option>" for x in ("Open", "Cancelled"))
    form = (f"<form method='get' action='/Billing/InvoiceList.aspx'>Lane: <select name='ddlLane' id='ddlLane'><option value=''>-- All --</option>{lane_opts}</select> "
            f"Month: <select name='ddlMonth' id='ddlMonth'><option value=''>-- All --</option>{month_opts}</select> "
            f"Status: <select name='ddlStatus' id='ddlStatus'><option value=''>-- All --</option>{stat_opts}</select> "
            "<input type='submit' class='btn' id='btnSearch' value='Search'></form>")

    def link(i):
        return f"<a href='/Billing/InvoiceView.aspx?InvNo={quote(i.invoice_no, safe='')}'>{esc(i.invoice_no)}</a>"
    if variant == "A":
        head = ["Invoice No", "Invoice Date", "Lane", "No. of Consignments", "Invoice Value (Rs.)", "Status"]
        cells = lambda i: [link(i), docs.dmy(i.invoice_date), i.lane, len(i.lines), f"{i.total_inr:,.2f}", i.status]
        tid = "gvInvoices"
    else:
        head = ["Date", "Inv #", "Status", "Route", "Total Amt", "Lines"]
        cells = lambda i: [docs.dmy(i.invoice_date), link(i), i.status, i.lane, docs.inr(i.total_inr), len(i.lines)]
        tid = "ctl00_Main_grdInv"
    trs = "".join(f"<tr class='{'alt' if n % 2 else ''}'>" + "".join(f"<td>{c}</td>" for c in cells(i)) + "</tr>" for n, i in enumerate(rows))
    q = f"ddlLane={quote(ddlLane)}&ddlMonth={quote(ddlMonth)}&ddlStatus={quote(ddlStatus)}"
    nav = " ".join(f"<b>{p}</b>" if p == pg else f"<a href='/Billing/InvoiceList.aspx?{q}&pg={p}'>{p}</a>" for p in range(1, pages + 1))
    shown = f"Showing {start + 1 if rows else 0}-{start + len(rows)} of {total} records"
    table = (f"<table class='grid' id='{tid}'><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr>{trs}</table>" if rows else "<p>No records found.</p>")
    return shell("Invoices", f"<h3>Invoice Enquiry</h3>{form}<p>{shown}</p>{table}<p>Page: {nav}</p>", s, notice=not s["ack"])


@app.get("/Billing/InvoiceView.aspx")
async def invoice_view(req: Request, InvNo: str = ""):
    s = get_session(req)
    if not s:
        return need_login(req)
    inv = S.world.invoices.get(InvNo)
    if not inv:
        return shell("Invoice", "<p class='err'>Invoice not found.</p>", s, notice=not s["ack"])
    cns = [c for c in S.world.credit_notes if c.invoice_no == inv.invoice_no]
    if inv.pdf_only:
        lines_html = "<p><i>Line item details are available in the attached PDF only (legacy invoice).</i></p>"
    else:
        if s["variant"] == "A":
            head = ["Consignment", "Booked On", "Weight (kg)", "Rate/kg", "Freight", "Fuel %", "Fuel Amt", "Detention", "Amount"]
            cell = lambda l: [l.shipment_id, docs.dmy(l.booked_on), l.billed_weight_kg, l.rate_per_kg, f"{l.freight:,.2f}", l.fuel_pct, f"{l.fuel_amount:,.2f}", f"{l.detention_inr:,.2f}", f"{l.total:,.2f}"]
        else:
            head = ["CN No.", "Amount", "Chargeable Wt", "Booking Dt", "Rate", "FSC %", "FSC", "Detention Chgs", "Basic Freight"]
            cell = lambda l: [l.shipment_id, docs.inr(l.total), l.billed_weight_kg, docs.dmy(l.booked_on), l.rate_per_kg, l.fuel_pct, docs.inr(l.fuel_amount), docs.inr(l.detention_inr), docs.inr(l.freight)]
        trs = "".join(f"<tr class='{'alt' if n % 2 else ''}'>" + "".join(f"<td>{esc(c)}</td>" for c in cell(l)) + "</tr>" for n, l in enumerate(inv.lines))
        lines_html = f"<table class='grid' id='gvLines'><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr>{trs}</table>"
        lines_html += "<p style='font-size:9px'>Note: in the line table above, CN No. means Consignment Number.</p>" if s["variant"] == "B" else ""
    cn_html = ""
    if cns:
        cn_html = "<h4>Credit Notes</h4><table class='grid'><tr><th>Credit Note</th><th>Date</th><th>Amount</th><th>Reason</th></tr>" + "".join(
            f"<tr><td>{esc(c.cn_no)}</td><td>{docs.dmy(c.cn_date)}</td><td>{docs.inr(c.amount_inr)}</td><td>{esc(c.reason)}</td></tr>" for c in cns) + "</table>"
    late = ""
    if CHAOS and random.random() < LATE_FAIL_VIEW:
        late = "<script>setTimeout(function(){document.getElementById('body').innerHTML='<p class=\"err\">An error occurred while loading invoice details. Please try again.</p>';},2500);</script>"
    tok = token(s["id"], inv.invoice_no)
    body = (f"<h3>Invoice {esc(inv.invoice_no)}</h3><table><tr><td class='lbl'>Invoice Date</td><td>{docs.dmy(inv.invoice_date)}</td></tr>"
            f"<tr><td>Lane</td><td>{inv.lane}</td></tr><tr><td>Status</td><td><b>{inv.status}</b></td></tr>"
            f"<tr><td>Remarks</td><td>{esc(inv.remarks) or '-'}</td></tr></table>{lines_html}"
            f"<table><tr><td class='lbl'>Sub Total</td><td>{docs.inr(inv.subtotal)}</td></tr><tr><td>IGST @ 12%</td><td>{docs.inr(inv.gst)}</td></tr>"
            f"<tr><td><b>Invoice Total</b></td><td><b id='lblTotal'>{docs.inr(inv.total_inr)}</b></td></tr></table>{cn_html}"
            f"<p><input type='button' class='btn' id='btnDownload' value='Download Invoice (PDF)' onclick=\"window.location='/Billing/GetDoc.ashx?t=inv&id='+encodeURIComponent('{esc(inv.invoice_no)}')+'&k={tok}'\"> "
            f"<a href='/Disputes/NewDispute.aspx?InvNo={quote(inv.invoice_no, safe='')}'>Raise Dispute</a></p>{late}")
    return shell(f"Invoice {inv.invoice_no}", body, s, notice=not s["ack"])


@app.get("/Billing/GetDoc.ashx")
async def get_doc(req: Request, t: str = "", id: str = "", k: str = ""):
    s = get_session(req)
    if not s:
        return need_login(req)
    if not hmac.compare_digest(k, token(s["id"], id)):
        return error_page(403, "Access Denied", "The document link is invalid or has expired.")
    key = (S.seed, t, id)
    if key not in S.doc_cache:
        if t == "inv" and id in S.world.invoices:
            inv = S.world.invoices[id]
            lines = docs.invoice_lines(S.world, inv)
            S.doc_cache[key] = docs.scanned_pdf(lines, hash(key) & 0xFFFF) if inv.scanned else docs.text_pdf(lines, landscape=True)
        elif t == "pod" and id in S.world.pods:
            pod = S.world.pods[id]
            lines = docs.pod_lines(S.world, pod)
            S.doc_cache[key] = docs.scanned_pdf(lines, hash(key) & 0xFFFF, signature=True) if pod.scanned else docs.text_pdf(lines)
        else:
            return error_page(404, "Not Found", "Document not found.")
    fname = id.replace("/", "_") + ".pdf"
    return Response(S.doc_cache[key], media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/Ops/PODSearch.aspx")
async def pod_search(req: Request, txtCons: str = ""):
    s = get_session(req)
    if not s:
        return need_login(req)
    out = ""
    if txtCons:
        pod = S.world.pods.get(txtCons.strip().upper())
        if not pod:
            out = "<p class='err'>No POD found for this consignment.</p>"
        else:
            dates = (f"<tr><td>Promised</td><td>{docs.dmy(pod.promised_on)}</td></tr><tr><td>Delivered</td><td>{docs.dmy(pod.delivered_on)}</td></tr>"
                     if pod.dates_on_page else "<tr><td>Delivery details</td><td><i>See attached POD</i></td></tr>")
            tok = token(s["id"], pod.shipment_id)
            out = (f"<table class='grid'><tr><td>Consignment</td><td>{pod.shipment_id}</td></tr><tr><td>Status</td><td>Delivered</td></tr>{dates}</table>"
                   f"<p><a id='lnkPod' href='/Billing/GetDoc.ashx?t=pod&id={pod.shipment_id}&k={tok}'>View POD</a></p>")
    form = ("<form method='get' action='/Ops/PODSearch.aspx'>Consignment No: <input name='txtCons' id='txtCons' value='" + esc(txtCons) +
            "'> <input type='submit' class='btn' id='btnFind' value='Find'></form>")
    return shell("POD Search", f"<h3>Proof of Delivery</h3>{form}{out}", s, notice=not s["ack"])


@app.get("/Masters/RateCard.aspx")
async def rate_card(req: Request):
    rows = "".join(f"<tr><td>{lane}</td><td>{rate}</td><td>{docs.dmy(a)}</td><td>{docs.dmy(b) if b else 'Till date'}</td></tr>"
                   for lane, periods in S.world.rates.items() for a, b, rate in periods)
    return page(req, "Rate Card", f"<h3>Contract Rate Card (Rs. per kg)</h3><table class='grid'><tr><th>Lane</th><th>Rate</th><th>Effective From</th><th>Effective To</th></tr>{rows}</table>")


@app.get("/Masters/FuelSurcharge.aspx")
async def fuel(req: Request):
    rows = "".join(f"<tr><td>{m}</td><td>{p}</td></tr>" for m, p in S.world.fuel.items())
    return page(req, "Fuel Surcharge", f"<h3>Fuel Surcharge Schedule</h3><p>Applied on the month of booking.</p><table class='grid'><tr><th>Month</th><th>FSC %</th></tr>{rows}</table>")


@app.get("/Disputes/DisputeList.aspx")
async def dispute_list(req: Request, pg: int = 1):
    s = get_session(req)
    if not s:
        return need_login(req)
    ds = sorted(S.disputes, key=lambda d: d.raised_on, reverse=True)
    pages = max(1, math.ceil(len(ds) / 10))
    pg = max(1, min(pg, pages))
    rows = "".join(f"<tr><td>{d.dispute_no}</td><td>{esc(d.invoice_no)}</td><td>{d.raised_on:%d-%b-%y %H:%M}</td><td>{esc(d.raised_by)}</td>"
                   f"<td>{docs.inr(d.claimed_inr)}</td><td>Open</td><td>{esc(', '.join(REASON_LABELS.get(r, r) for r in d.reasons))}</td></tr>"
                   for d in ds[(pg - 1) * 10: pg * 10])
    nav = " ".join(f"<b>{p}</b>" if p == pg else f"<a href='/Disputes/DisputeList.aspx?pg={p}'>{p}</a>" for p in range(1, pages + 1))
    body = (f"<h3>Disputes</h3><table class='grid' id='gvDisputes'><tr><th>Dispute No</th><th>Invoice No</th><th>Raised On</th><th>Raised By</th>"
            f"<th>Correct Amount Claimed</th><th>Status</th><th>Reasons</th></tr>{rows}</table><p>Page: {nav}</p>")
    return shell("Disputes", body, s, notice=not s["ack"])


@app.get("/Disputes/NewDispute.aspx")
async def dispute_form(req: Request, InvNo: str = "", msg: str = ""):
    s = get_session(req)
    if not s:
        return need_login(req)
    note = {"amt": "<p class='err'>Enter a valid amount.</p>", "reason": "<p class='err'>Select at least one reason.</p>",
            "inv": "<p class='err'>Invoice not found.</p>"}.get(msg, "")
    boxes = "".join(f"<input type='checkbox' name='reason' value='{code}' id='chk_{code}'><label for='chk_{code}'>{label}</label><br>"
                    for code, label in REASON_LABELS.items())
    body = (f"<h3>Raise Billing Dispute</h3>{note}<form method='post' action='/Disputes/NewDispute.aspx'>"
            f"<p><span class='lbl'>Invoice No:</span><input name='txtInvNo' id='txtInvNo' value='{esc(InvNo)}'></p>"
            f"<p><span class='lbl'>Dispute reason(s):</span><br>{boxes}</p>"
            "<p><span class='lbl'>Correct amount (Rs.):</span><input name='txtClaimed' id='txtClaimed'></p>"
            "<p><span class='lbl'>Remarks:</span><textarea name='txtRemarks' id='txtRemarks' rows='3' cols='40'></textarea></p>"
            "<p><input type='submit' class='btn' id='btnSubmit' value='Submit Dispute'></p></form>")
    return shell("New Dispute", body, s, notice=not s["ack"])


@app.post("/Disputes/NewDispute.aspx")
async def dispute_submit(req: Request):
    s = get_session(req)
    if not s:
        return need_login(req)
    form = await req.form()
    inv_no = str(form.get("txtInvNo", "")).strip()
    reasons = [str(r) for r in form.getlist("reason") if str(r) in REASON_LABELS]
    if inv_no not in S.world.invoices:
        return RedirectResponse(f"/Disputes/NewDispute.aspx?InvNo={quote(inv_no, safe='')}&msg=inv", status_code=302)
    if not reasons:
        return RedirectResponse(f"/Disputes/NewDispute.aspx?InvNo={quote(inv_no, safe='')}&msg=reason", status_code=302)
    try:
        claimed = Decimal(str(form.get("txtClaimed", "")).replace(",", "").replace("Rs.", "").strip())
    except Exception:
        return RedirectResponse(f"/Disputes/NewDispute.aspx?InvNo={quote(inv_no, safe='')}&msg=amt", status_code=302)
    S.dispute_seq += 1
    no = f"DSP-{S.dispute_seq}"
    if CHAOS and random.random() < LATE_FAIL_DISPUTE:
        body = (f"<p class='ok' id='lblResult'>Dispute {no} submitted successfully.</p>"
                "<script>setTimeout(function(){document.getElementById('lblResult').className='err';"
                "document.getElementById('lblResult').innerHTML='Session expired. Your dispute was NOT saved. Please submit again.';},3000);</script>")
        return shell("Dispute", body, s)
    S.disputes.append(gen.Dispute(no, inv_no, sorted(reasons), claimed, __import__("datetime").datetime.now(), USER,
                                  str(form.get("txtRemarks", ""))[:500]))
    return shell("Dispute", f"<p class='ok' id='lblResult'>Dispute {no} submitted successfully.</p>", s)


# ───────────────────────── operator (humans only) ─────────────────────────
@app.get("/operator")
async def operator(req: Request):
    if req.cookies.get("TIROP") != hmac.new(SECRET, b"op", hashlib.sha256).hexdigest():
        return HTMLResponse("<form method='post' style='font:13px sans-serif;margin:40px'><h3>Operator inbox</h3>"
                            "Operator password: <input type='password' name='p'> <button>Open</button></form>")
    rows = "".join(f"<tr><td>{t}</td><td style='font-size:20px;letter-spacing:3px'><b>{c}</b></td></tr>" for t, c in S.otp_inbox)
    return HTMLResponse("<meta http-equiv='refresh' content='5'><div style='font:13px sans-serif;margin:40px'><h3>SMS inbox ******4417</h3>"
                        f"<p>One-time passwords sent by the portal (newest first). Refreshes every 5 seconds.</p><table cellpadding='6'>{rows}</table></div>")


@app.post("/operator")
async def operator_login(p: str = Form("")):
    r = RedirectResponse("/operator", status_code=302)
    if hmac.compare_digest(p, OPERATOR_PASS):
        r.set_cookie("TIROP", hmac.new(SECRET, b"op", hashlib.sha256).hexdigest(), httponly=True)
    return r


# ───────────────────────── team data (key required) ─────────────────────────
@app.get("/internal/meridian-shipments.csv")
async def meridian_csv(key: str = ""):
    if not hmac.compare_digest(key, TEAM_KEY):
        return PlainTextResponse("forbidden", status_code=403)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["shipment_id", "booked_on", "lane", "actual_weight_kg", "delivered_on", "meridian_ref"])
    for s in sorted(S.world.shipments.values(), key=lambda s: s.shipment_id):
        w.writerow([s.shipment_id, s.booked_on.isoformat(), s.lane, s.actual_weight_kg, s.delivered_on.isoformat(), s.meridian_ref])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


@app.get("/internal/tasks.json")
async def tasks(key: str = ""):
    if not hmac.compare_digest(key, TEAM_KEY):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"mode": S.mode, "tasks": S.world.tasks})


# ───────────────────────── admin (instructors) ─────────────────────────
def admin_ok(key: str) -> bool:
    return hmac.compare_digest(key, ADMIN_KEY)


@app.get("/admin")
async def admin(key: str = ""):
    if not admin_ok(key):
        return PlainTextResponse("forbidden", status_code=403)
    new = [d for d in S.disputes if d.raised_by == USER]
    return HTMLResponse(f"""<div style="font:14px sans-serif;margin:32px;max-width:760px">
<h2>TransIndus portal · instructor admin</h2>
<p>Mode: <b>{S.mode}</b> · seed {S.seed} · {len(S.world.invoices)} invoices · {len(S.world.tasks)} tasks · chaos {'on' if CHAOS else 'off'} ·
disputes raised by the team since reset: <b>{len(new)}</b></p>
<form method="post" action="/admin/reset?key={quote(key)}"><button name="mode" value="public">Reset to public data (20 tasks)</button>
<button name="mode" value="hidden" {'disabled' if not HIDDEN_SEED else ''}>Reset to HIDDEN data (30 tasks)</button></form>
<p><a href="/admin/answer-key.json?key={quote(key)}">Answer key (JSON)</a> · <a href="/admin/disputes.json?key={quote(key)}">Disputes (JSON)</a> ·
<a href="/internal/tasks.json?key={quote(TEAM_KEY)}">Tasks as the team sees them</a></p>
<p>Grade: <code>POST /admin/grade?key=…</code> with the team's results as JSON: <code>{{"results": [ {{"task_id": "T-01", ...}}, ... ]}}</code>.</p></div>""")


@app.post("/admin/reset")
async def admin_reset(key: str = "", mode: str = Form("public")):
    if not admin_ok(key):
        return PlainTextResponse("forbidden", status_code=403)
    S.reset(mode)
    return RedirectResponse(f"/admin?key={quote(key)}", status_code=302)


@app.get("/admin/answer-key.json")
async def answer_key(key: str = ""):
    if not admin_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"mode": S.mode, "seed": S.seed, "tasks": S.world.tasks, "answers": S.world.answers})


@app.get("/admin/disputes.json")
async def disputes_json(key: str = ""):
    if not admin_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse([{"dispute_no": d.dispute_no, "invoice_no": d.invoice_no, "reasons": d.reasons, "claimed_inr": float(d.claimed_inr),
                          "raised_by": d.raised_by, "raised_on": d.raised_on.isoformat()} for d in S.disputes])


@app.post("/admin/grade")
async def grade(req: Request, key: str = ""):
    if not admin_ok(key):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    import grader
    payload = await req.json()
    return JSONResponse(grader.grade(S.world, S.disputes, payload.get("results", []), USER))
