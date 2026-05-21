import os
import json
import requests
import smtplib
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

# ==========================================
# 🧠 3. AI 기반 맥락 평가 (중복 제거 및 우선순위 정렬)
# ==========================================
def process_data_with_ai_batch(data_list, data_type, api_key):
    if not api_key or not data_list: return data_list
    
    try:
        genai.configure(api_key=api_key)
        # 🚨 [오류 해결] deprecate된 1.5-flash 대신 2.5-flash 사용
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # AI에게 던질 데이터 경량화 (토큰 절약)
        input_data = [{"id": i, "title": d["title"], "company": d.get("company", "")} for i, d in enumerate(data_list)]
        
        prompt = f"""
        당신은 IT 동향 및 채용 공고를 분석하는 수석 평가자입니다.
        아래 제공된 JSON 배열 데이터를 분석하여 다음 규칙에 따라 평가하고 그 결과를 반드시 JSON 배열 형태로만 반환하세요.
        
        [데이터 유형]: {data_type}
        
        [평가 규칙]
        1. (중복 판별) 내용과 맥락이 중복되는 기사/공고가 여러 개 있다면 가장 대표적인 하나만 남기고 나머지는 배제하세요 (score = 0, is_main = false).
        2. (우선순위 산정) 기업 규모나 영향력을 추론하여 점수(0~100점)를 부여하세요.
           - 해외 글로벌 기업 및 국내 대기업 (삼성, SK, 네이버 등): 80~100점
           - 중견기업: 60~79점
           - 스타트업/미상: 30~59점
        3. {data_type}가 GDC나 AX 관련 뉴스인 경우, 관련성이 높을수록 가점을 줍니다.
        4. {data_type}가 채용 공고인 경우, 베트남 현지법인의 한국인 채용 등 파견/주재원 등 배제 조건 뉘앙스가 보이면 0점 처리하세요.
        5. 점수가 40점 이상이면 'is_main': true, 아니면 false로 설정하세요.
        
        [출력 형식] (다른 설명 없이 JSON 배열만 출력)
        [
          {{"id": 0, "score": 95, "is_main": true}},
          {{"id": 1, "score": 0, "is_main": false}}
        ]
        
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
                    
            # 0점 이하 및 False 데이터 제거 후 정렬
            filtered_data = [item for item in data_list if item.get("is_main")]
            return sorted(filtered_data, key=lambda x: x.get('score', 0), reverse=True)
            
    except Exception as e:
        print(f"⚠️ AI 평가 오류 ({data_type}): {e}")
        for item in data_list: item['is_main'] = True
        return data_list

def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return ""
    
    try:
        genai.configure(api_key=api_key)
        # 🚨 [오류 해결] 시사점 도출에도 2.5-flash 적용
        model = genai.GenerativeModel('gemini-2.5-flash')
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음 기사 제목들을 분석하여 비즈니스 측면에서 전체적인 시사점을 1~2문장으로 요약하세요.\n{titles}"
        
        response = model.generate_content(prompt)
        if response and response.text: 
            return response.text.strip()
    except Exception as e:
        return f"시사점 생성 실패: {e}"
    return "요약 생성 실패"

# ==========================================
# 📧 4. 이메일 템플릿 및 발송 (5개 제한 및 전체보기 링크)
# ==========================================
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight: 
        html += f"<div style='margin-bottom:10px;'>💡 <b>[AI 시사점]</b> {insight}</div>"
    
    display_list = [item for item in data_list if item.get('is_main', True)]
    html += "<ul>"
    
    if not data_list:
        html += "<li>수집된 데이터가 없습니다.</li>"
    elif not display_list:
        html += "<li>📌 추천 기준에 부합하는 데이터가 없습니다. 전체보기에서 확인하세요.</li>"
    else:
        # ✅ 요청하신 대로 5개만 노출되도록 제한
        for item in display_list[:5]: 
            if is_job: 
                html += f"<li><a href='{item['link']}' target='_blank'>[{item.get('company', '')}] {item['title']}</a></li>"
            else: 
                html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a> <div class='meta'>{item.get('pubDate', '')}</div></li>"
    html += "</ul>"
    
    # ✅ 전체보기(more.html) 하이퍼링크 복구
    if more_link:
        html += f"<div style='margin-top:10px;'><a href='{more_link}' target='_blank'>🔗 [웹페이지에서 전체 보기]</a></div>"
        
    return html

def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_env = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not receiver_env: 
        print("⚠️ 이메일 환경 변수가 누락되었습니다.")
        return

    receiver_emails = [email.strip() for email in receiver_env.split(',') if email.strip()]
    
    html_content = """
    <style>
        body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } 
        h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } 
        ul { list-style-type: none; padding: 0; } 
        li { padding: 8px 0; border-bottom: 1px dashed #ccc; } 
        a { color: #007bff; text-decoration: none; font-weight: bold; } 
        .meta { color: #888; font-size: 12px; margin-top: 3px; }
    </style>
    <h1>📊 일일 트렌드 및 경쟁사 동향 리포트</h1>
    """
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 원티드 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", True)
    html_content += build_email_section("💼 원티드 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", True)

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 트렌드 및 채용 동향 리포트"
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
    raw_gdc = get_naver_news(NAVER_ID, NAVER_SECRET, query="GDC") + get_naver_news(NAVER_ID, NAVER_SECRET, query="글로벌 딜리버리 센터") + get_naver_news(NAVER_ID, NAVER_SECRET, query="오프쇼어")
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환") + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI")
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse', '통역', '번역'])
    raw_ax_jobs = get_wanted_postings("AX")
    
    print("--- 🧠 AI 기반 맥락 평가 및 중복 제거 중 ---")
    sorted_gdc = process_data_with_ai_batch(raw_gdc, 'GDC 동향 뉴스', GEMINI_KEY)
    sorted_ax_news = process_data_with_ai_batch(raw_ax_news, 'AX 근황 뉴스', GEMINI_KEY)
    sorted_vn_jobs = process_data_with_ai_batch(raw_vn_jobs, '베트남 IT 채용 공고', GEMINI_KEY)
    sorted_ax_jobs = process_data_with_ai_batch(raw_ax_jobs, 'AX 채용 공고', GEMINI_KEY)
    
    print("--- 💡 시사점 도출 중 ---")
    gdc_insight = get_ai_insight(sorted_gdc, GEMINI_KEY)
    ax_insight = get_ai_insight(sorted_ax_news, GEMINI_KEY)
    
    result = {
        "gdc": {"data": sorted_gdc, "insight": gdc_insight},
        "ax_news": {"data": sorted_ax_news, "insight": ax_insight},
        "vn_jobs": {"data": sorted_vn_jobs},
        "ax_jobs": {"data": sorted_ax_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 저장 완료.")
    
    send_email(result, GITHUB_PAGES_URL)
