"""
망상 오토캠핑리조트 취소자리 알림
- 자동 로그인 (PBKDF2+AES128 암호화)
- 7월 전체 날짜 체크
- 선호 방 매칭 시 텔레그램 알림
- 중복 알림 방지 (1시간)
"""

import os, re, json, base64, binascii, hmac, hashlib
import requests
from datetime import date, timedelta, datetime, timezone
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Util.Padding import pad

KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

# ── 암호화 설정 (openworks.password.js) ───────────────────────
PASS_SALT      = "97f2fde29cd4493f199c2f3e9b7df120"
PASS_IV        = "4c1f89c42e9f06036385e90aadd7389f"
PASS_PHRASE    = "v4.0"
PASS_ITERATION = 1000

def op_encrypt(plain_text: str) -> str:
    salt = binascii.unhexlify(PASS_SALT)
    iv   = binascii.unhexlify(PASS_IV)
    key  = PBKDF2(
        PASS_PHRASE.encode('utf-8'), salt, dkLen=16, count=PASS_ITERATION,
        prf=lambda p, s: hmac.new(p, s, hashlib.sha1).digest()
    )
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plain_text.encode('utf-8'), AES.block_size))
    return base64.b64encode(ct).decode('utf-8')

# ── 설정 ──────────────────────────────────────────────────────
BASE_URL  = "https://www.campingkorea.or.kr"
TRRSRT    = "1000"

USER_ID   = os.environ.get("CK_ID", "")
USER_PW   = os.environ.get("CK_PW", "")  # 평문 비번 → 자동 암호화

TARGET_DATES = [
    (date(2026, 7, d), date(2026, 7, d) + timedelta(days=1))
    for d in range(1, 32)
]

CATEGORIES = {
    "db": {"name": "든바다",   "fcltyCode": "1300", "resveNoCode": "MA"},
    "nb": {"name": "난바다",   "fcltyCode": "1400", "resveNoCode": "MB"},
    "hb": {"name": "허허바다", "fcltyCode": "1500", "resveNoCode": "MB"},
}

PREFERRED = {
    "db": {
        "1순위": ["109", "116", "103"],
        "2순위": ["112", "115", "119"],
        "3순위": ["121", "123", "120", "122"],
    },
    "nb": {
        "1순위": ["105", "108", "112", "104"],
        "2순위": ["107", "111", "103"],
    },
    "hb": {
        "1순위": ["104", "105", "107", "106"],
    },
}

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")
SENT_LOG = "sent_log.json"
DEDUP_TTL = 3600
# ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL,
})


def login() -> bool:
    """캠핑코리아 자동 로그인"""
    # 1. 메인 페이지 먼저 방문 (쿠키 초기화)
    SESSION.get(f"{BASE_URL}/", timeout=10)
    SESSION.get(f"{BASE_URL}/login/BD_loginForm.do", timeout=10)

    # 2. 비번 암호화
    enc_pw = op_encrypt(USER_PW)
    print(f"  로그인 시도: {USER_ID}")

    # 3. 로그인 POST
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/login/BD_loginForm.do",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
    })
    resp = SESSION.post(
        f"{BASE_URL}/login/ND_loginAction.do",
        data={
            "returnUrl": f"{BASE_URL}/index.do",
            "userId": USER_ID,
            "userPassword": enc_pw,
        },
        timeout=15,
        allow_redirects=True,
    )

    # 4. 로그인 성공 여부 확인
    # 세션 쿠키에 USER_JSESSIONID 있으면 성공
    cookies = dict(SESSION.cookies)
    if "USER_JSESSIONID" in cookies:
        print(f"  ✅ 로그인 성공!")
        return True

    # 응답 body로도 확인
    try:
        data = resp.json()
        if data.get("result") == "success" or data.get("loginYn") == "Y":
            print(f"  ✅ 로그인 성공! (JSON)")
            return True
    except Exception:
        pass

    # 리다이렉트 후 index.do 도달 확인
    if "index.do" in resp.url or resp.status_code == 200:
        # 한번 더 확인 - 마이페이지 접근 가능한지
        my = SESSION.get(f"{BASE_URL}/user/mypage/BD_myPage.do", timeout=10)
        if "로그인" not in my.text[:500]:
            print(f"  ✅ 로그인 성공! (마이페이지 확인)")
            return True

    print(f"  ❌ 로그인 실패 (status={resp.status_code})")
    print(f"  응답: {resp.text[:200]}")
    return False


def fetch_rooms(cat_key: str, begin_de: str, end_de: str) -> list:
    cat = CATEGORIES[cat_key]
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    url = f"{BASE_URL}/user/reservation/ND_selectChildFcltyList.do"
    payload = {
        "trrsrtCode":   TRRSRT,
        "fcltyCode":    cat["fcltyCode"],
        "resveNoCode":  cat["resveNoCode"],
        "resveBeginDe": begin_de,
        "resveEndDe":   end_de,
    }
    try:
        resp = SESSION.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"    [{cat['name']}] API 오류: {e}")
        return []

    if not data.get("result"):
        return []

    available = []
    for f in data.get("value", {}).get("childFcltyList", []):
        if f.get("resveAt") == "Y":
            available.append({
                "fcltyCode":   f["fcltyCode"],
                "fcltyTyCode": f.get("fcltyTyCode", ""),
                "resveNoCode": cat["resveNoCode"],
            })
    return available


def get_priority(cat_key: str, fclty_code: str):
    num = re.sub(r"\D", "", fclty_code)
    for rank, nums in PREFERRED.get(cat_key, {}).items():
        if num in nums:
            return rank
    return None


def load_sent_log() -> dict:
    try:
        with open(SENT_LOG, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sent_log(log: dict):
    cutoff = datetime.now().timestamp() - 86400
    log = {k: v for k, v in log.items() if v > cutoff}
    with open(SENT_LOG, "w") as f:
        json.dump(log, f)


def is_already_sent(log: dict, key: str) -> bool:
    ts = log.get(key)
    return ts is not None and (datetime.now().timestamp() - ts) < DEDUP_TTL


def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[텔레그램 미설정]\n", message)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=10
    )
    resp.raise_for_status()
    print(f"  텔레그램 전송 완료!")


def main():
    now_str = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str} KST] 망상 오토캠핑 잔여현황 확인 시작...")

    if not USER_ID or not USER_PW:
        print("❌ CK_ID / CK_PW 환경변수 없음!")
        return

    if not login():
        print("❌ 로그인 실패로 종료")
        return

    sent_log = load_sent_log()
    new_alerts = {}

    for begin_dt, end_dt in TARGET_DATES:
        begin_str = begin_dt.strftime("%Y-%m-%d")
        end_str   = end_dt.strftime("%Y-%m-%d")

        day_hits = {}
        for cat_key in CATEGORIES:
            rooms = fetch_rooms(cat_key, begin_str, end_str)
            if not rooms:
                continue
            new_rooms = [
                r for r in rooms
                if not is_already_sent(sent_log, f"{begin_str}_{cat_key}_{r['fcltyCode']}")
            ]
            if new_rooms:
                day_hits[cat_key] = new_rooms

        if day_hits:
            new_alerts[begin_str] = day_hits
            print(f"  ✅ {begin_str}: 신규 잔여! {[CATEGORIES[k]['name'] for k in day_hits]}")
        else:
            print(f"  - {begin_str}: 없음")

    if not new_alerts:
        print("신규 잔여 없음")
        return

    # 텔레그램 메시지
    lines = [f"🏕️ <b>망상 오토캠핑 취소자리 발생!</b>  ⏰ {now_str} (KST)\n"]
    has_preferred_global = False

    for date_str, cats in sorted(new_alerts.items()):
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📅 <b>{date_str}</b>")
        for cat_key, rooms in cats.items():
            parts = []
            has_preferred = False
            for r in rooms:
                code = r["fcltyCode"]
                rank = get_priority(cat_key, code)
                if rank:
                    parts.append(f"<b>{code}[{rank}]⭐</b>")
                    has_preferred = True
                    has_preferred_global = True
                else:
                    parts.append(code)
            prefix = "★ " if has_preferred else "  "
            lines.append(f"{prefix}[{CATEGORIES[cat_key]['name']}] {', '.join(parts)}")

    lines.append("━━━━━━━━━━━━━━━━━━")
    if has_preferred_global:
        lines.append("⚡ <b>선호 방 포함! 지금 바로 예약하세요!</b>")
    lines.append(f'👉 <a href="{BASE_URL}/user/reservation/BD_reservation.do">예약 페이지</a>')

    send_telegram("\n".join(lines))

    # 알림 기록 저장
    for date_str, cats in new_alerts.items():
        for cat_key, rooms in cats.items():
            for r in rooms:
                key = f"{date_str}_{cat_key}_{r['fcltyCode']}"
                sent_log[key] = datetime.now().timestamp()
    save_sent_log(sent_log)
    print(f"완료! {len(new_alerts)}개 날짜 알림 전송")


if __name__ == "__main__":
    main()
