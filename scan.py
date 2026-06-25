"""
망상 오토캠핑 - 방 현황 스캔 (디버그/검증용)
- 6/26 ~ 7/25 각 날짜(1박 기준) 든/난/허 카테고리
- 🟢 빈방(resveAt=Y & preocpcYn=N) / ⚫ 취소중(resveAt=Y & preocpcYn=Y)
- Actions 로그로 출력
"""

import os, re, base64, binascii, hmac, hashlib
import requests
from datetime import date, timedelta, datetime, timezone
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Util.Padding import pad

KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

PASS_SALT="97f2fde29cd4493f199c2f3e9b7df120"; PASS_IV="4c1f89c42e9f06036385e90aadd7389f"
PASS_PHRASE="v4.0"; PASS_ITERATION=1000
def op_encrypt(p):
    key=PBKDF2(PASS_PHRASE.encode(),binascii.unhexlify(PASS_SALT),dkLen=16,count=PASS_ITERATION,
               prf=lambda a,b:hmac.new(a,b,hashlib.sha1).digest())
    c=AES.new(key,AES.MODE_CBC,binascii.unhexlify(PASS_IV))
    return base64.b64encode(c.encrypt(pad(p.encode(),AES.block_size))).decode()

BASE_URL="https://www.campingkorea.or.kr"; TRRSRT="1000"
USER_ID=os.environ.get("CK_ID",""); USER_PW=os.environ.get("CK_PW","")

# 스캔 범위 (체크인 기준, 1박)
START = date(2026, 6, 26)
END   = date(2026, 7, 25)

CATEGORIES = {
    "db": {"name": "든바다",   "fcltyCode": "1300", "resveNoCode": "MA"},
    "nb": {"name": "난바다",   "fcltyCode": "1400", "resveNoCode": "MB"},
    "hb": {"name": "허허바다", "fcltyCode": "1500", "resveNoCode": "MB"},
}

# 선호(타겟) 방 표시용
TARGET_ROOMS = {
    "db": ["109","116","103","112","115","119","121","123","120","122"],
    "nb": ["105","108","112","104"],
    "hb": ["104"],
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language":"ko-KR,ko;q=0.9","Origin":BASE_URL,"Referer":BASE_URL,
})

def login():
    SESSION.get(f"{BASE_URL}/",timeout=10)
    SESSION.get(f"{BASE_URL}/login/BD_loginForm.do",timeout=10)
    SESSION.headers.update({"Referer":f"{BASE_URL}/login/BD_loginForm.do",
        "X-Requested-With":"XMLHttpRequest","Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Accept":"*/*"})
    SESSION.post(f"{BASE_URL}/login/ND_loginAction.do",
        data={"returnUrl":f"{BASE_URL}/index.do","userId":USER_ID,"userPassword":op_encrypt(USER_PW)},
        timeout=15,allow_redirects=True)
    return "USER_JSESSIONID" in dict(SESSION.cookies)

def scan(cat_key, begin_de, end_de):
    cat=CATEGORIES[cat_key]
    SESSION.headers.update({"Referer":f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Accept":"application/json, text/javascript, */*; q=0.01","X-Requested-With":"XMLHttpRequest",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"})
    try:
        r=SESSION.post(f"{BASE_URL}/user/reservation/ND_selectChildFcltyList.do",
            data={"trrsrtCode":TRRSRT,"fcltyCode":cat["fcltyCode"],"resveNoCode":cat["resveNoCode"],
                  "resveBeginDe":begin_de,"resveEndDe":end_de},timeout=10)
        data=r.json()
    except Exception as e:
        return None
    if not data.get("result"):
        return []
    out=[]
    for f in data.get("value",{}).get("childFcltyList",[]):
        if f.get("resveAt")=="Y":
            status = "free" if f.get("preocpcYn")=="N" else "cancel"
            out.append((f["fcltyCode"], status))
    return out

def is_target(cat_key, code):
    return re.sub(r"\D","",code) in TARGET_ROOMS.get(cat_key,[])

def main():
    print(f"[{now_kst().strftime('%Y-%m-%d %H:%M:%S')} KST] 방 현황 스캔")
    print(f"범위: {START} ~ {END} (1박 기준)")
    print("🟢=빈방  ⚫=취소중(둘다 선점가능)  ★=타겟방\n")

    if not login():
        print("❌ 로그인 실패"); return
    print("✅ 로그인 성공\n")
    print("="*70)

    d = START
    while d <= END:
        begin_str=d.strftime("%Y-%m-%d")
        end_str=(d+timedelta(days=1)).strftime("%Y-%m-%d")
        wd="월화수목금토일"[d.weekday()]

        day_lines=[]
        for ck in CATEGORIES:
            rooms=scan(ck, begin_str, end_str)
            if rooms is None:
                day_lines.append(f"  {CATEGORIES[ck]['name']}: (조회오류)")
                continue
            if not rooms:
                continue
            parts=[]
            for code,status in rooms:
                dot="🟢" if status=="free" else "⚫"
                star="★" if is_target(ck,code) else ""
                parts.append(f"{dot}{star}{code}")
            day_lines.append(f"  {CATEGORIES[ck]['name']}: {' '.join(parts)}")

        if day_lines:
            print(f"📅 {begin_str}({wd})")
            for ln in day_lines:
                print(ln)
        else:
            print(f"📅 {begin_str}({wd}): 가용 없음")
        d += timedelta(days=1)

    print("="*70)
    print("스캔 완료")

if __name__=="__main__":
    main()
