import streamlit as st
import requests
import xmltodict
import datetime
import os
import re
import io
import json
import hashlib
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
from types import SimpleNamespace
import yfinance as yf

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None

# ─────────────────────────────────────────────────────────────
#  키 설정
# ─────────────────────────────────────────────────────────────
def _secret(name, default=""):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)

OPINET_KEY    = _secret("OPINET_KEY", "내 키")
EXIM_KEY      = _secret("EXIM_KEY", "내 키")
LLM_API_KEY   = _secret("LLM_API_KEY", "내 키")
LLM_MODEL     = _secret("LLM_MODEL", "gemini-2.5-flash")
SMTP_EMAIL    = _secret("SMTP_EMAIL", "orep0822@gmail.com")
SMTP_PASSWORD = _secret("SMTP_PASSWORD", "내 키")

HISTORY_FILE = "oil_history.csv"
LOG_FILE = "query_log.csv"
PROFILE_DIR = "profiles"
os.makedirs(PROFILE_DIR, exist_ok=True)

def _llm_ready():
    return bool(LLM_API_KEY) and not LLM_API_KEY.startswith("여기에") and LLM_API_KEY not in ["my key", "내 키"]

# ── 한글 폰트 ──
for _p in ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
           "/System/Library/Fonts/AppleSDGothicNeo.ttc",
           "C:/Windows/Fonts/malgun.ttf"]:
    try:
        if os.path.exists(_p):
            font_manager.fontManager.addfont(_p)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=_p).get_name()
            break
    except Exception:
        continue
plt.rcParams["axes.unicode_minus"] = False

PRODUCTS = {"휘발유": "B027", "자동차용경유": "D047", "실내등유": "C004", "고급휘발유": "B034"}
SIDO_LIST = ["서울","부산","대구","인천","광주","대전","울산","경기","강원",
             "충북","충남","전북","전남","경북","경남","제주","세종"]

TEMPLATES = {
    "vehicle": {
        "roles": ["count", "distance_per_month", "efficiency"],
        "calc": lambda v, price: v["count"] * v["distance_per_month"] / max(v["efficiency"] or 0.1, 0.1),
        "fallback": {
            "count": ("보유 차량/장비 대수", "예: 5", "대", 5, "int"),
            "distance_per_month": ("한 대당 월 평균 주행거리", "예: 6000", "km", 6000, "int"),
            "efficiency": ("평균 연비", "예: 4.5", "km/L", 4.0, "float"),
        },
    },
    "equipment_hours": {
        "roles": ["count", "hours_per_month", "consumption_per_hour"],
        "calc": lambda v, price: v["count"] * v["hours_per_month"] * v["consumption_per_hour"],
        "fallback": {
            "count": ("사용 중인 장비 대수", "예: 3", "대", 3, "int"),
            "hours_per_month": ("한 대당 월 평균 가동시간", "예: 100", "시간", 100, "int"),
            "consumption_per_hour": ("시간당 평균 연료 소비량", "예: 5", "L/시간", 5.0, "float"),
        },
    },
    "spend_based": {
        "roles": ["monthly_spend"],
        "calc": lambda v, price: (v["monthly_spend"] / price) if price else 0,
        "fallback": {
            "monthly_spend": ("월 평균 유류비 총 지출액", "예: 3000000", "원", 2000000, "int"),
        },
    },
    "direct_liters": {
        "roles": ["liters_month"],
        "calc": lambda v, price: v["liters_month"],
        "fallback": {
            "liters_month": ("월 평균 연료 사용량", "예: 3000", "L", 3000, "int"),
        },
    },
}
TEMPLATE_LABELS = {
    "vehicle": "차량 운행 기준", "equipment_hours": "장비 가동시간 기준",
    "spend_based": "월 유류비 지출액 기준", "direct_liters": "직접 리터 입력",
}

# ═════════════════════════════════════════════════════════════
#  데이터 함수
# ═════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600)
def get_oil(prodcd):
    url = "https://www.opinet.co.kr/api/avgRecentPrice.do"
    params = {"out":"xml","certkey":OPINET_KEY,"prodcd":prodcd}
    res = requests.get(url, params=params, timeout=10)
    df = pd.DataFrame(xmltodict.parse(res.text)["RESULT"]["OIL"])
    df["PRICE"] = df["PRICE"].astype(float)
    df["DATE"] = df["DATE"].astype(str)
    return df.sort_values("DATE").reset_index(drop=True)

def update_history(df, prodcd):
    new = df[["DATE","PRICE"]].copy()
    new["DATE"] = new["DATE"].astype(str)
    new["PRODCD"] = prodcd
    if os.path.exists(HISTORY_FILE):
        old = pd.read_csv(HISTORY_FILE, dtype={"DATE":str, "PRODCD":str})
        merged = pd.concat([old, new], ignore_index=True)
    else:
        merged = new
    merged["DATE"] = merged["DATE"].astype(str)
    merged = merged.drop_duplicates(subset=["DATE","PRODCD"], keep="last").sort_values("DATE")
    merged.to_csv(HISTORY_FILE, index=False)
    return merged[merged["PRODCD"] == prodcd].reset_index(drop=True)

def save_log(업종, 유종명, 현재가, 기간옵션):
    row = pd.DataFrame([{"접속시각": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "업종": 업종, "유종": 유종명, "현재가": 현재가, "기간": 기간옵션}])
    if os.path.exists(LOG_FILE):
        row = pd.concat([pd.read_csv(LOG_FILE), row], ignore_index=True)
    row.tail(200).to_csv(LOG_FILE, index=False)

def load_log():
    return pd.read_csv(LOG_FILE) if os.path.exists(LOG_FILE) else pd.DataFrame()

@st.cache_data(ttl=3600)
def get_oil_sido(prodcd):
    url = "https://www.opinet.co.kr/api/avgSidoPrice.do"
    params = {"out":"xml","certkey":OPINET_KEY,"prodcd":prodcd}
    res = requests.get(url, params=params, timeout=10)
    df = pd.DataFrame(xmltodict.parse(res.text)["RESULT"]["OIL"])
    df["PRICE"] = df["PRICE"].astype(float)
    return df

@st.cache_data(ttl=3600)
def get_exchange():
    today = datetime.datetime.now()
    if today.weekday() >= 5:
        today -= datetime.timedelta(days=today.weekday()-4)
    d = today.strftime("%Y%m%d")
    url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {"authkey":EXIM_KEY,"searchdate":d,"data":"AP01"}
    try:
        res = requests.get(url, params=params, verify=False, timeout=10)
        data = res.json()
        if not isinstance(data, list):
            return None, d
        for item in data:
            if isinstance(item, dict) and item.get("cur_unit") == "USD":
                return float(item["deal_bas_r"].replace(",","")), d
    except Exception:
        pass
    return None, d

@st.cache_data(ttl=3600)
def get_intl_oil_data():
    tickers = {"WTI": "CL=F", "Brent": "BZ=F"}
    df_list = []
    for name, ticker in tickers.items():
        try:
            data = yf.download(ticker, period="1mo", progress=False)
            if not data.empty:
                if isinstance(data.columns, pd.MultiIndex):
                    series = data['Close'].iloc[:, 0].reset_index()
                else:
                    series = data['Close'].reset_index()
                series.columns = ['날짜', name]
                series['날짜'] = pd.to_datetime(series['날짜']).dt.tz_localize(None)
                df_list.append(series)
        except Exception:
            pass

    if df_list:
        res = df_list[0]
        for df in df_list[1:]:
            res = pd.merge(res, df, on='날짜', how='outer')
        res = res.sort_values('날짜').dropna().reset_index(drop=True)
        return res
    return pd.DataFrame()

@st.cache_data(ttl=1800)
def get_news(query="국제유가"):
    try:
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        res = requests.get(url, timeout=10)
        items = re.findall(r"<item>(.*?)</item>", res.text, re.DOTALL)[:6]
        out = []
        for it in items:
            t = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
            l = re.search(r"<link>(.*?)</link>", it, re.DOTALL)
            dd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.DOTALL)
            if t and l:
                out.append({"title": re.sub(r"<.*?>","",t.group(1)).strip(),
                            "link": l.group(1).strip(),
                            "date": dd.group(1)[:16] if dd else ""})
        return out
    except Exception:
        return []

def trend_forecast(series, days=7):
    y = np.array(series, dtype=float)
    if len(y) < 3:
        return None
    x = np.arange(len(y))
    a, b = np.polyfit(x, y, 1)
    future_x = np.arange(len(y), len(y)+days)
    return a*future_x + b, a

def breakeven_price(현재가, 월_사용량, 전가율, 월_매출, 영업이익률):
    현재_이익 = 월_매출 * 영업이익률/100
    미전가율 = 1 - 전가율/100
    if 월_사용량 * 미전가율 <= 0:
        return None
    허용_상승액 = 현재_이익 / (월_사용량 * 미전가율)
    return 현재가 + 허용_상승액

def send_alert_email(to_email, subject, body, pdf_bytes=None, filename="OREP_report.pdf"):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))
    if pdf_bytes is not None:
        part = MIMEApplication(pdf_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(SMTP_EMAIL, SMTP_PASSWORD)
        s.send_message(msg)

def build_recommendations(기간변동률, 실제_손실, 전가율, 점수):
    recs = []
    if 기간변동률 > 3:
        recs.append("🔺 유가 상승세입니다. 필요 물량은 **미리 확보(선구매)**해 추가 상승 부담을 줄이세요.")
        recs.append("📄 거래처와 **단가 조정·유류할증료 협상**을 지금 시작해 두세요.")
    elif 기간변동률 < -3:
        recs.append("🔻 유가 하락세입니다. **급한 대량구매는 잠시 보류**하고 추이를 지켜보세요.")
        recs.append("💰 절감된 연료비를 **비상 자금으로 적립**해 다음 상승기에 대비하세요.")
    else:
        recs.append("⚖️ 유가가 안정적입니다. **정기 구매 리듬을 유지**하세요.")
    if 전가율 < 30:
        recs.append("⚠️ 전가율이 낮습니다. 계약에 **유가 연동 조항**을 넣는 것을 검토하세요.")
    if 점수 >= 66:
        recs.append("🚨 리스크가 높습니다. **2~3개월 연료비 예산에 여유분**을 확보하세요.")
    if 실제_손실 > 0:
        recs.append("📉 이 기간 실질 부담이 늘었습니다. **원가 반영 항목**을 점검하세요.")
    return recs

# ─────────────────────────────────────────────────────────────
#  AI 함수
# ─────────────────────────────────────────────────────────────
def gemini_call(prompt):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + LLM_MODEL + ":generateContent")
    headers = {"x-goog-api-key": LLM_API_KEY, "Content-Type": "application/json"}
    r = requests.post(url, headers=headers,
                      json={"contents":[{"parts":[{"text":prompt}]}]}, timeout=30)
    data = r.json()
    if "candidates" not in data:
        raise RuntimeError("Gemini 오류: " + str(data.get("error", data))[:200])
    return data["candidates"][0]["content"]["parts"][0]["text"]

def _fallback_business_analysis(desc):
    role = "monthly_spend"
    flabel, fph, funit, fdefault, ftype = TEMPLATES["spend_based"]["fallback"][role]
    return {
        "업종명": "일반 사업체",
        "특징": desc.strip(),
        "전가율": 30, "연료비중": 10, "환율민감도": 0, "주유종": "자동차용경유",
        "계산방식": "spend_based",
        "필수질문": [{"role": role, "label": flabel, "placeholder": fph,
                   "unit": funit, "default": fdefault, "type": ftype}],
        "추가질문": [],
    }

def analyze_business(desc):
    if not (_llm_ready() and desc.strip()):
        return _fallback_business_analysis(desc)
    try:
        prompt = (
            "너는 중소기업 유가 리스크 진단 시스템의 설계자야. 아래 사업 설명을 읽고, "
            "이 사업의 유가 리스크를 계산하는 데 필요한 맞춤 질문을 이 사업에 딱 맞게 직접 설계해줘. "
            "업종을 어떤 정해진 목록에서 고르지 말고, 이 사업을 가장 잘 나타내는 표현을 자유롭게 지어내.\n\n"
            "반드시 아래 JSON 형식으로만 답하고 다른 말은 절대 붙이지 마.\n"
            "{\n"
            '  "업종명": "이 사업을 부르는 자유로운 명칭 (예: \'수도권 냉동식품 배송업\')",\n'
            '  "특징": "이 사업의 유가 리스크 관점에서 핵심 특징 한 문장",\n'
            '  "전가율": 0~100 정수 (유가 상승분을 판매가·서비스가에 반영할 수 있는 비율 추정),\n'
            '  "연료비중": 0~100 정수 (매출 대비 연료비가 차지하는 비중 추정),\n'
            '  "환율민감도": 0~100 정수 (원자재를 달러로 수입하는 등 환율 노출 정도, 없으면 0),\n'
            '  "주유종": "휘발유|자동차용경유|실내등유|고급휘발유 중 이 사업이 실제 가장 많이 쓰는 것",\n'
            '  "계산방식": "vehicle|equipment_hours|spend_based|direct_liters 중 이 사업의 월 연료 사용량(L)을 '
            '가장 정확하고 답하기 쉽게 구할 수 있는 방식 하나. 대표가 차량 대수·연비 같은 세부 수치를 '
            '모를 것 같은 사업이면 spend_based를 선택해",\n'
            '  "필수질문": [ {"role":"<아래 role명 그대로>","label":"<이 사업에 맞게 직접 만든 구체적 질문 문구>",'
            '"placeholder":"<입력 예시>","unit":"<단위>","default":<합리적 기본값 숫자>,"type":"int 또는 float"} ],\n'
            '  "추가질문": [ {"id":"영문id","label":"<계산엔 안 쓰지만 참고할 이 사업만의 특수 사정, 최대 2개>"} ]\n'
            "}\n\n"
            "계산방식별 필요한 role (정확히 이 role명을 그대로 사용하고 개수도 맞출 것):\n"
            "- vehicle: count(차량/장비 대수), distance_per_month(대당 월 주행거리 km), efficiency(연비 km/L)\n"
            "- equipment_hours: count(장비 대수), hours_per_month(대당 월 가동시간), consumption_per_hour(시간당 연료소비 L)\n"
            "- spend_based: monthly_spend(월 유류비 총 지출액 원)\n"
            "- direct_liters: liters_month(월 연료 사용량 L)\n\n"
            "사업 설명: " + desc)

        raw = gemini_call(prompt)
        cleaned_raw = re.sub(r"```json", "", raw, flags=re.IGNORECASE)
        cleaned_raw = re.sub(r"```", "", cleaned_raw)
        m = re.search(r"\{.*\}", cleaned_raw, re.DOTALL)
        if not m:
            raise ValueError("JSON 형식을 찾을 수 없음")
        data = json.loads(m.group(0))

        method = str(data.get("계산방식", "")).strip()
        if method not in TEMPLATES:
            method = "spend_based"
        roles = TEMPLATES[method]["roles"]
        fb = TEMPLATES[method]["fallback"]

        by_role = {}
        raw_qs = data.get("필수질문", [])
        if isinstance(raw_qs, list):
            for q in raw_qs:
                if isinstance(q, dict) and q.get("role") in roles:
                    by_role[q["role"]] = q

        질문들 = []
        for role in roles:
            q = by_role.get(role)
            flabel, fph, funit, fdefault, ftype = fb[role]
            if q:
                label = str(q.get("label") or flabel).strip()
                placeholder = str(q.get("placeholder") or fph).strip()
                unit = str(q.get("unit") or funit).strip()
                typ = q.get("type") if q.get("type") in ("int", "float") else ftype
                try:
                    default = float(q.get("default")) if typ == "float" else int(float(q.get("default")))
                except Exception:
                    default = fdefault
            else:
                label, placeholder, unit, default, typ = flabel, fph, funit, fdefault, ftype
            질문들.append({"role": role, "label": label, "placeholder": placeholder,
                          "unit": unit, "default": default, "type": typ})

        추가질문 = []
        raw_extra = data.get("추가질문", [])
        if isinstance(raw_extra, list):
            for i, q in enumerate(raw_extra[:2]):
                if isinstance(q, dict) and str(q.get("label", "")).strip():
                    qid = re.sub(r"[^a-zA-Z0-9_]", "", str(q.get("id", "")))[:20] or ("extra" + str(i))
                    추가질문.append({"id": qid, "label": str(q["label"]).strip()})

        def clamp(v, default):
            try:
                return max(0, min(100, int(float(v))))
            except Exception:
                return default

        주유종 = str(data.get("주유종", "")).strip()
        if 주유종 not in PRODUCTS:
            주유종 = "자동차용경유"

        return {
            "업종명": str(data.get("업종명") or "").strip() or "일반 사업체",
            "특징": str(data.get("특징") or "").strip(),
            "전가율": clamp(data.get("전가율"), 30),
            "연료비중": clamp(data.get("연료비중"), 10),
            "환율민감도": clamp(data.get("환율민감도"), 0),
            "주유종":주유종,
            "계산방식": method,
            "필수질문": 질문들,
            "추가질문": 추가질문,
        }
    except Exception as e:
        return _fallback_business_analysis(desc)

def lino_chat(history, user_msg, context_summary):
    sys = ("너의 이름은 Lino야. 중소기업 대표를 곁에서 돕는 유가·원가 리스크 전문 컨설턴트이자 "
           "개인 비서야. OREP 프로그램의 데이터를 근거로, 경영 의사결정을 돕는 실무 자문을 제공해. "
           "핵심 원칙: 아래 [현재 데이터]의 실제 숫자를 반드시 활용해 구체적 금액·비율·유가 수치로 답하고, "
           "필요하면 계산 과정을 보여줘.\n\n"
           "[현재 데이터]\n" + context_summary)
    if not _llm_ready():
        return "Lino를 쓰려면 Gemini API 키가 필요해요."
    convo = sys + "\n\n[대화]\n"
    for role, msg in history[-6:]:
        convo += ("사용자: " if role == "user" else "Lino: ") + msg + "\n"
    convo += "사용자: " + user_msg + "\nLino: "
    try:
        return gemini_call(convo)
    except Exception as e:
        return "답변 생성 중 오류가 났어요: " + str(e)[:120]

def ai_briefing(ctx, recs):
    if _llm_ready():
        try:
            prompt = ("너는 중소기업 유가 리스크 자문가야. 아래 데이터·권장사항 참고해 "
                      "(1)현재 상황 요약 2~3문장 (2)'이렇게 하세요' 실행 권장 3가지 불릿으로 "
                      "구체적으로. 미래 가격 예측 금지.\n\n[데이터]\n" + ctx + "\n\n[참고]\n" + "\n".join(recs))
            return gemini_call(prompt), "AI (Gemini)"
        except Exception:
            pass
    return ("**현재 상황 요약**\n\n" + ctx + "\n\n**이렇게 하세요**\n\n"
            + "\n".join("- " + r for r in recs)), "규칙기반 (LLM 키 미설정)"

def ai_profit_judge(업종, 연료비_변화, 실제_손실_총, 매출대비, 기간변동률):
    상태 = "손해" if 실제_손실_총 > 0 else ("이익" if 실제_손실_총 < 0 else "변화 없음")
    if _llm_ready():
        try:
            prompt = (f"너는 중소기업 재무 자문가야. 지난 기간 유가 변동에 따른 우리 회사 실제 결과를 "
                      f"근거로 손해/이익을 명확히 판정하고 이유를 2~3문장으로 쉽게 설명해. 예측 금지.\n"
                      f"- 업종 {업종}\n- 유가 변동률 {기간변동률:+.1f}%\n"
                      f"- 연료비 변화 {연료비_변화:+,.0f}원\n"
                      f"- 실제 부담 {실제_손실_총:+,.0f}원 (매출의 {매출대비:+.2f}%)")
            return gemini_call(prompt), "AI (Gemini)"
        except Exception:
            pass
    return (f"이번 기간은 **{상태}**입니다. 유가가 {기간변동률:+.1f}% 변하면서 연료비가 "
            f"{연료비_변화:+,.0f}원 바뀌었고, 전가분을 빼면 실제 부담은 **{실제_손실_총:+,.0f}원**"
            f"(매출의 {매출대비:+.2f}%)입니다."), "규칙기반 (LLM 키 미설정)"

def ai_deep_analysis(업종, 유종명, 현재가, 기간변동률, 실제_손실_총, 매출대비, 전가율, 점수, 등급, 월_사용량):
    ctx = ("업종 " + 업종 + ", 유종 " + 유종명 + ", 현재가 " + format(현재가, ",.0f") +
           "원, 변동률 " + format(기간변동률, "+.1f") + "%, 월연료 " + format(월_사용량, ",.0f") +
           "L, 전가율 " + str(전가율) + "%, 실제부담 " + format(실제_손실_총, "+,.0f") +
           "원(매출의 " + format(매출대비, "+.2f") + "%), 리스크 " + format(점수, ".0f") + "점(" + 등급 + ")")
    if _llm_ready():
        try:
            prompt = ("너는 중소기업 유가 리스크 전문 컨설턴트야. 아래 데이터를 바탕으로 "
                      "심층 분석 리포트를 작성해줘. 다음 4개 항목을 각각 2~3문장으로 구체적으로 써: "
                      "1) 현재 상황 진단 2) 이 업종에 미치는 영향 3) 단기(1개월) 대응 전략 "
                      "4) 중장기 리스크 관리 방안. 미래 유가 가격 예측은 하지 마. "
                      "항목 제목은 [ ]로 감싸.\n\n[데이터]\n" + ctx)
            return gemini_call(prompt), "AI (Gemini)"
        except Exception:
            pass
    fb = ("[현재 상황 진단]\n유가가 " + format(기간변동률, "+.1f") + "% 변동했으며, 회사가 실제로 떠안은 "
          "부담은 " + format(실제_손실_총, "+,.0f") + "원입니다. 리스크 점수는 " + format(점수, ".0f") + "점입니다.\n\n"
          "[업종 영향]\n" + 업종 + "은 전가율이 " + str(전가율) + "%로, 유가 상승분을 판매가에 "
          "반영하기 " + ("어려운" if 전가율 < 30 else "비교적 가능한") + " 구조입니다.\n\n"
          "[단기 대응]\n" + ("상승기이므로 선구매와 단가협상을" if 기간변동률 > 0 else "하락/안정기이므로 정기구매 리듬 유지를") + " 권합니다.\n\n"
          "[중장기 관리]\n연료 예산에 여유분을 두고, 계약에 유가 연동 조항 도입을 검토하세요.\n\n"
          "(정확한 AI 분석을 원하면 Gemini API 키를 설정하세요.)")
    return fb, "규칙기반 (LLM 키 미설정)"

def make_pdf_report(업종, 유종명, 현재가, 기간변동률, 실제_손실_총, 매출대비, 점수, 등급, recs, ai_text=None, 마지노선=None):
    import re as _re
    import textwrap as _tw
    emoji_pat = _re.compile("[" +
        "\U0001F000-\U0001FAFF" + "\U00002600-\U000027BF" +
        "\U0001F1E6-\U0001F1FF" + "\U00002190-\U000021FF" +
        "\U00002B00-\U00002BFF" + "\uFE0F" + "]")

    def clean(s):
        return emoji_pat.sub("", s.replace("**","")).strip()

    kfont = None
    for p in ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
              "/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "C:/Windows/Fonts/malgun.ttf"]:
        if os.path.exists(p):
            kfont = font_manager.FontProperties(fname=p)
            break
    plt.rcParams["pdf.fonttype"] = 42
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.patch.set_facecolor("white")

    마지노선_줄 = "적자 전환 마지노선: 계산 불가"
    if 마지노선:
        마지노선_줄 = ("적자 전환 마지노선: " + format(마지노선, ",.0f") + "원/L (현재 대비 +" +
                    format(마지노선-현재가, ",.0f") + "원, " +
                    format((마지노선-현재가)/현재가*100, ".1f") + "% 여유)")

    lines = [
        ("OREP 유가 리스크 리포트", 16, "bold", "#3182F6"),
        (datetime.datetime.now().strftime("발행일 %Y-%m-%d %H:%M"), 9, "normal", "#666"),
        ("", 6, "normal", "#000"),
        (clean("업종: " + 업종), 11, "bold", "#000"),
        ("유종: " + 유종명 + "   현재가: " + format(현재가, ",.0f") + "원", 11, "normal", "#000"),
        ("기간 변동률: " + format(기간변동률, "+.1f") + "%", 11, "normal", "#000"),
        ("실제 부담 변화: " + format(실제_손실_총, "+,.0f") + "원 (매출의 " + format(매출대비, "+.2f") + "%)", 11, "normal", "#000"),
        ("리스크 점수: " + format(점수, ".0f") + "/100 (" + 등급 + ")", 11, "bold", "#000"),
        (마지노선_줄, 11, "bold", "#dc2626"),
        ("", 6, "normal", "#000"),
        ("[ 권장 조치 ]", 12, "bold", "#3182F6"),
    ]
    for r in recs:
        wrapped = _tw.wrap(clean(r), 44) or [""]
        for i, wl in enumerate(wrapped):
            lines.append(("· " + wl if i == 0 else "  " + wl, 11, "normal", "#000"))
    if ai_text:
        lines.append(("", 6, "normal", "#000"))
        lines.append(("[ AI 심층 분석 ]", 12, "bold", "#3182F6"))
        for para in clean(ai_text).split("\n"):
            if not para.strip():
                lines.append(("", 4, "normal", "#000")); continue
            for wl in _tw.wrap(para, 44):
                lines.append((wl, 10, "normal", "#222"))
    lines.append(("", 6, "normal", "#000"))
    lines.append(("본 리포트는 한국석유공사 오피넷 공공데이터 기반이며,", 8, "normal", "#888"))
    lines.append(("유가의 미래 가격을 예측하지 않습니다.", 8, "normal", "#888"))

    pages = [fig]
    y = 0.94
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off")
    for text, size, weight, color in lines:
        if y < 0.06:
            newfig = plt.figure(figsize=(8.27, 11.69)); newfig.patch.set_facecolor("white")
            nax = newfig.add_axes([0, 0, 1, 1]); nax.axis("off")
            pages.append(newfig); ax = nax; y = 0.94
        ax.text(0.09, y, text, fontsize=size, fontweight=weight, color=color,
                va="top", fontproperties=kfont, transform=ax.transAxes)
        y -= 0.023 if text else 0.012

    buf = io.BytesIO()
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(buf) as pdf:
        for f in pages:
            pdf.savefig(f); plt.close(f)
    buf.seek(0)
    return buf

# ═════════════════════════════════════════════════════════════
#  차트
# ═════════════════════════════════════════════════════════════
def show_fig(fig):
    st.pyplot(fig)
    plt.close(fig)

def styled_chart(data, 유종명, 기간라벨, forecast=None):
    fig, ax = plt.subplots(figsize=(10, 4.2))
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f4f6f8")
    x = list(range(len(data)))
    y = data["PRICE"].values
    ax.plot(x, y, color="#3182F6", linewidth=2.5, zorder=3, label="실제")
    base = min(y) - (max(y)-min(y))*0.15 if max(y) > min(y) else min(y)-1
    ax.fill_between(x, y, base, color="#3182F6", alpha=0.08, zorder=1)
    ax.scatter([x[-1]], [y[-1]], color="#3182F6", s=70, zorder=4, edgecolor="white", linewidth=1.5)
    ax.annotate(f"{y[-1]:,.0f}원", (x[-1], y[-1]), textcoords="offset points",
                xytext=(-10, 12), fontsize=11, fontweight="bold", color="#3182F6")
    if forecast is not None:
        fx = list(range(len(data)-1, len(data)-1+len(forecast)+1))
        fy = [y[-1]] + list(forecast)
        ax.plot(fx, fy, color="#FFC043", linewidth=2, linestyle="--", zorder=3, label="추세 연장")
        ax.legend(loc="upper left", fontsize=9, frameon=False)
    step = max(1, len(data)//8)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([str(d)[4:6]+"/"+str(d)[6:8] for d in data["DATE"].values[::step]], fontsize=9)
    ax.set_title(f"{유종명} 가격 추이 ({기간라벨})", fontsize=13, fontweight="bold", pad=12, loc="left")
    for s in ["top","right"]:
        ax.spines[s].set_visible(False)
    for s in ["left","bottom"]:
        ax.spines[s].set_color("#D1D6DB")
    ax.grid(axis="y", color="#E5E8EB", linewidth=0.8, zorder=0)
    ax.tick_params(colors="#6B7684")
    return fig

def ai_explain(text):
    st.markdown(
        "<div style='background:#F2F6FF;border-left:4px solid #3182F6;"
        "border-radius:10px;padding:11px 15px;margin:6px 0 16px 0;"
        "font-size:13.5px;color:#4E5968;line-height:1.6'>"
        "<b style='color:#3182F6'>AI 해석</b> · " + text + "</div>", unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════
#  페이지 설정 + TDS 스타일
# ═════════════════════════════════════════════════════════════
st.set_page_config(page_title="OREP · 유가 리스크", page_icon="⛽", layout="wide")

TDS_CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
@import url('https://fonts.googleapis.com/css2?family=Nanum+Gothic:wght@400;700;800&display=swap');

html, body, [class*="css"], .stApp, .block-container, p, span, div, label, input, textarea, button, li {
    font-family: 'Pretendard', 'Nanum Gothic', -apple-system, sans-serif;
}
.stApp { background-color: #F9FAFB; }
header[data-testid="stHeader"] { background: transparent; box-shadow: none; }
.block-container { padding-top: 2.4rem; max-width: 1120px; }

section[data-testid="stSidebar"] { background-color: #FFFFFF; border-right: 1px solid #E5E8EB; }
section[data-testid="stSidebar"] .block-container { padding-top: 1.2rem; }

h1, h2, h3, h4 { color: #191F28; letter-spacing: -0.4px; font-weight: 700; }

div[data-testid="stStatusWidget"] svg, div[data-testid="stStatusWidget"] img { display:none !important; }
div[data-testid="stStatusWidget"] > div:first-child::before {
    content:""; width:15px; height:15px; border:2px solid #E5E8EB; border-top-color:#3182F6;
    border-radius:50%; display:inline-block; animation: orep-spin .7s linear infinite; }
@keyframes orep-spin { to { transform: rotate(360deg); } }

div[data-testid="stMetric"] {
    background:#fff; border:1px solid #E5E8EB; border-radius:18px; padding:16px 18px;
    box-shadow:0 1px 3px rgba(23,31,40,.04); container-type: inline-size; }
div[data-testid="stMetricLabel"] { color:#8B95A1; font-size:13px; font-weight:600; }
div[data-testid="stMetricValue"] {
    color:#191F28; font-weight:700;
    font-size: clamp(1rem, 9.5cqw, 2.1rem) !important;
    white-space:nowrap; overflow:visible !important; text-overflow:unset !important; }
div[data-testid="stMetricDelta"] { font-size: clamp(.72rem, 4cqw, .9rem) !important; }

.stButton > button, .stDownloadButton > button {
    border-radius:14px; border:1px solid #E5E8EB; background:#fff; color:#4E5968;
    font-weight:600; padding:.5rem 1rem; transition:all .12s ease; }
.stButton > button:hover, .stDownloadButton > button:hover { border-color:#D1D6DB; background:#F9FAFB; }
.stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
    background:#3182F6; border:none; color:#fff; box-shadow:0 2px 8px rgba(49,130,246,.25); }
.stButton > button[kind="primary"]:hover, .stDownloadButton > button[kind="primary"]:hover { background:#2272EB; }

.stTextInput input, .stNumberInput input, .stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {
    border-radius:12px; border:1px solid #E5E8EB; background:#fff; color:#191F28; padding:.55rem .8rem; }
.stTextInput input:focus, .stNumberInput input:focus { border-color:#3182F6; box-shadow:0 0 0 3px rgba(49,130,246,0.15); }
</style>
"""
st.markdown(TDS_CSS, unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════
#  회원 관리 & 구글 시트 프로필 저장소
# ═════════════════════════════════════════════════════════════
USER_FILE = "users_db.json"
PROFILE_KEY_PREFIXES = ("pf_", "biz_")

def load_users():
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_users(d):
    with open(USER_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _hash_pw(pw, salt):
    return hashlib.sha256((pw + salt).encode()).hexdigest()

ss = st.session_state
ss.setdefault("auth_user", None)
ss.setdefault("nav", "mypage")
ss.setdefault("ob_step", 0)
ss.setdefault("onboarded", False)
ss.setdefault("lino_open", False)
ss.setdefault("lino_history", [])

def _current_user_id():
    return ss["auth_user"] if ss["auth_user"] else "unknown"

def _profile_path():
    return os.path.join(PROFILE_DIR, f"profile_{_current_user_id()}.json")

@st.cache_resource
def _gsheet_client():
    try:
        if "GSHEET_CREDENTIALS" in st.secrets:
            info = json.loads(st.secrets["GSHEET_CREDENTIALS"])
            creds = Credentials.from_service_account_info(info,
                scopes=["https://www.googleapis.com/auth/spreadsheets"])
            client = gspread.authorize(creds)
            return client.open("OREP_Profiles").sheet1
    except Exception:
        pass
    return None

def _gsheet_find_row(ws, uid):
    try:
        cells = ws.col_values(1)
        if uid in cells:
            return cells.index(uid) + 1
    except Exception:
        pass
    return None

def load_profile():
    uid = _current_user_id()
    if not uid or uid == "unknown":
        return None
    # 1) 구글 시트 우선 조회
    ws = _gsheet_client()
    if ws is not None:
        try:
            row = _gsheet_find_row(ws, uid)
            if row:
                raw = ws.cell(row, 2).value
                if raw:
                    return json.loads(raw)
        except Exception:
            pass
    # 2) 구글 시트가 설정 안 됐거나 실패한 경우 로컬 백업 파일 사용
    p = _profile_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def save_profile():
    """온보딩·마이페이지에서 입력한 프로필 값을 로그인 계정별로 저장한다."""
    uid = _current_user_id()
    if not uid or uid == "unknown":
        return

    # 기존 저장된 프로필을 불러와 현재 세션에 있는 값만 덮어씌움
    existing = load_profile() or {}
    for k, v in ss.items():
        if k.startswith(PROFILE_KEY_PREFIXES) or k in ("onboarded", "ob_step", "nav", "user_first_name", "user_last_name", "user_email"):
            existing[k] = v
            
    payload = json.dumps(existing, ensure_ascii=False, default=str)

    ws = _gsheet_client()
    if ws is not None:
        try:
            row = _gsheet_find_row(ws, uid)
            now = datetime.datetime.now().isoformat()
            if row:
                ws.update_cell(row, 2, payload)
                ws.update_cell(row, 3, now)
            else:
                ws.append_row([uid, payload, now])
        except Exception:
            pass

    try:
        with open(_profile_path(), "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass

    # 세션 상태가 삭제되지 않도록 보존 처리
    _saved_profile = load_profile()
    if _saved_profile:
        for k, v in _saved_profile.items():
            if k not in ss:
                ss[k] = v
        ss["onboarded"] = _saved_profile.get("onboarded", ss.get("onboarded", False))
        ss["ob_step"] = _saved_profile.get("ob_step", ss.get("ob_step", 0))

# 세션의 기본 초기값 보조 설정
_saved_profile = load_profile()
if _saved_profile:
    for _k, _v in _saved_profile.items():
        ss.setdefault(_k, _v)
    ss["onboarded"] = _saved_profile.get("onboarded", ss.get("onboarded", False))
    ss["ob_step"] = _saved_profile.get("ob_step", ss.get("ob_step", 0))

# ═════════════════════════════════════════════════════════════
#  인증 화면 처리
# ═════════════════════════════════════════════════════════════
if not ss["auth_user"]:
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            "<div style='text-align:center;padding:6px 0 8px 0'>"
            "<span style='font-family:Georgia,serif;font-size:44px;font-weight:900;"
            "letter-spacing:-2px;color:#3182F6'>OREP<span style='color:#191F28'>.</span></span></div>"
            "<div style='text-align:center;font-size:12px;color:#8B95A1;letter-spacing:2.5px;"
            "font-weight:700;margin-bottom:26px'>OIL RISK EDURE PROGRAM</div>"
            "<div style='text-align:center;color:#4E5968;font-size:15px;margin-bottom:20px;line-height:1.6'>"
            "아이디로 로그인하면 계정별로 데이터가 분리 저장돼요.</div>", unsafe_allow_html=True)
        
        tab_login, tab_signup = st.tabs(["로그인", "회원가입"])
        with tab_login:
            with st.form("login_form"):
                u = st.text_input("아이디", key="li_user")
                p = st.text_input("비밀번호", type="password", key="li_pw")
                login_submitted = st.form_submit_button("로그인", type="primary", use_container_width=True)
                if login_submitted:
                    users = load_users()
                    rec = users.get(u)
                    if not rec:
                        st.error("존재하지 않는 아이디예요.")
                    elif _hash_pw(p, rec["salt"]) != rec["pw_hash"]:
                        st.error("비밀번호가 틀렸어요.")
                    else:
                        ss["auth_user"] = u
                        # 로그인 직후 해당 유저 프로필 복원
                        p_data = load_profile()
                        if p_data:
                            for pk, pv in p_data.items():
                                ss[pk] = pv
                        st.rerun()
        with tab_signup:
            with st.form("signup_form"):
                nu = st.text_input("사용할 아이디", key="su_user")
                np1 = st.text_input("비밀번호", type="password", key="su_pw1")
                np2 = st.text_input("비밀번호 확인", type="password", key="su_pw2")
                signup_submitted = st.form_submit_button("회원가입", type="primary", use_container_width=True)
                if signup_submitted:
                    users = load_users()
                    if not nu.strip():
                        st.error("아이디를 입력하세요.")
                    elif nu in users:
                        st.error("이미 사용 중인 아이디예요.")
                    elif np1 != np2:
                        st.error("비밀번호가 서로 달라요.")
                    elif len(np1) < 4:
                        st.error("비밀번호는 4자리 이상으로 해주세요.")
                    else:
                        salt = os.urandom(16).hex()
                        users[nu] = {"salt": salt, "pw_hash": _hash_pw(np1, salt)}
                        save_users(users)
                        st.success("회원가입이 완료되었습니다! 로그인 탭에서 로그인해 주세요.")

    st.stop()

# ─────────────────────────────────────────────────────────────
#  온보딩 단계 정의
# ─────────────────────────────────────────────────────────────
OB_STEPS = ["welcome", "biz_desc", "confirm", "usage", "tune", "region"]

def apply_business_analysis(res, desc):
    ss["pf_업종명"] = res["업종명"]
    ss["biz_특징"] = res["특징"]
    ss["pf_전가율"] = res["전가율"]
    ss["pf_연료비중"] = res["연료비중"]
    ss["pf_환율민감도"] = res["환율민감도"]
    ss["pf_유종명"] = res["주유종"]
    ss["biz_계산방식"] = res["계산방식"]
    ss["biz_질문들"] = res["필수질문"]
    ss["biz_추가질문"] = res["추가질문"]
    for q in res["필수질문"]:
        ss["pf_role_" + q["role"]] = q["default"]
    for q in res["추가질문"]:
        ss["pf_extra_" + q["id"]] = ""
    ss["_applied_override"] = False
    ss.pop("_orig_계산방식", None)
    ss.pop("_orig_질문들", None)
    ss["pf_override_spend"] = False
    ss.setdefault("pf_영업이익률", 5)
    ss.setdefault("pf_월매출", 100000000)
    ss.setdefault("pf_지역", "서울")
    ss.setdefault("pf_기간", "7일")
    ss.setdefault("pf_비교유종", [])
    ss.setdefault("pf_notes", desc)

# ═════════════════════════════════════════════════════════════
#  지표 계산
# ═════════════════════════════════════════════════════════════
def compute_metrics():
    업종명 = ss.get("pf_업종명", "일반 사업체")
    특징 = ss.get("biz_특징", "")
    계산방식 = ss.get("biz_계산방식", "spend_based")
    질문들 = ss.get("biz_질문들", [])
    유종명 = ss.get("pf_유종명", "자동차용경유")
    전가율 = ss.get("pf_전가율", 30)
    연료비중 = ss.get("pf_연료비중", 10)
    환율민감도 = ss.get("pf_환율민감도", 0)
    영업이익률 = ss.get("pf_영업이익률", 5)
    월_매출 = ss.get("pf_월매출", 0)
    내지역 = ss.get("pf_지역", "서울")
    기간옵션 = ss.get("pf_기간", "7일")
    비교유종 = ss.get("pf_비교유종", [])
    owner_notes = ss.get("pf_notes", "")

    df7 = get_oil(PRODUCTS[유종명])
    hist = update_history(df7, PRODUCTS[유종명])
    if 기간옵션 == "7일":
        view = hist.tail(7).reset_index(drop=True)
    elif 기간옵션 == "30일":
        view = hist.tail(30).reset_index(drop=True)
    else:
        view = hist.reset_index(drop=True)
    view = view.copy()

    현재가 = view["PRICE"].iloc[-1]
    어제가 = view["PRICE"].iloc[-2] if len(view) > 1 else 현재가
    시작가 = view["PRICE"].iloc[0]
    하루변화 = 현재가 - 어제가
    기간변동률 = (현재가 - 시작가) / 시작가 * 100 if 시작가 else 0
    방향 = "상승" if 기간변동률 > 0 else ("하락" if 기간변동률 < 0 else "보합")

    # 월 연료 사용량 산출
    v_inputs = {}
    for q in 질문들:
        v_inputs[q["role"]] = ss.get("pf_role_" + q["role"], q["default"])

    if ss.get("pf_override_spend") and "monthly_spend" in ss:
        월_사용량 = ss["monthly_spend"] / 현재가 if 현재가 else 0
    else:
        calc_fn = TEMPLATES.get(계산방식, TEMPLATES["spend_based"])["calc"]
        월_사용량 = calc_fn(v_inputs, 현재가)

    # 기간 유류비 변화량
    기간_사용량 = 월_사용량 * (7/30 if 기간옵션=="7일" else (1 if 기간옵션=="30일" else 3))
    연료비_변화 = (현재가 - 시작가) * 기간_사용량

    # 환율 연동 반영
    환율_현재, _ = get_exchange()
    환차_패널티 = 0
    _fx_txt = "불러오기 실패"
    if 환율_현재 and 환율민감도 > 0:
        _fx_txt = f"{환율_현재:,.1f}원"
        환차_패널티 = 연료비_변화 * (환율민감도 / 100) * 0.1

    실제_손실_총 = 연료비_변화 * (1 - 전가율/100) + 환차_패널티
    매출대비 = (실제_손실_총 / 월_매출 * 100) if 월_매출 else 0

    # 리스크 점수 모델링
    변동폭_점수 = min(40, max(0, abs(기간변동률) * 6))
    비중_점수 = (연료비중 / 100) * 30
    전가_방어_점수 = ((100 - 전가율) / 100) * 20
    환율_점수 = (환율민감도 / 100) * 10
    점수 = min(100, max(0, 변동폭_점수 + 비중_점수 + 전가_방어_점수 + 환율_점수))

    if 점수 >= 66:
        등급 = "심각 (High)"
        색 = "🔴"
    elif 점수 >= 33:
        등급 = "주의 (Moderate)"
        색 = "🟡"
    else:
        등급 = "안정 (Low)"
        색 = "🟢"

    recs = build_recommendations(기간변동률, 실제_손실_총, 전가율, 점수)
    마지노선 = breakeven_price(현재가, 월_사용량, 전가율, 월_매출, 영업이익률)

    return SimpleNamespace(
        업종명=업종명, 특징=특징, 계산방식=계산방식, 질문들=질문들, 유종명=유종명, 전가율=전가율,
        연료비중=연료비중, 환율민감도=환율민감도, 영업이익률=영업이익률, 월_매출=월_매출, 내지역=내지역,
        기간옵션=기간옵션, 비교유종=비교유종, owner_notes=owner_notes, view=view, hist=hist,
        현재가=현재가, 하루변화=하루변화, 기간변동률=기간변동률, 방향=방향, 월_사용량=월_사용량,
        연료비_변화=연료비_변화, 실제_손실_총=실제_손실_총, 매출대비=매출대비, 점수=점수, 등급=등급,
        색=색, recs=recs, 마지노선=마지노선, _fx_txt=_fx_txt, 환차_패널티=환차_패널티
    )

# ═════════════════════════════════════════════════════════════
#  조기 경보 (자동)
# ═════════════════════════════════════════════════════════════
def check_and_trigger_alert(M):
    email = ss.get("user_email")
    if not email or "@" not in email:
        return
    if len(M.view) < 2:
        return
    prices = M.view["PRICE"].values
    diffs = np.abs(np.diff(prices))
    평소 = np.mean(diffs) if len(diffs) > 0 else 1.0

    급변감지 = False
    이유 = ""
    if abs(M.하루변화) >= 평소 * 3:
        급변감지 = True
        이유 = f"일일 가격 변동폭({M.하루변화:+.1f}원)이 평소({평소:.1f}원) 대비 3배 이상 크게 발생했습니다."
    elif abs(M.기간변동률) >= 5.0:
        급변감지 = True
        이유 = f"설정하신 기간 내 누적 변동률({M.기간변동률:+.1f}%)이 5%를 초과했습니다."

    last_price = ss.get("last_alert_price")
    if last_price and 급변감지:
        if abs(M.현재가 - last_price) / last_price * 100 < 2.0:
            급변감지 = False

    if 급변감지:
        제목 = f"[OREP 조기경보] {M.유종명} 가격 큰 변동 감지"
        본문 = (f"안녕하세요 {ss.get('user_first_name', '고객')}님,\n\n"
               f"현재 {M.유종명} 시장에서 유가 큰 변동이 감지되었습니다.\n"
               f"💡 이유: {이유}\n"
               f"현재가: {M.현재가:,.0f}원\n\n"
               f"OREP 시스템에 접속하여 우리 회사의 리스크 심층 분석 및 손익분기점을 확인하시기 바랍니다.\n"
               f"감사합니다.")
        try:
            send_alert_email(email, 제목, 본문)
            ss["last_alert_price"] = M.현재가
            st.toast("⚠️ 유가 큰 변동이 감지되어 조기 경보 이메일이 자동 발송되었습니다.")
        except Exception:
            pass

# ═════════════════════════════════════════════════════════════
#  온보딩 위저드 렌더링
# ═════════════════════════════════════════════════════════════
def render_onboarding():
    st.markdown("<style>section[data-testid='stSidebar']{display:none !important;}</style>", unsafe_allow_html=True)
    step = OB_STEPS[ss.ob_step]
    total = len(OB_STEPS)

    _, mid, _ = st.columns([1, 3.2, 1])
    with mid:
        st.markdown(
            "<div style='text-align:center;padding:6px 0 2px 0'>"
            "<span style='font-family:Georgia,serif;font-size:40px;font-weight:900;color:#3182F6'>OREP.</span>"
            "</div>", unsafe_allow_html=True)
        st.progress((ss.ob_step + 1) / total)
        st.markdown(f"<div style='text-align:right;font-size:12px;color:#8B95A1;margin-bottom:24px;font-weight:600'>STEP {ss.ob_step+1} / {total}</div>", unsafe_allow_html=True)

        # ── Step 1: Welcome ──
        if step == "welcome":
            st.markdown("<div style='text-align:center;margin-bottom:20px'>"
                        "<span style='font-size:26px;font-weight:800;color:#191F28'>반가워요! 이름과 이메일을 알려주세요</span>"
                        "</div>", unsafe_allow_html=True)
            st.text_input("성 (Last Name)", placeholder="예: 김", key="user_last_name")
            st.text_input("이름 (First Name)", placeholder="예: 길동", key="user_first_name")
            st.text_input("이메일 주소 (유가 조기경보 리포트 수신용)", placeholder="example@gmail.com", key="user_email")
            st.caption("입력하신 이메일은 유가가 급변할 때 자동 리스크 보고서를 보내드리는 용도로만 안전하게 활용됩니다.")
            
            if st.button("시작하기 →", type="primary", use_container_width=True, key="ob_welcome_next"):
                if not ss.get("user_first_name", "").strip() or not ss.get("user_email", "").strip():
                    st.warning("이름과 이메일 주소는 필수 항목입니다.")
                else:
                    ss.ob_step = 1
                    save_profile()
                    st.rerun()

        # ── Step 2: Business Description ──
        elif step == "biz_desc":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>어떤 사업을 운영하시나요?</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "대표님의 사업 모델을 자유롭게 적어주시면, AI가 맞춤형 질문을 만듭니다.</div>", unsafe_allow_html=True)
            desc = st.text_area("사업 설명", height=130, key="ob_desc",
                                placeholder="예: 서울/경기권에서 신선 물류 새벽배송 대행업을 하고 있습니다. 1톤 탑차 6대를 운영 중이며 거래처와는 분기 단위로 계약합니다.")
            
            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_desc_prev"):
                ss.ob_step = 0
                save_profile()
                st.rerun()
            if c2.button("AI 맞춤 진단 시작 →", type="primary", use_container_width=True, key="ob_desc_next"):
                if not ss.get("ob_desc", "").strip():
                    st.warning("사업 내용을 한두 줄이라도 적어주세요. (필수)")
                else:
                    with st.spinner("AI가 우리 사업에 맞는 질문을 준비하고 있어요..."):
                        result = analyze_business(ss["ob_desc"])
                        apply_business_analysis(result, ss["ob_desc"])
                    ss.ob_step = 2
                    save_profile()
                    st.rerun()

        # ── Step 3: Confirm ──
        elif step == "confirm":
            특징 = ss.get("biz_특징", "")
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>이렇게 이해했어요</span></div>", unsafe_allow_html=True)
            if 특징:
                st.markdown(f"<div style='background:#F2F6FF;border-radius:12px;padding:12px 16px;margin:12px 0;color:#4E5968;font-size:14px'>"
                            f"<b style='color:#3182F6'>AI 요약</b> · {특징}</div>", unsafe_allow_html=True)
            st.text_input("업종명 (마음에 안 들면 직접 고쳐도 돼요)", key="pf_업종명")
            st.caption("업종을 정해진 목록에서 고른 게 아니라, AI가 설명을 읽고 자유롭게 지어낸 이름이에요.")
            
            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_confirm_prev"):
                ss.ob_step = 1
                save_profile()
                st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_confirm_next"):
                ss.ob_step = 3
                save_profile()
                st.rerun()

        # ── Step 4: Usage ──
        elif step == "usage":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>연료 사용량을 알려주세요</span></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        f"AI가 <b>{TEMPLATE_LABELS.get(ss.get('biz_계산방식','spend_based'))}</b> 방식으로 설계했습니다.</div>", unsafe_allow_html=True)
            
            질문들 = ss.get("biz_질문들", [])
            # 다음 단계 진입 시 값이 증발하는 것을 방지하기 위해 각 컴포넌트 데이터 처리
            for q in 질문들:
                label = q["label"] + (f" ({q['unit']})" if q.get("unit") else "")
                key_str = "pf_role_" + q["role"]
                if q["type"] == "float":
                    st.number_input(label, min_value=0.0, step=0.1, key=key_str)
                else:
                    st.number_input(label, min_value=0, step=1, key=key_str)

            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_usage_prev"):
                ss.ob_step = 2
                save_profile()
                st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_usage_next"):
                ss.ob_step = 4
                save_profile()
                st.rerun()

        # ── Step 5: Tune ──
        elif step == "tune":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>판매가 반영과 주 유종</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "잘 모르시면 그대로 두고 건너뛰어도 돼요. (선택)</div>", unsafe_allow_html=True)
            
            st.selectbox("주 사용 유종", list(PRODUCTS.keys()), key="pf_유종명")
            st.number_input("판매가 전가율 (%) · 유가 상승분을 판매가에 반영하는 비율", min_value=0, max_value=100, step=1, key="pf_전가율")
            st.number_input("환율 민감도 (%) · 원자재 달러 결제 등 환율 영향도", min_value=0, max_value=100, step=1, key="pf_환율민감도")
            
            c1, c2, c3 = st.columns([1, 1, 1.4])
            if c1.button("← 이전", use_container_width=True, key="ob_tune_prev"):
                ss.ob_step = 3
                save_profile()
                st.rerun()
            if c2.button("건너뛰기", use_container_width=True, key="ob_tune_skip"):
                ss.ob_step = 5
                save_profile()
                st.rerun()
            if c3.button("다음 →", type="primary", use_container_width=True, key="ob_tune_next"):
                ss.ob_step = 5
                save_profile()
                st.rerun()

        # ── Step 6: Region & Final ──
        elif step == "region":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>마지막이에요</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "지역·조회 기간과, 우리 회사만의 특별한 사정을 적어주세요. (선택)</div>", unsafe_allow_html=True)
            
            st.selectbox("사업장 소재 지역", SIDO_LIST, key="pf_지역")
            st.selectbox("리스크 조회 기준 기간", ["7일", "30일", "3개월 (90일)"], key="pf_기간")
            st.number_input("회사 연간 목표 영업이익률 (%)", min_value=0, max_value=100, value=ss.get("pf_영업이익률", 5), key="pf_영업이익률")
            st.number_input("회사 월 평균 총매출액 (원)", min_value=0, step=1000000, value=ss.get("pf_월매출", 100000000), key="pf_월매출")
            st.multiselect("함께 가격 흐름을 비교해보고 싶은 다른 유종 (선택)", list(PRODUCTS.keys()), key="pf_비교유종")
            
            추가질문 = ss.get("biz_추가질문", [])
            for q in 추가질문:
                st.text_input(q["label"], key="pf_extra_" + q["id"])

            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_region_prev"):
                ss.ob_step = 4
                save_profile()
                st.rerun()
            if c2.button("분석 완료 및 대시보드 진입 🚀", type="primary", use_container_width=True, key="ob_region_done"):
                ss["onboarded"] = True
                ss["nav"] = "mypage"
                save_profile() # 온보딩 데이터를 세션과 구글시트에 완벽하게 최종 확정
                st.rerun()

# ═════════════════════════════════════════════════════════════
#  메인 화면 제어 (온보딩 여부 체크)
# ═════════════════════════════════════════════════════════════
if not ss.get("onboarded", False):
    render_onboarding()
    st.stop()

# ═════════════════════════════════════════════════════════════
#  마이페이지 대시보드 렌더링 영역
# ═════════════════════════════════════════════════════════════
def _banner_마지노선(M):
    if M.마지노선 is None:
        return
    여유 = M.마지노선 - M.현재가
    if 여유 > 0:
        st.success(f"💡 **적자 전환 마지노선**: {M.마지노선:,.0f}원/L (현재가 대비 +{여유:,.0f}원 여유가 있습니다.)")
    else:
        st.error(f"🚨 **적자 구조 진입**: 현재 유가({M.현재가:,.0f}원)가 손익분기 마지노선({M.마지노선:,.0f}원)을 초과하여 영업손실 위험 노출 상태입니다!")

def page_dashboard(M):
    기준일_표시 = str(M.view["DATE"].iloc[-1])
    기준일_표시 = f"{기준일_표시[:4]}-{기준일_표시[4:6]}-{기준일_표시[6:8]}"
    st.markdown(f"<div style='text-align:right;margin-top:-10px;margin-bottom:12px'>"
                f"<span style='font-size:12px;color:#6B7684'>오피넷 공공데이터 기준일 <b>{기준일_표시}</b> · {M.기간옵션}치</span><br>"
                f"<span style='font-size:13px;color:#8B95A1'>축적 데이터 {len(M.hist)}일</span></div>", unsafe_allow_html=True)
    st.divider()
    _banner_마지노선(M)
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 " + M.유종명, f"{M.현재가:,.0f}원", f"{M.하루변화:+.1f}원")
    c2.metric(M.기간옵션 + " 변동률", f"{M.기간변동률:+.1f}%")
    c3.metric("실제 부담(환율포함)", f"{M.실제_손실_총:+,.0f}원", f"매출의 {M.매출대비:+.2f}%")
    c4.metric("리스크 " + M.색, f"{M.점수:.0f}점", M.등급)

    if M.환차_패널티 > 0:
        st.caption("이 업종은 원자재를 달러로 수입해, 환율 상승분 약 " + format(M.환차_패널티, ",.0f") + "원이 추가 반영됐습니다. (유가+환율 이중고)")
        
    st.markdown("### AI 리스크 브리핑 & 권장사항")
    with st.container(border=True):
        ctx = (f"업종 {M.업종명}, {M.유종명} 현재가 {M.현재가:,.0f}원, {M.기간옵션} 변동률 {M.기간변동률:+.1f}%({M.방향}), "
               f"월 연료 {M.월_사용량:,.0f}L, 전가율 {M.전가율}%, 실제 부담변화 {M.실제_손실_총:+,.0f}원, "
               f"리스크 {M.점수:.0f}점({M.등급})")
        if st.button("AI 브리핑 생성하기", type="primary", use_container_width=True):
            with st.spinner("분석 중..."):
                text, src = ai_briefing(ctx, M.recs)
                ss["ai_briefing_cached"] = text
        if ss.get("ai_briefing_cached"):
            st.markdown(ss["ai_briefing_cached"])

    st.markdown("### 가격 흐름 시각화 및 추세 연장")
    f_series = None
    if _llm_ready() and len(M.view) >= 3:
        f_series, _ = trend_forecast(M.view["PRICE"].values, days=7)
    fig = styled_chart(M.view, M.유종명, M.기간옵션, forecast=f_series)
    show_fig(fig)

    if M.비교유종:
        st.markdown("#### 선택하신 비교 유종 트렌드")
        for sub_name in M.비교유종:
            if sub_name == M.유종명:
                continue
            sub_df = get_oil(PRODUCTS[sub_name])
            sub_hist = update_history(sub_df, PRODUCTS[sub_name])
            sub_view = sub_hist.tail(7 if M.기간옵션=="7일" else (30 if M.기간옵션=="30일" else 90))
            sub_fig = styled_chart(sub_view, sub_name, M.기간옵션)
            show_fig(sub_fig)

def page_pl(M):
    st.subheader("우리 회사 손익 판단 AI")
    상태 = "손해" if M.실제_손실_총 > 0 else ("이익" if M.실제_손실_총 < 0 else "변화 없음")
    아이콘 = "🔴" if M.실제_손실_총 > 0 else ("🟢" if M.실제_손실_총 < 0 else "⚪")
    
    j1, j2, j3 = st.columns(3)
    j1.metric("판정", 아이콘 + " " + 상태)
    j2.metric("실제 부담(환율포함)", f"{M.실제_손실_총:+,.0f}원")
    j3.metric("연 환산", f"{M.실제_손실_총*12:+,.0f}원")
    
    ai_explain(f"최근 유가·환율 변동을 반영한 결과, 우리 회사는 지금 <b>{상태}</b> 상태로 판정됩니다. "
               f"판매가에 일부 반영(전가율 {M.전가율}%)하고도 실제로 떠안는 금액은 "
               f"<b>{M.실제_손실_총:+,.0f}원</b>이며 이는 총매출의 <b>{M.매출대비:+.2f}%</b> 비중입니다.")
    
    if st.button("AI 정밀 재무 판정 받기", type="primary"):
        with st.spinner("손익 구조를 심층 판정하고 있습니다..."):
            txt, _ = ai_profit_judge(M.업종명, M.연료비_변화, M.실제_손실_총, M.매출대비, M.기간변동률)
            ss["ai_profit_cached"] = txt
    if ss.get("ai_profit_cached"):
        st.info(ss["ai_profit_cached"])

def page_deep(M):
    st.subheader("업종별 유가 리스크 심층 진단")
    st.caption("OREP 핵심 기능 · AI가 다차원 데이터와 외부 지표를 연동해 맞춤형 경영 대응 리포트를 생성합니다.")
    
    if st.button("AI 심층 분석 리포트 발행하기", type="primary", use_container_width=True):
        with st.spinner("전략 리포트 인쇄 중..."):
            txt, _ = ai_deep_analysis(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총, M.매출대비, M.전가율, M.점수, M.등급, M.월_사용량)
            ss["ai_analysis"] = txt
            
    if ss.get("ai_analysis"):
        st.markdown(ss["ai_analysis"])
        st.divider()
        st.subheader("PDF 국가 공인 규격 리포트 추출")
        pdf = make_pdf_report(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총, M.매출대비, M.점수, M.등급, M.recs, ss.get("ai_analysis"), M.마지노선)
        st.download_button("PDF 리포트 내려받기", pdf, "OREP_리포트.pdf", "application/pdf")
        
        st.divider()
        st.subheader("유가 경보 이메일 발송 (수동)")
        st.caption("등록된 이메일 외 다른 주소로 PDF 리포트와 현재 리스크를 전송합니다.")
        받는메일 = st.text_input("받을 이메일 주소", value=ss.get("user_email", ""))
        if st.button("리포트 메일 수동 보내기", type="primary"):
            if SMTP_EMAIL.startswith("여기에"):
                st.warning("발신 계정(SMTP_EMAIL/PASSWORD)이 설정되지 않았습니다.")
            elif not 받는메일:
                st.info("받을 이메일을 입력하세요.")
            else:
                try:
                    본문 = ("[OREP 유가 리스크 경보]\n\n업종: " + M.업종명 + "\n유종: " + M.유종명 + " / 현재가 " + format(M.현재가, ",.0f") + "원\n" + M.기간옵션 + " 변동률: " + format(M.기간변동률, "+.1f") + "%\n실제 부담 변화: " + format(M.실제_손실_총, "+,.0f") + "원\n리스크 점수: " + format(M.점수, ".0f") + "/100 (" + M.등급 + ")\n\n" "[권장 조치]\n" + "\n".join("- " + r.replace("**", "") for r in M.recs) + "\n\n첨부된 PDF 리포트를 확인하세요.\n- OREP")
                    pdf첨부 = make_pdf_report(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총, M.매출대비, M.점수, M.등급, M.recs, ss.get("ai_analysis"), M.마지노선)
                    send_alert_email(받는메일, "[OREP] 유가 리스크 리포트", 본문, pdf_bytes=pdf첨부.getvalue(), filename="OREP_report.pdf")
                    st.success("리포트를 보냈습니다. (PDF 첨부) : " + 받는메일)
                except Exception as ex:
                    st.error("메일 발송 실패: " + str(ex))

def page_news():
    st.subheader("유가 동향 및 환율 실시간 지표")
    n_list = get_news()
    if n_list:
        for item in n_list:
            st.markdown(f"🔗 [{item['title']}]({item['link']}) <span style='font-size:11px;color:#8B95A1'>({item['date']})</span>", unsafe_allow_html=True)
    else:
        st.write("실시간 뉴스를 가져오지 못했습니다.")

    st.divider()
    st.subheader("국제 원유 및 경제 지표")
    intl = get_intl_oil_data()
    if not intl.empty:
        st.dataframe(intl.tail(10), use_container_width=True)
    else:
        st.caption("YFinance 금융망 연동 대기 중입니다.")

def page_profile_edit():
    st.subheader("회사 원가 지표 고도화 및 프로필 변경")
    st.caption("언제든 비즈니스 정보를 변경할 수 있으며, 변경된 즉시 모든 재무 시뮬레이션에 자동 반영됩니다.")
    
    st.markdown("#### 비즈니스 원가 입력 문항")
    질문들 = ss.get("biz_질문들", [])
    for q in 질문들:
        label = q["label"] + (f" ({q['unit']})" if q.get("unit") else "")
        if q["type"] == "float":
            st.number_input(label, min_value=0.0, step=0.1, key="pf_role_" + q["role"])
        else:
            st.number_input(label, min_value=0, step=1, key="pf_role_" + q["role"])

    if ss.get("biz_추가질문"):
        st.markdown("##### 추가 참고 정보")
        for q in ss["biz_추가질문"]:
            st.text_input(q["label"], key="pf_extra_" + q["id"])

    st.markdown("#### 유가 반영·환율 민감도")
    a, b, c = st.columns(3)
    with a:
        st.number_input("판매가 전가율 (%)", min_value=0, max_value=100, step=1, key="pf_전가율")
    with b:
        st.number_input("매출 대비 연료비 비중 (%)", min_value=0, max_value=100, step=1, key="pf_연료비중")
    with c:
        st.number_input("환율 민감도 (%)", min_value=0, max_value=100, step=1, key="pf_환율민감도")
    st.caption("AI 추정치예요. 실제와 다르면 직접 조정하세요 — 리스크 점수·손익분기 계산에 즉시 반영됩니다.")

    st.markdown("#### 유종·매출")
    a, b = st.columns(2)
    with a:
        st.selectbox("주 사용 유종", list(PRODUCTS.keys()), key="pf_유종명")
        st.number_input("영업이익률 (%)", min_value=0, max_value=100, step=1, key="pf_영업이익률")
    with b:
        st.number_input("월 평균 매출액 (원)", min_value=0, step=1000000, key="pf_월매출")
        st.selectbox("사업장 위치", SIDO_LIST, key="pf_지역")

    if st.button("수정된 정보 영구 저장 및 동기화", type="primary", use_container_width=True):
        save_profile()
        st.success("데이터가 안전하게 동기화되었습니다.")
        st.rerun()

# ═════════════════════════════════════════════════════════════
#  사이드바 메뉴 및 레이아웃 제어
# ═════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("<div style='padding:14px 0 6px 0'><span style='font-family:Georgia,serif;font-size:28px;font-weight:900;color:#3182F6'>OREP.</span></div>", unsafe_allow_html=True)
    st.caption(f"반가워요, **{ss.get('user_last_name','')} {ss.get('user_first_name','')}** 대표님")
    
    st.divider()
    if st.button("📊 실시간 유가 대시보드", use_container_width=True):
        ss["nav"] = "mypage"; st.rerun()
    if st.button("📉 우리 회사 손익 판단 AI", use_container_width=True):
        ss["nav"] = "pl_judge"; st.rerun()
    if st.button("🔍 리스크 심층 분석 리포트", use_container_width=True):
        ss["nav"] = "deep_report"; st.rerun()
    if st.button("🌐 국제 지표 & 뉴스", use_container_width=True):
        ss["nav"] = "news"; st.rerun()
    if st.button("⚙️ 회사 프로필 원가 수정", use_container_width=True):
        ss["nav"] = "profile_edit"; st.rerun()
        
    st.divider()
    if st.button("💬 Lino AI 비서 소환", type="primary", use_container_width=True):
        ss["lino_open"] = not ss.get("lino_open", False)
        st.rerun()
        
    st.markdown("</div>", unsafe_allow_html=True)
    st.caption("유가·원가 리스크를 상담하는 AI 비서")
    
    st.divider()
    st.caption("👤 " + _current_user_id())
    if st.button("로그아웃", key="do_logout", use_container_width=True):
        save_profile()
        ss["auth_user"] = None
        st.rerun()
        
    with st.expander("내 데이터 초기화"):
        st.caption("내 계정에 저장된 프로필을 지우고 온보딩을 처음부터 다시 시작합니다.")
        if st.button("초기화 후 다시 시작", key="reset_profile", use_container_width=True):
            for k in list(ss.keys()):
                if k.startswith(PROFILE_KEY_PREFIXES) or k in ("onboarded", "ob_step"):
                    del ss[k]
            _p = _profile_path()
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except Exception:
                    pass
            st.rerun()

# 지표 메트릭스 계산 후 뷰 표출
M = None
try:
    M = compute_metrics()
except Exception as e:
    st.error("유가 데이터를 불러오지 못했습니다. 오피넷 키/네트워크를 확인하세요.\n\n" + str(e))
    st.stop()

if M is None:
    st.error("앱을 초기화하지 못했습니다. `streamlit run app.py` 로 실행했는지 확인해주세요.")
    st.stop()

_logkey = (M.업종명, M.유종명, M.기간옵션)
if ss.get("_last_logkey") != _logkey:
    save_log(M.업종명, M.유종명, M.현재가, M.기간옵션)
    ss["_last_logkey"] = _logkey

check_and_trigger_alert(M)

# Lino 채팅 창 사이드 오픈 레이아웃 처리
if ss.get("lino_open", False):
    c_main, c_lino = st.columns([1.6, 1])
    with c_main:
        if ss["nav"] == "mypage": page_dashboard(M)
        elif ss["nav"] == "pl_judge": page_pl(M)
        elif ss["nav"] == "deep_report": page_deep(M)
        elif ss["nav"] == "news": page_news()
        elif ss["nav"] == "profile_edit": page_profile_edit()
    with c_lino:
        def render_lino(M):
            top1, top2 = st.columns([4, 1])
            with top1:
                st.markdown("<div style='display:flex;align-items:baseline;gap:9px'><span style='font-family:Georgia,serif;font-size:30px;font-weight:900;color:#191F28'>Lino</span><span style='font-size:13px;color:#8B95A1;font-weight:600'>AI 비서 · 유가·원가 상담</span></div>", unsafe_allow_html=True)
            with top2:
                if st.button("닫기", use_container_width=True, key="lino_close_top"):
                    ss.lino_open = False
                    st.rerun()
            
            with st.expander("이렇게 물어보세요 (질문 예시)"):
                st.markdown("- 지금 우리 회사 유가 리스크가 어느 정도인가요?\n- 유가가 10% 오르면 우리 부담은 얼마나 늘어나나요?\n- 지금 연료를 미리 사두는 게 나을까요?")
            
            if not ss.lino_history:
                with st.chat_message("assistant"):
                    st.write("안녕하세요, 대표님. 유가·원가 리스크를 함께 살펴드릴게요. 무엇이 궁금하신가요?")
            
            for role, msg in ss.lino_history:
                with st.chat_message("user" if role == "user" else "assistant"):
                    st.write(msg)
                    
            if st.button("대화 초기화", key="clear_lino"):
                ss.lino_history = []
                st.rerun()
                
            if prompt := st.chat_input("Lino에게 유가 원가 리스크 물어보기"):
                ss.lino_history.append(("user", prompt))
                with st.chat_message("user"):
                    st.write(prompt)
                with st.chat_message("assistant"):
                    with st.spinner("답변 생각 중..."):
                        ctx_summary = (f"업종 {M.업종명}, {M.유종명} 현재가 {M.현재가:,.0f}원, 월 연료 {M.월_사용량:,.0f}L, 전가율 {M.전가율}%, 리스크 {M.점수:.0f}점")
                        res_lino = lino_chat(ss.lino_history, prompt, ctx_summary)
                        st.write(res_lino)
                ss.lino_history.append(("assistant", res_lino))
                st.rerun()
        render_lino(M)
else:
    if ss["nav"] == "mypage": page_dashboard(M)
    elif ss["nav"] == "pl_judge": page_pl(M)
    elif ss["nav"] == "deep_report": page_deep(M)
    elif ss["nav"] == "news": page_news()
    elif ss["nav"] == "profile_edit": page_profile_edit()
