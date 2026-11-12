"""Invoice and POD documents: plain text PDFs, or 'scanned' image PDFs."""
from __future__ import annotations

import io
import random
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def inr(x) -> str:
    """Rs. 1,23,456.00 (Indian digit grouping)."""
    x = Decimal(x).quantize(Decimal("0.01"))
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}Rs. {whole}.{frac}"


def dmy(d) -> str:
    return d.strftime("%d-%b-%y")


# ───────────────────────── tiny text-PDF writer ─────────────────────────
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(lines: list[tuple[str, bool]], landscape: bool = False) -> bytes:
    """lines: (text, bold). A4 (landscape for invoices), Helvetica 10 pt."""
    per = 42 if landscape else 60
    w, h = (842, 595) if landscape else (595, 842)
    pages = [lines[i:i + per] for i in range(0, len(lines), per)] or [[]]
    objs: list[bytes] = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for i, pg in enumerate(pages):
        ops = ["BT", "/F1 10 Tf", "12 TL", f"40 {h - 42} Td"]
        for text, bold in pg:
            ops.append(f"/{'F2' if bold else 'F1'} 10 Tf ({_esc(text)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


# ───────────────────────── 'scanned' image PDF ─────────────────────────
def scanned_pdf(lines: list[tuple[str, bool]], seed: int, signature: bool = False) -> bytes:
    rng = random.Random(seed)
    W, H = 1754, 1240
    img = Image.new("L", (W, H), 250)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=24)
    bold = ImageFont.load_default(size=26)
    y = 90
    for text, b in lines:
        draw.text((70 + rng.randint(-2, 2), y), text, fill=rng.randint(20, 60), font=bold if b else font)
        y += 36
        if y > H - 160:
            break
    if signature:
        x0, y0 = 1150, H - 260
        pts = [(x0 + i * 9, y0 + int(18 * __import__("math").sin(i / 2.3)) + rng.randint(-6, 6)) for i in range(40)]
        draw.line(pts, fill=40, width=3)
        draw.text((1150, H - 210), "Receiver's signature", fill=60, font=font)
    # paper noise, a slight tilt, a little blur
    px = img.load()
    for _ in range(9000):
        px[rng.randrange(W), rng.randrange(H)] = rng.randint(120, 200)
    img = img.rotate(rng.uniform(-1.6, 1.6), fillcolor=250, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF", resolution=150)
    return buf.getvalue()


# ───────────────────────── document content ─────────────────────────
def invoice_lines(world, inv) -> list[tuple[str, bool]]:
    L = [("TRANSINDUS ROADWAYS PVT. LTD.", True), ("GSTIN 27AAACT0000X1Z5  |  Bhosari MIDC, Pune 411026", False), ("", False),
         ("TAX INVOICE", True), (f"Invoice No: {inv.invoice_no}      Date: {dmy(inv.invoice_date)}      Lane: {inv.lane}", False),
         ("Bill To: Meridian Freight Pvt. Ltd., Pune", False), ("", False),
         ("Consignment   Booked     Wt(kg)    Rate/kg   Freight          Fuel%   Fuel amt        Detention       Line total", True)]
    for l in inv.lines:
        L.append((f"{l.shipment_id:<13} {dmy(l.booked_on):<10} {l.billed_weight_kg:>7}   {l.rate_per_kg:>6}    {inr(l.freight):<16} {l.fuel_pct:>5}   "
                  f"{inr(l.fuel_amount):<15} {inr(l.detention_inr):<15} {inr(l.total)}", False))
    L += [("", False), (f"Sub Total: {inr(inv.subtotal)}", False), (f"IGST @ 12%: {inr(inv.gst)}", False),
          (f"INVOICE TOTAL: {inr(inv.total_inr)}", True)]
    if inv.remarks:
        L += [("", False), (f"Remarks: {inv.remarks}", False)]
    L += [("", False), ("This is a computer generated invoice.", False)]
    return L


def pod_lines(world, pod) -> list[tuple[str, bool]]:
    s = world.shipments.get(pod.shipment_id)
    return [("TRANSINDUS ROADWAYS PVT. LTD.", True), ("PROOF OF DELIVERY", True), ("", False),
            (f"Consignment: {pod.shipment_id}", False), (f"Lane: {s.lane if s else '-'}", False),
            (f"Promised delivery: {dmy(pod.promised_on)}", False), (f"Delivered on: {dmy(pod.delivered_on)}", False),
            (f"Received by: {pod.receiver}", False), ("Condition on receipt: Good / Sealed", False)]
