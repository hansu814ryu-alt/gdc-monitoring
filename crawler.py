import os
import json
import requests
import smtplib
import feedparser
import hashlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai

### ==========================================
### ⏱️ 1. 공통 수집 기간 설정 (최근 1개월)
### ==========================================
ONE_MONTH_AGO = datetime.now(timezone.utc) - timedelta(days=30)

def is_recent_enough(pub_date_str):
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt >= ONE_MONTH_AGO
    except Exception:
        return True

### ==========================================
### 💾 2. 히스토리 데이터 로드 및 저장 함수
### ==========================================
def load_yesterday_context(filepath='history.json'):
    context_str = ""
    seen_links = set()
    seen_titles = set()

    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                history = json.load(f)
                
                gdc_data = history.get('gdc', {}).get('data', [])
                ax_data = history.get('ax_news', {}).get('data', [])
                overseas_data = history.get('overseas', {}).get('data', [])
                
                for item in gdc_data + ax_data + overseas_data:
                    if 'link' in item: seen_links.add(item['link'])
                    if 'title' in item: seen_titles.add(item['title'])
                    if 'translated_title' in item: seen_titles.add(item['translated_title'])

                gdc_titles = [item.get('title', '') for item in gdc_data[:10]]
                ax_titles = [item.get('title', '') for item in ax_data[:10]]
                overseas_titles = [item.get('translated_title', item.get('title', '')) for item in overseas_data[:10]]
                
                context_str = f"[어제 GDC 이슈]: {', '.join(gdc_titles)} / [어제 AX 이슈]: {', '.join(ax_titles)} / [어제 해외 이슈]: {', '.join(overseas_titles)}"
        except Exception as e:
            print(f"히스토리 로드 실패: {e}")
            
    return context_str, seen_links, seen_titles

def save_today_history(data, filepath='history.json'):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"히스토리 저장 실패: {e}")

### ==========================================
### 📰 3. 뉴스 및 채용 데이터 수집 (ID 생성 로직 추가)
### ==========================================
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
                    link = item['link']
                    item_id = hashlib.md5(link.encode('utf-8')).hexdigest()[:10]
                    filtered_news.append({
                        "id": item_id,
                        "title": item['title'].replace("<b>", "").replace("</b>", ""),
                        "description": item['description'].replace("<b>", "").replace("</b>", ""),
                        "link": link,
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
                    item_id = hashlib.md5(link.encode('utf-8')).hexdigest()[:10]
                    
                    if include_keywords:
                        if any(kw in title.lower() for kw in include_keywords):
                            filtered_jobs.append({"id": item_id, "title": title, "company": company, "link": link})
                    else:
                        filtered_jobs.append({"id": item_id, "title": title, "company": company, "link": link})
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
                    link = entry.link
                    item_id = hashlib.md5(link.encode('utf-8')).hexdigest()[:10]
                    filtered_news.append({
                        "id": item_id,
                        "title": entry.title,
                        "description": entry.get('description', '')[:500],
                        "link": link,
                        "pubDate": entry.published
                    })
        except Exception as e:
            print(f"RSS 파싱 오류 ({url}): {e}")
            
    return filtered_news

### ==========================================
### 🧠 4. AI 기반 맥락 평가 및 번역
### ==========================================
def process_data_with_ai_batch(data_list, data_type, api_key, yesterday_context="", seen_links=None, seen_titles=None):
    if not api_key or not data_list: return data_list
    
    seen_links = seen_links or set()
    seen_titles = seen_titles or set()
    
    filtered_initial = []
    for d in data_list:
        if d['link'] in seen_links or d['title'] in seen_titles:
            continue
        filtered_initial.append(d)
        
    if not filtered_initial: return []
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        input_data = [{"id": i, "title": d["title"], "company": d.get("company", ""), "description": d.get("description", "")} for i, d in enumerate(filtered_initial)]
        
        custom_rule = ""
        if data_type == 'GDC 동향 뉴스':
            custom_rule = """
        2. 기사 평가 및 분류 기준 (GDC & 오프쇼어링):
           - 국내 동향 (DOMESTIC_GDC): LG CNS, SK AX 등 대기업 자회사에서 활용하는 GDC 사례 또는 한국에서 GDC 사업을 전개하는 베트남 기업 동향 (80점 이상 부여).
           - 해외 동향 (GLOBAL_GDC): Accenture, IBM 등 글로벌 IT기업이 활용하는 오프쇼어링 및 해외 GDC 동향 (70점 이상 부여).
           - 단순 웹/앱 외주 개발은 50점. 글로벌 게임 컨퍼런스(GDC)는 0점 처리.
        3. 반드시 'category_code' 속성에 'DOMESTIC_GDC' 또는 'GLOBAL_GDC'를 할당하세요.
            """
        elif data_type == 'AX 근황 뉴스':
            custom_rule = """
        2. 국내 기업/공공기관이 기존 레거시를 AI로 현대화하거나, 사내 RAG 구축 등 전사적 AX 운영 모델을 도입한 사례인지 평가 (국내 대기업 90점 이상, 중견 70점 이상)
            """
        elif data_type == '베트남 IT 채용 공고':
            custom_rule = """
        2. 기업 규모나 영향력을 추론하여 점수 부여 (대기업: 80~100점, 스타트업: 30~59점).
        3. 3가지 카테고리 중 하나로 정확히 분류하여 'category_code' 속성에 추가하세요.
           - MSP_PLAYER: 대형 SI/MSP 업체의 채용
           - VET_GDC_FIRM: 한국 진출 베트남계 GDC 전문 기업 채용
           - DOMESTIC_VET_IT: 외국인 IT 엔지니어를 직접 채용하는 국내 기업 공고
            """

        prompt = f"""
        당신은 IT 동향 및 채용 공고 평가자입니다. 평가 결과를 JSON 배열 형태로만 반환하세요.
        [데이터 유형]: {data_type}
        [어제 주요 뉴스 맥락]: {yesterday_context if yesterday_context else '없음'}

        [규칙]
        1. 동일 기사 중복 배제: 가장 정보가 풍부한 대표 기사 4개만 남기고 나머지는 배제. 어제 맥락과 95% 일치 시 0점.
        {custom_rule}
        4. 점수가 70점을 초과하면 'is_main': true, 이하면 false 설정.
        5. is_main이 true인 경우 1줄 요약('summary') 작성.

        [형식]: [ {{"id": 0, "score": 95, "is_main": true, "summary": "...", "category_code": "VET_GDC_FIRM"}} ]
        [입력 데이터]: {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        import re
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_scores = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_scores}
            for i, item in enumerate(filtered_initial):
                if i in score_dict:
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = score_dict[i].get("is_main", False)
                    item["summary"] = score_dict[i].get("summary", "")
                    item["category_code"] = score_dict[i].get("category_code", "DOMESTIC_VET_IT")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in filtered_initial if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
            
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        return filtered_initial

def process_overseas_with_ai_translation(data_list, api_key, yesterday_context="", seen_links=None, seen_titles=None):
    if not api_key or not data_list: return data_list
    
    seen_links = seen_links or set()
    seen_titles = seen_titles or set()
    
    filtered_initial = []
    for d in data_list:
        if d['link'] in seen_links or d['title'] in seen_titles: continue
        filtered_initial.append(d)
        
    if not filtered_initial: return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "description": d.get("description", "")} for i, d in enumerate(filtered_initial)]
        
        prompt = f"""
        글로벌 IT 평가자로서 아래 영문 뉴스를 처리하세요.
        [어제 맥락]: {yesterday_context if yesterday_context else '없음'}

        1. 중복 판별: 대표 기사 4개만 남기고 나머지는 0점. 어제 맥락과 80% 일치 시 0점.
        2. 평가: 빅테크의 AI 원천 기술 및 아키텍처, 기업의 AI 실제 활용 사례 위주로 높은 점수(0~100) 부여. 주가/단순 기업 뉴스는 0점.
        3. 점수가 80점 초과 시(is_main: true) 제목을 한글로 번역(translated_title)하고 핵심 요약(summary) 작성.

        [형식]: [ {{"id": 0, "score": 90, "is_main": true, "translated_title": "...", "summary": "..."}} ]
        [입력 데이터]: {json.dumps(input_data, ensure_ascii=False)}
        """
        
        response = model.generate_content(prompt)
        import re
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_evals = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_evals}
            for i, item in enumerate(filtered_initial):
                if i in score_dict and score_dict[i].get("is_main", False):
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = True
                    item["translated_title"] = score_dict[i].get("translated_title", item["title"])
                    item["summary"] = score_dict[i].get("summary", "")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in filtered_initial if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ 해외 뉴스 번역/평가 오류: {e}")
        return []

### ==========================================
### 🧵 5. 쓰레드(Threads) 자동 포스팅 연동
### ==========================================
def format_for_threads(item, category_name):
    title = item.get('translated_title', item.get('title', ''))
    summary = item.get('summary', 'AI 요약이 제공되지 않았습니다.')
    link = item.get('link', '')
    return f"💡 [오늘의 {category_name} 인사이트]\n\n📌 {title}\n\n✨ {summary}\n\n🔗 전문 보기: {link}\n\n#GDC #AI동향 #AX전환 #IT트렌드"

def post_to_threads(text):
    THREADS_USER_ID = os.environ.get("THREADS_USER_ID")
    THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN")
    
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        print("⚠️ 쓰레드 API 토큰이 없습니다. 건너뜁니다.")
        return

    create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    try:
        create_response = requests.post(create_url, data={"media_type": "TEXT", "text": text, "access_token": THREADS_ACCESS_TOKEN})
        container_id = create_response.json().get("id")
        if container_id:
            publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
            requests.post(publish_url, data={"creation_id": container_id, "access_token": THREADS_ACCESS_TOKEN})
            print("✅ 쓰레드 업로드 성공!")
    except Exception as e:
        print(f"❌ 쓰레드 업로드 실패: {e}")

def process_threads_posting(data_json):
    threads_candidates = []
    for category_key, category_name in [('gdc', 'GDC'), ('overseas', '글로벌 IT'), ('ax_news', 'AX')]:
        if category_key in data_json:
            for item in data_json[category_key].get('data', []):
                if item.get('is_main') and item.get('score', 0) >= 90:
                    threads_candidates.append((item, category_name))
                    
    threads_candidates = sorted(threads_candidates, key=lambda x: x[0].get('score', 0), reverse=True)[:2]
    for item, cat_name in threads_candidates:
        post_to_threads(format_for_threads(item, cat_name))


### ==========================================
### 📧 6. 이메일/웹 통합 HTML 빌드
### ==========================================
def build_matrix_section(gdc_domestic, gdc_global, overseas_data, ax_data):
    domestic_market = ax_data[:3] if ax_data else []
    global_market = overseas_data[:3] if overseas_data else []
    domestic_competitor = gdc_domestic[:3] if gdc_domestic else []
    global_competitor = gdc_global[:3] if gdc_global else []

    def to_list_html(items):
        if not items: return '<ul style="list-style-type: disc; padding-left: 20px; margin: 0; font-size: 13px; color: #333;"><li style="padding: 6px 0;">수집된 데이터가 없습니다.</li></ul>'
        html = '<ul style="list-style-type: disc; padding-left: 20px; margin: 0; font-size: 13px; color: #333;">'
        for item in items:
            title = item.get('translated_title', item['title'])
            html += f'<li style="padding: 6px 0;"><a href="{item["link"]}" target="_blank" style="color: #1a1a1a; text-decoration: none;">{title}</a></li>'
        html += '</ul>'
        return html

    return f"""
    <div style="margin-bottom: 50px;">
        <h2 style="color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;">💡 IT/AI 트렌드 요약</h2>
        <table style="width: 100%; border-collapse: collapse; margin-top: 20px; margin-bottom: 30px; background-color: #fff; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
            <thead>
                <tr>
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; font-weight: bold; text-align: center; font-size: 15px; width: 15%;">구분</th>
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; font-weight: bold; text-align: left; padding-left: 20px; font-size: 15px; width: 42.5%;">🇰🇷 국내 시장 (Domestic)</th>
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; font-weight: bold; text-align: left; padding-left: 20px; font-size: 15px; width: 42.5%;">🌍 글로벌 시장 (International)</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; vertical-align: middle; font-weight: bold; color: #1a1a1a; font-size: 14px;">시장 전반<br><span style="font-size: 11px; color: #888; display: block; margin-top: 4px;">(Market & IT)</span></td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_market)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_market)}</td>
                </tr>
                <tr>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; vertical-align: middle; font-weight: bold; color: #1a1a1a; font-size: 14px;">경쟁사 동향<br><span style="font-size: 11px; color: #888; display: block; margin-top: 4px;">(Competitors)</span></td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_competitor)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_competitor)}</td>
                </tr>
            </tbody>
        </table>
    </div>
    <hr style="border: 0; border-top: 1px solid #e1e8ed; margin: 40px 0;">
    """

def build_email_section(title, data_list, more_link, category_type, pages_url, is_job=False, is_overseas=False):
    html = f"<div style='margin-bottom: 50px;'><h2 style='color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;'>{title}</h2>"
    
    display_list = [item for item in data_list if item.get('is_main', True) and item.get('score', 0) > 80]
    
    if not display_list:
        html += "<p style='color: #888; font-style: italic; padding: 15px 0; text-align: center;'>📌 80점 이상의 기준에 부합하는 프리미엄 데이터가 없습니다.</p>"
    else:
        display_items = display_list[:4]
        html += '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 15px; table-layout: fixed;">'
        
        for i in range(0, len(display_items), 2):
            html += '<tr>'
            for j in range(2):
                if j == 1: html += '<td width="4%" style="width: 4%;"></td>' 
                
                if i + j < len(display_items):
                    item = display_items[i + j]
                    item_id = item.get('id', '')
                    
                    # 이메일용 피드백 링크 (more.html 자동 연동)
                    fb_url_good = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=good"
                    fb_url_normal = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=normal"
                    fb_url_bad = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=bad"

                    score_html = f"<span style='color: #e74c3c;'>[{item.get('score', 0)}점]</span>"
                    display_title = item.get('translated_title', item['title']) if is_overseas else item['title']
                    source_text = item.get('company', '기업명 미상')
                    
                    badge_html = score_html
                    if is_job:
                        category = item.get('category_code', '')
                        badge_style = "display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 10px;"
                        if category == 'MSP_PLAYER': badge_html = f"<span style='{badge_style} background-color: #e3f2fd; color: #1565
