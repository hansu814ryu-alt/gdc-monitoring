import os
import json
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# 1. 네이버 뉴스 공통 수집 함수
def get_naver_news(client_id, client_secret, query, exclude_keywords=None, display=20):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    params = {"query": query, "display": display, "sort": "sim"}
    
    filtered_news = []
    try:
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            news_data = response.json().get('items', [])
            
            for item in news_data:
                title_lower = item['title'].lower()
                desc_lower = item['description'].lower()
                
                if exclude_keywords:
                    if any(ex in title_lower or ex in desc_lower for ex in exclude_keywords):
                        continue
                
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "link": item['link'],
                    "pubDate": item['pubDate']
                })
        else:
            print(f"❌ 네이버 API 에러 ({query}): 상태코드 {response.status_code}")
            
    except Exception as e:
        print(f"네이버 뉴스 통신 중 오류 발생 ({query}): {e}")
        
    return filtered_news

# 2. 잡코리아 공통 수집 함수 (강화된 탐색 로직 및 봇 차단 확인)
def get_jobkorea_postings(search_keyword, include_keywords=None):
    url = f"https://www.jobkorea.co.kr/Search/?stext={search_keyword}"
    # 봇 차단을 우회하기 위해 헤더 정보 강화
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.jobkorea.co.kr/"
    }
    
    filtered_jobs = []
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 잡코리아 구조 변경 대비: 다양한 클래스명으로 공고 리스트 탐색
            job_lists = soup.select('li.list-post, div.post, .list-default li.clear, article.list-item')
            
            if not job_lists:
                print(f"⚠️ 잡코리아 검색결과를 찾지 못했습니다. ({search_keyword})")
                # 봇 차단(캡차) 여부를 확인하기 위해 페이지 텍스트 200자 미리보기 출력
                page_text = soup.get_text(separator=' ', strip=True)[:200]
                print(f"   [디버깅] 접속된 페이지 내용: {page_text}...")
            
            for job in job_lists:
                try:
                    # 제목과 회사명도 다양한 가능성을 두고 탐색
                    title_elem = job.select_one('.title, .information > a, .post-list-info a')
                    company_elem = job.select_one('.name, .corp-name, .post-list-corp a')
                    
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        title_lower = title.lower()
                        company = company_elem.get_text(strip=True) if company_elem else "기업명 미상"
                        
                        # 링크 추출 (a 태그 직접 선택 또는 href 속성 찾기)
                        link = ""
                        if title_elem.has_attr('href'):
                            link = title_elem['href']
                        else:
                            a_tag = job.select_one('a')
                            if a_tag and a_tag.has_attr('href'):
                                link = a_tag['href']
                        
                        if link and not link.startswith('http'):
                            link = "https://www.jobkorea.co.kr" + link
                            
                        # 키워드 필터링 (OR 로직)
                        if include_keywords:
                            if any(kw in title_lower for kw in include_keywords):
                                filtered_jobs.append({"title": title, "company": company, "link": link})
                        else:
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
        else:
            print(f"❌ 잡코리아 크롤링 에러 ({search_keyword}): 상태코드 {response.status_code}")
            
    except Exception as e:
        print(f"잡코리아 통신 중 오류 발생 ({search_keyword}): {e}")
        
    return filtered_jobs

# 3. 이메일 발송 함수
def send_email(data):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("⚠️ 이메일 환경변수가 설정되지 않아 메일을 발송하지 않습니다.")
        return

    html_content = """
    <html>
    <head>
        <style>
            body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; }
            h2 { color: #0056b3; border-bottom: 2px solid #0056b3; padding-bottom: 5px; margin-top: 30px; }
            ul { list-style-type: none; padding: 0; }
            li { padding: 8px 0; border-bottom: 1px dashed #ccc; }
            a { color: #007bff; text-decoration: none; font-weight: bold; }
            a:hover { text-decoration: underline; }
            .meta { color: #888; font-size: 12px; margin-top: 3px; }
        </style>
    </head>
    <body>
        <h1>📊 IT/AX 및 GDC 모니터링 대시보드</h1>
    """
    
    html_content += "<h2>📰 GDC 뉴스 (게임 제외)</h2><ul>"
    if not data['gdc_news']: html_content += "<li>수집된 데이터가 없습니다.</li>"
    for item in data['gdc_news']: html_content += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a><div class='meta'>{item['pubDate']}</div></li>"
    html_content += "</ul>"

    html_content += "<h2>📰 AI 기술 근황 & AX 전환 사례</h2><ul>"
    if not data['ax_news']: html_content += "<li>수집된 데이터가 없습니다.</li>"
    for item in data['ax_news']: html_content += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a><div class='meta'>{item['pubDate']}</div></li>"
    html_content += "</ul>"

    html_content += "<h2>💼 베트남 채용 (IT/BSE/통번역)</h2><ul>"
    if not data['vn_jobs']: html_content += "<li>수집된 데이터가 없습니다.</li>"
    for item in data['vn_jobs']: html_content += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
    html_content += "</ul>"

    html_content += "<h2>💼 AX 전담 인력 채용</h2><ul>"
    if not data['ax_jobs']: html_content += "<li>수집된 데이터가 없습니다.</li>"
    for item in data['ax_jobs']: html_content += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
    html_content += "</ul></body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = "📊 [자동화] IT/AX 트렌드 및 GDC 모니터링 결과"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
        print("✅ 이메일 발송 성공!")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

if __name__ == "__main__":
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    
    gdc_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="GDC", exclude_keywords=['game', '게임', '게임개발', 'game development', '물류'])
    ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환", display=15)
    ax_news += get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입", display=15)
    
    vn_jobs = get_jobkorea_postings(search_keyword="베트남", include_keywords=['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    ax_jobs = get_jobkorea_postings(search_keyword="AI", include_keywords=['ax', 'ai', 'AX전환', '트랜스포메이션', '데이터'])
    
    print("--- 🏁 데이터 크롤링 완료 ---")
    
    result = {
        "gdc_news": gdc_news,
        "ax_news": ax_news,
        "vn_jobs": vn_jobs,
        "ax_jobs": ax_jobs
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 파일 저장 완료.")

    print("--- 📧 이메일 발송 시작 ---")
    send_email(result)
