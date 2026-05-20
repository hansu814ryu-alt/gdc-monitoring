import os
import json
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 1. 뉴스 정렬 함수 (가중치 기반)
def sort_items_by_relevance(items, keywords):
    for item in items:
        score = 0
        title_lower = item['title'].lower()
        for kw in keywords:
            if kw in title_lower:
                score += 1
        item['score'] = score
    return sorted(items, key=lambda x: x.get('score', 0), reverse=True)

# 2. [신규] 베트남 채용 전용 필터링 및 정렬 함수
def process_vn_jobs(jobs):
    processed = []
    for job in jobs:
        score = 0
        title = job['title'].lower()
        
        # [조건 3] 한국인의 베트남/해외 파견 제외
        exclude_kw = ['주재원', '파견', '현지법인', '해외발령', '교민', '베트남 주재', '해외 주재', '해외법인']
        if any(kw in title for kw in exclude_kw):
            continue
            
        # [조건 2] 베트남인이 한국에서 IT직군 채용 (1순위 가점)
        kr_work_kw = ['베트남인', '외국인', '국내근무', '한국어', '한국근무', 'd-10', 'e-7', 'f-2', 'f-5']
        it_kw = ['개발', 'it', 'sw', 'bse', '브릿지', '소프트웨어', '프로그래머']
        
        if any(kw in title for kw in kr_work_kw): score += 20
        if any(kw in title for kw in it_kw): score += 10
            
        # [조건 1] 베트남 법인/현지 채용은 후순위 (감점)
        local_kw = ['법인', '현지', '하노이', '호치민', '다낭', '해외근무', '베트남 근무']
        if any(kw in title for kw in local_kw): score -= 10
            
        job['score'] = score
        processed.append(job)
        
    return sorted(processed, key=lambda x: x.get('score', 0), reverse=True)

# 3. [신규] AX 전담 인력 전용 정렬 함수
def process_ax_jobs(jobs):
    for job in jobs:
        score = 0
        title = job['title'].lower()
        company = job['company'].lower()
        
        # [조건 4] 대기업 채용 우선순위 가점
        large_corps = ['삼성', 'sk', 'lg', '현대', '롯데', 'cj', '한화', '신세계', 'kt', '네이버', '카카오', '포스코', 'hd', '대기업']
        if any(kw in company for kw in large_corps) or '대기업' in title:
            score += 20
            
        # [조건 5] AX 전환 직접 연관성 가점
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
        response = None
        
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

# 5. 네이버 뉴스 크롤링
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

# 6. 사람인(Saramin) 공통 수집 함수
def get_saramin_postings(search_keyword, include_keywords=None):
    url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={search_keyword}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            job_lists = soup.select('.item_recruit, .box_item')
            
            for job in job_lists:
                try:
                    title_elem = job.select_one('.job_tit a, .str_tit, .box_item a')
                    company_elem = job.select_one('.corp_name a, .box_item .name')
                    
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        company = company_elem.get_text(strip=True) if company_elem else "기업명 미상"
                        link = title_elem['href']
                        if link and not link.startswith('http'): 
                            link = "https://www.saramin.co.kr" + link
                        
                        if include_keywords:
                            if any(kw in title.lower() for kw in include_keywords):
                                filtered_jobs.append({"title": title, "company": company, "link": link})
                        else:
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
    except Exception as e:
        print(f"사람인 크롤링 오류 ({search_keyword}): {e}")
    return filtered_jobs

# 7. 이메일 섹션 생성 헬퍼
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight:
        html += f"<div style='background-color:#f0f7ff; padding:12px; margin-bottom:15px; border-left:4px solid #0056b3; font-size:14px; color:#333;'><strong>💡 [AI 시사점]</strong><br>{insight}</div>"
    html += "<ul>"
    if not data_list: html += "<li>수집된 데이터가 없습니다.</li>"
    for item in data_list[:5]:
        if is_job: html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
        else: html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a><div class='meta'>{item['pubDate']}</div></li>"
    html += "</ul>"
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[웹페이지에서 전체 보기]</a></div><br>"
    return html

# 8. 이메일 발송
def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL")
    if not sender_email or not sender_password or not receiver_email: return

    html_content = "<html><head><style>body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } ul { list-style-type: none; padding: 0; } li { padding: 8px 0; border-bottom: 1px dashed #ccc; } a { color: #007bff; text-decoration: none; font-weight: bold; } .meta { color: #888; font-size: 12px; margin-top: 3px; }</style></head><body><h1>📊 일일 트렌드 및 경쟁사 동향 리포트</h1>"
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 사람인 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", True)
    html_content += build_email_section("💼 사람인 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", True)
    html_content += "</body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] 일일 트렌드 및 경쟁사 동향 리포트"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
    except Exception as e:
        print(f"이메일 발송 실패: {e}")


if __name__ == "__main__":
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
    
    # 3 & 4. 채용 공고 수집 
    raw_vn_jobs = get_saramin_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_saramin_postings("AX")
    
    print("--- 🛠️ 핵심 키워드 가중치 기반 정렬 ---")
    news_keywords = ['반도체', '차세대', 'llm', 'ai', '가치', '평가', 'pbr', 'per', '실적', '성능', '도입', '성공']
    sorted_gdc = sort_items_by_relevance(raw_gdc, news_keywords)
    sorted_ax_news = sort_items_by_relevance(raw_ax_news, news_keywords)
    
    # [적용] 전용 프로세스(필터링/정렬) 태우기
    sorted_vn_jobs = process_vn_jobs(raw_vn_jobs)
    sorted_ax_jobs = process_ax_jobs(raw_ax_jobs)
    
    print("--- 🧠 AI 시사점 분석 중 ---")
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
