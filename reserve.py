"""
망상 오토캠핑리조트 자동 예약
- 타겟 날짜(7/22 이후)에 타겟 방 발견 시 → 선점 → 예약정보 제출 → 예약 완료
- 취소중(검은색) 상태는 선점은 되나 최종 예약 거부됨 → 응답 검증으로 구분
- cron-job.org가 5분마다 트리거 → 2시간 동안 자연스럽게 재시도
- 예약 성공 시 텔레그램 알림 + 중복 예약 방지(성공 기록)

⚠️ 실제 예약을 수행하는 스크립트입니다. 모니터링용 check.py와 별도.
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

# ── 암호화 ────────────────────────────────────────────────────
PASS_SALT      = "97f2fde29cd4493f199c2f3e9b7df120"
PASS_IV        = "4c1f89c42e9f06036385e90aadd7389f"
PASS_PHRASE    = "v4.0"
PASS_ITERATION = 1000

def op_encrypt(plain_text: str) -> str:
    salt = binascii.unhexlify(PASS_SALT)
    iv   = binascii.unhexlify(PASS_IV)
    key  = PBKDF2(PASS_PHRASE.encode(), salt, dkLen=16, count=PASS_ITERATION,
                  prf=lambda p, s: hmac.new(p, s, hashlib.sha1).digest())
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plain_text.encode(), AES.block_size))
    return base64.b64encode(ct).decode()

# ── 설정 ──────────────────────────────────────────────────────
BASE_URL = "https://www.campingkorea.or.kr"
TRRSRT   = "1000"

USER_ID  = os.environ.get("CK_ID", "")   # 예약 계정 (apfldidy)
USER_PW  = os.environ.get("CK_PW", "")

# 예약정보 입력 필드 (BD_reservationInfo.do)
EMGNC_CTTPC = "01074607811"   # 비상연락처
RSVCTM_AREA = "1001"          # 거주지역: 서울특별시

# 타겟 날짜: 7/22 이후 (22 포함) ~ 7/31, 1박
TARGET_DATES = [
    (date(2026, 7, d), date(2026, 7, d) + timedelta(days=1))
    for d in range(22, 32)
]

CATEGORIES = {
    "db": {"name": "든바다",   "fcltyCode": "1300", "resveNoCode": "MA"},
    "nb": {"name": "난바다",   "fcltyCode": "1400", "resveNoCode": "MB"},
    "hb": {"name": "허허바다", "fcltyCode": "1500", "resveNoCode": "MB"},
}

# 자동 예약 타겟 방 (숫자만)
TARGET_ROOMS = {
    "db": ["109", "116", "103",        # 1순위
           "112", "115", "119",        # 2순위
           "121", "123", "120", "122"], # 3순위
    "nb": ["105", "108", "112", "104"], # 1순위
    "hb": ["104"],                      # 허허바다 104호만
}

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

# 예약 성공 기록 (한 번 성공하면 다시 시도 안 함)
DONE_LOG = "reserved_log.json"

# ── 테스트 모드 ───────────────────────────────────────────────
# TEST_DATE(예: 2026-07-07) + TEST_ROOM(예: NF106) 지정 시
# 해당 방으로만 선점→예약 시도하고 모든 응답 원문을 텔레그램 전송
TEST_DATE   = os.environ.get("TEST_DATE", "").strip()
TEST_ROOM   = os.environ.get("TEST_ROOM", "").strip()
TEST_NIGHTS = int(os.environ.get("TEST_NIGHTS", "1"))

LAST_PREOCPC_RAW = ""  # 선점 실패 응답 보관(디버그용)
# ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": BASE_URL,
    "Referer": BASE_URL,
})


def login() -> bool:
    SESSION.get(f"{BASE_URL}/", timeout=10)
    SESSION.get(f"{BASE_URL}/login/BD_loginForm.do", timeout=10)
    enc_pw = op_encrypt(USER_PW)
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/login/BD_loginForm.do",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
    })
    resp = SESSION.post(
        f"{BASE_URL}/login/ND_loginAction.do",
        data={"returnUrl": f"{BASE_URL}/index.do", "userId": USER_ID, "userPassword": enc_pw},
        timeout=15, allow_redirects=True,
    )
    if "USER_JSESSIONID" in dict(SESSION.cookies):
        print(f"  ✅ 로그인 성공: {USER_ID}")
        return True
    print(f"  ❌ 로그인 실패: {resp.text[:200]}")
    return False


def fetch_rooms(cat_key: str, begin_de: str, end_de: str) -> list:
    """예약가능(resveAt=Y) 방 목록 조회"""
    cat = CATEGORIES[cat_key]
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    try:
        resp = SESSION.post(
            f"{BASE_URL}/user/reservation/ND_selectChildFcltyList.do",
            data={"trrsrtCode": TRRSRT, "fcltyCode": cat["fcltyCode"],
                  "resveNoCode": cat["resveNoCode"], "resveBeginDe": begin_de,
                  "resveEndDe": end_de},
            timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"    [{cat['name']}] 조회 오류: {e}")
        return []

    if not data.get("result"):
        return []

    rooms = []
    for f in data.get("value", {}).get("childFcltyList", []):
        if f.get("resveAt") == "Y":
            rooms.append({
                "fcltyCode":   f["fcltyCode"],
                "fcltyTyCode": f.get("fcltyTyCode", ""),
                "resveNoCode": cat["resveNoCode"],
            })
    return rooms


def is_target_room(cat_key: str, fclty_code: str) -> bool:
    num = re.sub(r"\D", "", fclty_code)
    return num in TARGET_ROOMS.get(cat_key, [])


def preoccupy(room: dict, begin_de: str, end_de: str):
    """선점 시도 → 성공 시 선점 데이터(dict) 반환, 실패 시 None"""
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        resp = SESSION.post(
            f"{BASE_URL}/user/reservation/ND_insertPreocpc.do",
            data={"trrsrtCode": TRRSRT, "fcltyCode": room["fcltyCode"],
                  "resveNoCode": room["resveNoCode"], "resveBeginDe": begin_de,
                  "resveEndDe": end_de},
            timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"    선점 오류: {e}")
        return None

    if data.get("preocpcTf") is True:
        print(f"    ★ 선점 성공: {room['fcltyCode']} (resveNo={data.get('resveNo')})")
        return data
    # 디버그용: 실패 응답도 전역에 저장
    global LAST_PREOCPC_RAW
    LAST_PREOCPC_RAW = json.dumps(data, ensure_ascii=False)[:400]
    return None


def submit_reservation(room: dict, preocpc: dict, begin_de: str, end_de: str):
    """예약정보 제출 (ND_insertresve.do) → (성공여부, 응답텍스트)"""
    SESSION.headers.update({
        "Referer": f"{BASE_URL}/user/reservation/BD_reservationInfo.do",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })

    # 선점 응답에서 받은 값 우선 사용, 없으면 조회값 폴백
    fclty_code   = preocpc.get("fcltyCode",   room["fcltyCode"])
    fclty_ty     = preocpc.get("fcltyTyCode", room["fcltyTyCode"])
    preocpc_code = preocpc.get("preocpcFcltyCode", fclty_code)
    resve_no_cd  = preocpc.get("resveNoCode", room["resveNoCode"])
    resve_no     = preocpc.get("resveNo", "")

    payload = {
        "trrsrtCode":        TRRSRT,
        "fcltyCode":         fclty_code,
        "fcltyTyCode":       fclty_ty,
        "preocpcFcltyCode":  preocpc_code,
        "resveNoCode":       resve_no_cd,
        "resveBeginDe":      begin_de,
        "resveEndDe":        end_de,
        "resveNo":           resve_no,
        "registerId":        USER_ID,
        "encptEmgncCttpc":   EMGNC_CTTPC,
        "rsvctmArea":        RSVCTM_AREA,
        "dspsnFcltyUseAt":   "N",
    }
    # entrceDelayCode는 선점 응답에 있으면 포함
    if preocpc.get("entrceDelayCode"):
        payload["entrceDelayCode"] = preocpc["entrceDelayCode"]

    try:
        resp = SESSION.post(
            f"{BASE_URL}/user/reservation/ND_insertresve.do",
            data=payload, timeout=15)
        text = resp.text.strip()
    except Exception as e:
        return False, f"제출 오류: {e}", payload

    # 성공/실패 판정
    fail_words = ["불가능", "다시 예약", "예약가능시설로 변경", "실패", "오류",
                  "문구", "captcha", "캡차", "캡챠", "방지"]
    if any(w in text for w in fail_words):
        return False, text[:400], payload

    try:
        data = json.loads(text)
        if data.get("result") in (True, "true", "Y", "success") or data.get("resveNo"):
            return True, text[:400], payload
        if data.get("result") in (False, "false", "N"):
            return False, text[:400], payload
    except Exception:
        pass

    return True, text[:400], payload


def load_done() -> dict:
    try:
        with open(DONE_LOG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_done(log: dict):
    with open(DONE_LOG, "w") as f:
        json.dump(log, f, ensure_ascii=False)


def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[텔레그램 미설정]\n", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10).raise_for_status()
        print("  텔레그램 전송 완료!")
    except Exception as e:
        print(f"  텔레그램 전송 실패: {e}")


def run_test():
    """테스트: TEST_ROOM 한 방으로 선점→예약 시도, 모든 응답 텔레그램 전송"""
    now_str = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str} KST] 🧪 테스트 모드: {TEST_DATE} {TEST_ROOM} ({TEST_NIGHTS}박)")

    if not login():
        send_telegram("🧪 테스트 실패: 로그인 안 됨")
        return

    begin_dt = datetime.strptime(TEST_DATE, "%Y-%m-%d").date()
    end_dt   = begin_dt + timedelta(days=TEST_NIGHTS)
    begin_str = begin_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    target_num = re.sub(r"\D", "", TEST_ROOM)  # 숫자만 (접두사 NG/NF 무관)

    report = [f"🧪 <b>예약 테스트</b>  ⏰ {now_str}",
              f"📅 {begin_str}~{end_str} / 🏕️ {TEST_ROOM} (숫자={target_num})", ""]

    # 3개 카테고리 전부 조회해서 숫자로 방 찾기 (접두사 그때그때 바뀜)
    room = None
    cat_key = None
    for ck in CATEGORIES:
        rooms = fetch_rooms(ck, begin_str, end_str)
        found = next((r for r in rooms if re.sub(r"\D", "", r["fcltyCode"]) == target_num), None)
        if found:
            room = found
            cat_key = ck
            break

    if not room:
        report.append("❌ 3개 카테고리에서 해당 번호 방 못 찾음 (예약가능 상태 아님)")
        send_telegram("\n".join(report))
        return

    report.append(f"1️⃣ 방 조회 OK: {room['fcltyCode']} ({CATEGORIES[cat_key]['name']}, ty={room['fcltyTyCode']})")

    # 선점
    preocpc = preoccupy(room, begin_str, end_str)
    if not preocpc:
        report.append(f"2️⃣ ❌ 선점 실패\n응답: <code>{LAST_PREOCPC_RAW}</code>")
        send_telegram("\n".join(report))
        return

    report.append(f"2️⃣ ★ 선점 성공! resveNo={preocpc.get('resveNo')}")
    report.append(f"   선점응답키: {', '.join(preocpc.keys())}")

    # 예약 제출
    ok, resp_text, payload = submit_reservation(room, preocpc, begin_str, end_str)
    report.append("")
    report.append(f"3️⃣ 예약제출 결과: {'✅ 성공' if ok else '❌ 실패/거부'}")
    report.append(f"📤 보낸값: <code>{json.dumps(payload, ensure_ascii=False)[:350]}</code>")
    report.append(f"📥 응답: <code>{resp_text}</code>")

    if ok:
        report.append("\n🎉 캡차 없이 예약 제출 통과! 무인화 가능!")
    else:
        report.append("\n⚠️ 거부됨 — 응답 내용으로 캡차 필요 여부 판단")

    send_telegram("\n".join(report))
    print("테스트 완료, 텔레그램 전송함")


def main():
    # 테스트 모드 우선
    if TEST_DATE and TEST_ROOM:
        run_test()
        return

    now_str = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str} KST] 망상 자동 예약 시작...")

    if not USER_ID or not USER_PW:
        print("❌ CK_ID / CK_PW 없음")
        return

    if not login():
        return

    done = load_done()

    for begin_dt, end_dt in TARGET_DATES:
        begin_str = begin_dt.strftime("%Y-%m-%d")
        end_str   = end_dt.strftime("%Y-%m-%d")

        for cat_key, cat in CATEGORIES.items():
            rooms = fetch_rooms(cat_key, begin_str, end_str)
            targets = [r for r in rooms if is_target_room(cat_key, r["fcltyCode"])]
            if not targets:
                continue

            for room in targets:
                done_key = f"{begin_str}_{room['fcltyCode']}"
                if done_key in done:
                    print(f"    이미 예약됨: {done_key} → skip")
                    continue

                print(f"  🎯 타겟 발견: {begin_str} [{cat['name']}] {room['fcltyCode']}")

                # 1. 선점
                preocpc = preoccupy(room, begin_str, end_str)
                if not preocpc:
                    print(f"    선점 실패 (이미 선점됨/취소중 아님) → 다음 기회에")
                    continue

                # 2. 예약정보 제출
                ok, resp_text, _payload = submit_reservation(room, preocpc, begin_str, end_str)

                if ok:
                    done[done_key] = {
                        "date": begin_str, "room": room["fcltyCode"],
                        "cat": cat["name"], "at": now_str,
                    }
                    save_done(done)
                    msg = (
                        "🎉🎉 <b>망상 예약 성공!!</b> 🎉🎉\n\n"
                        f"📅 {begin_str} ~ {end_str}\n"
                        f"🏕️ {cat['name']} <b>{room['fcltyCode']}</b>\n"
                        f"👤 {USER_ID}\n"
                        f"⏰ {now_str} (KST)\n\n"
                        f"👉 <a href=\"{BASE_URL}/user/mypage/BD_myReservationList.do\">예약 확인</a>"
                    )
                    send_telegram(msg)
                    print(f"  ✅✅ 예약 성공! {done_key}")
                    return  # 하나 성공하면 종료
                else:
                    print(f"    ❌ 예약 거부 (취소중 추정): {resp_text[:100]}")
                    # 취소중이면 아직 안 풀린 것 → 다음 트리거에 재시도

    print("이번 회차 예약 성공 없음 (재시도 대기)")


if __name__ == "__main__":
    main()
