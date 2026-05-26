import os
import json
import requests
import smtplib
import feedparser
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
                    filtered_news.append({
                        "title": item['title'].replace("<b>", "").replace("</b>", ""),
                        "description": item['description'].replace("<b>", "").replace("</b>", ""),
                        "link": item['link'],
                        "pubDate": item['pubDate']
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
                    
                    if include_keywords:
                        if any(kw in title.lower() for kw in include_keywords):
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                    else:
                        filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
    except Exception as e:
        print(f"원티드 API 오류 ({search_keyword}): {e}")
    return filtered_jobs

# ✅ [수정] 옵션 C: 순수 해외 IT 언론 매체 RSS 직접 수집 (기술 블로그 배제)
def get_overseas_rss_news():
    # AWS, Google 등 벤더사 자사 블로그를 제외하고, 공신력 있는 글로벌 테크 뉴스 매체만 타겟팅
    rss_urls = [
        "https://techcrunch.com/category/artificial-intelligence/feed/", # TechCrunch (테크/스타트업/AI 뉴스)
        "https://venturebeat.com/category/ai/feed/", # VentureBeat (엔터프라이즈 AI 트렌드 특화)
        "https://www.theverge.com/rss/artificial-intelligence/index.xml" # The Verge (대중적/산업적 AI 이슈)
    ]
    filtered_news = []
    
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]: # 피드당 최신 10개만 검토
                if 'published' in entry and is_recent_enough(entry.published):
                    filtered_news.append({
                        "title": entry.title,
                        "description": entry.get('description', '')[:500], # 요약이 너무 길면 자름
                        "link": entry.link,
                        "pubDate": entry.published
                    })
        except Exception as e:
            print(f"RSS 파싱 오류 ({url}): {e}")
            
    return filtered_news

# ==========================================
# 🧠 3. AI 기반 맥락 평가 및 번역 (LLM-as-a-Judge)
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
           - 단순 웹/앱 외주 개발은 50점. 글로벌 게임 컨퍼런스(GDC)는 0점 처리.
            """
        elif data_type == 'AX 근황 뉴스':
            custom_rule = """
        2. 국내 기업/공공기관이 기존 레거시를 AI로 현대화하거나, 사내 RAG 구축, AI 거버넌스 수립 등 전사적 AX(AI 전환) 운영 모델을 도입한 실제 사례인지 평가하세요. (국내 대기업 90점 이상, 중견 70점 이상)
            """
        else:
            custom_rule = """
        2. 기업 규모나 영향력을 추론하여 점수를 부여하세요 (대기업: 80~100점, 스타트업: 30~59점). 베트남 파견/주재원 등 배제 조건 시 0점.
            """

        prompt = f"""
        당신은 IT 동향 및 채용 공고 수석 평가자입니다.
        아래 데이터를 분석하여 평가하고 JSON 배열 형태로만 반환하세요.
        
        [데이터 유형]: {data_type}
        [평가 규칙]
        1. 내용 중복 배제 (가장 대표적인 하나만 score 유지, 나머지 0점).
        {custom_rule}
        3. 점수가 40점 이상이면 'is_main': true.
        
        [출력 형식]
        [ {{"id": 0, "score": 95, "is_main": true}} ]
        
        [입력 데이터]
        {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        text = response.text.strip()
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            ai_scores = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_scores}
            for i, item in enumerate(data_list):
                if i in score_dict:
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = score_dict[i].get("is_main", False)
                else:
                    item["score"] = 0
                    item["is_main"] = False
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
            
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        for item in data_list: item['is_main'] = True
        return data_list

# ✅ [신규] 해외 영문 뉴스 원스톱 평가 및 한글 번역
def process_overseas_with_ai_translation(data_list, api_key):
    if not api_key or not data_list: return data_list
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "description": d.get("description", "")} for i, d in enumerate(data_list)]
        
        prompt = f"""
        당신은 글로벌 IT 기술 번역가이자 수석 평가자입니다.
        아래 [영문 뉴스 데이터]를 읽고 다음 규칙에 따라 처리하세요.
        
        1. 평가 및 분류: 이 기사가 'Agentic Foundation Model, Multimodal, MCP, LLMOps 등 해외 AI 원천 기술 아키텍처 트렌드'에 부합하는지 분석하여 점수(0~100점)를 부여하세요.
        2. 한글 번역 및 요약: 점수가 50점 이상이라면, 기사의 영문 제목과 요약문을 IT 전문 용어를 살려 자연스러운 한글로 번역하고 1~2문장으로 요약하세요.
        3. 점수가 50점 미만이면 is_main을 false로 설정하세요.
        
        [출력 형식] (JSON 배열만 출력)
        [ {{"id": 0, "score": 90, "is_main": true, "translated_title": "오픈AI, 새로운 에이전트 런타임 발표", "translated_desc": "한글 요약 내용..."}} ]
        
        [입력 데이터]
        {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        import re
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_evals = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_evals}
            for i, item in enumerate(data_list):
                if i in score_dict and score_dict[i].get("is_main", False):
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = True
                    item["translated_title"] = score_dict[i].get("translated_title", item["title"])
                    item["translated_desc"] = score_dict[i].get("translated_desc", "")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ 해외 뉴스 번역/평가 오류: {e}")
        return []

def get_ai_insight(news_list, api_key, is_translated=False):
    if not api_key or not news_list: return ""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        titles = [news.get('translated_title', news['title']) if is_translated else news['title'] for news in news_list[:5]]
        prompt = f"다음 기사 제목들을 분석하여 비즈니스 측면에서 전체적인 시사점을 딱 1~2문장으로 요약하세요.\n{titles}"
        response = model.generate_content(prompt)
        if response and response.text: return response.text.strip()
    except Exception:
        pass
    return ""

# ==========================================
# 📧 4. 이메일 템플릿 및 발송
# ==========================================
def build_email_section(title, insight, data_list, more_link, is_job=False, is_overseas=False):
    html = f"<h2>{title}</h2>"
    if insight: html += f"<div style='margin-bottom:10px;'>💡 <b>[AI 시사점]</b> {insight}</div>"
    
    display_list = [item for item in data_list if item.get('is_main', True)]
    html += "<ul>"
    
    if not display_list:
        html += "<li>📌 수집된 유효 데이터가 없습니다.</li>"
    else:
        for item in display_list[:5]: 
            if is_job: 
                html += f"<li><a href='{item['link']}' target='_blank'>[{item.get('company', '')}] {item['title']}</a></li>"
            elif is_overseas:
                # 해외 뉴스는 번역된 제목 노출
                html += f"<li><a href='{item['link']}' target='_blank'>🌍 {item.get('translated_title', item['title'])}</a> <div class='meta'>{item.get('translated_desc', '')}</div></li>"
            else: 
                html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a></li>"
    html += "</ul>"
    
    if more_link:
        html += f"<div style='margin-top:10px;'><a href='{more_link}' target='_blank'>🔗 [웹페이지에서 전체 보기]</a></div>"
    return html

def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_env = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not receiver_env: return

    # ✅ 쉼표 분리 및 공백 제거 (1순위 에러 해결 적용)
    receiver_emails = [email.strip() for email in receiver_env.split(',') if email.strip()]
    print(f"📧 수신 대상 목록 확인: {receiver_emails}")
    
    html_content = """
    <style>
        body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } 
        h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } 
        ul { list-style-type: none; padding: 0; } 
        li { padding: 8px 0; border-bottom: 1px dashed #ccc; } 
        a { color: #007bff; text-decoration: none; font-weight: bold; } 
        .meta { color: #888; font-size: 13px; margin-top: 3px; }
    </style>
    <h1>📊 일일 트렌드 및 기술 동향 리포트</h1>
    """
    
    html_content += build_email_section("📊 GDC 오프쇼어링 (MSP/ITO 위탁) 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("🌍 해외 AI 원천기술 및 아키텍처", data['overseas']['insight'], data['overseas']['data'], f"{pages_url}/more.html?type=overseas", is_overseas=True)
    html_content += build_email_section("🏢 국내 기업 Enterprise AX (운영모델 전환)", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 베트남 온사이트인력 채용", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", is_job=True)
    html_content += build_email_section("💼 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", is_job=True)

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 기술 트렌드 및 채용 동향 리포트"
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_emails)
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_emails, msg.as_string())
            print("✅ 이메일 발송 성공!")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

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
        
    # 4. 채용
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse', '통역', '번역'])
    raw_ax_jobs = get_wanted_postings("AX")
    
    print("--- 🧠 AI 기반 맥락 평가 / 번역 및 정렬 중 ---")
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY)
    sorted_overseas = process_overseas_with_ai_translation(raw_overseas, GEMINI_KEY)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY)
    sorted_ax_jobs = process_data_with_ai_batch(raw_ax_jobs, 'AX 채용 공고', GEMINI_KEY)
    
    print("--- 💡 시사점 도출 중 ---")
    gdc_insight = get_ai_insight(sorted_gdc, GEMINI_KEY)
    overseas_insight = get_ai_insight(sorted_overseas, GEMINI_KEY, is_translated=True)
    ax_insight = get_ai_insight(sorted_ax_news, GEMINI_KEY)
    
    result = {
        "gdc": {"data": sorted_gdc, "insight": gdc_insight},
        "overseas": {"data": sorted_overseas, "insight": overseas_insight},
        "ax_news": {"data": sorted_ax_news, "insight": ax_insight},
        "vn_jobs": {"data": sorted_vn_jobs},
        "ax_jobs": {"data": sorted_ax_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 저장 완료.")
    
    send_email(result, GITHUB_PAGES_URL)
