import os
import json
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# [신규] 키워드 가중치 기반 데이터 정렬 함수
def sort_items_by_relevance(items, keywords):
    for item in items:
        score = 0
        title_lower = item['title'].lower()
        for kw in keywords:
            if kw in title_lower:
                score += 1
        item['score'] = score
    # 가중치 점수가 높은 순서대로 정렬하여 반환
    return sorted(items, key=lambda x: x.get('score', 0), reverse=True)

# AI 시사점 도출 함수 (오류 픽스 및 모델 안정화)
def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 기사가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        # 최신 API 버전에 맞는 모델명 적용 및 폴백(Fallback) 구조
        try:
            model = genai.GenerativeModel('gemini-1.5-flash-latest')
        except Exception:
            model = genai.GenerativeModel('gemini-pro')
            
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 오늘의 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하게 요약해 주세요."
        
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# 네이버 뉴스 크롤링
def get_naver_news(client_id, client_secret, query, exclude_keywords=None):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": 30, "sort": "sim"}
    filtered_news = []
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            for item in response.json().get('items', []):
                title_lower = item['title'].lower()
                desc_lower = item['description'].lower()
                if exclude_keywords and any(ex in title_lower or ex in desc_lower for ex in exclude_keywords):
                    continue
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "link": item['link'],
                    "pubDate": item['pubDate']
                })
    except Exception as e:
        print(f"네이버 뉴스 오류 ({query}): {e}")
    return filtered_news

# 잡코리아 크롤링
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

# 이메일 섹션 생성 헬퍼
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
    
    # 이메일에서는 메인 웹 대시보드로 유도
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[웹페이지에서 전체 보기]</a></div><br>"
    return html

# 이메일 발송
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
    # 아래 URL을 본인의 깃허브 페이지 주소로 변경하시면 이메일의 더보기 링크가 정상 작동합니다.
    GITHUB_PAGES_URL = "https://본인아이디.github.io/저장소이름" 
    
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    raw_gdc = get_naver_news(NAVER_ID, NAVER_SECRET, query="GDC", exclude_keywords=['game', '게임', '게임개발', 'game development'])
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환") + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입")
    raw_vn_jobs = get_jobkorea_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_jobkorea_postings("AX", ['ax', 'ai', '인공지능', '전환', '트랜스포메이션', '데이터'])
    
    print("--- 🛠️ 핵심 키워드 가중치 기반 정렬 ---")
    # 뉴스: 기술 성능평가 및 기업 재무/투자 지표 관련 키워드 우대
    news_keywords = ['반도체', '차세대', 'llm', 'ai', '가치', '평가', 'pbr', 'per', '실적', '성능']
    sorted_gdc = sort_items_by_relevance(raw_gdc, news_keywords)
    sorted_ax_news = sort_items_by_relevance(raw_ax_news, news_keywords)
    
    # 채용: 핵심 인력 직무 관련 우대
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
