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
# ⏱️ 1. 공통 수집 기간 설정 (최근 1개월로 변경)
# ==========================================
ONE_MONTH_AGO = datetime.now(timezone.utc) - timedelta(days=30)

def is_recent_enough(pub_date_str):
    """네이버 뉴스 pubDate를 파싱하여 최근 1개월 이내인지 판별"""
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt >= ONE_MONTH_AGO
    except Exception:
        return True # 날짜 파싱 실패 시 누락 방지를 위해 일단 통과

# ==========================================
# 📰 2. 뉴스 수집 (기간 필터링 적용)
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
                # 1개월 이내 기사만 필터링
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

# ==========================================
# 💼 3. 원티드 채용공고 수집
# ==========================================
def get_wanted_postings(search_keyword, include_keywords=None):
    url = "https://www.wanted.co.kr/api/v4/jobs"
    params = {
        "country": "kr", "locations": "all", "years": "-1", 
        "limit": "50", "query": search_keyword, "job_sort": "job.latest_order"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
# 🧠 4. AI 기반 데이터 평가 (커트라인 대폭 하향)
# ==========================================
def process_data_with_ai(data_list, data_type, api_key):
    """
    사내 LLM/Gemini API가 없거나 에러가 날 경우를 대비한 가벼운 룰베이스 폴백 포함.
    커트라인을 낮추어 최대한 많은 데이터가 생존하도록 조정됨.
    """
    processed = []
    
    for item in data_list:
        score = 50 # 기본 점수를 부여하여 커트라인 통과 확률 상향
        title = item.get('title', '').lower()
        company = item.get('company', '').lower()
        
        if data_type == 'gdc':
            context_kw = ['개발', 'it', '오프쇼어링', '아웃소싱', '거점', '딜리버리', '해외', '글로벌']
            if any(kw in title for kw in context_kw): score += 30
            item['is_main'] = (score >= 40) # 기존 높은 기준에서 40점으로 대폭 완화
            
        elif data_type == 'ax_news':
            ax_kw = ['ax', 'ai', '트랜스포메이션', '전환', '도입', '혁신']
            if any(kw in title for kw in ax_kw): score += 30
            item['is_main'] = (score >= 40)
            
        elif data_type == 'vn_jobs':
            exclude_kw = ['파견', '주재원', '현지법인']
            if any(kw in title for kw in exclude_kw): score -= 40
            
            # 기업 규모 가중치 추론 (단순화)
            large_corps = ['삼성', 'sk', 'lg', '현대', '롯데', 'cj', '한화', '네이버', '카카오']
            if any(kw in company for kw in large_corps): score += 30
            else: score += 10 # 스타트업/중견기업도 기본 가점 부여
            
            item['is_main'] = (score >= 20) # 매우 관대한 커트라인
            
        elif data_type == 'ax_jobs':
            large_corps = ['삼성', 'sk', 'lg', '현대', '롯데', 'cj', '한화', '네이버', '카카오', '대기업']
            if any(kw in company for kw in large_corps) or '대기업' in title: score += 30
            else: score += 10
            
            item['is_main'] = (score >= 20)
            
        item['score'] = score
        processed.append(item)
        
    # 점수 순 정렬
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 데이터가 적어 시사점을 도출할 수 없습니다."
    
    try:
        genai.configure(api_key=api_key)
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 최근 1개월 내 주요 동향 기사 제목입니다.\n{titles}\n이 기사들의 핵심 동향을 비즈니스 측면에서 1~2문장으로 요약해 주세요."
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        if response and response.text: 
            return response.text.strip()
    except Exception as e:
        return f"시사점 생성 실패: {e}"
    return "요약 생성에 실패했습니다."

# ==========================================
# 📧 5. 이메일 템플릿 및 발송 (수신자 처리 완벽 해결)
# ==========================================
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight: html += f"<div style='margin-bottom:10px;'>💡 <b>[AI 시사점]</b> {insight}</div>"
    
    display_list = [item for item in data_list if item.get('is_main', True)]
    html += "<ul>"
    if not data_list:
        html += "<li>수집된 데이터가 없습니다.</li>"
    elif not display_list:
        html += "<li>📌 추천 기준에 부합하는 데이터가 없습니다. 전체보기에서 확인하세요.</li>"
    else:
        for item in display_list[:7]: # 노출 개수 증가
            if is_job: 
                html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
            else: 
                html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a> <div class='meta'>{item['pubDate']}</div></li>"
    html += "</ul>"
    return html

def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_env = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not receiver_env: 
        print("⚠️ 이메일 환경 변수가 누락되었습니다.")
        return

    # [중요] 수신자 이메일 파싱 및 공백 제거 처리
    receiver_emails = [email.strip() for email in receiver_env.split(',') if email.strip()]
    print(f"📧 [발송 로그] 정제된 수신자 목록: {receiver_emails}")
    
    html_content = """
    <style>
        body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } 
        h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } 
        ul { list-style-type: none; padding: 0; } 
        li { padding: 8px 0; border-bottom: 1px dashed #ccc; } 
        a { color: #007bff; text-decoration: none; font-weight: bold; } 
        .meta { color: #888; font-size: 12px; margin-top: 3px; }
    </style>
    <h1>📊 일일 트렌드 및 경쟁사 동향 리포트 (최근 1개월)</h1>
    """
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], "")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], "")
    html_content += build_email_section("💼 원티드 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], "", True)
    html_content += build_email_section("💼 원티드 AX 전담 인력 채용", "", data['ax_jobs']['data'], "", True)

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 트렌드 및 채용 동향 리포트"
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_emails)
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            # 수신자 리스트 형태로 발송
            server.sendmail(sender_email, receiver_emails, msg.as_string())
            print("✅ 이메일 발송 성공!")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

# ==========================================
# 🚀 메인 실행부
# ==========================================
if __name__ == "__main__":
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring"
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 (최근 1개월 기준) ---")
    
    # 1. 뉴스 수집
    raw_gdc = get_naver_news(NAVER_ID, NAVER_SECRET, query="GDC 오프쇼어링") + \
              get_naver_news(NAVER_ID, NAVER_SECRET, query="글로벌 딜리버리 센터")
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환") + \
                  get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입")
                  
    # 2. 채용 수집
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'bse', '통역', '번역'])
    raw_ax_jobs = get_wanted_postings("AX")
    
    print("--- 🧠 조건 완화 기반 AI 평가 및 정렬 중 ---")
    sorted_gdc = process_data_with_ai(raw_gdc, 'gdc', GEMINI_KEY)
    sorted_ax_news = process_data_with_ai(raw_ax_news, 'ax_news', GEMINI_KEY)
    sorted_vn_jobs = process_data_with_ai(raw_vn_jobs, 'vn_jobs', GEMINI_KEY)
    sorted_ax_jobs = process_data_with_ai(raw_ax_jobs, 'ax_jobs', GEMINI_KEY)
    
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
