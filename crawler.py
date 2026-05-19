import os
import json
import requests
from bs4 import BeautifulSoup

# 1. 네이버 뉴스 가져오기 및 필터링
def get_naver_news(client_id, client_secret):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }
    params = {"query": "GDC", "display": 50, "sort": "sim"}
    response = requests.get(url, headers=headers, params=params)
    
    filtered_news = []
    if response.status_code == 200:
        news_data = response.json().get('items', [])
        exclude_keywords = ['game', '게임', '게임개발', 'game development']
        
        for item in news_data:
            title_lower = item['title'].lower()
            desc_lower = item['description'].lower()
            
            # 제외 키워드가 포함되어 있지 않은 경우만 저장
            if not any(ex in title_lower or ex in desc_lower for ex in exclude_keywords):
                filtered_news.append({
                    "title": item['title'].replace("<b>", "").replace("</b>", ""),
                    "link": item['link'],
                    "pubDate": item['pubDate']
                })
    return filtered_news

# 2. 잡코리아 채용공고 가져오기 및 필터링
def get_jobkorea_postings():
    # '베트남' 키워드로 기본 검색 (IT 분야 및 상세 필터링은 코드에서 수행)
    url = "https://www.jobkorea.co.kr/Search/?stext=베트남"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers)
    
    filtered_jobs = []
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        job_lists = soup.select('.post') # 잡코리아 공고 리스트 아이템 선택자
        
        # 필수 포함 키워드 세트
        it_keywords = ['it', '개발', '소프트웨어', 'software', 'bse', '브릿지', 'bridge', '통역', '번역']
        
        for job in job_lists:
            try:
                title_elem = job.select_one('.title')
                company_elem = job.select_one('.name')
                link_elem = job.select_one('.title a')
                
                if title_elem and link_elem:
                    title = title_elem.get_text(strip=True).lower()
                    company = company_elem.get_text(strip=True) if company_elem else "기업명 미상"
                    link = "https://www.jobkorea.co.kr" + link_elem['href']
                    
                    # 필수 조건 충족 여부 확인 (IT/BSE/통번역 관련 단어가 제목에 포함된 경우)
                    if any(kw in title for kw in it_keywords):
                        filtered_jobs.append({
                            "title": title_elem.get_text(strip=True),
                            "company": company,
                            "link": link
                        })
            except Exception as e:
                continue
    return filtered_jobs

if __name__ == "__main__":
    # GitHub Secrets에서 API 키를 가져옵니다.
    NAVER_ID = os.environ.get("3lSojPilqFSwIV_4Mbf9", "")
    NAVER_SECRET = os.environ.get("hRxch7MUUe", "")
    
    news = get_naver_news(NAVER_ID, NAVER_SECRET)
    jobs = get_jobkorea_postings()
    
    # 결과를 하나의 json 파일로 저장
    result = {
        "news": news,
        "jobs": jobs
    }
    
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=4)
    print("데이터 수집 완료 및 data.json 저장 성공!")