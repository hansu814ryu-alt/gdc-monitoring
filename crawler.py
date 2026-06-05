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
### 💾 2. 히스토리 데이터 로드 및 저장 함수 (90% 중복 배제용 + 하드 필터링용 데이터 반환)
### ==========================================
def load_yesterday_context(filepath='history.json'):
    context_str = ""
    seen_links = set()
    seen_titles = set()

    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                history = json.load(f)
                
                # 어제 수집된 모든 기사 데이터 풀
                gdc_data = history.get('gdc', {}).get('data', [])
                ax_data = history.get('ax_news', {}).get('data', [])
                overseas_data = history.get('overseas', {}).get('data', [])
                
                # 1차 기계적 필터링을 위한 어제자 링크 및 제목 세팅 (전체 대상)
                for item in gdc_data + ax_data + overseas_data:
                    if 'link' in item: seen_links.add(item['link'])
                    if 'title' in item: seen_titles.add(item['title'])
                    if 'translated_title' in item: seen_titles.add(item['translated_title'])

                # AI에게 줄 컨텍스트는 판단력을 높이기 위해 상위 10개로 확대
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
### 📰 3. 뉴스 및 채용 데이터 수집
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
                    filtered_news.append({
                        "title": entry.title,
                        "description": entry.get('description', '')[:500],
                        "link": entry.link,
                        "pubDate": entry.published
                    })
        except Exception as e:
            print(f"RSS 파싱 오류 ({url}): {e}")
            
    return filtered_news

### ==========================================
### 🧠 4. AI 기반 맥락 평가 및 번역 (LLM-as-a-Judge)
### ==========================================
def process_data_with_ai_batch(data_list, data_type, api_key, yesterday_context="", seen_links=None, seen_titles=None):
    if not api_key or not data_list: return data_list
    
    seen_links = seen_links or set()
    seen_titles = seen_titles or set()
    
    # 1차 기계적 필터링: 어제와 URL이나 제목이 완전히 똑같으면 배제
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
           - 국내 동향 (DOMESTIC_GDC): LG CNS, SK AX 등 대기업 자회사에서 활용하는 GDC 사례 또는 FPT Korea, CMC Korea, Sotatek, VTI 등 한국에서 GDC 사업을 전개하는 베트남 기업 동향 (90점 이상 부여).
           - 해외 동향 (GLOBAL_GDC): Accenture, IBM 등 글로벌 IT기업이 인도/폴란드/멕시코 등에서 활용하는 오프쇼어링 및 해외 GDC 동향 (90점 이상 부여).
           - 단순 웹/앱 외주 개발은 50점. 글로벌 게임 컨퍼런스(GDC)는 철저히 0점 처리.
        3. 🚨 반드시 위 기준에 따라 'category_code' 속성에 'DOMESTIC_GDC' 또는 'GLOBAL_GDC'를 할당하세요.
            """
        elif data_type == 'AX 근황 뉴스':
            custom_rule = """
        2. 국내 기업/공공기관이 기존 레거시를 AI로 현대화하거나, 사내 RAG 구축, AI 거버넌스 수립 등 전사적 AX(AI 전환) 운영 모델을 도입한 실제 사례인지 평가하세요. (국내 대기업 90점 이상, 중견 70점 이상)
            """
        elif data_type == '베트남 IT 채용 공고':
            custom_rule = """
        2. 기업 규모나 영향력을 추론하여 점수를 부여하세요 (대기업: 80~100점, 스타트업: 30~59점).
        3. 🚨 채용 공고를 분석하여 다음 3가지 카테고리 중 하나로 정확히 분류하고 'category_code' 속성에 추가하세요.
           - MSP_PLAYER: 국내 대형 SI/MSP 업체(LG CNS, 삼성SDS, SK 등)의 '클라우드 및 MSP 사업' 관련 채용. (비-MSP 사업은 0점)
           - VET_GDC_FIRM: FPT Korea, CMC Korea, Sotatek, VTI 등 한국에 진출한 베트남계 GDC 전문 기업 채용
           - DOMESTIC_VET_IT: 국내 기업이 베트남 등 외국인 IT 엔지니어를 직접 채용하거나 원격 계약하는 공고
            """

        prompt = f"""
        당신은 IT 동향 및 채용 공고 수석 평가자입니다.
        아래 데이터를 분석하여 평가하고 JSON 배열 형태로만 반환하세요.

        [데이터 유형]: {data_type}
        
        [어제 주요 뉴스 맥락]
        {yesterday_context if yesterday_context else '비교할 어제 데이터 없음'}

        [평가 규칙]
        1. 🚨 동일 기사 및 중복 배제 (매우 엄격): 
           - (오늘 데이터 내 중복) 가장 정보가 풍부한 대표 기사 1개만 남기고 나머지는 배제.
           - (어제 뉴스 철저 배제) 제공된 '어제 주요 뉴스 맥락'과 비교하여 팩트나 맥락이 90% 이상 일치하는 기사(단순 재탕)는 철저히 0점 처리하고 is_main을 false로 설정하세요. (추가 등재 절대 금지)
        {custom_rule}
        4. 🚨 컷오프: 점수가 80점을 초과(81점 이상)하면 'is_main': true, 80점 이하면 false로 설정하세요.
        5. is_main이 true인 경우 해당 기사의 'summary'(1줄 요약)를 반드시 작성하세요. (에디터의 시선은 작성하지 마세요)

        [출력 형식]
        [ {{"id": 0, "score": 95, "is_main": true, "summary": "요약...", "category_code": "VET_GDC_FIRM"}} ]

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
        for item in filtered_initial: item['is_main'] = True
        return filtered_initial

def process_overseas_with_ai_translation(data_list, api_key, yesterday_context="", seen_links=None, seen_titles=None):
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
        input_data = [{"id": i, "title": d["title"], "description": d.get("description", "")} for i, d in enumerate(filtered_initial)]
        
        prompt = f"""
        당신은 글로벌 IT 평가자이자 GDC(Global development center)사업담당자입니다.
        아래 [영문 뉴스 데이터]를 읽고 다음 규칙에 따라 처리하세요.
        
        [어제 주요 뉴스 맥락]
        {yesterday_context if yesterday_context else '비교할 어제 데이터 없음'}

        1. 🚨 중복 판별 (엄격한 기준 적용):
           - (오늘 데이터 내 중복) 가장 정보가 풍부한 대표 기사 1개만 점수를 주고 나머지는 0점 처리.
           - (어제 뉴스 배제) 제공된 '어제 주요 뉴스 맥락'과 비교하여 팩트가 90% 이상 일치하는 기사는 철저히 0점 처리 (추가 등재 금지).
        2. 평가 및 분류: 
           - (AI 원천기술) Google, Microsoft, Anthropic, OpenAI 등 빅테크의 AI 원천 기술 및 아키텍처를 다루는 사례.
           - (AI 활용) Physical AI, Agentic AI 등 최신 AI를 기업, 개인, 단체 등이 실제 활용하는 사례.
           - 🚨 금지 규칙(엄격 적용): 'Anthropic 상장', '주가 및 실적 발표', '단순 투자금 유치', '경영진 인사' 등 기술/아키텍처/활용 사례와 무관한 단순 기업/금융/주식 뉴스는 철저히 0점 처리하세요.
           - 위 기준에 부합할수록 높은 점수(0~100점)를 부여하세요.
        3. 🚨 컷오프 및 번역: 점수가 80점을 초과한다면(is_main: true), 기사의 영문 제목과 요약문을 한글로 번역(translated_title)하고, 핵심 요약(summary)을 작성하세요. (에디터의 시선은 작성하지 않습니다).

        [출력 형식] (JSON 배열만 출력)
        [ {{"id": 0, "score": 90, "is_main": true, "translated_title": "번역제목", "summary": "요약..."}} ]

        [입력 데이터]
        {json.dumps(input_data, ensure_ascii=False)}
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
    """500자 제한을 고려한 쓰레드용 텍스트 포맷팅"""
    title = item.get('translated_title', item.get('title', ''))
    summary = item.get('summary', 'AI 요약이 제공되지 않았습니다.')
    link = item.get('link', '')
    
    threads_text = f"💡 [오늘의 {category_name} 인사이트]\n\n"
    threads_text += f"📌 {title}\n\n"
    threads_text += f"✨ {summary}\n\n"
    threads_text += f"🔗 전문 보기: {link}\n\n"
    threads_text += "#GDC #AI동향 #AX전환 #IT트렌드"
    
    return threads_text

def post_to_threads(text):
    """Meta API를 통해 Threads에 퍼블리싱"""
    THREADS_USER_ID = os.environ.get("THREADS_USER_ID")
    THREADS_ACCESS_TOKEN = os.environ.get("THREADS_ACCESS_TOKEN")
    
    if not THREADS_USER_ID or not THREADS_ACCESS_TOKEN:
        print("⚠️ 쓰레드 API 토큰이 없습니다. 포스팅을 건너뜁니다.")
        return

    # 1. 쓰레드 미디어 컨테이너 생성
    create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    create_payload = {"media_type": "TEXT", "text": text, "access_token": THREADS_ACCESS_TOKEN}
    
    try:
        create_response = requests.post(create_url, data=create_payload)
        container_id = create_response.json().get("id")
        
        if container_id:
            # 2. 컨테이너 실제 발행
            publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
            publish_payload = {"creation_id": container_id, "access_token": THREADS_ACCESS_TOKEN}
            requests.post(publish_url, data=publish_payload)
            print("✅ 쓰레드 업로드 성공!")
    except Exception as e:
        print(f"❌ 쓰레드 업로드 실패: {e}")

def process_threads_posting(data_json):
    """90점 이상 프리미엄 데이터 선별 및 포스팅"""
    threads_candidates = []
    
    # GDC, 글로벌IT(overseas), AX뉴스 카테고리 순회
    for category_key, category_name in [('gdc', 'GDC'), ('overseas', '글로벌 IT'), ('ax_news', 'AX')]:
        if category_key in data_json:
            for item in data_json[category_key].get('data', []):
                # 90점 이상이고 메인 기사로 분류된 경우만 추출
                if item.get('is_main') and item.get('score', 0) >= 90:
                    threads_candidates.append((item, category_name))
                    
    # 우선순위(점수) 기준 내림차순 정렬 후 최상위 2개만 선택 (도배 방지용)
    threads_candidates = sorted(threads_candidates, key=lambda x: x[0].get('score', 0), reverse=True)[:2]
    
    for item, cat_name in threads_candidates:
        post_text = format_for_threads(item, cat_name)
        post_to_threads(post_text)


### ==========================================
### 📧 6. 이메일/웹 통합 HTML 빌드
### ==========================================

def build_matrix_section(gdc_domestic, gdc_global, overseas_data, ax_data):
    # 매트릭스 데이터 분배 로직
    domestic_market = ax_data[:3] if ax_data else []
    global_market = overseas_data[:3] if overseas_data else []
    domestic_competitor = gdc_domestic[:3] if gdc_domestic else []
    global_competitor = gdc_global[:3] if gdc_global else []

    def to_list_html(items):
        if not items:
            return '<ul style="list-style-type: disc; padding-left: 20px; margin: 0; font-size: 13px; color: #333;"><li style="padding: 6px 0;">수집된 데이터가 없습니다.</li></ul>'
        html = '<ul style="list-style-type: disc; padding-left: 20px; margin: 0; font-size: 13px; color: #333;">'
        for item in items:
            title = item.get('translated_title', item['title'])
            html += f'<li style="padding: 6px 0;"><a href="{item["link"]}" target="_blank" style="color: #1a1a1a; text-decoration: none;">{title}</a></li>'
        html += '</ul>'
        return html

    html = f"""
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
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; vertical-align: middle; font-weight: bold; color: #1a1a1a; font-size: 14px;">시장 전반<br><span style="font-size: 11px; color: #888; font-weight: normal; display: block; margin-top: 4px;">(Market & IT)</span></td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_market)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_market)}</td>
                </tr>
                <tr>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; vertical-align: middle; font-weight: bold; color: #1a1a1a; font-size: 14px;">경쟁사 동향<br><span style="font-size: 11px; color: #888; font-weight: normal; display: block; margin-top: 4px;">(Competitors)</span></td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_competitor)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_competitor)}</td>
                </tr>
            </tbody>
        </table>
    </div>
    <hr style="border: 0; border-top: 1px solid #e1e8ed; margin: 40px 0;">
    """
    return html

def build_email_section(title, data_list, more_link, is_job=False, is_overseas=False):
    html = f"<div style='margin-bottom: 50px;'><h2 style='color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;'>{title}</h2>"
    
    display_list = [item for item in data_list if item.get('is_main', True) and item.get('score', 0) > 80]
    
    if not display_list:
        html += "<p style='color: #888; font-style: italic; padding: 15px 0; text-align: center;'>📌 80점 이상의 기준에 부합하는 프리미엄 데이터가 없습니다.</p>"
    else:
        # 최대 4개 구도 고정
        display_items = display_list[:4]
        html += '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 15px; table-layout: fixed;">'
        
        for i in range(0, len(display_items), 2):
            html += '<tr>'
            for j in range(2):
                if j == 1:
                    html += '<td width="4%" style="width: 4%;"></td>' 
                
                if i + j < len(display_items):
                    item = display_items[i + j]
                    
                    score_html = f"<span style='color: #e74c3c;'>[{item.get('score', 0)}점]</span>"
                    display_title = item.get('translated_title', item['title']) if is_overseas else item['title']
                    source_text = item.get('company', '기업명 미상')
                    
                    badge_html = score_html
                    if is_job:
                        category = item.get('category_code', '')
                        badge_style = "display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 10px;"
                        if category == 'MSP_PLAYER':
                            badge_html = f"<span style='{badge_style} background-color: #e3f2fd; color: #1565c0;'>MSP 채용</span> {score_html}"
                        elif category == 'VET_GDC_FIRM':
                            badge_html = f"<span style='{badge_style} background-color: #e8f5e9; color: #2e7d32;'>GDC 전문업체</span> {score_html}"
                        else:
                            badge_html = f"<span style='{badge_style} background-color: #fff3e0; color: #ef6c00;'>외국인 IT 채용</span> {score_html}"

                    summary_text = item.get('summary') or item.get('translated_desc') or item.get('description') or 'AI 핵심 요약 정보가 없습니다.'
                    meta_text = source_text if is_job else item.get('pubDate', item.get('date', '날짜 미상'))
                    
                    card_html = f"""
                    <div style="background-color: #ffffff; border: 1px solid #e1e8ed; border-radius: 10px; padding: 20px; height: 100%; box-sizing: border-box;">
                        <div style="font-size: 13px; font-weight: bold; margin-bottom: 10px;">{badge_html}</div>
                        <a href="{item['link']}" target="_blank" style="font-size: 16px; font-weight: bold; color: #1a1a1a; text-decoration: none; line-height: 1.4; display: block; margin-bottom: 15px;">{display_title}</a>
                        <div style="background-color: #f8fafc; border-left: 3px solid #3498db; padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                            <p style="font-size: 13px; color: #4a5568; margin: 0;"><strong>💡 핵심 요약:</strong> {summary_text}</p>
                        </div>
                        <div style="font-size: 12px; color: #a0aec0; text-align: right; margin-top: 15px; border-top: 1px dashed #edf2f7; padding-top: 10px;">{meta_text}</div>
                    </div>
                    """
                    html += f'<td width="48%" valign="top" style="width: 48%; padding-bottom: 20px;">{card_html}</td>'
                else:
                    html += '<td width="48%" style="width: 48%;"></td>'
            html += '</tr>'
        html += '</table>'
            
    if more_link:
        html += f"<div style='margin-top: 20px;'><a href='{more_link}' target='_blank' style='color: #3498db; font-weight: bold; text-decoration: none; font-size: 15px;'>🔗 [해당 카테고리 기사/공고 전체 보기]</a></div>"
    html += "</div>"
    return html

def generate_html_content(data, pages_url):
    html_content = """
    <div style="font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; max-width: 900px; margin: 0 auto; background-color: #f4f7f6; padding: 20px;">
        <div style="background-color: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.08);">
            <div style="text-align: center; margin-bottom: 40px;">
                <h1 style="color: #003366; border-bottom: 3px solid #3498db; padding-bottom: 15px; display: inline-block; font-weight: 800; font-size: 24px;">📊 GDC & AX 일일 트렌드 리포트</h1>
                <p style="color: #7f8c8d; margin-top: -10px;">Daily Insights & Competitor Matrix</p>
            </div>
    """
    
    # 데이터 세분화 및 추출
    gdc_domestic = [item for item in data['gdc']['data'] if item.get('category_code') == 'DOMESTIC_GDC']
    gdc_global = [item for item in data['gdc']['data'] if item.get('category_code') == 'GLOBAL_GDC']
    overseas_news = data['overseas']['data']
    ax_news = data['ax_news']['data']
    
    # 1. 요약 매트릭스 
    html_content += build_matrix_section(gdc_domestic, gdc_global, overseas_news, ax_news)
    
    # 2. 경쟁사 동향 분리 (국내/해외)
    html_content += build_email_section("📊 경쟁사 동향 1. 국내 GDC/오프쇼어링", gdc_domestic, f"{pages_url}/more.html?type=gdc_domestic")
    html_content += build_email_section("📊 경쟁사 동향 2. 해외 글로벌 오프쇼어링", gdc_global, f"{pages_url}/more.html?type=gdc_global")
    
    # 3. AI 및 AX 기사
    html_content += build_email_section("🌍 해외 AI 원천기술 및 아키텍처", overseas_news, f"{pages_url}/more.html?type=overseas", is_overseas=True)
    html_content += build_email_section("🏢 국내 기업 Enterprise AX (운영모델 전환)", ax_news, f"{pages_url}/more.html?type=ax")
    
    # 4. 채용 3개 세션 분리
    msp_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'MSP_PLAYER']
    gdc_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'VET_GDC_FIRM']
    vet_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'DOMESTIC_VET_IT']
    
    html_content += build_email_section("💼 채용 1. MSP Player", msp_jobs, f"{pages_url}/more.html?type=msp_jobs", is_job=True)
    html_content += build_email_section("💼 채용 2. 베트남 GDC 업체", gdc_jobs, f"{pages_url}/more.html?type=gdc_jobs", is_job=True)
    html_content += build_email_section("💼 채용 3. 외국인 IT 인력", vet_jobs, f"{pages_url}/more.html?type=vet_jobs", is_job=True)
    
    html_content += """
            <div style="text-align: center; margin-top: 50px; padding-top: 20px; border-top: 1px solid #ddd; color: #7f8c8d; font-size: 13px;">
                <p>※ 상세 기사 및 채용 리스트는 AI 분석을 통해 고품질 데이터만 선별하여 제공됩니다.</p>
                <p>오늘의 프리미엄 리포트는 여기까지입니다! 🚀</p>
            </div>
        </div>
    </div>
    """
    return html_content

def send_email(html_content):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    #receiver_emails = ["hansu814.ryu@samsung.com"]
    receiver_emails = ["hansu814.ryu@samsung.com", "th.jeong@samsung.com","jihoon33.kim@samsung.com","glassman@samsung.com","chaneast.kim@samsung.com","bangz0@samsung.com","tjsong@samsung.com","hj71.song@samsung.com","yoonsj@samsung.com","heeseon.yoon@samsung.com","laguna@samsung.com","jackie.chung@samsung.com","ally.chae@samsung.com","yoonseok@samsung.com","eunji0313.choi@samsung.com","yh0721.chung@samsung.com"]
    
    if not sender_email or not sender_password: 
        print("⚠️ 발신자 정보 누락")
        return

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

### ==========================================
### 🚀 7. 메인 실행부
### ==========================================
if __name__ == "__main__":
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring"
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    yesterday_context, seen_links, seen_titles = load_yesterday_context()
    
    gdc_queries = ["GDC", "글로벌 딜리버리 센터", "오프쇼어", "MSP 오프쇼어링", "클라우드 딜리버리 센터", "IT 인프라 원격 운영"]
    raw_gdc = []
    for q in gdc_queries:
        raw_gdc.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    raw_overseas = get_overseas_rss_news()
    
    ax_queries = ["엔터프라이즈 AX", "AI 운영모델", "레거시 AI 전환", "사내 RAG"]
    raw_ax_news = []
    for q in ax_queries:
        raw_ax_news.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse', '통역', '번역'])
    
    print("--- 🧠 AI 기반 맥락 평가 / 번역 및 정렬 중 ---")
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_overseas = process_overseas_with_ai_translation(raw_overseas, GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    
    result = {
        "gdc": {"data": sorted_gdc},
        "overseas": {"data": sorted_overseas},
        "ax_news": {"data": sorted_ax_news},
        "vn_jobs": {"data": sorted_vn_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 저장 완료.")
    
    # 💡 웹/이메일 통일 UI 생성
    final_html = generate_html_content(result, GITHUB_PAGES_URL)
    
    # 💡 index.html 파일 생성 (GitHub Pages에 이메일과 100% 동일하게 웹 호스팅)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(final_html)
    print("✅ index.html 통일 웹페이지 저장 완료.")
        
    save_today_history(result)
    
    print("--- 📧 이메일 발송 중 ---")
    send_email(final_html)
    
    print("--- 🧵 쓰레드(Threads) 자동 포스팅 중 ---")
    process_threads_posting(result)
    
    print("✅ 모든 파이프라인 완료!")
