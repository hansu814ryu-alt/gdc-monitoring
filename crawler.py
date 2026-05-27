import os
import json
import requests
import smtplib
import feedparser
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai

# ==========================================
# ⏱️ 1. 공통 수집 기간 설정 (최근 1개월)
# ==========================================
ONE_MONTH_AGO = datetime.now(timezone.utc) - timedelta(days=30)

def is_recent_enough(pub_date_str):
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt >= ONE_MONTH_AGO
    except Exception:
        return True 

# ==========================================
# 📰 2. 뉴스 및 채용 데이터 수집 
# ==========================================
def get_naver_news(client_id, client_secret, query, display=30):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": display, "sort": "sim"}
    filtered_news = []
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            for item in response.json().get('items', []):
                if is_recent_enough(item['pubDate']):
                    # 날짜 표준 포맷 생성 (%Y-%m-%d %H:%M)
                    try:
                        dt = parsedate_to_datetime(item['pubDate'])
                        pub_time = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pub_time = datetime.now().strftime("%Y-%m-%d %H:%M")

                    filtered_news.append({
                        "title": item['title'].replace("<b>", "").replace("</b>", "").replace("&quot;", '"'),
                        "description": item['description'].replace("<b>", "").replace("</b>", "").replace("&quot;", '"'),
                        "link": item['link'],
                        "pubDate": item['pubDate'],
                        "pub_time": pub_time,
                        "source": "네이버 뉴스"
                    })
    except Exception as e:
        print(f"네이버 뉴스 오류 ({query}): {e}")
    return filtered_news

def get_wanted_postings(search_keyword, include_keywords=None):
    url = "https://www.wanted.co.kr/api/v4/jobs"
    params = {
        "country": "kr", "locations": "all", "years": "-1", 
        "limit": "50", "query": search_keyword, "job_sort": "job.latest_order"
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.wanted.co.kr/"
    }
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            job_lists = response.json().get('data', [])
            for job in job_lists:
                try:
                    title = job.get('position', '')
                    company = job.get('company', {}).get('name', '기업명 미상')
                    job_id = job.get('id', '')
                    link = f"https://www.wanted.co.kr/wd/{job_id}"
                    pub_time = datetime.now().strftime("%Y-%m-%d %H:%M")
                    
                    if include_keywords:
                        if any(kw in title.lower() for kw in include_keywords):
                            filtered_jobs.append({"title": title, "company": company, "link": link, "pub_time": pub_time, "source": "원티드"})
                    else:
                        filtered_jobs.append({"title": title, "company": company, "link": link, "pub_time": pub_time, "source": "원티드"})
                except Exception:
                    continue
    except Exception as e:
        print(f"원티드 API 오류 ({search_keyword}): {e}")
    return filtered_jobs

def get_overseas_rss_news():
    rss_urls = [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.theverge.com/rss/artificial-intelligence/index.xml"
    ]
    filtered_news = []
    
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                if 'published' in entry and is_recent_enough(entry.published):
                    try:
                        dt = parsedate_to_datetime(entry.published)
                        pub_time = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pub_time = datetime.now().strftime("%Y-%m-%d %H:%M")

                    # 언론사 소스 매핑
                    source = "TechCrunch"
                    if "venturebeat" in url: source = "VentureBeat"
                    elif "theverge" in url: source = "The Verge"

                    filtered_news.append({
                        "title": entry.title,
                        "description": entry.get('description', '')[:500],
                        "link": entry.link,
                        "pubDate": entry.published,
                        "pub_time": pub_time,
                        "source": source
                    })
        except Exception as e:
            print(f"RSS 파싱 오류 ({url}): {e}")
            
    return filtered_news

# ==========================================
# 🧠 3. AI 기반 개별 맥락 평가 및 요약 생성 (LLM-as-a-Judge)
# ==========================================
def process_data_with_ai_batch(data_list, data_type, api_key):
    if not api_key or not data_list: return data_list
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        input_data = [{"id": i, "title": d["title"], "company": d.get("company", ""), "description": d.get("description", "")} for i, d in enumerate(data_list)]
        
        custom_rule = ""
        if data_type == 'GDC 동향 뉴스':
            custom_rule = """
            2. 이 기사가 기업의 MSP를 활용한 ITO운영하거나, 기존 레거시 시스템을 위탁 운영 및 관제(MSP)하는 사업 동향을 다루고 있는지 분석하세요.
               - 국내가 아닌 해외의 IT 인력을 활용하여 원격으로 개발 및 유지보수를 수행하는 딜리버리 센터(GDC) 운영, 인건비 절감 관련 시 90점 이상.
               - 단순 웹/앱 외주 개발은 50점. 글로벌 게임 컨퍼런스(GDC) 소식은 0점 처리.
            3. 각 개별 기사 단위로 '핵심 요약 문장 2개'와 삼성SDS 관점에서의 '에디터의 시선(제안 기회 및 비즈니스 의미)' 텍스트를 전문적인 한국어로 작성해 주세요.
            """
        elif data_type == 'AX 근황 뉴스':
            custom_rule = """
            2. 국내 기업/공공기관이 기존 레거시를 AI로 현대화하거나, 사내 RAG 구축, AI 거버넌스 수립 등 전사적 AX(AI 전환) 운영 모델을 도입한 실제 사례인지 평가하세요. (국내 대기업 90점 이상, 중견 70점 이상)
            3. 각 개별 기사 단위로 '핵심 요약 문장 2개'와 '에디터의 시선' 텍스트를 작성해 주세요.
            """
        else: # 채용 공고
            custom_rule = """
            2. 기업 규모나 영향력을 추론하여 점수를 부여하세요 (대기업: 80~100점, 스타트업: 30~59점). 베트남 파견/주재원 등 배제 조건 시 0점.
            3. 각 개별 채용공고 단위로 업무 요약 2줄과 우대사항 요약을 바탕으로 한 '에디터의 시선'을 작성해 주세요.
            """

        prompt = f"""
        당신은 IT 동향 및 채용 공고를 분석하는 수석 평가자이자 비즈니스 애널리스트입니다.
        아래 제공된 JSON 데이터를 꼼꼼히 읽고 다음 규칙에 따라 평가 및 요약을 수행하세요.
        반환 결과는 명시된 JSON 배열 형태 필드 규격을 정확히 지켜야 합니다. 다른 불필요한 설명은 포함하지 마세요.
        
        [데이터 유형]: {data_type}
        [평가 규칙]
        1. 내용 중복 배제 (가장 대표적인 하나만 score 유지, 나머지는 0점 및 is_main: false 처리).
        {custom_rule}
        4. 점수가 40점 이상이면 'is_main': true, 이하는 false로 판단하세요.
        
        [출력 형식 가이드] (반드시 JSON 배열 형태로만 응답)
        [
          {{
            "id": 0,
            "score": 95,
            "is_main": true,
            "summary_1": "가비아가 고성능 컴퓨팅 지원사업 공급기업으로 참여해 국산 AI 반도체 기반 컴퓨팅 인프라를 공급합니다.",
            "summary_2": "총 167억원 규모의 사업이며, 리벨리온 아톰맥스 NPU 기반 인프라를 활용하여 지원을 가속화합니다.",
            "editor_view": "GPU 중심의 AI 인프라에서 추론용 NPU 수요 확대로 이어지는 흐름입니다. 삼성SDS 관점에서는 공공형 AI 추론 플랫폼 연계형 서비스 제안 기회로 연결할 수 있습니다."
          }}
        ]
        
        [입력 데이터]
        {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            ai_scores = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_scores}
            for i, item in enumerate(data_list):
                if i in score_dict:
                    ai_res = score_dict[i]
                    item["score"] = ai_res.get("score", 0)
                    item["is_main"] = ai_res.get("is_main", False)
                    item["summary_1"] = ai_res.get("summary_1", "원문 및 상세 요약 가이드를 확인하세요.")
                    item["summary_2"] = ai_res.get("summary_2", "해당 도메인의 비즈니스 최신 소식입니다.")
                    item["editor_view"] = ai_res.get("editor_view", "해당 도메인의 모니터링 강화를 제안합니다.")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    item["summary_1"] = ""
                    item["summary_2"] = ""
                    item["editor_view"] = ""
            
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
            
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        for item in data_list: 
            item['is_main'] = True
            item["summary_1"] = "데이터를 파싱하여 기본 요약을 대체합니다."
            item["summary_2"] = "세부 사항은 원문 링크를 참고하세요."
            item["editor_view"] = "시장 모니터링 지속이 필요합니다."
        return data_list

# 해외 영문 뉴스 원스톱 평가 및 한글 번역/개별 요약 통합 생성
def process_overseas_with_ai_translation(data_list, api_key):
    if not api_key or not data_list: return data_list
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "description": d.get("description", "")} for i, d in enumerate(data_list)]
        
        prompt = f"""
        당신은 글로벌 IT 기술 번역가이자 수석 비즈니스 평가자입니다.
        아래 [영문 뉴스 데이터]를 읽고 다음 규칙에 따라 처리하세요.
        
        1. 평가 및 분류: 이 기사가 'Agentic Foundation Model, Multimodal, MCP, LLMOps 등 해외 AI 원천 기술 아키텍처 트렌드'에 부합하는지 분석하여 점수(0~100점)를 부여하세요.
        2. 한글 번역 및 개별 요약: 점수가 50점 이상이라면, 기사의 영문 제목을 IT 전문 용어를 살려 자연스러운 한글로 번역하고, 핵심 요약 문장 2개와 삼성SDS 관점에서의 비즈니스 시사점을 담은 '에디터의 시선'을 한글로 작성해 주세요.
        3. 점수가 50점 미만이면 is_main을 false로 설정하세요.
        
        [출력 형식 가이드] (JSON 배열만 출력)
        [
          {{
            "id": 0,
            "score": 90,
            "is_main": true,
            "translated_title": "오픈AI, 새로운 에이전트 아키텍처 및 런타임 표준 공개",
            "summary_1": "오픈AI에서 자율 에이전트 간 오케스트레이션을 제어할 수 있는 표준 런타임 환경을 릴리즈했습니다.",
            "summary_2": "보안 격리된 환경 내에서 다양한 비즈니스 툴과 외부 데이터 소스를 안전하게 체이닝할 수 있게 설계되었습니다.",
            "editor_view": "단순 파운데이션 모델 경쟁이 에이전틱 인프라 플랫폼 경쟁으로 확장되는 국면입니다. 사내 LLM 설계와 원격 딜리버리 인프라 내 에이전트 탑재 방향을 고도화할 필요가 있습니다."
          }}
        ]
        
        [입력 데이터]
        {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_evals = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_evals}
            for i, item in enumerate(data_list):
                if i in score_dict and score_dict[i].get("is_main", False):
                    ai_res = score_dict[i]
                    item["score"] = ai_res.get("score", 0)
                    item["is_main"] = True
                    item["title"] = ai_res.get("translated_title", item["title"])
                    item["summary_1"] = ai_res.get("summary_1", "")
                    item["summary_2"] = ai_res.get("summary_2", "")
                    item["editor_view"] = ai_res.get("editor_view", "")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ 해외 뉴스 번역/평가 오류: {e}")
        return []

# ==========================================
# 📧 4. 이메일 템플릿 및 발송 (디자인 전면 리뉴얼)
# ==========================================
def build_email_section_v3(data_list, more_link, is_job=False):
    """
    제공해주신 캡처 이미지의 UI 카드 레이아웃과 보더라인을 정밀 재현하는 함수
    """
    if not data_list:
        return """
        <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; text-align: center; color: #64748b; font-size: 14px; margin-bottom: 25px;">
            📌 추천 기준에 부합하는 수집 데이터가 없습니다.
        </div>
        """

    html = ""
    # 최대 5개 선별 노출 제한
    display_list = [item for item in data_list if item.get('is_main', True)][:5]

    for item in display_list:
        title = item.get('title', '제목 없음')
        pub_time = item.get('pub_time', '2026-05-26 08:37')
        source = item.get('source', '원천 소스')
        link = item.get('link', '#')
        
        # 기사별 요약 및 에디터 시선 매핑 (1번 핵심 변경사항)
        summary_1 = item.get('summary_1', '세부 요약 분석 진행 중입니다.')
        summary_2 = item.get('summary_2', '상세 내용은 뉴스 원문을 참고하시기 바랍니다.')
        editor_view = item.get('editor_view', '시장 인프라 흐름에 대한 실무진의 지속적인 모니터링과 선제적 대응이 필요합니다.')

        # 캡처본 화면의 흰색 라운드 박스 디자인 구현 (3번 변경사항)
        html += f"""
        <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 25px 30px; margin-bottom: 25px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.02), 0 2px 4px -1px rgba(0,0,0,0.02);">
            <h3 style="margin-top: 0; margin-bottom: 10px; font-size: 20px; color: #0f172a; line-height: 1.4; font-weight: bold;">
                {title}
            </h3>
            <div style="font-size: 13px; color: #64748b; margin-bottom: 18px;">
                원문 출처: {source} &nbsp;|&nbsp; 발행 시각: {pub_time}
            </div>
            
            <div style="margin-bottom: 20px;">
                <h4 style="margin: 0 0 8px 0; font-size: 14px; color: #1e293b; font-weight: bold;">핵심 요약</h4>
                <ul style="margin: 0; padding-left: 20px; color: #334155; font-size: 14px; line-height: 1.6;">
                    <li style="margin-bottom: 5px;">{summary_1}</li>
                    <li>{summary_2}</li>
                </ul>
            </div>
            
            <div style="background-color: #f8fafc; border-left: 4px solid #1e4eb8; padding: 15px 20px; margin-bottom: 18px; border-radius: 0 4px 4px 0;">
                <h4 style="margin: 0 0 6px 0; font-size: 13px; color: #1e4eb8; font-weight: bold;">💡 에디터의 시선</h4>
                <p style="margin: 0; color: #334155; font-size: 14px; line-height: 1.6; white-space: pre-line;">
                    {editor_view}
                </p>
            </div>
            
            <div style="margin-top: 12px;">
                <a href="{link}" target="_blank" style="color: #2563eb; text-decoration: none; font-size: 13px; font-weight: bold;">기사 바로가기 &rarr;</a>
            </div>
        </div>
        """
        
    if more_link:
        html += f"""
        <div style="text-align: center; margin-top: 15px; margin-bottom: 35px;">
            <a href="{more_link}" target="_blank" style="display: inline-block; padding: 10px 22px; background-color: #f1f5f9; color: #475569; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: bold;">🔗 웹페이지에서 전체 보기</a>
        </div>
        """
    return html

def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_env = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not receiver_env: 
        print("⚠️ 환경 변수 부재로 이메일을 발송할 수 없습니다.")
        return

    receiver_emails = [email.strip() for email in receiver_env.split(',') if email.strip()]
    current_date = datetime.now().strftime("%Y.%m.%d")
    
    # 상단 짙은 청색 메인 비주얼 배너 스타일 구현 (3번 변경사항)
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
    </head>
    <body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: 'Malgun Gothic', sans-serif; -webkit-font-smoothing: antialiased;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 730px; margin: 20px auto; background-color: #f8fafc;">
            <tr>
                <td style="padding: 0;">
                    <div style="background-color: #1e4eb8; padding: 35px 40px; color: #ffffff;">
                        <p style="margin: 0 0 8px 0; font-size: 13px; font-weight: 500; opacity: 0.85; letter-spacing: 0.5px;">MSP Daily Brief</p>
                        <h1 style="margin: 0 0 15px 0; font-size: 26px; font-weight: bold; line-height: 1.3; color: #ffffff; letter-spacing: -0.5px;">
                            {current_date} AI 인프라·금융 AX·리테일 클라우드
                        </h1>
                        <p style="margin: 0; font-size: 13px; opacity: 0.75;">발행일: {current_date}</p>
                    </div>
                    
                    <div style="padding: 30px 40px 10px 40px; color: #334155; font-size: 15px; line-height: 1.75;">
                        오늘 오전 8시 50분까지 공개된 기사 중에서는 직접적인 MSP 경쟁사 신규 발표보다 <span style="font-weight: bold; color: #0f172a; border-bottom: 2px solid #cbd5e1;">공공 AI 인프라 공급, 금융권 내부 AI 확산, AWS 기반 리테일 운영 현대화</span>가 더 선명했습니다.<br>
                        삼성SDS MSP 관점에서는 업종별 운영 문제를 AI·클라우드·데이터로 해결하는 수요가 실제 사업 기회로 이어질 가능성이 큽니다.
                    </div>
                </td>
            </tr>
            <tr>
                <td style="padding: 15px 40px;">
                    <h2 style="font-size: 18px; color: #0f172a; margin-top: 20px; margin-bottom: 15px; border-bottom: 2px solid #1e4eb8; padding-bottom: 6px; font-weight: bold;">📊 GDC 시장 및 경쟁사 동향</h2>
                    {build_email_section_v3(data['gdc']['data'], f"{pages_url}/more.html?type=gdc")}
                    
                    <h2 style="font-size: 18px; color: #0f172a; margin-top: 15px; margin-bottom: 15px; border-bottom: 2px solid #1e4eb8; padding-bottom: 6px; font-weight: bold;">🌍海外 AI 원천기술 및 아키텍처</h2>
                    {build_email_section_v3(data['overseas']['data'], f"{pages_url}/more.html?type=overseas")}
                    
                    <h2 style="font-size: 18px; color: #0f172a; margin-top: 15px; margin-bottom: 15px; border-bottom: 2px solid #1e4eb8; padding-bottom: 6px; font-weight: bold;">📰 국내 기업 Enterprise AX 사례</h2>
                    {build_email_section_v3(data['ax_news']['data'], f"{pages_url}/more.html?type=ax")}
                    
                    <h2 style="font-size: 18px; color: #0f172a; margin-top: 15px; margin-bottom: 15px; border-bottom: 2px solid #1e4eb8; padding-bottom: 6px; font-weight: bold;">💼 원티드 베트남 채용 (IT/BSE/통번역)</h2>
                    {build_email_section_v3(data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", is_job=True)}
                    
                    </td>
            </tr>
            <tr>
                <td style="padding: 10px 40px 40px 40px;">
                    <div style="background-color: #f1f5f9; border: 1px solid #e2e8f0; padding: 22px 25px; border-radius: 8px; color: #475569; font-size: 14px; line-height: 1.6; font-weight: 500;">
                        ✨ <strong style="color: #0f172a;">마무리 한마디:</strong> 급변하는 글로벌 AX 인프라 및 트렌드 동향 속에서 업종별 현장의 핵심 페인포인트를 포착하는 것이 비즈니스 수주의 첫걸음입니다. 오늘 하루도 인사이트와 함께 활기찬 실무 성과 이루시길 응원합니다!
                    </div>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    msg = MIMEMultipart()
    msg['Subject'] = f"📊 [Daily Brief] {current_date} AI 인프라 및 비즈니스 동향 보고"
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_emails)
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_emails, msg.as_string())
            print("✅ 신규 개별 요약 및 지정 디자인 양식으로 이메일 발송 성공!")
    except Exception as e:
        print(f"❌ 이메일 발송 작업 실패: {e}")

# ==========================================
# 🚀 5. 메인 실행부
# ==========================================
if __name__ == "__main__":
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring"
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    
    # 1. GDC 동향 (네이버)
    gdc_queries = ["GDC", "글로벌 딜리버리 센터", "오프쇼어", "MSP 오프쇼어링", "클라우드 딜리버리 센터", "IT 인프라 원격 운영"]
    raw_gdc = []
    for q in gdc_queries:
        raw_gdc.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    # 2. 해외 AI 원천기술 (RSS)
    raw_overseas = get_overseas_rss_news()
    
    # 3. 국내 기업 AX (네이버)
    ax_queries = ["엔터프라이즈 AX", "AI 운영모델", "레거시 AI 전환", "사내 RAG"]
    raw_ax_news = []
    for q in ax_queries:
        raw_ax_news.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    # 4. 채용 (AX 채용공고 수집 제외)
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse', '통역', '번역'])
    
    print("--- 🧠 AI 기반 개별 맥락 분석 / 번역 및 우선순위 정렬 중 ---")
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY)
    sorted_overseas = process_overseas_with_ai_translation(raw_overseas, GEMINI_KEY)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY)
    
    # 결과 조립용 딕셔너리 구조화 (기존의 공통 대분류 종합 insight 필드는 제거)
    result = {
        "gdc": {"data": sorted_gdc},
        "overseas": {"data": sorted_overseas},
        "ax_news": {"data": sorted_ax_news},
        "vn_jobs": {"data": sorted_vn_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ 데이터 정제 및 기사별 맵핑 완료. data.json 저장 성공.")
    
    send_email(result, GITHUB_PAGES_URL)
