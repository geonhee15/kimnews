# KIMNEWS

매일 오전 9시(KST), 흩어진 AI · 테크 · 개발 · IT 뉴스를 한 곳에 모아 펼치는 일간지.

🌐 **Live: [kimnews.kimkim.io](https://kimnews.kimkim.io)**

## 어떻게 동작하나요

```
GitHub Actions (cron 0 0 * * *  ≡ 09:00 KST)
        │
        ▼
scripts/aggregate.py
   ├─ RSS 14곳 (AI타임스 · TechCrunch · Verge · Wired · OpenAI · Anthropic · DeepMind · HF · HN · GitHub Blog · …)
   └─ YouTube 8채널 (Lex Fridman · Two Minute Papers · Fireship · 노마드코더 · 조코딩 · …)
        │
        ▼   ─ 중복 제거 · 키워드 필터링 · 시간 정렬
data/latest.json + data/YYYY-MM-DD.json
        │
        ▼   ─ commit & push (Pages 자동 재배포)
index.html — fetch('data/latest.json') → 카드 그리드 렌더
```

## 소스

| 카테고리 | RSS | YouTube |
|---|---|---|
| 🤖 AI | AI타임스, OpenAI Blog, Anthropic, Google DeepMind, Hugging Face, MIT Tech Review, VentureBeat AI | Lex Fridman, Two Minute Papers, Yannic Kilcher, AI Explained |
| 💻 Tech | TechCrunch, The Verge, Ars Technica, Wired, ZDNet Korea | — |
| 🛠 Dev | Hacker News, GitHub Blog | Fireship, ThePrimeagen, 노마드 코더, 조코딩 |

> X(Twitter)는 공식 API 키가 필요해 v1에서는 빠져있어요. 추후 토큰 받으면 추가 예정.

## 로컬에서 띄우기

```bash
# 1) 데이터 한 번 모으기
python3 scripts/aggregate.py

# 2) 정적 서버
python3 -m http.server 5189
# → http://localhost:5189
```

`index.html` 한 장. React/Babel CDN. 빌드 단계 없음.

## 카테고리 / 키워드 추가

- 새 소스: [`scripts/aggregate.py`](scripts/aggregate.py)의 `RSS_SOURCES` 또는 `YOUTUBE_CHANNELS` 리스트에 추가
- 일반 뉴스 매체에서 관련 기사만 걸러내는 키워드: 같은 파일의 `KEYWORDS`

## 배포

- 호스팅: GitHub Pages (정적) — `kimnews.kimkim.io`
- 매일 자동 업데이트: [`.github/workflows/daily.yml`](.github/workflows/daily.yml)
- 수동 트리거: Actions 탭 → "Daily Aggregate" → "Run workflow"
