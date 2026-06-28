"""
망상 오토캠핑리조트 자동 예약 (멀티계정 + 날짜별 플랜)
- 날짜마다 (ID 우선순위, 방 등급 우선순위)를 PLAN에 정의
- 한 실행에서 각 날짜를 ID 우선순위 순으로 순차 로그인하며 시도
- 방 등급 우선순위대로 선점→예약, 연박은 체크인 날짜 기준 3박→2박→1박
- 이미 잡은 날(계정별 SKIP + 자동기록)은 숙박일 기준 침범 안 함
- 캡차 불필요(순수 API 경로)
"""

import os, re, json, base64, binascii, hmac, hashlib
import requests
from datetime import date, timedelta, datetime, timezone
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Util.Padding import pad

KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

# ── 암호화 ────────────────────────────────────────────────────
PASS_SALT="97f2fde29cd4493f199c2f3e9b7df120"; PASS_IV="4c1f89c42e9f06036385e90aadd7389f"
PASS_PHRASE="v4.0"; PASS_ITERATION=1000
def op_encrypt(p):
    key=PBKDF2(PASS_PHRASE.encode(),binascii.unhexlify(PASS_SALT),dkLen=16,count=PASS_ITERATION,
               prf=lambda a,b:hmac.new(a,b,hashlib.sha1).digest())
    c=AES.new(key,AES.MODE_CBC,binascii.unhexlify(PASS_IV))
    return base64.b64encode(c.encrypt(pad(p.encode(),AES.block_size))).decode()

# ── 기본 설정 ─────────────────────────────────────────────────
BASE_URL="https://www.campingkorea.or.kr"; TRRSRT="1000"
EMGNC_CTTPC="01074607811"   # 비상연락처
RSVCTM_AREA="1001"          # 거주지역: 서울특별시

# 계정 3개 (PW 공통)
ACCOUNTS = {
    "#1": os.environ.get("CK_ID_1",""),
    "#2": os.environ.get("CK_ID_2",""),
    "#3": os.environ.get("CK_ID_3",""),
}
COMMON_PW = os.environ.get("CK_PW","")

CATEGORIES = {
    "db": {"name":"든바다",   "fcltyCode":"1300", "resveNoCode":"MA"},
    "nb": {"name":"난바다",   "fcltyCode":"1400", "resveNoCode":"MB"},
    "hb": {"name":"허허바다", "fcltyCode":"1500", "resveNoCode":"MB"},
}

# 방 등급 정의: 등급명 -> (카테고리, 방번호 우선순위)
GRADE = {
    "든1":   ("db", ["109","116","103"]),
    "든2":   ("db", ["112","115","119"]),
    "든3":   ("db", ["121","123","120","122"]),
    "난1":   ("nb", ["105","108","112","104"]),
    "난2":   ("nb", ["107","111","103"]),
    "허1":   ("hb", ["104","105","107","106"]),
    "허104": ("hb", ["104"]),
    "난105": ("nb", ["105"]),
}

# 날짜별 플랜: 체크인날짜 -> (ID우선순위, 방등급 우선순위)
PLAN = {
    "2026-07-22": (["#1"],           ["든1","든2","허104"]),
    "2026-07-23": (["#2","#1"],      ["든1","든2","허104"]),
    "2026-07-24": (["#2"],           ["든1","든2","허104"]),
    "2026-07-25": (["#3","#2"],      ["든1","든2","허104","난105"]),
    "2026-07-26": (["#3","#2"],      ["든1","든2","허104","난105"]),
    "2026-07-27": (["#3","#2","#1"], ["든1","든2","든3","난1","난2","허1"]),
    "2026-07-28": (["#3","#2"],      ["든1","든2","허104","난105"]),
    "2026-07-29": (["#3","#2"],      ["든1","든2","허104","난105"]),
    "2026-07-30": (["#3","#2"],      ["든1","든2","허104","난105"]),
    "2026-07-31": (["#3","#2"],      ["든1","든2","허104","난105"]),
}

# 계정별 "이미 확보 / 건드리지 말 날짜" (숙박일 기준, 체크아웃 제외)
# 별칭(#1/#2/#3) 기준으로 관리
SKIP_DATES_BY_ACCT = {
    "#1": ["2026-07-19","2026-07-20","2026-07-21",   # 19~22 든121
           "2026-07-25","2026-07-26",                # 25~27 든121
           "2026-07-28","2026-07-29","2026-07-30"],  # 28~31 난112
    "#2": ["2026-07-22"],                            # 22~23 든121
    "#3": ["2026-07-23","2026-07-24"],               # 23~25 난108
}

TG_TOKEN=os.environ.get("TG_TOKEN",""); TG_CHAT=os.environ.get("TG_CHAT","")
DONE_LOG="reserved_log.json"

# 테스트 모드 (단일계정)
TEST_DATE=os.environ.get("TEST_DATE","").strip()
TEST_ROOM=os.environ.get("TEST_ROOM","").strip()
TEST_NIGHTS=int(os.environ.get("TEST_NIGHTS","1"))
TEST_ACCT=os.environ.get("TEST_ACCT","#1").strip()

LAST_PREOCPC_RAW=""
# ──────────────────────────────────────────────────────────────

def new_session():
    s=requests.Session()
    s.headers.update({
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language":"ko-KR,ko;q=0.9","Origin":BASE_URL,"Referer":BASE_URL,
    })
    return s


def login(session, user_id) -> bool:
    session.get(f"{BASE_URL}/",timeout=10)
    session.get(f"{BASE_URL}/login/BD_loginForm.do",timeout=10)
    session.headers.update({"Referer":f"{BASE_URL}/login/BD_loginForm.do",
        "X-Requested-With":"XMLHttpRequest","Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Accept":"*/*"})
    resp=session.post(f"{BASE_URL}/login/ND_loginAction.do",
        data={"returnUrl":f"{BASE_URL}/index.do","userId":user_id,"userPassword":op_encrypt(COMMON_PW)},
        timeout=15,allow_redirects=True)
    if "USER_JSESSIONID" in dict(session.cookies):
        return True
    print(f"    ❌ 로그인 실패({user_id}): {resp.text[:150]}")
    return False


def fetch_rooms(session, cat_key, begin_de, end_de) -> list:
    cat=CATEGORIES[cat_key]
    session.headers.update({"Referer":f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Accept":"application/json, text/javascript, */*; q=0.01","X-Requested-With":"XMLHttpRequest",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"})
    try:
        resp=session.post(f"{BASE_URL}/user/reservation/ND_selectChildFcltyList.do",
            data={"trrsrtCode":TRRSRT,"fcltyCode":cat["fcltyCode"],"resveNoCode":cat["resveNoCode"],
                  "resveBeginDe":begin_de,"resveEndDe":end_de},timeout=10)
        data=resp.json()
    except Exception as e:
        return []
    if not data.get("result"): return []
    rooms=[]
    for f in data.get("value",{}).get("childFcltyList",[]):
        if f.get("resveAt")=="Y":
            rooms.append({"fcltyCode":f["fcltyCode"],"fcltyTyCode":f.get("fcltyTyCode",""),
                          "resveNoCode":cat["resveNoCode"]})
    return rooms


def preoccupy(session, room, begin_de, end_de):
    global LAST_PREOCPC_RAW
    session.headers.update({"Referer":f"{BASE_URL}/user/reservation/BD_reservationReq.do",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Accept":"application/json, text/javascript, */*; q=0.01","X-Requested-With":"XMLHttpRequest"})
    try:
        resp=session.post(f"{BASE_URL}/user/reservation/ND_insertPreocpc.do",
            data={"trrsrtCode":TRRSRT,"fcltyCode":room["fcltyCode"],"resveNoCode":room["resveNoCode"],
                  "resveBeginDe":begin_de,"resveEndDe":end_de},timeout=10)
        data=resp.json()
    except Exception as e:
        print(f"    선점 오류: {e}"); return None
    if data.get("preocpcTf") is True:
        return data
    LAST_PREOCPC_RAW=json.dumps(data,ensure_ascii=False)[:300]
    return None


def submit_reservation(session, user_id, room, preocpc, begin_de, end_de):
    session.headers.update({"Referer":f"{BASE_URL}/user/reservation/BD_reservationInfo.do",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Accept":"application/json, text/javascript, */*; q=0.01","X-Requested-With":"XMLHttpRequest"})
    def pick(k,fb):
        v=preocpc.get(k); return v if v not in (None,"","null") else fb
    payload={
        "trrsrtCode":TRRSRT,
        "fcltyCode":pick("fcltyCode",room["fcltyCode"]),
        "fcltyTyCode":pick("fcltyTyCode",room["fcltyTyCode"]),
        "preocpcFcltyCode":pick("preocpcFcltyCode",pick("fcltyCode",room["fcltyCode"])),
        "resveNoCode":pick("resveNoCode",room["resveNoCode"]),
        "resveBeginDe":begin_de,"resveEndDe":end_de,
        "resveNo":pick("resveNo",""),
        "registerId":user_id,
        "encptEmgncCttpc":EMGNC_CTTPC,"rsvctmArea":RSVCTM_AREA,"dspsnFcltyUseAt":"N",
    }
    if preocpc.get("entrceDelayCode"):
        payload["entrceDelayCode"]=preocpc["entrceDelayCode"]
    try:
        resp=session.post(f"{BASE_URL}/user/reservation/ND_insertresve.do",data=payload,timeout=15)
        text=resp.text.strip()
    except Exception as e:
        return False,f"제출오류:{e}",payload
    fail_words=["불가능","다시 예약","예약가능시설로 변경","실패","오류","문구","captcha","캡차","캡챠","방지","존재"]
    if any(w in text for w in fail_words):
        return False,text[:300],payload
    try:
        data=json.loads(text)
        if data.get("result") in (True,"true","Y","success") or data.get("resveNo"):
            return True,text[:300],payload
        if data.get("result") in (False,"false","N"):
            return False,text[:300],payload
    except Exception:
        pass
    return True,text[:300],payload


def num_of(code): return re.sub(r"\D","",code)


def load_done():
    try:
        with open(DONE_LOG) as f: return json.load(f)
    except Exception: return {}

def save_done(log):
    cutoff=now_kst().timestamp()-30*86400
    log={k:v for k,v in log.items() if v.get("ts",9e18)>cutoff}
    with open(DONE_LOG,"w") as f: json.dump(log,f,ensure_ascii=False)


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("[텔레그램 미설정]\n",msg); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True},
            timeout=10).raise_for_status()
        print("  텔레그램 전송 완료!")
    except Exception as e:
        print(f"  텔레그램 전송 실패: {e}")


# 계정별 회피 숙박일 집합 만들기 (수동 SKIP + 자동기록)
def booked_set_for(acct, done):
    s=set(SKIP_DATES_BY_ACCT.get(acct,[]))
    for v in done.values():
        if v.get("acct")==acct and v.get("begin"):
            bd=datetime.strptime(v["begin"],"%Y-%m-%d").date()
            for i in range(v.get("nights",1)):
                s.add((bd+timedelta(days=i)).strftime("%Y-%m-%d"))
    return s


def try_one_account_date(session, acct, user_id, begin_str, grades, done):
    """특정 계정·날짜에서 방등급 우선순위 + 3박→2박→1박 시도.
    성공 시 (nights, room, cat_key) 반환, 실패 시 None"""
    begin_dt=datetime.strptime(begin_str,"%Y-%m-%d").date()
    booked=booked_set_for(acct,done)

    if begin_str in booked:
        return None

    # 이 계정이 침범하면 안 되는 날짜 고려해 최대 박수 계산 (상한 3박)
    max_nights=0
    for n in range(1,4):
        night_day=(begin_dt+timedelta(days=n-1)).strftime("%Y-%m-%d")
        if night_day in booked: break
        max_nights=n
    if max_nights==0:
        return None

    # 긴 박수 우선
    for nights in range(max_nights,0,-1):
        end_str=(begin_dt+timedelta(days=nights)).strftime("%Y-%m-%d")
        # 방 등급 우선순위대로
        for g in grades:
            cat_key, nums = GRADE[g]
            rooms=fetch_rooms(session,cat_key,begin_str,end_str)
            # 등급 내 방번호 우선순위대로 정렬
            avail={num_of(r["fcltyCode"]):r for r in rooms}
            for target_num in nums:
                if target_num not in avail: continue
                room=avail[target_num]
                print(f"    🎯 {begin_str}~{end_str}({nights}박) [{g}] {room['fcltyCode']} 시도")
                preocpc=preoccupy(session,room,begin_str,end_str)
                if not preocpc:
                    print(f"      선점 실패")
                    continue
                ok,resp_text,_=submit_reservation(session,user_id,room,preocpc,begin_str,end_str)
                if ok:
                    return (nights,room,cat_key,g,end_str)
                else:
                    print(f"      거부: {resp_text[:60]}")
    return None


def run_plan():
    now_str=now_kst().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str} KST] 망상 멀티계정 자동예약 시작")

    # 계정 유효성
    for a in ("#1","#2","#3"):
        if not ACCOUNTS[a]:
            print(f"  ⚠️ {a} 계정 ID(CK_ID_{a[-1]}) 미설정")
    if not COMMON_PW:
        print("  ❌ CK_PW 없음"); return

    done=load_done()

    # 계정별 세션 캐시 (한 실행 내 재로그인 방지)
    sessions={}
    def get_session(acct):
        if acct in sessions: return sessions[acct]
        uid=ACCOUNTS.get(acct,"")
        if not uid: return None
        s=new_session()
        if login(s,uid):
            sessions[acct]=s
            print(f"  ✅ 로그인: {acct}")
            return s
        return None

    # 현황 출력
    for a in ("#1","#2","#3"):
        print(f"  {a} 회피숙박일: {sorted(booked_set_for(a,done))}")

    success=0
    # 날짜 순서대로
    for begin_str in sorted(PLAN.keys()):
        id_order, grades = PLAN[begin_str]
        for acct in id_order:
            uid=ACCOUNTS.get(acct,"")
            if not uid: continue
            s=get_session(acct)
            if not s: continue
            result=try_one_account_date(s,acct,uid,begin_str,grades,done)
            if result:
                nights,room,cat_key,grade,end_str=result
                key=f"{begin_str}_{nights}박_{acct}_{room['fcltyCode']}"
                done[key]={"begin":begin_str,"end":end_str,"nights":nights,
                           "acct":acct,"room":room["fcltyCode"],"grade":grade,
                           "at":now_str,"ts":now_kst().timestamp()}
                save_done(done)
                success+=1
                msg=("🎉🎉 <b>망상 예약 성공!!</b> 🎉🎉\n\n"
                     f"📅 {begin_str} ~ {end_str} (<b>{nights}박</b>)\n"
                     f"🏕️ {CATEGORIES[cat_key]['name']} <b>{room['fcltyCode']}</b> [{grade}]\n"
                     f"👤 {uid} ({acct})\n"
                     f"⏰ {now_str} (KST)\n\n"
                     f"👉 <a href=\"{BASE_URL}/user/mypage/BD_myReservationList.do\">예약 확인</a>")
                send_telegram(msg)
                print(f"  ✅✅ 성공! {key}")
                break  # 이 날짜는 잡았으니 다음 ID 안 봄, 다음 날짜로
            # 실패면 다음 ID로 스위칭

    if success:
        print(f"완료! 이번 회차 {success}건 성공")
    else:
        print("이번 회차 성공 없음 (재시도 대기)")


def run_test():
    now_str=now_kst().strftime("%Y-%m-%d %H:%M:%S")
    uid=ACCOUNTS.get(TEST_ACCT,"")
    print(f"[{now_str} KST] 🧪 테스트: {TEST_ACCT}({uid}) {TEST_DATE} {TEST_ROOM} {TEST_NIGHTS}박")
    if not uid or not COMMON_PW:
        send_telegram("🧪 테스트 실패: 계정/PW 없음"); return
    s=new_session()
    if not login(s,uid):
        send_telegram("🧪 테스트 실패: 로그인 안 됨"); return
    begin_dt=datetime.strptime(TEST_DATE,"%Y-%m-%d").date()
    end_str=(begin_dt+timedelta(days=TEST_NIGHTS)).strftime("%Y-%m-%d")
    target=num_of(TEST_ROOM)
    report=[f"🧪 <b>예약 테스트</b> ⏰ {now_str}",
            f"📅 {TEST_DATE}~{end_str} / 🏕️ {TEST_ROOM}(숫자={target}) / {TEST_ACCT}({uid})",""]
    room=None;cat_key=None
    for ck in CATEGORIES:
        rooms=fetch_rooms(s,ck,TEST_DATE,end_str)
        f=next((r for r in rooms if num_of(r["fcltyCode"])==target),None)
        if f: room=f;cat_key=ck;break
    if not room:
        report.append("❌ 해당 번호 방 없음(예약가능 상태 아님)")
        send_telegram("\n".join(report));return
    report.append(f"1️⃣ 조회 OK: {room['fcltyCode']} ({CATEGORIES[cat_key]['name']})")
    preocpc=preoccupy(s,room,TEST_DATE,end_str)
    if not preocpc:
        report.append(f"2️⃣ ❌ 선점 실패\n{LAST_PREOCPC_RAW}")
        send_telegram("\n".join(report));return
    report.append(f"2️⃣ ★ 선점 성공 resveNo={preocpc.get('resveNo')}")
    ok,resp_text,payload=submit_reservation(s,uid,room,preocpc,TEST_DATE,end_str)
    report.append(f"3️⃣ 결과: {'✅ 성공' if ok else '❌ 거부'}")
    report.append(f"📥 {resp_text}")
    send_telegram("\n".join(report))


def main():
    if TEST_DATE and TEST_ROOM:
        run_test()
    else:
        run_plan()

if __name__=="__main__":
    main()
