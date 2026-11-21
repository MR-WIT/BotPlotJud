import io
import os
import re
import math
import requests
from PIL import Image, ImageOps
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, ImageMessage, TextSendMessage,
)

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# OCR.space (ตั้งค่าใน Render > Environment)
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "")
OCR_SPACE_URL = "https://api.ocr.space/parse/image"
OCR_MAX_BYTES = 900_000  # free plan จำกัด 1 MB

# ============================================================
# STATION DATA
# 'type': 'NDB' -> แสดงเป็น B- (bearing) แทน R- (radial)
# ============================================================
STATIONS = {
    'UBL': {'lat': '151443', 'lon': '1045157', 'region': 'อุบลราชธานี'},
    'ROT': {'lat': '160701', 'lon': '1034620', 'region': 'อุบลราชธานี'},
    'BRM': {'lat': '151422', 'lon': '1031532', 'region': 'อุบลราชธานี'},
    'NKR': {'lat': '145648', 'lon': '1021840', 'region': 'อุบลราชธานี'},
    'KRT': {'lat': '145502', 'lon': '1020823', 'region': 'อุบลราชธานี'},
    'KKN': {'lat': '162815', 'lon': '1024717', 'region': 'ขอนแก่น'},
    'UDN': {'lat': '172305', 'lon': '1024630', 'region': 'ขอนแก่น'},
    'SKN': {'lat': '171251', 'lon': '1040812', 'region': 'ขอนแก่น'},
    'LOY': {'lat': '172649', 'lon': '1014323', 'region': 'ขอนแก่น'},
    'CTR': {'lat': '195656', 'lon': '0995259', 'region': 'เชียงราย'},
    'PCB': {'lat': '164034', 'lon': '1011148', 'region': 'เชียงราย'},
    'PAE': {'lat': '180803', 'lon': '1000959', 'region': 'เชียงราย'},
    'NAN': {'lat': '184833', 'lon': '1004657', 'region': 'เชียงราย'},
    'STN': {'lat': '090746', 'lon': '0990805', 'region': 'สมุย'},
    'CPN': {'lat': '104240', 'lon': '0992156', 'region': 'สมุย'},
    'SMU': {'lat': '093249', 'lon': '1000342', 'region': 'สมุย'},
    'NKS': {'lat': '083230', 'lon': '0995649', 'region': 'สมุย'},
    'TRT': {'lat': '121628', 'lon': '1021850', 'region': 'สมุย', 'type': 'NDB'},
}

RATIO = 57.4  # Excel W14

# ============================================================
# CALCULATION — ตรงตาม Excel สูตรทุกบรรทัด
# ============================================================
def encode_lat(s):
    s = str(s).zfill(6)
    return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])

def encode_lon(s):
    s = str(s).zfill(7)
    return int(s[0:3]) * RATIO * RATIO + int(s[3:5]) * RATIO + int(s[5:7])

def calc_radial_distance(tlat_str, tlon_str, slat_str, slon_str):
    J = encode_lat(slat_str)   # LAT station
    K = encode_lon(slon_str)   # LON station
    L = encode_lat(tlat_str)   # LAT target
    M = encode_lon(tlon_str)   # LON target

    N = (L - J) / 60           # DIF LAT
    O = (M - K) / RATIO        # DIF LON

    # Excel ATAN2(N, O) = Python atan2(O, N)
    P = math.degrees(math.atan2(O, N))
    Q = P if P > 1 else (360 + P if P < 0 else 360)  # RADIAL
    R = math.sqrt(N * N + O * O)                      # DISTANCE

    return round(Q), round(R, 1)

def find_top3(tlat_str, tlon_str):
    results = []
    for name, info in STATIONS.items():
        radial, dist = calc_radial_distance(tlat_str, tlon_str, info['lat'], info['lon'])
        prefix = 'B' if info.get('type') == 'NDB' else 'R'
        results.append((name, info['region'], prefix, radial, dist))
    results.sort(key=lambda x: x[4])
    return results[:3]

# ============================================================
# COORDINATE HELPERS
# วินาทีที่มีทศนิยมจะถูกปัดเป็นวินาทีเต็ม (เช่น 1020953.90 -> 1020954)
# ============================================================
def dms_round(raw, deg_digits):
    """'172314.10' (deg_digits=2) -> '172314' ; '1020953.90' (deg_digits=3) -> '1020954'"""
    d = int(raw[:deg_digits])
    m = int(raw[deg_digits:deg_digits + 2])
    s = float(raw[deg_digits + 2:])
    total = int(math.floor(d * 3600 + m * 60 + s + 0.5))
    d, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{d:0{deg_digits}d}{m:02d}{s:02d}"

def make_coord(lat_raw, lon_raw):
    """รับ lat 'DDMMSS(.ss)' / lon 'DDDMMSS(.ss)' คืน (lat6, lon7) หรือ None"""
    lat_raw = lat_raw.strip('.')
    lon_raw = lon_raw.strip('.')
    if re.fullmatch(r'\d{6}(\.\d+)?', lat_raw) and re.fullmatch(r'\d{7}(\.\d+)?', lon_raw):
        return dms_round(lat_raw, 2), dms_round(lon_raw, 3)
    return None

# ============================================================
# PARSE TEXT INPUT
# รองรับ: "152155 1044343" / "152155N 1044343E" / "152155.10 1044343.90"
# ============================================================
def parse_coord(text):
    text_clean = re.sub(r'[NE]', ' ', text.strip().upper())
    parts = text_clean.split()

    if len(parts) >= 2:
        lat_raw = re.sub(r'[^\d.]', '', parts[0])
        lon_raw = re.sub(r'[^\d.]', '', parts[1])
        return make_coord(lat_raw, lon_raw)

    return None

# ============================================================
# OCR: รูป -> ข้อความ
# ============================================================
def prepare_image(raw_bytes, max_bytes=OCR_MAX_BYTES):
    """หมุนตาม EXIF, แปลงเป็นขาวดำ, ย่อ/บีบให้ไม่เกิน 1 MB"""
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img).convert('L')

    max_side, quality = 2200, 85
    while True:
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        out = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS) if scale < 1 else img
        buf = io.BytesIO()
        out.save(buf, 'JPEG', quality=quality)
        if buf.tell() <= max_bytes or (max_side <= 800 and quality <= 50):
            return buf.getvalue()
        max_side = int(max_side * 0.8)
        quality = max(50, quality - 10)

def ocr_space(image_bytes, engine):
    resp = requests.post(
        OCR_SPACE_URL,
        headers={"apikey": OCR_SPACE_API_KEY},
        files={"file": ("notam.jpg", image_bytes, "image/jpeg")},
        data={
            "language": "eng",
            "OCREngine": str(engine),
            "scale": "true",
            "detectOrientation": "true",
            "isTable": "false",
        },
        timeout=25,
    )
    resp.raise_for_status()
    j = resp.json()
    if j.get("IsErroredOnProcessing"):
        raise RuntimeError(str(j.get("ErrorMessage")))
    return "\n".join(p.get("ParsedText", "") for p in j.get("ParsedResults", []))

# ============================================================
# PARSE NOTAM TEXT
# ============================================================
COORD_RE = re.compile(
    r'([\dOIL]{6}(?:[.,][\dOIL]+)?)\s*N\s*([\dOIL]{7}(?:[.,][\dOIL]+)?)\s*E'
)

def _fix_digits(s):
    return s.replace('O', '0').replace('I', '1').replace('L', '1').replace(',', '.')

def _fmt_alt(s):
    s = s.strip().strip(')').strip()
    m = re.match(r'(\d+)\s*FT\s*(AGL|AMSL)?', s)
    if m:
        return f"{int(m.group(1)):,} FT" + (f" {m.group(2)}" if m.group(2) else "")
    m = re.match(r'FL\s*(\d{3})', s)
    if m:
        return f"FL{m.group(1)}"
    return s  # GND, SFC, UNL ฯลฯ

def _fmt_dt(s):
    """'2606060200' -> ('06/06/26', '0200')"""
    return f"{s[4:6]}/{s[2:4]}/{s[0:2]}", s[6:10]

def parse_notam(text):
    t = text.upper().replace('\r', '\n')
    info = {}

    # ID  (J4757/26 NOTAMN) -> J4757
    m = re.search(r'([A-Z]\d{4})\s*/\s*\d{2}\s*NOTAM', t) or \
        re.search(r'\b([A-Z]\d{4})\s*/\s*\d{2}\b', t)
    info['id'] = m.group(1) if m else None

    # วันที่/เวลา  B) YYMMDDHHMM  C) YYMMDDHHMM
    mb = re.search(r'B[\)\]]\s*(\d{10})', t)
    mc = re.search(r'C[\)\]]\s*(\d{10}|PERM)', t)
    if not mb:
        nums = re.findall(r'\b(\d{10})\b', t)
        if nums:
            mb = re.match(r'(\d{10})', nums[0])
            if len(nums) > 1 and not mc:
                mc = re.match(r'(\d{10})', nums[1])
    info['start'] = mb.group(1) if mb else None
    info['end'] = mc.group(1) if mc else None

    # Q-line fallback:  /000/100/1723N10210E003
    mq = re.search(r'/(\d{3})/(\d{3})/(\d{4}[NS]\d{5}[EW])(\d{3})', t)

    # radius
    m = re.search(r'RADIUS\s*([\dO]+(?:[.,][\dO]+)?)\s*NM', t)
    if m:
        info['radius'] = float(_fix_digits(m.group(1)))
    elif mq:
        info['radius'] = float(int(mq.group(4)))
    else:
        info['radius'] = None

    # พิกัด (ใช้จุดแรกที่เจอ)
    info['coord'] = None
    m = COORD_RE.search(t)
    if m:
        info['coord'] = make_coord(_fix_digits(m.group(1)), _fix_digits(m.group(2)))

    # ความสูง  F) lower  G) upper
    m = re.search(r'F\)\s*([\s\S]{1,40}?)\s*[G6]\)\s*([\s\S]{1,40}?)(?:\)|\n|$)', t)
    if m:
        info['alt'] = f"{_fmt_alt(m.group(1))} - {_fmt_alt(m.group(2))}"
    elif mq:
        lo, up = int(mq.group(1)), int(mq.group(2))
        lo_txt = "GND" if lo == 0 else f"{lo * 100:,} FT"
        info['alt'] = f"{lo_txt} - {up * 100:,} FT"
    else:
        info['alt'] = None

    return info

# ============================================================
# FORMAT REPLY
# ============================================================
MEDALS = ["🔴", "🟠", "🟡"]
RANKS  = ["1st", "2nd", "3rd"]

def station_lines(top3):
    return [
        f"{MEDALS[i]} {RANKS[i]}: {name} ({region}) -> {prefix}-{radial:03d} / D-{dist} NM"
        for i, (name, region, prefix, radial, dist) in enumerate(top3)
    ]

def format_reply(tlat, tlon, top3):
    return "\n".join([f"🎯 COORD: {tlat}N / {tlon}E"] + station_lines(top3))

def format_notam_reply(info, top3):
    NA = "อ่านไม่ได้"
    if info['start']:
        d1, t1 = _fmt_dt(info['start'])
        if info['end'] and info['end'].isdigit():
            d2, t2 = _fmt_dt(info['end'])
            when = f"{d1} {t1}-{t2} UTC" if d1 == d2 else f"{d1} {t1} - {d2} {t2} UTC"
        elif info['end']:
            when = f"{d1} {t1} - {info['end']}"
        else:
            when = f"{d1} {t1}-?"
    else:
        when = NA

    r = info['radius']
    radius = (f"{r:g} NM") if r is not None else NA
    tlat, tlon = info['coord']

    lines = [
        f"📄 NOTAM ID: {info['id'] or NA}",
        f"📅 วันที่/เวลา: {when}",
        f"⭕ Radius: {radius}",
        f"🎯 COORD: {tlat}N / {tlon}E",
    ] + station_lines(top3) + [
        f"⬆️ ความสูง: {info['alt'] or NA}",
    ]
    return "\n".join(lines)

ERROR_MSG = (
    "⚠️ WRONG FORMAT!\n"
    "กรุณาใส่พิกัดให้ถูกต้องตามรูปแบบดังนี้ครับ:\n\n"
    "💡 แบบเว้นวรรค (พิมพ์ง่ายสุด):\n"
    "เช่น 172314 1020953\n"
    "หรือมีทศนิยม: 172314.10 1020953.90\n\n"
    "📋 แบบคัดลอก NOTAM:\n"
    "เช่น 172314N 1020953E\n\n"
    "⚠️ ข้อควรระวัง: นำหน้าด้วย Lat (6 หลัก) เสมอ "
    "และตามด้วย Lon (7 หลัก) ห้ามสลับกันครับ\n\n"
    "📷 หรือส่งรูปกระดาษ NOTAM มาให้อ่านอัตโนมัติก็ได้ครับ"
)

OCR_FAIL_MSG = (
    "⚠️ อ่านพิกัดจากรูปไม่สำเร็จ\n"
    "ลองถ่ายใหม่ให้ตัวหนังสือชัด ตรง และเห็นครบทั้งใบ "
    "หรือพิมพ์พิกัดเองตามรูปแบบ เช่น 172314 1020953"
)

OCR_ERROR_MSG = "⚠️ ระบบอ่านรูป (OCR) ขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้ง หรือพิมพ์พิกัดเองครับ"

# ============================================================
# PING / HEALTH CHECK — สำหรับใช้กับ cron-job.org หรือ UptimeRobot
# กัน Render free tier sleep
# ============================================================
@app.route("/", methods=["GET"])
@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200

# ============================================================
# WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    coord = parse_coord(text)

    if coord:
        tlat, tlon = coord
        top3 = find_top3(tlat, tlon)
        reply = format_reply(tlat, tlon, top3)
    else:
        reply = ERROR_MSG

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    try:
        content = line_bot_api.get_message_content(event.message.id)
        raw = b"".join(content.iter_content())
        img_bytes = prepare_image(raw)

        info = None
        for engine in (2, 1):  # ลอง Engine 2 ก่อน ถ้าไม่ได้พิกัดค่อยลอง Engine 1
            text = ocr_space(img_bytes, engine)
            print(f"[OCR engine {engine}]\n{text}\n[/OCR]", flush=True)
            parsed = parse_notam(text)
            if info is None or (parsed['coord'] and not info['coord']):
                info = parsed
            if parsed['coord']:
                break

        if info and info['coord']:
            tlat, tlon = info['coord']
            reply = format_notam_reply(info, find_top3(tlat, tlon))
        else:
            reply = OCR_FAIL_MSG
    except Exception as e:
        print(f"[OCR error] {e!r}", flush=True)
        reply = OCR_ERROR_MSG

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
