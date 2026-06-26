import os
import json
import requests
import smtplib
import feedparser
import hashlib
import re
from bs4 import BeautifulSoup
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
                telegram_data = history.get('telegram', {}).get('data', [])
                
                for item in gdc_data + ax_data + overseas_data + telegram_data:
                    if 'link' in item: seen_links.add(item['link'])
                    if 'title' in item: seen_titles.add(item['title'])
                    if 'translated_title' in item: seen_titles.add(item['translated_title'])

                gdc_titles = [item.get('title', '') for item in gdc_data[:5]]
                ax_titles = [item.get('title', '') for item in ax_data[:5]]
                
                context_str = f"[어제 주요 이슈]: {', '.join(gdc_titles)} / {', '.join(ax_titles)}"
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
### 📰 3. 뉴스, 채용, 텔레그램 데이터 수집
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
    headers = {"User-Agent": "Mozilla/5.0"}
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
        print(f"원티드 API 오류: {e}")
    return filtered_jobs

def get_overseas_rss_news():
    rss_urls = [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://venturebeat.com/category/ai/feed/"
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
            print(f"RSS 파싱 오류: {e}")
    return filtered_news

# [NEW] 텔레그램 채널 스크래핑 함수
def get_telegram_messages():
    channels = [
        ("AI MASTERS 🇰🇷", "ai_masters_community"),
        ("The Prompt Index 🇺🇸", "ChatGPTMastermind"),
        ("모두를 위한 AI 🇰🇷", "AI_managerkim"),
        ("AI Innovation Studio 🇰🇷", "aiinnovationstudio")
    ]
    scraped_data = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    for name, channel in channels:
        url = f"https://t.me/s/{channel}"
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                msgs = soup.find_all('div', class_='tgme_widget_message')
                
                for msg in msgs[-10:]: # 각 채널별 최근 10개 메시지
                    text_div = msg.find('div', class_='tgme_widget_message_text')
                    if not text_div: continue
                    
                    text = text_div.get_text(separator='\n', strip=True)
                    if len(text) < 50: continue # 너무 짧은 메시지는 스킵
                    
                    post_id = msg.get('data-post', '')
                    link = f"https://t.me/{post_id}" if post_id else url
                    item_id = hashlib.md5((channel+text).encode('utf-8')).hexdigest()[:10]
                    
                    scraped_data.append({
                        "id": item_id,
                        "channel_name": name,
                        "text": text,
                        "link": link
                    })
        except Exception as e:
            print(f"스크래핑 오류 ({channel}): {e}")
            
    return scraped_data

### ==========================================
### 🧠 4. AI 기반 맥락 평가 및 번역
### ==========================================
def process_data_with_ai_batch(data_list, data_type, api_key, yesterday_context="", seen_links=None, seen_titles=None):
    if not api_key or not data_list: return data_list
    seen_links = seen_links or set()
    seen_titles = seen_titles or set()
    
    filtered_initial = [d for d in data_list if d['link'] not in seen_links and d['title'] not in seen_titles]
    if not filtered_initial: return []
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "desc": d.get("description", "")[:200]} for i, d in enumerate(filtered_initial)]
        
        prompt = f"""
        당신은 IT 동향 및 채용 평가자입니다. 평가 결과를 JSON 배열 형태로만 반환하세요.
        [데이터 유형]: {data_type} / [어제 맥락]: {yesterday_context}
        1. 대표 기사/공고만 남기고 중복 제거. 70점 초과면 'is_main': true 설정.
        2. is_main이 true인 경우 1줄 요약('summary') 작성.
        [형식]: [ {{"id": 0, "score": 95, "is_main": true, "summary": "...", "category_code": "..."}} ]
        [데이터]: {json.dumps(input_data, ensure_ascii=False)}
        """
        response = model.generate_content(prompt)
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_scores = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_scores}
            for i, item in enumerate(filtered_initial):
                if i in score_dict and score_dict[i].get("is_main"):
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = True
                    item["summary"] = score_dict[i].get("summary", "")
                    item["category_code"] = score_dict[i].get("category_code", "DOMESTIC_VET_IT")
                else: item["is_main"] = False
            return sorted([item for item in filtered_initial if item.get("is_main")], key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        return []

def process_overseas_with_ai_translation(data_list, api_key, yesterday_context="", seen_links=None, seen_titles=None):
    if not api_key or not data_list: return data_list
    seen_links = seen_links or set()
    seen_titles = seen_titles or set()
    
    filtered_initial = [d for d in data_list if d['link'] not in seen_links and d['title'] not in seen_titles]
    if not filtered_initial: return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "title": d["title"], "desc": d.get("description", "")[:200]} for i, d in enumerate(filtered_initial)]
        
        prompt = f"""
        글로벌 IT 뉴스 평가 및 번역가입니다. JSON 배열로 반환하세요.
        1. 80점 초과 시(is_main: true) 제목을 한글로 번역(translated_title)하고 핵심 요약(summary) 작성.
        [형식]: [ {{"id": 0, "score": 90, "is_main": true, "translated_title": "...", "summary": "..."}} ]
        [데이터]: {json.dumps(input_data, ensure_ascii=False)}
        """
        response = model.generate_content(prompt)
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_evals = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_evals}
            for i, item in enumerate(filtered_initial):
                if i in score_dict and score_dict[i].get("is_main"):
                    item["score"] = score_dict[i].get("score", 0)
                    item["is_main"] = True
                    item["translated_title"] = score_dict[i].get("translated_title", item["title"])
                    item["summary"] = score_dict[i].get("summary", "")
                else: item["is_main"] = False
            return sorted([item for item in filtered_initial if item.get("is_main")], key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        return []

# [NEW] 텔레그램 AI 가이드 생성 함수
def process_telegram_with_ai(data_list, api_key, yesterday_context="", seen_links=None):
    if not api_key or not data_list: return []
    seen_links = seen_links or set()
    filtered_initial = [d for d in data_list if d['link'] not in seen_links]
    if not filtered_initial: return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        input_data = [{"id": i, "channel": d["channel_name"], "text": d["text"]} for i, d in enumerate(filtered_initial)]
        
        prompt = f"""
        당신은 기업 실무자를 위한 'AI 업무 자동화 컨설턴트' 및 '번역가'입니다.
        아래 텔레그램 메시지들을 분석하여 다음 규칙에 따라 JSON 배열로 반환하세요.
        
        [평가 룰]
        1. 직장인 실전성: 일반 회사원(기획, 마케팅, 인사 등)이 당장 쓸 수 있는 프롬프트, 엑셀/문서 자동화 툴 소개면 85점 이상 부여. 개발자용 코드는 70점, 단순 주식 뉴스는 0점.
        2. 영어 원문일 경우 전문 용어를 살려 100% 자연스러운 한국어로 번역/의역.
        3. 80점 이상인 경우에만 아래 [3단 포맷]으로 재구성.
        
        [형식]:
        [{{
            "id": 0,
            "score": 90,
            "is_main": true,
            "title": "요약된 직관적인 제목 (한국어)",
            "situation": "어떤 업무를 할 때 쓰면 좋은지 1줄 요약",
            "method": "실제 입력할 프롬프트 예시나 툴 사용법 (원문을 바탕으로 구체적 작성)",
            "effect": "시간 단축, 퀄리티 상승 등 얻을 수 있는 이점"
        }}]
        [데이터]: {json.dumps(input_data, ensure_ascii=False)}
        """
        response = model.generate_content(prompt)
        json_match = re.search(r'\[.*\]', response.text.strip(), re.DOTALL)
        if json_match:
            ai_evals = json.loads(json_match.group(0))
            score_dict = {item["id"]: item for item in ai_evals}
            
            for i, item in enumerate(filtered_initial):
                eval_data = score_dict.get(i, {})
                if eval_data.get("is_main") and eval_data.get("score", 0) >= 80:
                    item["score"] = eval_data.get("score", 0)
                    item["is_main"] = True
                    item["title"] = eval_data.get("title", "제목 없음")
                    item["situation"] = eval_data.get("situation", "")
                    item["method"] = eval_data.get("method", "")
                    item["effect"] = eval_data.get("effect", "")
                else:
                    item["is_main"] = False
                    
            return sorted([item for item in filtered_initial if item.get("is_main")], key=lambda x: x.get('score', 0), reverse=True)
    except Exception as e:
        print(f"⚠️ AI 평가 오류: {e}")
        return []

### ==========================================
### 📧 5. 이메일/웹 통합 HTML 빌드
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
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; text-align: center; font-size: 15px; width: 15%;">구분</th>
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; text-align: left; padding-left: 20px; font-size: 15px; width: 42.5%;">🇰🇷 국내 시장</th>
                    <th style="border: 1px solid #e1e8ed; padding: 15px; background-color: #f8fafc; color: #003366; text-align: left; padding-left: 20px; font-size: 15px; width: 42.5%;">🌍 글로벌 시장</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; font-weight: bold; font-size: 14px;">시장 전반</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_market)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_market)}</td>
                </tr>
                <tr>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; background-color: #fcfcfc; text-align: center; font-weight: bold; font-size: 14px;">경쟁사 동향</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(domestic_competitor)}</td>
                    <td style="border: 1px solid #e1e8ed; padding: 15px; vertical-align: top;">{to_list_html(global_competitor)}</td>
                </tr>
            </tbody>
        </table>
    </div>
    """

def build_email_section(title, data_list, more_link, category_type, pages_url, is_job=False, is_overseas=False):
    html = f"<div style='margin-bottom: 50px;'><h2 style='color: #003366; border-bottom: 2px solid #3498db; padding-bottom: 8px; margin-top: 25px; font-size: 22px; font-weight: bold;'>{title}</h2>"
    display_list = [item for item in data_list if item.get('is_main', True) and item.get('score', 0) > 80]
    
    if not display_list:
        return html + "<p style='color: #888; font-style: italic; text-align: center;'>📌 80점 이상의 기준에 부합하는 데이터가 없습니다.</p></div>"
        
    html += '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 15px; table-layout: fixed;">'
    display_items = display_list[:4]
    
    for i in range(0, len(display_items), 2):
        html += '<tr>'
        for j in range(2):
            if j == 1: html += '<td width="4%" style="width: 4%;"></td>' 
            if i + j < len(display_items):
                item = display_items[i + j]
                item_id = item.get('id', '')
                fb_url_good = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=good"
                fb_url_normal = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=normal"
                fb_url_bad = f"{pages_url}/more.html?type={category_type}&feedback_id={item_id}&rating=bad"

                score_html = f"<span style='color: #e74c3c;'>[{item.get('score', 0)}점]</span>"
                display_title = item.get('translated_title', item['title']) if is_overseas else item['title']
                badge_html = score_html
                
                if is_job:
                    cat = item.get('category_code', '')
                    b_style = "display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 10px;"
                    if cat == 'MSP_PLAYER': badge_html = f"<span style='{b_style} background-color: #e3f2fd; color: #1565c0;'>MSP 채용</span> {score_html}"
                    elif cat == 'VET_GDC_FIRM': badge_html = f"<span style='{b_style} background-color: #e8f5e9; color: #2e7d32;'>GDC 전문업체</span> {score_html}"
                    else: badge_html = f"<span style='{b_style} background-color: #fff3e0; color: #ef6c00;'>외국인 IT 채용</span> {score_html}"

                summary_text = item.get('summary') or 'AI 핵심 요약 정보가 없습니다.'
                
                html += f"""
                <td width="48%" valign="top" style="width: 48%; padding-bottom: 20px;">
                    <div style="background-color: #ffffff; border: 1px solid #e1e8ed; border-radius: 10px; padding: 20px; height: 100%; box-sizing: border-box;">
                        <div style="font-size: 13px; font-weight: bold; margin-bottom: 10px;">{badge_html}</div>
                        <a href="{item['link']}" target="_blank" style="font-size: 16px; font-weight: bold; color: #1a1a1a; text-decoration: none; line-height: 1.4; display: block; margin-bottom: 15px;">{display_title}</a>
                        <div style="background-color: #f8fafc; border-left: 3px solid #3498db; padding: 12px; border-radius: 4px; margin-bottom: 15px;">
                            <p style="font-size: 13px; color: #4a5568; margin: 0;"><strong>💡 핵심 요약:</strong> {summary_text}</p>
                        </div>
                        <div style="margin-top: 15px; padding-top: 10px; border-top: 1px dashed #eee;">
                            <span style="font-size: 12px; color: #666; margin-right: 10px;">유용했나요?</span>
                            <a href="{fb_url_good}" style="text-decoration:none; font-size:16px; margin-right:5px;">👍</a>
                            <a href="{fb_url_normal}" style="text-decoration:none; font-size:16px; margin-right:5px;">😐</a>
                            <a href="{fb_url_bad}" style="text-decoration:none; font-size:16px;">👎</a>
                        </div>
                    </div>
                </td>
                """
            else:
                html += '<td width="48%" style="width: 48%;"></td>'
        html += '</tr>'
    html += '</table>'
    if more_link: html += f"<div style='margin-top: 20px;'><a href='{more_link}' target='_blank' style='color: #3498db; font-weight: bold; text-decoration: none;'>🔗 [전체 보기]</a></div>"
    html += "</div>"
    return html

# [NEW] 텔레그램 가이드 렌더링 섹션
def build_telegram_section(data_list, pages_url):
    html = f"<div style='margin-bottom: 50px;'><h2 style='color: #6a1b9a; border-bottom: 2px solid #9b59b6; padding-bottom: 8px; margin-top: 40px; font-size: 22px; font-weight: bold;'>💬 [실전] 직장인 AI 업무 팁</h2>"
    
    if not data_list:
        return html + "<p style='color: #888; font-style: italic; text-align: center;'>📌 오늘 수집된 실무 팁이 없습니다.</p></div>"

    for item in data_list[:5]:
        item_id = item.get('id', '')
        fb_url_good = f"{pages_url}/more.html?type=telegram&feedback_id={item_id}&rating=good"
        fb_url_normal = f"{pages_url}/more.html?type=telegram&feedback_id={item_id}&rating=normal"
        fb_url_bad = f"{pages_url}/more.html?type=telegram&feedback_id={item_id}&rating=bad"
        
        html += f"""
        <div style="background-color: #ffffff; border: 1px solid #e1e8ed; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); border-left: 5px solid #9b59b6;">
            <div style="font-size: 13px; font-weight: bold; margin-bottom: 10px;">
                <span style="display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 11px; background-color: #f3e5f5; color: #6a1b9a;">출처: {item.get('channel_name', 'Telegram')}</span>
                <span style='color: #e74c3c; margin-left: 5px;'>[{item.get('score', 0)}점]</span>
            </div>
            <h3 style="margin-top: 0; font-size: 18px;"><a href="{item['link']}" target="_blank" style="color: #2c3e50; text-decoration: none;">{item['title']}</a></h3>
            
            <div style="background-color: #faf5ff; padding: 15px; border-radius: 6px; margin-bottom: 15px; border: 1px solid #f3e5f5;">
                <p style="margin: 0 0 10px 0; font-size: 14px; color: #34495e;"><strong>🎯 적용 상황:</strong> {item.get('situation', '')}</p>
                <p style="margin: 0 0 10px 0; font-size: 14px; color: #34495e;"><strong>🛠️ 구체적 활용법:</strong><br><span style="background-color: #ffffff; padding: 8px; border-radius: 4px; display: inline-block; margin-top: 5px; font-family: monospace; border: 1px solid #e1e8ed; width: 95%;">{item.get('method', '').replace(chr(10), '<br>')}</span></p>
                <p style="margin: 0; font-size: 14px; color: #34495e;"><strong>✨ 기대 효과:</strong> {item.get('effect', '')}</p>
            </div>
            
            <div style="margin-top: 15px; padding-top: 10px; border-top: 1px dashed #eee;">
                <span style="font-size: 12px; color: #666; margin-right: 10px;">이 팁이 유용했나요?</span>
                <a href="{fb_url_good}" style="text-decoration:none; font-size:16px; margin-right:5px;">👍</a>
                <a href="{fb_url_normal}" style="text-decoration:none; font-size:16px; margin-right:5px;">😐</a>
                <a href="{fb_url_bad}" style="text-decoration:none; font-size:16px;">👎</a>
            </div>
        </div>
        """
    html += f"<div style='text-align: right;'><a href='{pages_url}/more.html?type=telegram' target='_blank' style='color: #9b59b6; font-weight: bold; text-decoration: none;'>🔗 [AI 활용 팁 전체보기]</a></div></div>"
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
    gdc_domestic = [item for item in data['gdc']['data'] if item.get('category_code') == 'DOMESTIC_GDC']
    gdc_global = [item for item in data['gdc']['data'] if item.get('category_code') == 'GLOBAL_GDC']
    
    html_content += build_matrix_section(gdc_domestic, gdc_global, data['overseas']['data'], data['ax_news']['data'])
    
    html_content += build_email_section("📊 경쟁사 동향 1. 국내 GDC", gdc_domestic, f"{pages_url}/more.html?type=gdc", "gdc", pages_url)
    html_content += build_email_section("📊 경쟁사 동향 2. 글로벌 오프쇼어링", gdc_global, f"{pages_url}/more.html?type=gdc", "gdc", pages_url)
    html_content += build_email_section("🌍 해외 AI 원천기술 및 아키텍처", data['overseas']['data'], f"{pages_url}/more.html?type=overseas", "overseas", pages_url, is_overseas=True)
    html_content += build_email_section("🏢 국내 기업 Enterprise AX", data['ax_news']['data'], f"{pages_url}/more.html?type=ax", "ax", pages_url)
    
    msp_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'MSP_PLAYER']
    gdc_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'VET_GDC_FIRM']
    vet_jobs = [j for j in data['vn_jobs']['data'] if j.get('category_code') == 'DOMESTIC_VET_IT']
    
    html_content += build_email_section("💼 채용 1. MSP Player", msp_jobs, f"{pages_url}/more.html?type=msp_jobs", "msp_jobs", pages_url, is_job=True)
    html_content += build_email_section("💼 채용 2. 베트남 GDC 업체", gdc_jobs, f"{pages_url}/more.html?type=gdc_jobs", "gdc_jobs", pages_url, is_job=True)
    html_content += build_email_section("💼 채용 3. 외국인 IT 인력", vet_jobs, f"{pages_url}/more.html?type=vet_jobs", "vet_jobs", pages_url, is_job=True)
    
    # 💡 텔레그램 섹션 추가
    html_content += build_telegram_section(data.get('telegram', {}).get('data', []), pages_url)

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
    receiver_emails = ["hansu814.ryu@samsung.com","th.jeong@samsung.com","jihoon33.kim@samsung.com","glassman@samsung.com","chaneast.kim@samsung.com","bangz0@samsung.com","tjsong@samsung.com","hj71.song@samsung.com","yoonsj@samsung.com","heeseon.yoon@samsung.com","laguna@samsung.com","jackie.chung@samsung.com","ally.chae@samsung.com","yoonseok@samsung.com","eunji0313.choi@samsung.com","yh0721.chung@samsung.com"] # 테스트용(필요시 본인 명단으로 복구하세요)
    
    if not sender_email or not sender_password: 
        print("⚠️ 발신자 정보 누락")
        return

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 기술 트렌드 및 AI 활용 팁 리포트"
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
    yesterday_context, seen_links, seen_titles = load_yesterday_context()
    
    gdc_queries = ["GDC", "글로벌 딜리버리 센터", "오프쇼어", "MSP 오프쇼어링"]
    raw_gdc = []
    for q in gdc_queries: raw_gdc.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    raw_overseas = get_overseas_rss_news()
    
    ax_queries = ["엔터프라이즈 AX", "AI 운영모델", "레거시 AI 전환"]
    raw_ax_news = []
    for q in ax_queries: raw_ax_news.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse'])
    
    raw_telegram = get_telegram_messages() # 텔레그램 수집
    
    print("--- 🧠 AI 기반 맥락 평가 / 번역 및 정렬 중 ---")
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_overseas = process_overseas_with_ai_translation(raw_overseas, GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY, yesterday_context, seen_links, seen_titles)
    
    sorted_telegram = process_telegram_with_ai(raw_telegram, GEMINI_KEY, yesterday_context, seen_links) # 텔레그램 AI 가공
    
    result = {
        "gdc": {"data": sorted_gdc},
        "overseas": {"data": sorted_overseas},
        "ax_news": {"data": sorted_ax_news},
        "vn_jobs": {"data": sorted_vn_jobs},
        "telegram": {"data": sorted_telegram} # 결과에 추가
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 저장 완료.")
    
    final_html = generate_html_content(result, GITHUB_PAGES_URL)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(final_html)
    print("✅ index.html 통일 웹페이지 저장 완료.")
        
    save_today_history(result)
    
    print("--- 📧 이메일 발송 중 ---")
    send_email(final_html)
    print("✅ 모든 파이프라인 완료!")
