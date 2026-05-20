import os
import json
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 1. 키워드 가중치 기반 데이터 정렬 함수
def sort_items_by_relevance(items, keywords):
    for item in items:
        score = 0
        title_lower = item['title'].lower()
        for kw in keywords:
            if kw in title_lower:
                score += 1
        item['score'] = score
    return sorted(items, key=lambda x: x.get('score', 0), reverse=True)

# 2. AI 시사점 도출 함수 (404 오류 완벽 해결 버젼)
def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 기사가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        # 404 에러 방지를 위해 사용 가능한 최신 모델들을 순서대로 시도합니다.
        model_candidates = ['gemini-2.5-flash', 'gemini-1.5-flash', 'models/gemini-1.5-flash', 'gemini-pro']
        response = None
        
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 오늘의 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적인 한글로 요약해 주세요."
        
        for model_name in model_candidates:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                if response and response.text:
                    print(f"✅ Gemini 모델 호출 성공: {model_name}")
                    return response.text.strip()
            except Exception as e:
                print(f"⚠️ {model_name} 호출 실패, 다음 모델 시도 중... (이유: {e})")
                continue
                
        return "⚠️ 모든 Gemini 모델 호출에 실패했습니다. API 키 권한이나 버전을 확인해주세요."
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# 3. 네이버 뉴스 크롤링
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

# 4. [신규] 사람인(Saramin) 공통 수집 함수 (잡코리아 대체)
def get_saramin_postings(search_keyword, include_keywords=None):
    url = f"https://www.saramin.co.kr/zf_user/search/recruit?searchword={search_keyword}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            # 사람인의 전형적인 검색 결과 공고 아이템 선택자 적용
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
                        
                        # 키워드 필터링 (OR 로직)
                        if include_keywords:
                            if any(kw in title.lower() for kw in include_keywords):
                                filtered_jobs.append({"title": title, "company": company, "link": link})
                        else:
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
        else:
            print(f"❌ 사람인 크롤링 에러 ({search_keyword}): 상태코드 {response.status_code}")
    except Exception as e:
        print(f"사람인 크롤링 오류 ({search_keyword}): {e}")
    return filtered_jobs

# 5. 이메일 섹션 생성 헬퍼
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

# 6. 이메일 발송
def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL")
    if not sender_email or not sender_password or not receiver_email: return

    html_content = "<html><head><style>body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; } h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; } ul { list-style-type: none; padding: 0; } li { padding: 8px 0; border-bottom: 1px dashed #ccc; } a { color: #007bff; text-decoration: none; font-weight: bold; } .meta { color: #888; font-size: 12px; margin-top: 3px; }</style></head><body><h1>📊 일일 트렌드 및 경쟁사 동향 리포트</h1>"
    
    html_content += build_email_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], f"{pages_url}/more.html?type=gdc")
    html_content += build_email_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], f"{pages_url}/more.html?type=ax")
    html_content += build_email_section("💼 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], f"{pages_url}/more.html?type=vn", True)
    html_content += build_email_section("💼 AX 전담 인력 채용", "", data['ax_jobs']['data'], f"{pages_url}/more.html?type=axjob", True)
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
    GITHUB_PAGES_URL = "https://본인아이디.github.io/저장소이름" 
    
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    
    # 1. GDC 뉴스 수집 (맥락 기반 필터링)
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
    
    # 3 & 4. 사람인(Saramin) 채용 공고 수집으로 변경
    raw_vn_jobs = get_saramin_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_saramin_postings("AX", ['ax', 'ai', '인공지능', '전환', '트랜스포메이션', '데이터'])
    
    print("--- 🛠️ 핵심 키워드 가중치 기반 정렬 ---")
    news_keywords = ['반도체', '차세대', 'llm', 'ai', '가치', '평가', 'pbr', 'per', '실적', '성능', '도입', '성공']
    sorted_gdc = sort_items_by_relevance(raw_gdc, news_keywords)
    sorted_ax_news = sort_items_by_relevance(raw_ax_news, news_keywords)
    
    job_keywords = ['리더', '매니저', 'pm', 'bse', '통역', '번역']
    sorted_vn_jobs = sort_items_by_relevance(raw_vn_jobs, job_keywords)
    sorted_ax_jobs = sort_items_by_relevance(raw_ax_jobs, job_keywords)
    
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
