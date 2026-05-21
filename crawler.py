import os
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

# 0. 공통: 최근 1주일 이내 데이터 판별 함수
def is_within_a_week(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        now = datetime.now(dt.tzinfo)
        return dt >= now - timedelta(days=7)
    except Exception:
        return True # 파싱 실패 시 기본적으로 통과시킴

# 0. 공통: 기업 규모별 가중치 평가 헬퍼 함수
def get_company_scale_score(company, title):
    score = 0
    comp_lower = company.lower()
    title_lower = title.lower()
    
    large = ['삼성', 'sk', 'lg', '현대', '롯데', 'cj', '한화', '신세계', 'kt', '포스코', 'hd', '네이버', '카카오', '대기업']
    mid = ['중견', '홀딩스', '그룹', '네트웍스', '시스템즈']
    startup = ['스타트업', '벤처', '시리즈']
    
    if any(kw in comp_lower for kw in large) or '대기업' in title_lower: score += 30
    elif any(kw in comp_lower for kw in mid) or '중견' in title_lower: score += 20
    elif any(kw in comp_lower for kw in startup) or '스타트업' in title_lower: score += 10
    
    return score

# 1. GDC 뉴스 정렬 및 필터링 (맥락 강화)
def process_gdc_news(news_list):
    processed = []
    seen = set()
    for news in news_list:
        title = news['title']
        if title in seen: continue
        seen.add(title)
        
        text = (title + " " + news['description']).lower()
        
        # IT 및 오프쇼어링 맥락이 둘 다 있는지 깐깐하게 확인
        it_kw = ['it', '개발', '소프트웨어', '시스템', '운영', 'sw']
        offshore_kw = ['오프쇼어링', '딜리버리', '거점', '글로벌 센터', '인도', '베트남', '해외 인력', '아웃소싱']
        
        has_it = any(kw in text for kw in it_kw)
        has_offshore = any(kw in text for kw in offshore_kw)
        
        if has_it and has_offshore:
            news['score'] = 10 # 기본 통과 점수
            processed.append(news)
            
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

# 2. AX 뉴스 정렬 (중복 제거 및 글로벌>대기업>스타트업)
def process_ax_news(news_list):
    processed = []
    seen = set()
    for news in news_list:
        title = news['title']
        if title in seen: continue
        seen.add(title)
        
        score = 0
        text = (title + " " + news['description']).lower()
        
        global_corp = ['구글', '마이크로소프트', 'ms', '아마존', 'aws', '애플', '메타', '엔비디아', '글로벌']
        large_corp = ['삼성', 'sk', 'lg', '현대', '네이버', '카카오', '대기업']
        startup = ['스타트업', '벤처']
        
        if any(kw in text for kw in global_corp): score += 30
        elif any(kw in text for kw in large_corp): score += 20
        elif any(kw in text for kw in startup): score += 10
            
        news['score'] = score
        processed.append(news)
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

# 3. 베트남 채용 필터링 및 정렬 (대기업>중견>스타트업)
def process_vn_jobs(jobs):
    processed = []
    for job in jobs:
        score = 0
        title = job['title'].lower()
        company = job['company'].lower()
        
        # [배제] 한국인의 베트남/해외 파견 제외
        exclude_kw = ['주재원', '파견', '현지법인', '해외발령', '교민', '베트남 주재', '해외 주재', '해외법인']
        if any(kw in title for kw in exclude_kw): continue
            
        kr_work_kw = ['베트남인', '외국인', '국내근무', '한국어', '한국근무', 'd-10', 'e-7', 'f-2', 'f-5']
        it_kw = ['개발', 'it', 'sw', 'bse', '브릿지', '소프트웨어', '프로그래머']
        local_kw = ['법인', '현지', '하노이', '호치민', '다낭', '해외근무', '베트남 근무']
        
        if any(kw in title for kw in kr_work_kw): score += 20
        if any(kw in title for kw in it_kw): score += 10
        if any(kw in title for local_k in local_kw): score -= 15
        
        # 기업 규모에 따른 우선순위 배점 합산
        score += get_company_scale_score(company, title)
            
        job['score'] = score
        job['is_main'] = (score >= 10)
        processed.append(job)
        
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

# 4. AX 전담 인력 정렬 (대기업>중견>스타트업)
def process_ax_jobs(jobs):
    for job in jobs:
        score = 0
        title = job['title'].lower()
        company = job['company'].lower()
        
        ax_direct = ['ax', 'ai 트랜스포메이션', 'ai전환', '인공지능 전환', 'ax 기획', 'ax전략', 'ai 혁신']
        ai_general = ['ai', '인공지능', '데이터']
        
        if any(kw in title for kw in ax_direct): score += 30
        elif any(kw in title for kw in ai_general): score += 15
        
        # 기업 규모에 따른 우선순위 배점 합산
        score += get_company_scale_score(company, title)
            
        job['score'] = score
        job['is_main'] = (score >= 10)
        
    return sorted(jobs, key=lambda x: x.get('score', 0), reverse=True)

# AI 시사점 도출 함수
def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 데이터가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model_candidates = ['gemini-2.5-flash', 'gemini-1.5-flash', 'models/gemini-1.5-flash', 'gemini-pro']
        
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 최근 1주일 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적인 한글로 요약해 주세요."
        
        for model_name in model_candidates:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                if response and response.text: return response.text.strip()
            except Exception:
                continue
        return "⚠️ 모델 호출 실패"
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# 네이버 뉴스 API (1주일 필터 적용)
def get_naver_news(client_id, client_secret, query, display=30):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": display, "sort": "sim"}
    filtered_news = []
    seen_links = set()
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            for item in response.json().get('items', []):
                # 1주일 이내 기사만 통과
                if not is_within_a_week(item['pubDate']):
                    continue
                
                link = item['link']
                if link in seen_links: continue
                seen_links.add(link)
                
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "description": item['description'].replace("<b>", "").replace("</b>", ""),
                    "link": link,
                    "pubDate": item['pubDate']
                })
    except Exception as e:
        print(f"네이버 뉴스 오류 ({query}): {e}")
    return filtered_news

# 원티드 API 수집
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
            for job in response.json().get('data', []):
                try:
                    title = job.get('position', '')
                    company = job.get('company', {}).get('name', '기업명 미상')
                    link = f"https://www.wanted.co.kr/wd/{job.get('id', '')}"
                    
                    if include_keywords and not any(kw in title.lower() for kw in include_keywords):
                        continue
                        
                    filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
    except Exception as e:
        print(f"원티드 API 오류 ({search_keyword}): {e}")
    return filtered_jobs

# 이메일 HTML 생성
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight:
        html += f"<div style='background-color:#f0f7ff; padding:12px; margin-bottom:15px; border-left:4px solid #0056b3; font-size:14px; color:#333;'><strong>💡 [AI 시사점]</strong><br>{insight}</div>"
    
    display_list = [item for item in data_list if item.get('is_main', True) or 'is_main' not in item]
    
    html += "<ul>"
    if not data_list: 
        html += "<li>최근 1주일 내 수집된 데이터가 없습니다.</li>"
    elif not display_list:
        html += "<li style='color:#7f8c8d; font-size:13px;'>📌 1순위 조건에 부합하는 공고가 없습니다. 전체보기에서 확인하세요.</li>"
    else:
        for item in display_list[:5]:
            if is_job: html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
            else: html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a></li>"
    html += "</ul>"
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[웹페이지에서 전체 보기]</a></div><br>"
    return html

# 이메일 발송
def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    raw_receiver_email = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not raw_receiver_email: return

    receiver_list = [email.strip() for email in raw_receiver_email.split(",") if email.strip()]
    if not receiver_list: return

    html_content = "<html><head><style>body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } ul { list-style-type: none; padding: 0; } li { padding: 8px 0; border-bottom: 1px dashed #ccc; } a { color: #007bff; text-decoration: none; font-weight: bold; } </style></head><body><h1>📊 일일 트렌드 및 경쟁사 동향 리포트 (최근 1주)</h1>"
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 원티드 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", True)
    html_content += build_email_section("💼 원티드 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", True)
    html_content += "</body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 주간 트렌드 및 경쟁사 동향 리포트"
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_list)
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_list, msg.as_string())
        print("✅ 이메일 발송 완료")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

if __name__ == "__main__":
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring" 
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 1주일 한정 데이터 크롤링 시작 ---")
    
    # 1. GDC 뉴스 (수
