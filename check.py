"""
망상 오토캠핑리조트 취소자리 알림
- 대상: 2026년 7월 전체 (1박 기준으로 각 날짜 체크)
- 든바다 / 난바다 / 허허바다 예약가능 방 감지
- 선호 방 매칭 시 텔레그램 알림
- 중복 알림 방지: 같은 날짜+같은 방은 1시간 내 재알림 없음
"""

import os
import re
import json
import hashlib
import requests
from datetime import date, timedelta, datetime

# ── 설정 ──────────────────────────────────────────────────────
BASE_URL = "https://www.campingkorea.or.kr"
TRRSRT   = "1000"

# 7월 전체 날짜 (체크인 기준, 1박)
TARGET_DATES = [
    (date(2026, 7, d), date(2026, 7, d) + timedelta(days=1))
    for d in range(1, 32)
]

CATEGORIES = {
    "db": {"name": "든바다",   "fcltyCode": "1300", "resveNoCode": "MA"},
    "nb": {"name": "난바다",   "fcltyCode": "1400", "resveNoCode": "MB"},
    "hb": {"name": "허허바다", "fcltyCode": "1500", "resveNoCode": "MB"},
}

# 선호 방 번호 (숫자만, v5.8 기준)
PREFERRED = {
    "db": {
        "1순위": ["109", "116", "103"],
        "2순위": ["112", "115", "119"],
        "3순위": ["121", "123", "120", "122"],
    },
    "nb": {
        "1순위": ["105"],
        "2순위": ["108", "112", "104"],
    },
    "hb": {
        "1순위": ["104", "105"],
    },
}

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT", "")

# 중복 방지: 이미 알림 보낸 (날짜+카테고리+방코드) 세트를 파일로 저장
SENT_LOG  = "sent_log.json"
DEDUP_TTL = 3600  # 1시간 내 같은 방 재알림 없음 (초)
# ──────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/user/reservation/BD_reservationReq.do",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
})


def warm_session():
    try:
        SESSION.get(f"{BASE_URL}/", timeout=10)
        SESSION.get(f"{BASE_URL}/user/reservation/BD_reservation.do", timeout=10)
    except Exception as e:
        print(f"  워밍업 실패 (계속 진행): {e}")


def fetch_rooms(cat_key: str, begin_de: str, end_de: str) -> list:
    cat = CATEGORIES[cat_key]
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


# ── 중복 방지 로직 ────────────────────────────────────────────
def load_sent_log() -> dict:
    try:
        with open(SENT_LOG, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sent_log(log: dict):
    with open(SENT_LOG, "w") as f:
        json.dump(log, f)


def make_key(date_str: str, cat_key: str, fclty_code: str) -> str:
    return f"{date_str}_{cat_key}_{fclty_code}"


def is_already_sent(log: dict, key: str) -> bool:
    ts = log.get(key)
    if ts is None:
        return False
    return (datetime.now().timestamp() - ts) < DEDUP_TTL


def mark_sent(log: dict, key: str):
    log[key] = datetime.now().timestamp()


# ── 텔레그램 ──────────────────────────────────────────────────
def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[텔레그램 미설정]\n", message)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TG_CHAT,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    resp.raise_for_status()
    print(f"  텔레그램 전송 완료 (id={resp.json()['result']['message_id']})")


# ── 메인 ──────────────────────────────────────────────────────
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] 망상 오토캠핑 잔여현황 확인 시작...")

    warm_session()
    sent_log = load_sent_log()

    # 날짜별 체크
    new_alerts = {}   # {date_str: {cat_key: [방dict, ...]}}

    for begin_dt, end_dt in TARGET_DATES:
        begin_str = begin_dt.strftime("%Y-%m-%d")
        end_str   = end_dt.strftime("%Y-%m-%d")

        day_hits = {}
        for cat_key in CATEGORIES:
            rooms = fetch_rooms(cat_key, begin_str, end_str)
            if not rooms:
                continue

            # 중복 제거: 이미 알림 보낸 방은 제외
            new_rooms = []
            for r in rooms:
                key = make_key(begin_str, cat_key, r["fcltyCode"])
                if not is_already_sent(sent_log, key):
                    new_rooms.append(r)

            if new_rooms:
                day_hits[cat_key] = new_rooms

        if day_hits:
            new_alerts[begin_str] = day_hits
            cats_found = [CATEGORIES[k]["name"] for k in day_hits]
            print(f"  ✅ {begin_str}: 신규 잔여! {cats_found}")
        else:
            print(f"  - {begin_str}: 없음 (또는 이미 알림됨)")

    if not new_alerts:
        print("신규 잔여 없음")
        return

    # 텔레그램 메시지 구성
    lines = [f"🏕️ <b>망상 오토캠핑 취소자리 발생!</b>  ⏰ {now_str}\n"]

    has_preferred_global = False
    for date_str, cats in sorted(new_alerts.items()):
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"📅 <b>{date_str}</b>")
        for cat_key, rooms in cats.items():
            cat_name = CATEGORIES[cat_key]["name"]
            room_parts = []
            has_preferred = False
            for r in rooms:
                code = r["fcltyCode"]
                rank = get_priority(cat_key, code)
                if rank:
                    room_parts.append(f"<b>{code}[{rank}]⭐</b>")
                    has_preferred = True
                    has_preferred_global = True
                else:
                    room_parts.append(code)
            prefix = "★ " if has_preferred else "  "
            lines.append(f"{prefix}[{cat_name}] {', '.join(room_parts)}")

    lines.append(f"━━━━━━━━━━━━━━━━━━")
    if has_preferred_global:
        lines.append("⚡ <b>선호 방 포함! 지금 바로 예약하세요!</b>")
    lines.append(f'👉 <a href="{BASE_URL}/user/reservation/BD_reservation.do">예약 페이지</a>')

    send_telegram("\n".join(lines))

    # 알림 보낸 방 기록
    for date_str, cats in new_alerts.items():
        for cat_key, rooms in cats.items():
            for r in rooms:
                key = make_key(date_str, cat_key, r["fcltyCode"])
                mark_sent(sent_log, key)

    # 오래된 로그 정리 (24시간 초과)
    cutoff = datetime.now().timestamp() - 86400
    sent_log = {k: v for k, v in sent_log.items() if v > cutoff}
    save_sent_log(sent_log)

    print(f"완료! {len(new_alerts)}개 날짜 알림 전송")


if __name__ == "__main__":
    main()
