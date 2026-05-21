import os
import json
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

# --- 0. 공통: 1주일 이내 데이터 필터링 ---
def is_within_a_week(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        now = datetime.now(dt.tzinfo)
        return dt >= now - timedelta(days=7)
    except Exception:
        return True 

# --- AI 맥락 기반 평가 (LLM-as-a-Judge) 헬퍼 함수 ---
def evaluate_with_llm(data_list, prompt, api_key):
    if not api_key or not data_list:
        return data_list

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        # JSON 출력을 안정적으로 받기 위해 1.5-flash 모델 사용
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # 데이터를 JSON 문자열로 변환하여 프롬프트에 첨부
        data_json = json.dumps(data_list, ensure_ascii=False)
        full_prompt = f"""
        {prompt}
        반드시 아래의 원본 데이터 길이와 순서를 유지하거나, 지시에 따라 필터링한 후 
        결과를 JSON 배열(Array) 형태로만 출력해 줘. (마크다운 ```json 기호는 제외할 것)
        
        [원본 데이터]
        {data_json}
        """
        
        response = model.generate_content(full_prompt)
        result_text = response.text.strip().replace("```json", "").replace("```", "")
        
        # LLM이 반환한 JSON 텍스트 파싱
        evaluated_data = json.loads(result_text)
        
        # 점수(score) 기준으로 내림차순 정렬
        return sorted(evaluated_data, key=lambda x: x.get('score', 0), reverse=True)
        
    except Exception as e:
        print(f"⚠️ AI 맥락 평가 실패 (기본 데이터 반환): {e}")
        # 실패 시 기본 점수 부여 후 반환
        for item in data_list:
            item['score'] = 10
        return data_list

# --- 1. GDC 동향 AI 필터링 ---
def process_gdc_with_ai(news_list, api_key):
    prompt = """
    이 뉴스 기사들이 'Global Development Center(GDC)', 즉 'IT 운영 및 개발의 해외 오프쇼어링/해외 거점/아웃소싱'과 관련된 
    맥락을 정확히 담고 있는지 판단해라. 
    단순히 게임, 바이오, 의료 행사 관련 GDC(Game Developers Conference 등)는 0점으로 처리해라.
    IT 오프쇼어링/해외 개발 센터 구축에 대한 맥락이면 100점을 부여하고, 부분적으로 연관되면 50점~80점을 부여해라.
    결과 JSON에는 원본의 title, link, pubDate를 유지하고 'score' 필드를 추가하여 50점 이상인 데이터만 남겨라.
    """
    return evaluate_with_llm(news_list, prompt, api_key)

# --- 2. AX 뉴스 AI 중복 제거 및 우선순위 ---
def process_ax_news_with_ai(news_list, api_key):
    prompt = """
    이 뉴스 기사들 중 같은 주제나 사건을 다루는 '중복 기사'는 문맥을 분석하여 가장 제목이 명확한 1개만 남기고 삭제해라.
    살아남은 기사들에 대해, 기사 맥락에 등장하는 기업의 규모를 스스로 추론하여 다음과 같이 'score'를 부여해라.
    - 해외 글로벌 기업(구글, MS, 애플, 엔비디아 등) 주도 사례: 100점
    - 국내 대기업(삼성, 현대, SK, 네이버 등) 주도 사례: 80점
    - 스타트업 또는 중견기업 주도 사례: 60점
    결과 JSON에는 원본의 title, link, pubDate를 유지하고 'score' 필드를 추가해라.
    """
    return evaluate_with_llm(news_list, prompt, api_key)

# --- 3 & 4. 원티드 채용 AI 기업 규모 및 맥락 평가 ---
def process_jobs_with_ai(jobs_list, api_key, job_type="VN"):
    extra_rule = ""
    if job_type == "VN":
        extra_rule = "단, 한국인의 베트남/해외 단순 주재원이나 파견직 맥락이 강하면 0점을 부여하여 하단으로 내려라."
        
    prompt = f"""
    이 채용 공고들의 'company(기업명)'과 'title(공고 제목)'을 기반으로 기업의 규모를 스스로 추론하여 'score'를 매겨라.
    - 대기업(계열사 포함) 및 글로벌 기업: 100점
    - 중견기업 및 유명 IT 플랫폼: 80점
    - 스타트업 및 소기업: 60점
    {extra_rule}
    결과 JSON에는 원본의 title, company, link를 유지하고 'score' 필드를 추가해라.
    """
    return evaluate_with_llm(jobs_list, prompt, api_key)

# --- AI 시사점 도출 함수 ---
def get_ai_insight(news_list, api_key):
    if not api_key: return "⚠️ GEMINI_API_KEY가 설정되지 않았습니다."
    if not news_list: return "수집된 데이터가 없어 시사점을 생성할 수 없습니다."
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        titles = [news['title'] for news in news_list[:5]]
        prompt = f"다음은 최근 1주일 주요 동향 뉴스 제목 5개입니다.\n{titles}\n이 기사들의 핵심 동향을 분석하여, 비즈니스 측면의 전체적인 시사점을 딱 1~2문장으로 간결하고 전문적인 한글로 요약해 주세요."
        
        response = model.generate_content(prompt)
        if response and response.text: 
            return response.text.strip()
        return "⚠️ 모델 호출 실패"
    except Exception as e:
        return f"시사점 생성 실패: {e}"

# --- 네이버 뉴스 API 수집 (1주일 한정) ---
def get_naver_news(client_id, client_secret, query, display=30):
    url = "[https://openapi.naver.com/v1/search/news.json](https://openapi.naver.com/v1/search/news.json)"
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

# --- 원티드 API 기반 채용 수집 ---
def get_wanted_postings(search_keyword, include_keywords=None):
    url = "[https://www.wanted.co.kr/api/v4/jobs](https://www.wanted.co.kr/api/v4/jobs)"
    params = {
        "country": "kr", "locations": "all", "years": "-1",
        "limit": "30", "query": search_keyword, "job_sort": "job.latest_order"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "[https://www.wanted.co.kr/](https://www.wanted.co.kr/)"
    }
    filtered_jobs = []
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            for job in response.json().get('data', []):
                try:
                    title = job.get('position', '')
                    company = job.get('company', {}).get('name', '기업명 미상')
                    link = f"[https://www.wanted.co.kr/wd/](https://www.wanted.co.kr/wd/){job.get('id', '')}"
                    
                    if include_keywords and not any(kw in title.lower() for kw in include_keywords):
                        continue
                        
                    filtered_jobs.append({"title": title, "company": company, "link": link})
                except Exception:
                    continue
    except Exception as e:
        print(f"원티드 API 오류 ({search_keyword}): {e}")
    return filtered_jobs

# --- 이메일 HTML 생성 헬퍼 ---
def build_email_section(title, insight, data_list, more_link, is_job=False):
    html = f"<h2>{title}</h2>"
    if insight:
        html += f"<div style='background-color:#f0f7ff; padding:12px; margin-bottom:15px; border-left:4px solid #0056b3; font-size:14px; color:#333;'><strong>💡 [AI 시사점]</strong><br>{insight}</div>"
    
    html += "<ul>"
    if not data_list: 
        html += "<li>최근 1주일 내 수집된 관련 데이터가 없습니다.</li>"
    else:
        # 평가된 스코어 기반으로 상위 5개 노출
        for item in data_list[:5]:
            if is_job: 
                html += f"<li><a href='{item['link']}' target='_blank'>[{item['company']}] {item['title']}</a></li>"
            else: 
                html += f"<li><a href='{item['link']}' target='_blank'>{item['title']}</a></li>"
    html += "</ul>"
    html += f"<div style='text-align:right; margin-top:8px;'><a href='{more_link}' target='_blank' style='font-size:13px; color:#555; text-decoration:none;'>[웹페이지에서 전체 보기]</a></div><br>"
    return html

# --- 이메일 발송 (다중 수신자 및 RFC 5321 오류 해결 적용) ---
def send_email(data, pages_url):
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    raw_receiver_email = os.environ.get("RECEIVER_EMAIL", "")
    
    if not sender_email or not sender_password or not raw_receiver_email: return

    # 수신자 분리 및 공백 제거 처리
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
    GITHUB_PAGES_URL = "[https://hansu814ryu-alt.github.io/gdc-monitoring](https://hansu814ryu-alt.github.io/gdc-monitoring)" 
    NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
    NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
    GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    print("--- 🚀 최근 1주일 한정 데이터 크롤링 시작 ---")
    
    # 1 & 2. 뉴스 원본 수집
    raw_gdc = []
    gdc_queries = ["GDC 오프쇼어링", "GDC 딜리버리", "글로벌 딜리버리 센터", "글로벌 개발 센터"]
    for q in gdc_queries:
        raw_gdc.extend(get_naver_news(NAVER_ID, NAVER_SECRET, query=q, display=15))
        
    raw_ax_news = get_naver_news(NAVER_ID, NAVER_SECRET, query="AX 전환", display=20) + get_naver_news(NAVER_ID, NAVER_SECRET, query="AI 기술 도입", display=20)
    
    # 3 & 4. 채용 공고 원본 수집
    raw_vn_jobs = get_wanted_postings("베트남", ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역'])
    raw_ax_jobs = get_wanted_postings("AX")
    
    print("--- 🧠 AI(LLM) 기반 맥락 평가 및 정렬 중 ---")
    # API 할당량 보호를 위해 최대 30개 항목까지만 LLM에 전달
    sorted_gdc = process_gdc_with_ai(raw_gdc[:30], GEMINI_KEY)
    sorted_ax_news = process_ax_news_with_ai(raw_ax_news[:30], GEMINI_KEY)
    sorted_vn_jobs = process_jobs_with_ai(raw_vn_jobs[:30], GEMINI_KEY, "VN")
    sorted_ax_jobs = process_jobs_with_ai(raw_ax_jobs[:30], GEMINI_KEY, "AX")
    
    print("--- 💡 AI 시사점 도출 중 ---")
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
    print("✅ data.json 최신화 완료.")

    send_email(result, GITHUB_PAGES_URL)
