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
            print(f"텔레그램 스크래핑 오류 ({channel}): {e}")
            
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
        1. 직장인 실전성: 일반 회사원(기획, 마케팅, 인사 등)이 당장 쓸 수 있는 프롬프트, 엑셀/문서 자동화 툴 소개면 85점 이상 부여. 개발자용 코드나 단순 주식 뉴스는 0점.
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
        print(f"⚠️ 텔레그램 AI 평가 오류: {e}")
        return []

### ==========================================
### 📧 5. 이메일/웹 통합 HTML 빌드
### ==========================================
def build_matrix_section(gdc_domestic, gdc_global, overseas_data, ax_data):
    domestic_market = ax_data[:3] if ax_data else []
