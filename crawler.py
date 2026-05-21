import os
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 1. 뉴스 정렬 함수 (가중치 기반)
def sort_items_by_relevance(items, keywords):
    for item in items:
        score = 0
        title_lower = item['title'].lower()
        for kw in keywords:
            if kw in title_lower: score += 1
        item['score'] = score
    return sorted(items, key=lambda x: x.get('score', 0), reverse=True)

# 2. 베트남 채용 전용 필터링 및 정렬 함수
def process_vn_jobs(jobs):
    processed = []
    for job in jobs:
        score = 0
        title = job['title'].lower()
        
        # [배제] 한국인의 베트남/해외 파견 제외
        exclude_kw = ['주재원', '파견', '현지법인', '해외발령', '교민', '베트남 주재', '해외 주재', '해외법인']
        if any(kw in title for kw in exclude_kw):
            continue
            
        kr_work_kw = ['베트남인', '외국인', '국내근무', '한국어', '한국근무', 'd-10', 'e-7', 'f-2', 'f-5']
        it_kw = ['개발', 'it', 'sw', 'bse', '브릿지', '소프트웨어', '프로그래머']
        local_kw = ['법인', '현지', '하노이', '호치민', '다낭', '해외근무', '베트남 근무']
        
        if any(kw in title for kw in kr_work_kw): score += 20
        if any(kw in title for kw in it_kw): score += 10
        if any(kw in title for local_k in local_kw): score -= 15
            
        job['score'] = score
        # 1순위 조건 부합 여부 판단 (10점 이상)
        job['is_main'] = (score >= 10)
        
        processed.append(job)
        
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

# 3. AX 전담 인력 전용 정렬 함수
def process_ax_jobs(jobs):
    for job in jobs:
        score = 0
        title = job['title'].lower()
        company = job['company'].lower()
        
        large_corps = ['삼성', 'sk', 'lg', '현대', '롯데', 'cj', '한화', '신세계', 'kt', '네이버', '카카오', '포스코', 'hd', '대기업']
        if any(kw in company for kw in large_corps) or '대기업' in title:
            score += 20
            
        ax_direct = ['ax', 'ai 트랜스포메이션', 'ai전환', '인공지능 전환', 'ax 기획', 'ax전략', 'ai 혁신']
        ai_general = ['ai', '인공지능', '데이터']
        
        if any(kw in title for kw in ax_direct): score += 30
        elif any(kw in title for kw in ai_general): score += 5
            
        job['score'] = score
    return sorted(jobs, key=lambda x: x.get('score', 0), reverse=True)

# 4. AI 시사점 도출 함수
def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 기사가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model_candidates = ['gemini-2.5-flash', 'gemini-1.5-flash', 'models/gemini-1.5-flash', 'gemini-pro']
        
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 오늘의 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적인 한글로 요약해 주세요."
        
        for model_name in model_candidates:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                if response and response.text: return response.text.strip()
            except Exception:
                continue
        return "⚠️ 모든 Gemini 모델 호출에 실패했습니다."
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# 5. 네이버 뉴스 API 수집
def get_naver_news(client_id, client_secret, query, display=20):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": display, "sort": "sim"}
    filtered_news = []
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            for item in response.json().get('items', []):
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "description": item['description'].replace("<b>", "").replace("</b>", ""),
                    "link": item['link'],
                    "pubDate": item['pubDate']
                })
    except Exception as e:
        print(f"네이버 뉴스 오류 ({query}): {e}")
    return filtered_news

# 6. [신규] 원티드(Wanted) API 기반 채용공고 수집
def get_wanted_postings(search_keyword, include_keywords=None):
    # 원티드 채용 검색 API 엔드포인트
    url = "https://www.wanted.co.kr/api/v4/jobs"
    params = {
        "country": "kr",
        "locations": "all",
        "years": "-1", # 경력 무관 전체
        "limit": "50", # 50개까지 수집
        "query": search_keyword,
        "job_sort": "job.latest_order" # 최신순
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
        else:
            print(f"❌ 원티드 API 통신 에러 ({search_keyword}): 상태코드 {response.status_code}")
    except Exception as e:
        print(f"원티드 API 오류 ({search_keyword}): {e}")
    return filtered_jobs

# 7. 이메일 섹션 생성 헬퍼
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight:
        html += f"<div style='background-color:#f0f7ff; padding:12px; margin-bottom:15px; border-left:4px solid #0056b3; font-size:14px; color:#333;'><strong>💡 [AI 시사점]</strong><br>{insight}</div>"
    
    display_list = [item for item in data_list if item.get('is_main', True)]
    
    html += "<ul>"
    if not data_list: 
        html += "<li>수집된 데이터가 없습니다.</li>"
    elif not display_list:
        html += "<li style='color:#7f8c8d; font-size:13px;'>📌 1순위 조건에 부합하는 공고가 없습니다. 전체보기에서 확인하세요.</li>"
    else:
        for item in display_list[:5]:
            if is_job: html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
            else: html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a><div class='meta'>{item['pubDate']}</div></li>"
    html += "</ul>"
    
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[웹페이지에서 전체 보기]</a></div><br>"
    return html

# 8. 이메일 발송 (다중 수신자 및 RFC 5321 오류 해결 적용)
def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    raw_receiver_email = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not raw_receiver_email: 
        print("⚠️ 이메일 환경변수가 누락되어 발송을 건너뜁니다.")
        return

    # 여러 명의 수신자를 쉼표 기준으로 분리하고 공백 제거하여 리스트로 생성
    receiver_list = [email.strip() for email in raw_receiver_email.split(",") if email.strip()]

    if not receiver_list:
        print("⚠️ 유효한 수신자 이메일 주소가 없습니다.")
        return

    html_content = "<html><head><style>body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } ul { list-style-type: none; padding: 0; } li { padding: 8px 0; border-bottom: 1px dashed #ccc; } a { color: #007bff; text-decoration: none; font-weight: bold; } .meta { color: #888; font-size: 12px; margin-top: 3px; }</style></head><body><h1>📊 일일 트렌드 및 경쟁사 동향 리포트</h1>"
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 원티드 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", True)
    html_content += build_email_section("💼 원티드 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", True)
    html_content += "</body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 일일 트렌드 및 경쟁사 동향 리포트"
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_list) # 수신자에게 보여지는 To 헤더는 쉼표로 연결된 문자열 형태
    msg.attach(MIMEText(html_content, 'html'))

    try:
        print(f"발송 대기 중... 수신자 주소 리스트: {receiver_list}") # 디버깅용 로그 추가
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            # sendmail 함수에는 반드시 리스트(receiver_list) 형태로 전달
            server.sendmail(sender_email, receiver_list, msg.as_string())
        print("✅ 이메일 발송 성공")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

if __name__ == "__main__":
    # 아래 URL을 본인의 깃허브 페이지 주소로 변경해주세요. (끝에 / 금지)
    GITHUB_PAGES_URL = "https://hansu814ryu-alt.github.io/gdc-monitoring" 
    
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    
    # 1. GDC 뉴스 수집
    raw_gdc = []
    gdc_links = set()
    gdc_queries = ["GDC 오프쇼어링", "GDC 딜리버리", "GDC 개발", "글로벌 딜리버리 센터", "글로벌 개발 센터"]
    exclude_kw = ['game', '게임', '컨퍼런스', '바이오', '의료', '전시']
    context_kw = ['it', '소프트웨어', '개발', '오프쇼어링', '아웃소싱', '거점', '인력', '해외', '딜리버리', '센터', '전환', '구축']

    for q in gdc_queries:
        items = get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15)
        for item in items:
            text_context = (item['title'] + " " + item['description']).lower()
            if any(ex in text_context for ex in exclude_kw): continue
            if any(ctx in text_context for ctx in context_kw):
                if item['link'] not in gdc_links:
                    raw_gdc.append(item)
                    gdc_links.add(item['link'])

    # 2. AX 뉴스 수집
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환", display=20) + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입", display=20)
    
    # 3 & 4. 원티드 채용 공고 수집 (함수 교체 완료)
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_wanted_postings("AX")
    
    print("--- 🛠️ 핵심 키워드 가중치 기반 정렬 ---")
    news_keywords = ['반도체', '차세대', 'llm', 'ai', '가치', '평가', 'pbr', 'per', '실적', '성능', '도입', '성공']
    sorted_gdc = sort_items_by_relevance(raw_gdc, news_keywords)
    sorted_ax_news = sort_items_by_relevance(raw_ax_news, news_keywords)
    
    sorted_vn_jobs = process_vn_jobs(raw_vn_jobs)
    sorted_ax_jobs = process_ax_jobs(raw_ax_jobs)
    
    print("--- 🧠 AI 시사점 분석 중 ---")
    gdc_insight = get_ai_insight(sorted_gdc, GEMINI_KEY)
    ax_insight = get_ai_insight(sorted_ax_news, GEMINI_KEY)
    
    result = {
        "gdc": {"data": sorted_gdc, "insight": gdc_insight},
        "ax_news": {"data": sorted_ax_news, "insight": ax_insight},
        "ax_jobs": {"data": sorted_ax_jobs}
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 저장 완료.")

    send_email(result, GITHUB_PAGES_URL)
