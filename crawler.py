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

# AI 시사점 도출 함수 (404 오류 완벽 해결 버젼)
def get_ai_insight(news_list, api_key):
    if not api_key: 
        return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: 
        return "수집된 기사가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        # v1beta 및 일부 구형 환경과의 하방 호환성을 고려한 가장 안정적인 기본 모델명 지정
        # 최신 GA(정식 버전) 모델명을 명시적으로 사용합니다.
        model_name = 'gemini-1.5-flash'
        
        try:
            model = genai.GenerativeModel(model_name)
        except Exception:
            # 환경에 따라 구형 패키지 버전을 쓸 경우를 대비한 폴백 모델명 지정
            model = genai.GenerativeModel('gemini-pro')
            
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 오늘의 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적인 한글로 요약해 주세요."
        
        response = model.generate_content(prompt)
        return response.text.strip()
        
    except Exception as e:
        # 최종 예외 처리 단계에서 에러 원인을 명확히 로그에 출력
        return f"시사점 생성 실패: {e}"

# 3. 네이버 뉴스 크롤링 (요약 본문 추출 추가)
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

# 4. 잡코리아 크롤링
def get_jobkorea_postings(search_keyword, include_keywords=None):
    url = f"https://www.jobkorea.co.kr/Search/?stext={search_keyword}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            for job in soup.select('li.list-post, div.post, .list-default li.clear, article.list-item'):
                try:
                    title_elem = job.select_one('.title, .information > a, .post-list-info a')
                    company_elem = job.select_one('.name, .corp-name, .post-list-corp a')
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        company = company_elem.get_text(strip=True) if company_elem else "기업명 미상"
                        link = title_elem['href'] if title_elem.has_attr('href') else job.select_one('a')['href']
                        if link and not link.startswith('http'): link = "https://www.jobkorea.co.kr" + link
                        
                        if include_keywords and any(kw in title.lower() for kw in include_keywords):
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                        elif not include_keywords:
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
    except Exception as e:
        print(f"잡코리아 오류 ({search_keyword}): {e}")
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
    # 아래 URL을 본인의 깃허브 페이지 주소로 변경해주세요. (예: https://myid.github.io/gdc-monitoring)
    GITHUB_PAGES_URL = "https://본인아이디.github.io/저장소이름" 
    
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    
    # ==========================================
    # [핵심 로직 변경] 1. GDC 뉴스 수집 (IT 오프쇼어링 이중 맥락 필터링)
    # ==========================================
    raw_gdc = []
    gdc_links = set() # 중복 기사 제거용
    
    # 다중 검색어로 네이버 DB에서 정확한 후보군 1차 확보
    gdc_queries = ["GDC 오프쇼어링", "GDC 딜리버리", "GDC 개발", "글로벌 딜리버리 센터", "글로벌 개발 센터"]
    
    exclude_kw = ['game', '게임', '컨퍼런스', '바이오', '의료', '전시']
    context_kw = ['it', '소프트웨어', '개발', '오프쇼어링', '아웃소싱', '거점', '인력', '해외', '딜리버리', '센터', '전환', '구축']

    for q in gdc_queries:
        items = get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15)
        for item in items:
            # 제목과 요약본을 합쳐서 문맥(Context) 파악
            text_context = (item['title'] + " " + item['description']).lower()
            
            # 1. 배제 키워드가 하나라도 있으면 스킵
            if any(ex in text_context for ex in exclude_kw):
                continue
                
            # 2. 필수 맥락 키워드가 포함된 경우만 수집 통과
            if any(ctx in text_context for ctx in context_kw):
                if item['link'] not in gdc_links:
                    raw_gdc.append(item)
                    gdc_links.add(item['link'])

    # 2. AI 뉴스 수집
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환", display=20) + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입", display=20)
    
    # 3 & 4. 잡코리아 채용 공고 수집
    raw_vn_jobs = get_jobkorea_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_jobkorea_postings("AX", ['ax', 'ai', '인공지능', '전환', '트랜스포메이션', '데이터'])
    
    print("--- 🛠️ 핵심 키워드 가중치 기반 정렬 ---")
    # 뉴스: 기술 평가 및 비즈니스 실적/투자 관련 용어 우대
    news_keywords = ['반도체', '차세대', 'llm', 'ai', '가치', '평가', 'pbr', 'per', '실적', '성능', '도입', '성공']
    sorted_gdc = sort_items_by_relevance(raw_gdc, news_keywords)
    sorted_ax_news = sort_items_by_relevance(raw_ax_news, news_keywords)
    
    # 채용: 핵심 책임자/전문가 직무 용어 우대
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
