import os
import json
import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# [신규] Gemini AI 시사점 도출 함수
def get_ai_insight(news_list, api_key):
    if not api_key:
        return "⚠️ GEMINI_API_KEY가 설정되지 않아 시사점을 생성할 수 없습니다."
    if not news_list:
        return "수집된 기사가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        # 최신 경량 모델 사용 (속도 및 비용 효율)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # 상위 5개 기사 제목 추출
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 오늘의 주요 관련 뉴스 기사 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적으로 요약해 주세요."
        
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# 1. 네이버 뉴스 공통 수집 함수 
def get_naver_news(client_id, client_secret, query, exclude_keywords=None, display=20):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
    params = {"query": query, "display": display, "sort": "sim"}
    filtered_news = []
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            news_data = response.json().get('items', [])
            for item in news_data:
                title_lower = item['title'].lower()
                desc_lower = item['description'].lower()
                if exclude_keywords and any(ex in title_lower or ex in desc_lower for ex in exclude_keywords):
                    continue
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "link": item['link'],
                    "pubDate": item['pubDate']
                })
        else:
            print(f"❌ 네이버 API 에러 ({query}): 상태코드 {response.status_code}")
    except Exception as e:
        print(f"네이버 뉴스 오류 ({query}): {e}")
    return filtered_news

# 2. 잡코리아 공통 수집 함수 
def get_jobkorea_postings(search_keyword, include_keywords=None):
    url = f"https://www.jobkorea.co.kr/Search/?stext={search_keyword}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://www.jobkorea.co.kr/"
    }
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            job_lists = soup.select('li.list-post, div.post, .list-default li.clear, article.list-item')
            
            for job in job_lists:
                try:
                    title_elem = job.select_one('.title, .information > a, .post-list-info a')
                    company_elem = job.select_one('.name, .corp-name, .post-list-corp a')
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        company = company_elem.get_text(strip=True) if company_elem else "기업명 미상"
                        
                        link = title_elem['href'] if title_elem.has_attr('href') else (job.select_one('a')['href'] if job.select_one('a') else "")
                        if link and not link.startswith('http'): link = "https://www.jobkorea.co.kr" + link
                            
                        if include_keywords:
                            if any(kw in title.lower() for kw in include_keywords):
                                filtered_jobs.append({"title": title, "company": company, "link": link})
                        else:
                            filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
        else:
            print(f"❌ 잡코리아 크롤링 에러 ({search_keyword}): 상태코드 {response.status_code}")
    except Exception as e:
        print(f"잡코리아 오류 ({search_keyword}): {e}")
    return filtered_jobs

# HTML 섹션 생성 헬퍼 함수 (5개 제한 및 더보기 적용)
def build_html_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight:
        html += f"<div style='background-color:#f0f7ff; padding:12px; margin-bottom:15px; border-left:4px solid #0056b3; font-size:14px; color:#333;'><strong>💡 [AI 시사점]</strong><br>{insight}</div>"
    
    html += "<ul>"
    if not data_list:
        html += "<li>수집된 데이터가 없습니다.</li>"
    else:
        # 상위 5개만 노출
        for item in data_list[:5]:
            if is_job:
                html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
            else:
                html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a><div class='meta'>{item['pubDate']}</div></li>"
    html += "</ul>"
    
    # 더보기 링크
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[+ 새 창에서 더보기]</a></div><br>"
    return html

# 3. 이메일 발송 함수
def send_email(data):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL")

    if not sender_email or not sender_password or not receiver_email:
        print("⚠️ 이메일 환경변수가 누락되었습니다.")
        return

    html_content = """
    <html>
    <head>
        <style>
            body { font-family: 'Malgun Gothic', sans-serif; line-height: 1.6; }
            h2 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 5px; margin-top: 25px; }
            ul { list-style-type: none; padding: 0; margin-top: 5px; }
            li { padding: 8px 0; border-bottom: 1px dashed #ccc; }
            a { color: #007bff; text-decoration: none; font-weight: bold; }
            a:hover { text-decoration: underline; }
            .meta { color: #888; font-size: 12px; margin-top: 3px; }
        </style>
    </head>
    <body>
        <h1>📊 일일 트렌드 및 경쟁사 동향 리포트</h1>
    """
    
    html_content += build_html_section("📊 GDC 시장 및 경쟁사 동향", data['gdc']['insight'], data['gdc']['data'], data['gdc']['more_link'])
    html_content += build_html_section("📰 AI 기술 근황 & AX 전환 사례", data['ax_news']['insight'], data['ax_news']['data'], data['ax_news']['more_link'])
    html_content += build_html_section("💼 베트남 채용 (IT/BSE/통번역)", "", data['vn_jobs']['data'], data['vn_jobs']['more_link'], True)
    html_content += build_html_section("💼 AX 전담 인력 채용", "", data['ax_jobs']['data'], data['ax_jobs']['more_link'], True)
    
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
        print("✅ 이메일 발송 성공!")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")

if __name__ == "__main__":
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 데이터 크롤링 시작 ---")
    raw_gdc = get_naver_news(NAVER_ID, NAVER_SECRET, query="GDC", exclude_keywords=['game', '게임', '게임개발', 'game development'])
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환", display=15) + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입", display=15)
    raw_vn_jobs = get_jobkorea_postings(search_keyword="베트남", include_keywords=['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_jobkorea_postings(search_keyword="AX", include_keywords=['ax', 'ai', '인공지능', '전환', '트랜스포메이션', '데이터'])
    
    print("--- 🧠 AI 시사점 분석 중 ---")
    gdc_insight = get_ai_insight(raw_gdc, GEMINI_KEY)
    ax_insight = get_ai_insight(raw_ax_news, GEMINI_KEY)
    
    # 구조화된 데이터 생성 ('더보기' 링크 포함)
    result = {
        "gdc": {
            "data": raw_gdc,
            "insight": gdc_insight,
            "more_link": "https://search.naver.com/search.naver?where=news&query=GDC"
        },
        "ax_news": {
            "data": raw_ax_news,
            "insight": ax_insight,
            "more_link": "https://search.naver.com/search.naver?where=news&query=AX+%EC%A0%84%ED%99%98"
        },
        "vn_jobs": {
            "data": raw_vn_jobs,
            "more_link": "https://www.jobkorea.co.kr/Search/?stext=베트남"
        },
        "ax_jobs": {
            "data": raw_ax_jobs,
            "more_link": "https://www.jobkorea.co.kr/Search/?stext=AX"
        }
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("✅ data.json 파일 저장 완료.")

    print("--- 📧 이메일 발송 시작 ---")
    send_email(result)
