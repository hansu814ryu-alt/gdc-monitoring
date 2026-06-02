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
### 💾 2. 히스토리 데이터 로드 및 저장 함수 (중복 배제용)
### ==========================================
def load_yesterday_context(filepath='history.json'):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                history = json.load(f)
                gdc_titles = [item.get('title', '') for item in history.get('gdc', {}).get('data', [])[:5]]
                ax_titles = [item.get('title', '') for item in history.get('ax_news', {}).get('data', [])[:5]]
                context = f"[어제 GDC 이슈]: {', '.join(gdc_titles)} / [어제 AX 이슈]: {', '.join(ax_titles)}"
                return context
        except Exception as e:
            print(f"히스토리 로드 실패: {e}")
            return ""
    return ""

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
def process_data_with_ai_batch(data_list, data_type, api_key, yesterday_context=""):
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
        elif data_type == '베트남 IT 채용 공고':
            custom_rule = """
        2. 기업 규모나 영향력을 추론하여 점수를 부여하세요 (대기업: 80~100점, 스타트업: 30~59점).
        3. 🚨 채용 공고를 분석하여 다음 3가지 카테고리 중 하나로 정확히 분류하고 'category_code' 속성에 추가하세요.
           - MSP_PLAYER: 국내 대형 SI/MSP 업체(LG CNS, 삼성SDS, SK 등)의 '클라우드 및 MSP 사업' 관련 채용. (단, 삼성SDS의 '물류사업' 등 비-MSP 사업은 0점 처리)
           - VET_GDC_FIRM: 한국에 진출한 베트남계 GDC 기업(FPT, CMC, Sotatek, VTI 등) 채용
           - DOMESTIC_VET_IT: 국내 기업이 외국인 IT 엔지니어를 직접 채용하거나 원격 계약하는 공고
            """

        prompt = f"""
        당신은 IT 동향 및 채용 공고 수석 평가자입니다.
        아래 데이터를 분석하여 평가하고 JSON 배열 형태로만 반환하세요.

        [데이터 유형]: {data_type}
        
        [어제 주요 뉴스 맥락]
        {yesterday_context if yesterday_context else '비교할 어제 데이터 없음'}

        [평가 규칙]
        1. 내용 중복 배제: (오늘 데이터 내 중복) 가장 정보가 풍부한 대표 기사 1개만 남기고 나머지는 배제.
        1-1. (어제 뉴스 철저 배제) 제공된 '어제 주요 뉴스 맥락'과 비교하여 팩트가 80% 이상 일치하는 기사(단순 재탕 기사)는 철저히 0점 처리하고 is_main을 false로 설정하세요.
        {custom_rule}
        4. 🚨 컷오프: 점수가 85점을 초과(86점 이상)하면 'is_main': true, 85점 이하면 false로 설정하세요.
        5. is_main이 true인 경우 해당 기사의 'summary'(1줄 요약)와 'editor_view'(에디터 시선)를 반드시 작성하세요.

        [출력 형식]
        [ {{"id": 0, "score": 95, "is_main": true, "summary": "요약...", "editor_view": "시선...", "category_code": "VET_GDC_FIRM"}} ]

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
                    item["summary"] = score_dict[i].get("summary", "")
                    item["editor_view"] = score_dict[i].get("editor_view", "")
                    item["category_code"] = score_dict[i].get("category_code", "DOMESTIC_VET_IT")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
            
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        for item in data_list: item['is_main'] = True
        return data_list

def process_overseas_with_ai_translation(data_list, api_key):
    if not api_key or not data_list: return data_list
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "description": d.get("description", "")} for i, d in enumerate(data_list)]
        
        prompt = f"""
        당신은 글로벌 IT 평가자이자 GDC(Global development center)사업담당자입니다.
        아래 [영문 뉴스 데이터]를 읽고 다음 규칙에 따라 처리하세요.

        1. 중복 판별: 내용이 중복되는 기사가 있다면 대표 기사 1개만 점수를 주고 나머지는 0점(is_main: false) 처리하세요.
        2. 평가 및 분류: 기사가 'Agentic Foundation Model, Multimodal, MCP 등 해외 AI 원천 기술 트렌드'에 부합하는지 분석하여 점수(0~100점)를 부여하세요.
        3. 🚨 컷오프 및 번역: 점수가 85점을 초과한다면(is_main: true), 기사의 영문 제목과 요약문을 자연스러운 한글로 번역(translated_title)하고, 핵심 요약(summary) 및 에디터 시선(editor_view)을 함께 작성하세요. 85점 이하는 is_main을 false로 처리.

        [출력 형식] (JSON 배열만 출력)
        [ {{"id": 0, "score": 90, "is_main": true, "translated_title": "번역제목", "summary": "요약...", "editor_view": "시선..."}} ]

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
                    item["summary"] = score_dict[i].get("summary", "")
                    item["editor_view"] = score_dict[i].get("editor_view", "")
                else:
                    item["score"] = 0
                    item["is_main"] = False
                    
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ 해외 뉴스 번역/평가 오류: {e}")
        return []

### ==========================================
### 📧 5. 이메일 템플릿 HTML 빌드 (웹페이지 통일 버전)
### ==========================================

def build_matrix_section(gdc_data, overseas_data, ax_data):
    """2x2 트렌드 요약 매트릭스를 이메일 친화적 HTML 테이블로 생성"""
    domestic_market = ax_data.get('data', [])[:3] if ax_data else []
    domestic_competitor = gdc_data.get('data', [])[:3] if gdc_data else []
    global_market = overseas_data.get('data', [])[:2] if overseas_data else []
    global_competitor = overseas_data.get('data', [])[2:4] if overseas_data else []

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
        <h2 style="color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;">💡 GDC & ITO 트렌드 매트릭스 (요약)</h2>
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

def build_email_section(title, insight, data_list, more_link, is_job=False, is_overseas=False):
    """각 기사를 index.html의 카드 디자인과 동일한 2x2 테이블 그리드로 생성"""
    html = f"<div style='margin-bottom: 50px;'><h2 style='color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;'>{title}</h2>"
    
    if insight: 
        html += f"<div style='background-color: #f8fafc; border-left: 5px solid #003366; padding: 15px 20px; margin-bottom: 20px; border-radius: 0 6px 6px 0; color: #1a5f8e; font-size: 15px;'>💡 <b>[AI 시사점 요약]</b><br> {insight}</div>"
        
    display_list = [item for item in data_list if item.get('is_main', True) and item.get('score', 0) > 85]
    
    if not display_list:
        html += "<p style='color: #888; font-style: italic; padding: 15px 0; text-align: center;'>📌 85점 이상의 기준에 부합하는 프리미엄 데이터가 없습니다.</p>"
    else:
        # 최대 4개를 2x2 테이블 그리드로 변환 (이메일 클라이언트 호환성 확보)
        display_items = display_list[:4]
        html += '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 15px; table-layout: fixed;">'
        
        for i in range(0, len(display_items), 2):
            html += '<tr>'
            for j in range(2):
                if j == 1:
                    html += '<td width="4%" style="width: 4%;"></td>' # 카드 간격(gap)
                
                if i + j < len(display_items):
                    item = display_items[i + j]
                    
                    score_html = f"<span style='color: #e74c3c;'>[{item.get('score', 0)}점]</span>"
                    display_title = item.get('translated_title', item['title']) if is_overseas else item['title']
                    source_text = item.get('company', '기업명 미상')
                    
                    # 뱃지 로직
                    badge_html = score_html
                    if is_job:
                        category = item.get('category_code', '')
                        comp_str = source_text.upper()
                        
                        badge_style = "display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 10px;"
                        if category == 'MSP_PLAYER' or any(x in comp_str for x in ['CNS', 'SDS', 'SK']):
                            badge_html = f"<span style='{badge_style} background-color: #e3f2fd; color: #1565c0;'>MSP 채용</span> {score_html}"
                        elif category == 'VET_GDC_FIRM' or any(x in comp_str for x in ['FPT', 'CMC', 'VTI', 'SOTATEK']):
                            badge_html = f"<span style='{badge_style} background-color: #e8f5e9; color: #2e7d32;'>GDC 전문업체</span> {score_html}"
                        else:
                            badge_html = f"<span style='{badge_style} background-color: #fff3e0; color: #ef6c00;'>국내 외국인 채용</span> {score_html}"

                    summary_text = item.get('summary') or item.get('translated_desc') or item.get('description') or 'AI 핵심 요약 정보가 없습니다.'
                    editor_view_html = f"<p style='font-size: 13px; color: #4a5568; margin: 0;'><strong>👁️ 에디터 시선:</strong> {item.get('editor_view')}</p>" if item.get('editor_view') else ""
                    
                    meta_text = source_text if is_job else item.get('pubDate', item.get('date', '날짜 미상'))
                    
                    # 개별 기사 카드 UI
                    card_html = f"""
                    <div style="background-color: #ffffff; border: 1px solid #e1e8ed; border-radius: 10px; padding: 20px; height: 100%; box-sizing: border-box;">
                        <div style="font-size: 13px; font-weight: bold; margin-bottom: 10px;">{badge_html}</div>
                        <a href="{item['link']}" target="_blank" style="font-size: 16px; font-weight: bold; color: #1a1a1a; text-decoration: none; line-height: 1.4; display: block; margin-bottom: 15px;">{display_title}</a>
                        <div style="background-color: #f8fafc; border-left: 3px solid #3498db; padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                            <p style="font-size: 13px; color: #4a5568; margin: 0 0 8px 0;"><strong>💡 핵심 요약:</strong> {summary_text}</p>
                            {editor_view_html}
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

def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    
    # 💡 수신자 이메일 하드코딩 유지
    receiver_emails = [
        "hansu814.ryu@samsung.com",
        "th.jeong@samsung.com",
        "jihoon33.kim@samsung.com",
        "glassman@samsung.com",
        "chaneast.kim@samsung.com",
        "bangz0@samsung.com",
        "tjsong@samsung.com",
        "hj71.song@samsung.com",
        "yoonsj@samsung.com",
        "heeseon.yoon@samsung.com",
        "laguna@samsung.com",
        "jackie.chung@samsung.com",
        "ally.chae@samsung.com",
        "yoonseok@samsung.com",
        "eunji0313.choi@samsung.com"
    ]
    
    if not sender_email or not sender_password: 
        print("⚠️ 발신자 이메일 정보(SENDER_EMAIL, SENDER_PASSWORD)가 누락되었습니다.")
        return

    # 메일 최상단 배너
    html_content = """
    <div style="font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; max-width: 900px; margin: 0 auto; background-color: #f4f7f6; padding: 20px;">
        <div style="background-color: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.08);">
            <div style="text-align: center; margin-bottom: 40px;">
                <h1 style="color: #003366; border-bottom: 3px solid #3498db; padding-bottom: 15px; display: inline-block; font-weight: 800; font-size: 24px;">📊 GDC & AX 일일 트렌드 리포트</h1>
                <p style="color: #7f8c8d; margin-top: -10px;">Daily Insights & Competitor Matrix</p>
            </div>
    """
    
    # 1. 상단 2x2 매트릭스 (웹페이지와 통일)
    html_content += build_matrix_section(data.get('gdc'), data.get('overseas'), data.get('ax_news'))
    
    # 2. 하단 2x2 카드 리스트 
    html_content += build_email_section("📊 GDC 오프쇼어링 (MSP/ITO 위탁) 동향", data['gdc'].get('insight', ''), data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("🌍 해외 AI 원천기술 및 아키텍처", data['overseas'].get('insight', ''), data['overseas']['data'], f"{pages_url}/more.html?type=overseas", is_overseas=True)
    html_content += build_email_section("🏢 국내 기업 Enterprise AX (운영모델 전환)", data['ax_news'].get('insight', ''), data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 GDC 관련 채용 (MSP / 베트남 GDC / 외국인 IT)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", is_job=True)
    
    # 마무리 꼬리말
    html_content += """
            <div style="text-align: center; margin-top: 50px; padding-top: 20px; border-top: 1px solid #ddd; color: #7f8c8d; font-size: 13px;">
                <p>※ 상세 기사 및 채용 리스트는 AI 분석을 통해 85점 이상의 고품질 데이터만 선별하여 제공됩니다.</p>
                <p>오늘의 프리미엄 리포트는 여기까지입니다! 🚀</p>
            </div>
        </div>
    </div>
    """

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
### 🚀 6. 메인 실행부
### ==========================================
if __name__ == "__main__":
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring"
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    yesterday_context = load_yesterday_context()
    
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
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY, yesterday_context)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY, yesterday_context)
    
    sorted_overseas = process_overseas_with_ai_translation(raw_overseas, GEMINI_KEY)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY)
    
    print("--- 💡 시사점 도출 중 ---")
    
    # 더 이상 전체 시사점 함수(get_ai_insight)를 별도 사용하지 않고 None으로 처리하거나 통과시킴
    # (개별 기사별 핵심 요약으로 대체되었음)
    
    result = {
        "gdc": {"data": sorted_gdc, "insight": ""},
        "overseas": {"data": sorted_overseas, "insight": ""},
        "ax_news": {"data": sorted_ax_news, "insight": ""},
        "vn_jobs": {"data": sorted_vn_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

    print("✅ data.json 저장 완료.")
    save_today_history(result)
    
    send_email(result, GITHUB_PAGES_URL)
