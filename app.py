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

# ═════════════════════════════════════════════════════════════
#  AI 함수
# ═════════════════════════════════════════════════════════════
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
    border-radius:12px !important; border-color:#E5E8EB !important; }

div[data-testid="stExpander"] { border:1px solid #E5E8EB; border-radius:14px; background:#fff; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px; }

.stProgress > div > div > div > div { background-color:#3182F6; }

div[data-testid="stChatMessage"] { background:transparent; }

hr { border-color:#E5E8EB !important; }
</style>
"""
st.markdown(TDS_CSS, unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════
#  로그인 (아이디/비밀번호, 외부 연동 없이 자체 구현)
# ═════════════════════════════════════════════════════════════
USERS_FILE = "users.json"

def _hash_pw(password, salt):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_users(users):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False)
    except Exception:
        pass

ss = st.session_state
ss.setdefault("auth_user", None)

if not ss["auth_user"]:
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            "<div style='text-align:center;padding:60px 0 8px 0'>"
            "<span style='font-family:Georgia,serif;font-size:44px;font-weight:900;"
            "letter-spacing:-2px;color:#3182F6'>OREP<span style='color:#191F28'>.</span></span></div>"
            "<div style='text-align:center;font-size:12px;color:#8B95A1;letter-spacing:2.5px;"
            "font-weight:700;margin-bottom:26px'>OIL RISK EDURE PROGRAM</div>"
            "<div style='text-align:center;color:#4E5968;font-size:15px;margin-bottom:20px;line-height:1.6'>"
            "아이디로 로그인하면 계정별로 데이터가 분리 저장돼요.</div>",
            unsafe_allow_html=True)

        tab_login, tab_signup = st.tabs(["로그인", "회원가입"])

        with tab_login:
            u = st.text_input("아이디", key="li_user")
            p = st.text_input("비밀번호", type="password", key="li_pw")
            if st.button("로그인", type="primary", use_container_width=True, key="li_btn"):
                users = load_users()
                rec = users.get(u)
                if not rec:
                    st.error("존재하지 않는 아이디예요.")
                elif _hash_pw(p, rec["salt"]) != rec["pw_hash"]:
                    st.error("비밀번호가 틀렸어요.")
                else:
                    ss["auth_user"] = u
                    st.rerun()

        with tab_signup:
            nu = st.text_input("사용할 아이디", key="su_user")
            np1 = st.text_input("비밀번호", type="password", key="su_pw1")
            np2 = st.text_input("비밀번호 확인", type="password", key="su_pw2")
            if st.button("회원가입", type="primary", use_container_width=True, key="su_btn"):
                users = load_users()
                if not nu.strip() or not np1:
                    st.warning("아이디와 비밀번호를 입력해주세요.")
                elif nu in users:
                    st.error("이미 있는 아이디예요.")
                elif np1 != np2:
                    st.error("비밀번호 확인이 일치하지 않아요.")
                else:
                    salt = os.urandom(16).hex()
                    users[nu] = {"salt": salt, "pw_hash": _hash_pw(np1, salt)}
                    save_users(users)
                    ss["auth_user"] = nu
                    st.rerun()
    st.stop()

def _current_user_id():
    return ss.get("auth_user") or "unknown"

# ═════════════════════════════════════════════════════════════
#  세션 초기화 (백업 및 복구 로직 포함)
# ═════════════════════════════════════════════════════════════
_first_boot = "_boot_done" not in ss
ss.setdefault("_boot_done", True)
ss.setdefault("onboarded", False)
ss.setdefault("ob_step", 0)
ss.setdefault("nav", "home")
ss.setdefault("lino_open", False)
ss.setdefault("lino_history", [])
ss.setdefault("ai_analysis", None)

PROFILE_KEY_PREFIXES = ("pf_", "biz_", "user_")

def _profile_path():
    uid = _current_user_id()
    safe = re.sub(r"[^a-zA-Z0-9_.@-]", "_", str(uid))
    return os.path.join(PROFILE_DIR, safe + ".json")

def save_profile():
    """온보딩·마이페이지에서 입력한 프로필 값을 로그인 계정별 파일에 저장한다.
    (기존에는 ss.backup이라는 세션 내부 dict에만 백업해서, 세션이 새로
    시작되면 백업 자체도 함께 사라져 값이 유지되지 않는 문제가 있었음)"""
    data = {k: v for k, v in ss.items() if k.startswith(PROFILE_KEY_PREFIXES)}
    try:
        with open(_profile_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        pass

def load_profile():
    p = _profile_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

# 같은 아이디로 새 세션이 시작됐는데 아직 온보딩 전이라면, 이 계정에
# 저장된 프로필이 있는지 확인해서 있으면 그대로 복원한다 → 온보딩 값이
# 마이페이지 등 어디서든 그대로 이어져 보인다.
if _first_boot and not ss.onboarded:
    _saved_profile = load_profile()
    if _saved_profile:
        for k, v in _saved_profile.items():
            ss[k] = v
        ss["onboarded"] = True

_REQUIRED_PROFILE_KEYS = [
    "user_first_name", "user_email",
    "biz_계산방식", "biz_질문들", "pf_업종명", "pf_유종명",
    "pf_전가율", "pf_연료비중", "pf_환율민감도",
    "pf_영업이익률", "pf_월매출", "pf_지역", "pf_기간",
]
if ss.onboarded and any(k not in ss for k in _REQUIRED_PROFILE_KEYS):
    ss.onboarded = False
    ss.ob_step = 0

OB_STEPS = ["user_info", "intro", "confirm", "usage", "sales", "tune", "region"]

WELCOME_MSGS = [
    "오늘도 현명한 결정, 함께 준비해요.",
    "유가의 흐름을 미리 읽어 대비하는 하루 되세요.",
    "작은 변동도 놓치지 않겠습니다.",
    "데이터로 리스크를 줄여드릴게요.",
    "대표님의 원가 부담을 덜어드릴 준비가 됐어요.",
    "위기를 기회로. OREP이 함께합니다.",
    "지금의 대비가 내일의 여유가 됩니다.",
]
if "welcome_idx" not in ss:
    import random as _rd
    ss["welcome_idx"] = _rd.randint(0, len(WELCOME_MSGS)-1)

def apply_business_analysis(result, desc):
    ss["biz_desc"] = desc
    ss["biz_특징"] = result["특징"]
    ss["biz_계산방식"] = result["계산방식"]
    ss["biz_질문들"] = result["필수질문"]
    ss["biz_추가질문"] = result["추가질문"]
    ss["pf_업종명"] = result["업종명"]
    ss["pf_유종명"] = result["주유종"]
    ss["pf_전가율"] = result["전가율"]
    ss["pf_연료비중"] = result["연료비중"]
    ss["pf_환율민감도"] = result["환율민감도"]
    for q in result["필수질문"]:
        ss["pf_role_" + q["role"]] = q["default"]
    for q in result["추가질문"]:
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
    업종명 = ss.get("pf_업종명", "일반 사업체"); 특징 = ss.get("biz_특징", "")
    계산방식 = ss.get("biz_계산방식", "spend_based"); 질문들 = ss.get("biz_질문들", [])
    유종명 = ss.get("pf_유종명", "자동차용경유"); 전가율 = ss.get("pf_전가율", 30)
    연료비중 = ss.get("pf_연료비중", 10); 환율민감도 = ss.get("pf_환율민감도", 0)
    영업이익률 = ss.get("pf_영업이익률", 5); 월_매출 = ss.get("pf_월매출", 0)
    내지역 = ss.get("pf_지역", "서울"); 기간옵션 = ss.get("pf_기간", "7일")
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
    view["변화"] = view["PRICE"].diff()
    평소변동 = view["변화"].abs().mean()
    if pd.isna(평소변동):
        평소변동 = None
    방향 = "상승" if 기간변동률 > 0 else ("하락" if 기간변동률 < 0 else "보합")

    role_values = {q["role"]: ss.get("pf_role_" + q["role"], q["default"]) for q in 질문들}
    try:
        월_사용량 = TEMPLATES[계산방식]["calc"](role_values, 현재가)
    except Exception:
        월_사용량 = 0
    월_사용량 = max(float(월_사용량 or 0), 0)

    연료비_변화 = (현재가 - 시작가) * 월_사용량
    실제_손실 = 연료비_변화 * (1 - 전가율/100)
    기준환율 = 1350.0
    try:
        _cur_fx, _ = get_exchange()
    except Exception:
        _cur_fx = None
    환차_패널티 = 0.0
    if _cur_fx and 환율민감도 > 0:
        환율상승률 = (_cur_fx - 기준환율) / 기준환율
        원자재_월원가 = 현재가 * 월_사용량 * (환율민감도/100) * 3
        환차_패널티 = 원자재_월원가 * max(환율상승률, 0)
    실제_손실_총 = 실제_손실 + 환차_패널티
    매출대비 = (실제_손실_총 / 월_매출 * 100) if 월_매출 else 0
    점수 = min(100, abs(기간변동률)*4 + (100-전가율)*0.4 + 연료비중*0.6)
    등급, 색 = ("낮음", "🟢") if 점수 < 33 else (("보통", "🟡") if 점수 < 66 else ("높음", "🔴"))
    recs = build_recommendations(기간변동률, 실제_손실_총, 전가율, 점수)
    마지노선 = breakeven_price(현재가, 월_사용량, 전가율, 월_매출, 영업이익률)

    return SimpleNamespace(**locals())

# ═════════════════════════════════════════════════════════════
#  조기 경보 알림 로직 (AI 판단 자동 발송)
# ═════════════════════════════════════════════════════════════
def check_and_send_alert(M):
    email = ss.get("user_email")
    if not email or SMTP_EMAIL.startswith("여기에"):
        return

    평소 = M.평소변동 if M.평소변동 else 1
    급변감지 = False
    이유 = ""

    # 1. 일일 가격 변동폭이 평소의 3배 이상인 경우 (기준 상향 조정)
    if abs(M.하루변화) >= 평소 * 3:
        급변감지 = True
        이유 = f"일일 가격 변동폭({M.하루변화:+.1f}원)이 평소({평소:.1f}원) 대비 3배 이상 크게 발생했습니다."
    # 2. 누적 기간 변동률이 5% 이상인 경우
    elif abs(M.기간변동률) >= 5.0:
        급변감지 = True
        이유 = f"설정하신 기간 내 누적 변동률({M.기간변동률:+.1f}%)이 5%를 초과했습니다."

    # 중복 발송 방지 로직: 이전 알림 발송 시점의 가격과 비교하여 2% 이상 추가 변동이 없으면 발송 무시
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
            ss["last_alert_price"] = M.현재가  # 알림을 보낸 기준 가격으로 업데이트
            st.toast("⚠️ 유가 큰 변동이 감지되어 조기 경보 이메일이 자동 발송되었습니다.")
        except Exception as e:
            pass

# ═════════════════════════════════════════════════════════════
#  온보딩 위저드
# ═════════════════════════════════════════════════════════════
def render_onboarding():
    st.markdown("<style>section[data-testid='stSidebar']{display:none !important;}</style>",
                unsafe_allow_html=True)
    step = OB_STEPS[ss.ob_step]
    total = len(OB_STEPS)
    _, mid, _ = st.columns([1, 3.2, 1])
    with mid:
        st.markdown(
            "<div style='text-align:center;padding:6px 0 2px 0'>"
            "<span style='font-family:Georgia,serif;font-size:40px;font-weight:900;"
            "letter-spacing:-2px;color:#3182F6'>OREP<span style='color:#191F28'>.</span></span></div>"
            "<div style='text-align:center;font-size:11px;color:#8B95A1;letter-spacing:2.5px;"
            "font-weight:700;margin-bottom:6px'>OIL RISK EDURE PROGRAM</div>",
            unsafe_allow_html=True)
        st.progress((ss.ob_step) / (total - 1))
        st.markdown(f"<div style='text-align:right;font-size:12px;color:#8B95A1;margin-top:-6px'>"
                    f"{ss.ob_step+1} / {total} 단계</div>", unsafe_allow_html=True)
        st.write("")

        if step == "user_info":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "사용자 정보와 조기 알림 설정</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;font-size:15px;margin:6px 0 18px 0'>"
                        "유가 큰 변동 시 AI가 상황을 판단하여 이메일로 즉시 알려드려요.</div>",
                        unsafe_allow_html=True)
            st.text_input("성 (Last Name)", key="user_last_name", placeholder="예: 김")
            st.text_input("이름 (First Name)", key="user_first_name", placeholder="예: 대표")
            st.text_input("조기 경보 알림을 받을 이메일", key="user_email", placeholder="예: user@company.com")

            if st.button("다음 →", type="primary", use_container_width=True, key="ob_user_info_next"):
                if not ss.get("user_first_name"):
                    st.warning("이름을 입력해주세요. (필수)")
                elif not ss.get("user_email"):
                    st.warning("경보 알림을 받을 이메일을 입력해주세요. (필수)")
                else:
                    ss.ob_step += 1; st.rerun()

        elif step == "intro":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:26px;font-weight:800;color:#191F28'>"
                        "어떤 사업을 하고 계신가요?</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;font-size:15px;margin:6px 0 18px 0'>"
                        "업종과 특징을 편하게 적어주시면, AI가 읽고 맞춤 질문을 준비해요.</div>",
                        unsafe_allow_html=True)
            st.text_area(
                "회사 소개",
                key="ob_desc",
                height=150,
                placeholder="예) 수도권에서 냉동식품을 배송하는 물류회사예요. 냉동탑차 8대를 운영하고, "
                            "여름철엔 물량이 두 배로 늘어요. 거래처와는 3개월 단위로 단가를 다시 정해요.",
                label_visibility="collapsed",
            )
            st.caption("이 내용은 나중에 Lino 비서가 상담할 때도 함께 참고해요.")
            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_intro_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_intro_next"):
                if not ss.get("ob_desc", "").strip():
                    st.warning("사업 내용을 한두 줄이라도 적어주세요. (필수)")
                else:
                    with st.spinner("AI가 우리 사업에 맞는 질문을 준비하고 있어요..."):
                        result = analyze_business(ss["ob_desc"])
                    apply_business_analysis(result, ss["ob_desc"])
                    ss.ob_step += 1
                    st.rerun()

        elif step == "confirm":
            특징 = ss.get("biz_특징", "")
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "이렇게 이해했어요</span></div>", unsafe_allow_html=True)
            if 특징:
                st.markdown(f"<div style='background:#F2F6FF;border-radius:12px;padding:12px 16px;"
                            f"margin:12px 0;color:#4E5968;font-size:14px'>"
                            f"<b style='color:#3182F6'>AI 요약</b> · {특징}</div>",
                            unsafe_allow_html=True)
            st.text_input("업종명 (마음에 안 들면 직접 고쳐도 돼요)", key="pf_업종명")
            st.caption("업종을 정해진 목록에서 고른 게 아니라, AI가 설명을 읽고 자유롭게 지어낸 이름이에요.")
            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_confirm_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_confirm_next"):
                ss.ob_step += 1; st.rerun()

        elif step == "usage":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "연료 사용량을 알려주세요</span></div>", unsafe_allow_html=True)
            st.markdown(f"<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        f"AI가 <b>{ss['pf_업종명']}</b>에 맞춰 준비한 질문이에요. (필수)</div>",
                        unsafe_allow_html=True)

            override = st.checkbox(
                "정확한 수치를 모르겠어요 → 월 유류비 총 지출액으로 대신 입력할게요",
                key="pf_override_spend")
            if override and not ss.get("_applied_override"):
                if ss["biz_계산방식"] != "spend_based":
                    ss["_orig_계산방식"] = ss["biz_계산방식"]
                    ss["_orig_질문들"] = ss["biz_질문들"]
                    role = "monthly_spend"
                    flabel, fph, funit, fdefault, ftype = TEMPLATES["spend_based"]["fallback"][role]
                    ss["biz_계산방식"] = "spend_based"
                    ss["biz_질문들"] = [{"role": role, "label": flabel, "placeholder": fph,
                                       "unit": funit, "default": fdefault, "type": ftype}]
                    ss["pf_role_" + role] = fdefault
                ss["_applied_override"] = True
            elif not override and ss.get("_applied_override"):
                if "_orig_계산방식" in ss:
                    ss["biz_계산방식"] = ss["_orig_계산방식"]
                    ss["biz_질문들"] = ss["_orig_질문들"]
                ss["_applied_override"] = False

            for q in ss["biz_질문들"]:
                label = q["label"] + (f"  ({q['unit']})" if q.get("unit") else "")
                if q["type"] == "float":
                    st.number_input(label, min_value=0.0, step=0.1, key="pf_role_" + q["role"])
                else:
                    st.number_input(label, min_value=0, step=1, key="pf_role_" + q["role"])
                if q.get("placeholder"):
                    st.caption(q["placeholder"])

            if ss.get("biz_추가질문"):
                st.markdown("**참고할 추가 정보 (선택)**")
                for q in ss["biz_추가질문"]:
                    st.text_input(q["label"], key="pf_extra_" + q["id"])

            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_usage_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_usage_next"):
                ss.ob_step += 1; st.rerun()

        elif step == "sales":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "매출 규모를 알려주세요</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "유가 부담이 매출·이익에서 차지하는 비중을 계산해요. (필수)</div>",
                        unsafe_allow_html=True)
            st.number_input("월 매출 (원)", min_value=0, step=1000000, key="pf_월매출")
            st.number_input("영업이익률 (%)", min_value=0, max_value=100, step=1, key="pf_영업이익률")
            c1, c2 = st.columns([1, 2])
            if c1.button("← 이전", use_container_width=True, key="ob_sales_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("다음 →", type="primary", use_container_width=True, key="ob_sales_next"):
                if ss["pf_월매출"] <= 0:
                    st.warning("월 매출을 입력해주세요. (필수)")
                else:
                    ss.ob_step += 1; st.rerun()

        elif step == "tune":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "판매가 반영과 주 유종</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "잘 모르시면 그대로 두고 건너뛰어도 돼요. (선택)</div>",
                        unsafe_allow_html=True)
            st.selectbox("주 사용 유종", list(PRODUCTS.keys()), key="pf_유종명")
            st.number_input("판매가 전가율 (%)  ·  유가 상승분을 판매가에 반영하는 비율",
                            min_value=0, max_value=100, step=1, key="pf_전가율")
            st.number_input("환율 민감도 (%)  ·  원자재 달러 결제 등 환율 영향도",
                            min_value=0, max_value=100, step=1, key="pf_환율민감도")

            c1, c2, c3 = st.columns([1, 1, 1.4])
            if c1.button("← 이전", use_container_width=True, key="ob_tune_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("건너뛰기", use_container_width=True, key="ob_tune_skip"):
                ss.ob_step += 1; st.rerun()
            if c3.button("다음 →", type="primary", use_container_width=True, key="ob_tune_next"):
                ss.ob_step += 1; st.rerun()

        elif step == "region":
            st.markdown("<div style='text-align:center'>"
                        "<span style='font-size:24px;font-weight:800;color:#191F28'>"
                        "마지막이에요</span></div>", unsafe_allow_html=True)
            st.markdown("<div style='text-align:center;color:#6B7684;margin:6px 0 16px 0'>"
                        "지역·조회 기간과, 우리 회사만의 특별한 사정을 적어주세요. (선택)</div>",
                        unsafe_allow_html=True)
            st.selectbox("우리 지역(시도)", SIDO_LIST, key="pf_지역")
            st.radio("기본 조회 기간", ["7일", "30일", "전체"], horizontal=True, key="pf_기간")
            st.multiselect("함께 비교할 유종", list(PRODUCTS.keys()), key="pf_비교유종")
            st.text_area("추가 정보 (자유 입력)", key="pf_notes", height=100,
                         placeholder="예) 특정 거래처와 3개월 고정단가 계약 중 / 성수기엔 물량 2배 등")
            c1, c2, c3 = st.columns([1, 1, 1.4])
            if c1.button("← 이전", use_container_width=True, key="ob_region_prev"):
                ss.ob_step -= 1; st.rerun()
            if c2.button("건너뛰기", use_container_width=True, key="ob_region_skip"):
                ss.onboarded = True; ss.nav = "home"; st.rerun()
            if c3.button("완료 · 시작하기", type="primary", use_container_width=True, key="ob_region_done"):
                ss.onboarded = True; ss.nav = "home"; st.rerun()

# ═════════════════════════════════════════════════════════════
#  Lino 오버레이
# ═════════════════════════════════════════════════════════════
def build_lino_ctx(M):
    _be_txt = (format(M.마지노선, ",.0f") + "원/L (현재 대비 " +
               format((M.마지노선-M.현재가)/M.현재가*100, "+.1f") + "% 여유)") if M.마지노선 else "계산 불가"
    _fx_txt = (format(M._cur_fx, ",.1f") + "원") if M._cur_fx else "미제공"
    ctx = (
        "- 업종: " + M.업종명 + " (연료비 원가비중 " + str(M.연료비중) +
        "%, 환율민감도 " + str(M.환율민감도) + "%)\n"
        "- 주 유종: " + M.유종명 + ", 현재가 " + format(M.현재가, ",.0f") + "원/L\n"
        "- 조회기간: " + M.기간옵션 + ", 기간 변동률 " + format(M.기간변동률, "+.1f") + "%\n"
        "- 월 연료 사용량: " + format(M.월_사용량, ",.0f") + "L\n"
        "- 현재 월 연료비(추정): " + format(M.현재가*M.월_사용량, ",.0f") + "원\n"
        "- 판매가 전가율: " + str(M.전가율) + "%\n"
        "- 이번 기간 실제 부담 변화(환율 포함): " + format(M.실제_손실_총, "+,.0f") +
        "원 (매출의 " + format(M.매출대비, "+.2f") + "%)\n"
        "- 연 환산 부담: " + format(M.실제_손실_총*12, "+,.0f") + "원\n"
        "- 적자 전환 마지노선 유가: " + _be_txt + "\n"
        "- 영업이익률: " + str(M.영업이익률) + "%\n"
        "- 원/달러 환율: " + _fx_txt + "\n"
        "- 리스크 점수: " + format(M.점수, ".0f") + "/100 (" + M.등급 + ")\n"
        "- 월 매출: " + format(M.월_매출, ",.0f") + "원")
    if M.owner_notes:
        ctx += "\n- 대표님이 직접 입력한 추가 정보:\n" + M.owner_notes
    return ctx

def render_lino(M):
    top1, top2 = st.columns([4, 1])
    with top1:
        st.markdown(
            "<div style='display:flex;align-items:baseline;gap:9px'>"
            "<span style='font-family:Georgia,serif;font-size:30px;font-weight:900;"
            "letter-spacing:-1px;color:#191F28'>Lino</span>"
            "<span style='font-size:13px;color:#8B95A1;font-weight:600'>AI 비서 · 유가·원가 상담</span>"
            "</div>", unsafe_allow_html=True)
    with top2:
        if st.button("닫기", use_container_width=True, key="lino_close_top"):
            ss.lino_open = False; st.rerun()

    with st.expander("이렇게 물어보세요 (질문 예시)"):
        st.markdown(
            "- 지금 우리 회사 유가 리스크가 어느 정도인가요?\n"
            "- 유가가 10% 오르면 우리 부담은 얼마나 늘어나나요?\n"
            "- 지금 연료를 미리 사두는 게 나을까요?\n"
            "- 거래처와 단가 협상은 어떻게 시작하면 좋을까요?\n"
            "- 적자 전환 마지노선까지 얼마나 여유가 있나요?")

    if not ss.lino_history:
        with st.chat_message("assistant"):
            st.write("안녕하세요, 대표님. 유가·원가 리스크를 함께 살펴드릴게요. 무엇이 궁금하신가요?")
    for role, msg in ss.lino_history:
        with st.chat_message("user" if role == "user" else "assistant"):
            st.write(msg)

    if st.button("대화 초기화", key="lino_reset"):
        ss.lino_history = []; st.rerun()

    q = st.chat_input("Lino에게 물어보기")
    if q:
        ss.lino_history.append(("user", q))
        with st.spinner("Lino가 생각 중..."):
            ans = lino_chat(ss.lino_history, q, build_lino_ctx(M))
        ss.lino_history.append(("lino", ans))
        st.rerun()

# ═════════════════════════════════════════════════════════════
#  개별 페이지
# ═════════════════════════════════════════════════════════════
def _banner_마지노선(M):
    if not M.마지노선:
        return
    여유 = M.마지노선 - M.현재가
    여유율 = 여유 / M.현재가 * 100 if M.현재가 else 0
    색 = "#F04452" if 여유율 < 10 else ("#FFA000" if 여유율 < 25 else "#15B76E")
    st.markdown(
        "<div style='background:" + 색 + "12;border-left:6px solid " + 색 +
        ";padding:14px 18px;border-radius:12px;margin-bottom:10px'>"
        "<span style='font-size:14px;color:#6B7684'>우리 회사 적자 전환 마지노선 (" + M.유종명 + ")</span><br>"
        "<span style='font-size:30px;font-weight:800;color:" + 색 + "'>" + format(M.마지노선, ",.0f") + "원/L</span>"
        "<span style='font-size:15px;color:#6B7684'>  ·  현재가 대비 +" + format(여유, ",.0f") +
        "원 (" + format(여유율, ".1f") + "% 여유)</span></div>", unsafe_allow_html=True)

def page_home(M):
    check_and_send_alert(M)

    now = datetime.datetime.now()
    시각 = now.hour
    인사 = "좋은 아침입니다" if 5 <= 시각 < 12 else ("좋은 오후입니다" if 12 <= 시각 < 18 else "안녕하세요")
    이름 = ss.get("user_first_name", "대표")
    welcome = WELCOME_MSGS[ss["welcome_idx"]]
    기준일 = M.view["DATE"].iloc[-1]
    기준일_표시 = 기준일[4:6] + "/" + 기준일[6:8]

    hl, hr = st.columns([3, 2])
    with hl:
        st.markdown("<div style='line-height:1.5;padding-top:2px'>"
            "<span style='font-size:26px;font-weight:800;color:#191F28'>" + 인사 + ", " + 이름 + "님</span><br>"
            "<span style='font-size:15px;color:#6B7684'>" + welcome + "</span></div>",
            unsafe_allow_html=True)
    with hr:
        st.markdown(f"<div style='text-align:right;line-height:1.7;padding-top:6px'>"
            f"<span style='font-size:13px;color:#8B95A1'>{now.strftime('%Y-%m-%d %H:%M')} 접속</span><br>"
            f"<span style='font-size:13px;color:#8B95A1'>유가 기준일 <b>{기준일_표시}</b> · {M.기간옵션}치</span><br>"
            f"<span style='font-size:13px;color:#8B95A1'>축적 데이터 {len(M.hist)}일</span></div>",
            unsafe_allow_html=True)
    st.divider()

    _banner_마지노선(M)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 " + M.유종명, f"{M.현재가:,.0f}원", f"{M.하루변화:+.1f}원")
    c2.metric(M.기간옵션 + " 변동률", f"{M.기간변동률:+.1f}%")
    c3.metric("실제 부담(환율포함)", f"{M.실제_손실_총:+,.0f}원", f"매출의 {M.매출대비:+.2f}%")
    c4.metric("리스크 " + M.색, f"{M.점수:.0f}점", M.등급)
    if M.환차_패널티 > 0:
        st.caption("이 업종은 원자재를 달러로 수입해, 환율 상승분 약 " + format(M.환차_패널티, ",.0f") +
                   "원이 추가 반영됐습니다. (유가+환율 이중고)")

    st.markdown("### AI 리스크 브리핑 & 권장사항")
    with st.container(border=True):
        ctx = (f"업종 {M.업종명}, {M.유종명} 현재가 {M.현재가:,.0f}원, {M.기간옵션} 변동률 {M.기간변동률:+.1f}%({M.방향}), "
               f"월 연료 {M.월_사용량:,.0f}L, 전가율 {M.전가율}%, 실제 부담변화 {M.실제_손실_총:+,.0f}원, "
               f"리스크 {M.점수:.0f}점({M.등급})")
        if st.button("AI 브리핑 생성하기", type="primary", use_container_width=True):
            with st.spinner("분석 중..."):
                text, source = ai_briefing(ctx, M.recs)
            st.markdown(text)
            st.caption("생성 방식: " + source)
        else:
            st.markdown("**지금 우리 회사가 해야 할 일**")
            for r in M.recs:
                st.markdown("- " + r)
    st.caption("좌측 메뉴에서 유가 추이·손익 판단·뉴스 등 상세 페이지를 확인하세요.")

def page_price(M):
    st.subheader("유가 추이")
    평소 = M.평소변동 if M.평소변동 else 1
    if abs(M.하루변화) > 평소 * 1.5:
        방향2 = "급등" if M.하루변화 > 0 else "급락"
        st.error("경보: " + M.유종명 + " " + 방향2 + " 감지!")
    else:
        st.success("안정: " + M.유종명 + " 변동이 평소 수준입니다.")
    show_fc = st.checkbox("추세 연장선 표시 (예측 아님, 참고용)", value=False)
    fc = None
    if show_fc:
        res_fc = trend_forecast(M.view["PRICE"].values, 7)
        if res_fc is not None:
            fc = res_fc[0]
    show_fig(styled_chart(M.view, M.유종명, M.기간옵션, fc))
    _상태 = "평소보다 크게 움직이고 있어 주의가 필요합니다" if abs(M.하루변화) > 평소 * 1.5 else "평소와 비슷한 수준으로 움직이고 있습니다"
    ai_explain(
        f"이 그래프는 최근 {M.기간옵션} 동안 {M.유종명} 가격이 어떻게 바뀌었는지 보여줍니다. "
        f"기간 시작 대비 <b>{M.방향}세로 {abs(M.기간변동률):.1f}%</b> 움직였고, 현재가는 <b>{M.현재가:,.0f}원</b>입니다. "
        f"전일 대비로는 {_상태}."
        + (" 점선은 최근 추세를 단순히 연장한 참고선일 뿐, 실제 미래 예측이 아닙니다." if fc is not None else ""))
    if M.비교유종:
        st.markdown("#### 유종 비교")
        fig2, ax2 = plt.subplots(figsize=(10, 3.6))
        팔레트 = ["#3182F6", "#191F28", "#15B76E", "#FFA000"]
        for i, uj in enumerate([M.유종명] + [u for u in M.비교유종 if u != M.유종명]):
            try:
                d = get_oil(PRODUCTS[uj]).tail(len(M.view))
                ax2.plot(range(len(d)), d["PRICE"].values, marker="o", markersize=3,
                         color=팔레트[i % len(팔레트)], label=uj)
            except Exception:
                pass
        ax2.legend(fontsize=9, frameon=False); ax2.grid(axis="y", alpha=0.3)
        for s in ["top", "right"]:
            ax2.spines[s].set_visible(False)
        show_fig(fig2)
        ai_explain(
            f"위 그래프는 주 사용 유종 <b>{M.유종명}</b>과 선택하신 유종({', '.join(M.비교유종)})의 "
            f"최근 가격 흐름을 나란히 비교한 것입니다. 선이 비슷하면 가격 상관성이 높다는 뜻이고, "
            f"벌어지면 유종 교체 시 비용 구조가 달라질 수 있다는 의미입니다.")

def page_pl(M):
    st.subheader("우리 회사 손익 판단 AI")
    상태 = "손해" if M.실제_손실_총 > 0 else ("이익" if M.실제_손실_총 < 0 else "변화 없음")
    아이콘 = "🔴" if M.실제_손실_총 > 0 else ("🟢" if M.실제_손실_총 < 0 else "⚪")
    j1, j2, j3 = st.columns(3)
    j1.metric("판정", 아이콘 + " " + 상태)
    j2.metric("실제 부담(환율포함)", f"{M.실제_손실_총:+,.0f}원")
    j3.metric("연 환산", f"{M.실제_손실_총*12:+,.0f}원")
    ai_explain(
        f"최근 유가·환율 변동을 반영한 결과, 우리 회사는 지금 <b>{상태}</b> 상태로 판정됩니다. "
        f"판매가에 일부 반영(전가율 {M.전가율}%)하고도 실제로 떠안는 금액은 "
        f"<b>{M.실제_손실_총:+,.0f}원</b>이며, 이는 월 매출의 <b>{M.매출대비:+.2f}%</b>에 해당합니다. "
        f"같은 흐름이 1년 지속되면 연간 약 <b>{M.실제_손실_총*12:+,.0f}원</b> 규모입니다.")
    if st.button("AI 손익 판단 받기", type="primary"):
        with st.spinner("판단 중..."):
            text, source = ai_profit_judge(M.업종명, M.연료비_변화, M.실제_손실_총, M.매출대비, M.기간변동률)
        st.markdown(text)
        st.caption("생성 방식: " + source)

def page_be(M):
    st.subheader("손익분기 유가")
    st.caption("유가가 어디까지 오르면 이익이 0이 되는지 계산합니다.")
    be = M.마지노선
    if be:
        여유 = be - M.현재가
        여유율 = 여유 / M.현재가 * 100
        b1, b2 = st.columns(2)
        b1.metric("손익분기 유가", f"{be:,.0f}원/L", f"현재가 대비 +{여유:,.0f}원")
        b2.metric("가격 여유", f"{여유율:+.1f}%")
        if 여유율 < 10:
            st.error(f"유가가 {여유율:.1f}%만 더 올라도 적자 전환됩니다. 매우 취약합니다.")
        elif 여유율 < 25:
            st.warning(f"유가가 {여유율:.1f}% 오르면 적자 전환됩니다. 대비가 필요합니다.")
        else:
            st.success(f"현재는 {여유율:.1f}%의 여유가 있습니다.")
        st.caption("영업이익률·전가율·사용량을 마이페이지에서 조정하면 실시간 반영됩니다.")
        ai_explain(
            f"지금 {M.유종명} 가격은 <b>{M.현재가:,.0f}원</b>인데, 이 가격이 <b>{be:,.0f}원</b>까지 오르면 "
            f"우리 회사 영업이익이 정확히 0원이 되는 지점(손익분기)입니다. 현재는 그 지점까지 "
            f"<b>{여유율:+.1f}%</b>의 여유가 있으며, 이 값이 낮을수록 유가 상승에 취약합니다.")
    else:
        st.info("마이페이지에서 연료 사용량·전가율을 입력하면 계산됩니다.")

def page_news(M):
    st.subheader("실시간 유가 뉴스")
    검색어 = st.selectbox("주제", ["국제유가", "유가 전망", "기름값", "환율 유가"], index=0)
    news = get_news(검색어)
    if news:
        ai_explain(
            f"‘{검색어}’ 관련 최신 뉴스 <b>{len(news)}건</b>을 모아왔습니다. 제목을 눌러 원문을 확인하고, "
            f"우리 회사 유가 리스크(현재 {M.점수:.0f}점, {M.등급})와 함께 참고하시면 시장 분위기를 "
            f"가늠하는 데 도움이 됩니다.")
        for n in news:
            st.markdown(f"**[{n['title']}]({n['link']})**")
            if n["date"]:
                st.caption(n["date"])
            st.divider()
    else:
        st.info("뉴스를 불러오지 못했습니다.")

def page_region(M):
    st.subheader("지역별 유가 비교")
    try:
        sido_df = get_oil_sido(PRODUCTS[M.유종명])
        col = "SIDONM" if "SIDONM" in sido_df.columns else sido_df.columns[0]
        내값 = sido_df[sido_df[col].astype(str).str.contains(M.내지역)]
        if not 내값.empty:
            지역가 = float(내값["PRICE"].iloc[0])
            st.metric(M.내지역 + " " + M.유종명, f"{지역가:,.0f}원",
                      f"전국평균 대비 {지역가 - M.hist['PRICE'].mean():+.0f}원")
        st.dataframe(sido_df[[col, "PRICE"]].sort_values("PRICE"), use_container_width=True, height=260)
        if not 내값.empty:
            _비교 = "비싼" if 지역가 > M.hist["PRICE"].mean() else "저렴한"
            ai_explain(
                f"<b>{M.내지역}</b>의 {M.유종명} 가격은 <b>{지역가:,.0f}원</b>으로, 전국 평균보다 "
                f"<b>{abs(지역가 - M.hist['PRICE'].mean()):,.0f}원</b> {_비교} 편입니다. "
                f"아래 표는 낮은 가격순 정렬이라, 위쪽일수록 그 지역 유가가 쌉니다.")
    except Exception:
        st.info("지역별 유가는 현재 제공되지 않습니다.")

    st.divider()
    st.subheader("국제 원유가 추이 (WTI, Brent)")
    st.caption("yfinance를 통해 최근 1개월 국제유가를 자동으로 연동하여 보여줍니다.")

    with st.spinner("국제유가 데이터를 불러오는 중입니다..."):
        intl = get_intl_oil_data()

    if not intl.empty:
        값열 = [c for c in intl.columns if c != "날짜"]
        fig_i, ax_i = plt.subplots(figsize=(10, 4.2))
        fig_i.patch.set_facecolor("#ffffff"); ax_i.set_facecolor("#f4f6f8")
        색상 = ["#3182F6", "#191F28", "#15B76E", "#FFA000"]

        for i, c in enumerate(값열):
            ax_i.plot(intl["날짜"], intl[c], linewidth=2.2,
                      color=색상[i % len(색상)], label=c, marker="o", markersize=3)
        ax_i.set_ylabel("가격 (USD/배럴)", fontsize=11, color="#6B7684")
        ax_i.set_title("최근 1개월 국제유가 동향",
                       fontsize=13, fontweight="bold", pad=12, loc="left")
        ax_i.legend(loc="upper left", fontsize=9, frameon=False)
        ax_i.grid(axis="y", color="#E5E8EB", linewidth=0.8, zorder=0)
        for s in ["top", "right"]:
            ax_i.spines[s].set_visible(False)
        for s in ["left", "bottom"]:
            ax_i.spines[s].set_color("#D1D6DB")
        ax_i.tick_params(colors="#6B7684")
        ax_i.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax_i.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        fig_i.tight_layout()
        show_fig(fig_i)

        _시작 = intl[값열[0]].iloc[0]; _끝 = intl[값열[0]].iloc[-1]
        _추세 = "상승" if _끝 > _시작 else ("하락" if _끝 < _시작 else "보합")
        ai_explain(
            f"자동 연동된 국제유가 데이터에 따르면, 최근 1개월간 <b>{_추세}</b> 흐름을 보였습니다"
            f"({값열[0]} 기준 {_시작:,.2f} → {_끝:,.2f} $/bbl). "
            f"국제유가는 보통 1~2주 시차를 두고 국내 유가에 반영되므로 선제적 리스크 관리에 유용합니다.")

        st.caption("표: 일자별 국제유가 (단위: USD/배럴)")
        표 = intl.copy()
        표["날짜"] = 표["날짜"].dt.strftime("%Y-%m-%d")
        col_cfg = {c: st.column_config.NumberColumn(c, format="$%.2f") for c in 값열}
        st.dataframe(표.sort_values("날짜", ascending=False), use_container_width=True, hide_index=True, column_config=col_cfg)
    else:
        st.info("현재 국제 유가 데이터를 불러오지 못했습니다. 네트워크 상태나 yfinance 라이브러리 설치를 확인하세요.")

    st.divider()
    st.subheader("원/달러 환율")
    환율, 환율날짜 = get_exchange()
    if 환율:
        st.metric("현재 환율", f"{환율:,.1f}원", help="기준일 " + 환율날짜)
        if 환율 > 1350:
            st.warning("환율이 높은 편입니다. 원화 부담이 커질 수 있습니다.")
        ai_explain(
            f"현재 원/달러 환율은 <b>{환율:,.1f}원</b>입니다. 원자재를 달러로 수입하는 업종일수록 "
            f"환율이 오르면 유가가 그대로여도 원화 기준 원가가 늘어나는 '이중고'가 생길 수 있습니다. "
            + ("지금 환율은 다소 높은 편이라 원가 부담이 커질 수 있는 구간입니다." if 환율 > 1350
               else "지금 환율은 비교적 안정적인 구간입니다."))
    else:
        st.info("환율 데이터를 불러오지 못했습니다.")

def page_report(M):
    st.subheader("AI 심층 분석")
    st.caption("현재 상황 진단 / 업종 영향 / 단기 대응 / 중장기 관리까지 상세 분석합니다.")
    if st.button("AI 심층 분석 생성", type="primary"):
        with st.spinner("AI가 분석 중..."):
            _atext, _asrc = ai_deep_analysis(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총,
                                             M.매출대비, M.전가율, M.점수, M.등급, M.월_사용량)
        ss["ai_analysis"] = _atext
        st.markdown(_atext)
        st.caption("생성 방식: " + _asrc)
    elif ss.get("ai_analysis"):
        st.markdown(ss["ai_analysis"])

    st.divider()
    st.subheader("PDF 리포트")
    st.caption("AI 심층 분석을 생성했다면 리포트에 함께 포함됩니다. 회의·협상용으로 저장하세요.")
    pdf = make_pdf_report(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총, M.매출대비,
                          M.점수, M.등급, M.recs, ss.get("ai_analysis"), M.마지노선)
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
                본문 = ("[OREP 유가 리스크 경보]\n\n업종: " + M.업종명 + "\n유종: " + M.유종명 +
                       " / 현재가 " + format(M.현재가, ",.0f") + "원\n" + M.기간옵션 + " 변동률: " +
                       format(M.기간변동률, "+.1f") + "%\n실제 부담 변화: " + format(M.실제_손실_총, "+,.0f") +
                       "원\n리스크 점수: " + format(M.점수, ".0f") + "/100 (" + M.등급 + ")\n\n"
                       "[권장 조치]\n" + "\n".join("- " + r.replace("**", "") for r in M.recs) +
                       "\n\n첨부된 PDF 리포트를 확인하세요.\n- OREP")
                pdf첨부 = make_pdf_report(M.업종명, M.유종명, M.현재가, M.기간변동률, M.실제_손실_총, M.매출대비,
                                        M.점수, M.등급, M.recs, ss.get("ai_analysis"), M.마지노선)
                send_alert_email(받는메일, "[OREP] 유가 리스크 리포트", 본문,
                                 pdf_bytes=pdf첨부.getvalue(), filename="OREP_report.pdf")
                st.success("리포트를 보냈습니다. (PDF 첨부) : " + 받는메일)
            except Exception as e:
                st.error("발송 실패: " + str(e))

def page_mypage(M):
    st.subheader("마이페이지 · 상세 설정")
    st.caption("여기서 값을 바꾸면 모든 페이지에 실시간으로 반영됩니다.")

    with st.container(border=True):
        st.markdown("**기본 사용자 정보**")
        a1, a2, a3 = st.columns(3)
        with a1:
            st.text_input("성 (Last Name)", key="user_last_name")
        with a2:
            st.text_input("이름 (First Name)", key="user_first_name")
        with a3:
            st.text_input("조기 경보 알림 이메일", key="user_email")

    with st.container(border=True):
        st.markdown("**우리 회사 정보 요약**")
        st.text_input("업종명", key="pf_업종명")
        if M.특징:
            st.caption("AI 요약 · " + M.특징)
        with st.expander("맨 처음 입력했던 사업 설명 보기"):
            st.write(ss.get("biz_desc", "") or "_(입력 내용 없음)_")
        st.caption("계산 방식 · " + TEMPLATE_LABELS.get(M.계산방식, M.계산방식))
        if st.button("업종 다시 설명하기 (AI가 질문을 새로 준비해요)"):
            ss.onboarded = False; ss.ob_step = 0; st.rerun()

    st.markdown("#### 연료 사용량 (AI 맞춤 질문)")
    override = st.checkbox("정확한 수치를 모르겠어요 → 월 유류비 총 지출액으로 대신 입력할게요",
                            key="pf_override_spend")
    if override and not ss.get("_applied_override"):
        if ss["biz_계산방식"] != "spend_based":
            ss["_orig_계산방식"] = ss["biz_계산방식"]
            ss["_orig_질문들"] = ss["biz_질문들"]
            role = "monthly_spend"
            flabel, fph, funit, fdefault, ftype = TEMPLATES["spend_based"]["fallback"][role]
            ss["biz_계산방식"] = "spend_based"
            ss["biz_질문들"] = [{"role": role, "label": flabel, "placeholder": fph,
                               "unit": funit, "default": fdefault, "type": ftype}]
            ss["pf_role_" + role] = fdefault
        ss["_applied_override"] = True
    elif not override and ss.get("_applied_override"):
        if "_orig_계산방식" in ss:
            ss["biz_계산방식"] = ss["_orig_계산방식"]
            ss["biz_질문들"] = ss["_orig_질문들"]
        ss["_applied_override"] = False

    cols = st.columns(2)
    for i, q in enumerate(ss.get("biz_질문들", [])):
        with cols[i % 2]:
            label = q["label"] + (f"  ({q['unit']})" if q.get("unit") else "")
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
        st.number_input("월 매출 (원)", min_value=0, step=1000000, key="pf_월매출")
        st.selectbox("우리 지역(시도)", SIDO_LIST, key="pf_지역")

    st.markdown("#### 조회 설정 · 추가 정보")
    st.radio("기본 조회 기간", ["7일", "30일", "전체"], horizontal=True, key="pf_기간")
    st.multiselect("함께 비교할 유종", list(PRODUCTS.keys()), key="pf_비교유종")
    st.text_area("추가 정보 (Lino 상담 시 참고)", key="pf_notes", height=110,
                 placeholder="예: 특정 거래처와 3개월 고정단가 계약 중 / 성수기엔 물량 2배 등")

    st.markdown("#### 현재 산출값")
    st.write(f"- 월 연료 사용량(산출): 약 **{M.월_사용량:,.0f} L**")
    st.write(f"- 현재 월 연료비(추정): 약 **{M.현재가*M.월_사용량:,.0f} 원**")
    st.write(f"- 리스크 점수: **{M.점수:.0f}점 ({M.등급})** · 축적 데이터 **{len(M.hist)}일**")

def page_log(M):
    st.subheader("조회 기록")
    log = load_log()
    if not log.empty:
        st.dataframe(log.iloc[::-1].reset_index(drop=True), use_container_width=True, height=300)
        lb1, lb2 = st.columns(2)
        lb1.download_button("기록 CSV 내려받기", log.to_csv(index=False).encode("utf-8-sig"),
                            "orep_query_log.csv", "text/csv", use_container_width=True)
        if lb2.button("조회 기록 초기화", use_container_width=True):
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
            st.rerun()
    else:
        st.info("아직 기록이 없습니다.")
    st.divider()
    st.write("**업종:** " + M.업종명 + "  ·  **유종:** " + M.유종명)
    st.write(f"**월 연료 사용량(산출):** 약 {M.월_사용량:,.0f} L  ·  **축적 데이터:** {len(M.hist)}일치")

# ═════════════════════════════════════════════════════════════
#  라우팅
# ═════════════════════════════════════════════════════════════
if not ss.onboarded:
    render_onboarding()
    st.stop()

NAV = [("홈 · 요약", "home"), ("유가 추이", "price"), ("손익 판단", "pl"),
       ("손익 분기", "be"), ("뉴스", "news"), ("지역·환율", "region"),
       ("리포트·알림", "report"), ("마이페이지", "mypage"), ("기록", "log")]

with st.sidebar:
    st.markdown(
        "<div style='line-height:1.0;padding:2px 0 8px 0'>"
        "<span style='font-size:30px;font-weight:900;letter-spacing:-1.5px;color:#3182F6;"
        "font-family:Georgia,serif'>OREP<span style='color:#191F28'>.</span></span><br>"
        "<span style='font-size:10px;color:#8B95A1;letter-spacing:2.5px;font-weight:700'>"
        "OIL RISK EDURE PROGRAM</span></div>", unsafe_allow_html=True)
    st.divider()
    for label, key in NAV:
        active = (ss.nav == key)
        if st.button(label, key="nav_" + key, use_container_width=True,
                     type="primary" if active else "secondary"):
            ss.nav = key; ss.lino_open = False; st.rerun()
    st.divider()
    st.markdown(
        "<style>"
        "div[data-testid='stSidebar'] .lino-btn button{"
        "background:linear-gradient(135deg,#3182F6,#1B64DA)!important;color:#fff!important;"
        "border:none!important;border-radius:16px!important;font-weight:800!important;"
        "padding:.7rem 1rem!important;box-shadow:0 4px 14px rgba(49,130,246,.28)!important;"
        "font-family:'Pretendard',sans-serif!important;letter-spacing:.2px!important;}"
        "div[data-testid='stSidebar'] .lino-btn button:hover{"
        "background:linear-gradient(135deg,#2272EB,#1858C6)!important;}"
        "</style><div class='lino-btn'>", unsafe_allow_html=True)
    if st.button("💬  Lino 비서 불러오기", key="open_lino", use_container_width=True):
        ss.lino_open = True; st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    st.caption("유가·원가 리스크를 상담하는 AI 비서")

    st.divider()
    st.caption("👤 " + _current_user_id())
    if st.button("로그아웃", key="do_logout", use_container_width=True):
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

if ss.lino_open:
    render_lino(M)
    st.stop()

PAGES = {"home": page_home, "price": page_price, "pl": page_pl, "be": page_be,
         "news": page_news, "region": page_region, "report": page_report,
         "mypage": page_mypage, "log": page_log}
PAGES.get(ss.nav, page_home)(M)

save_profile()

st.caption("© 2026 OREP · Oil Risk Edure Program")
