# GitHub로 자동 실행하기 (PC 꺼져 있어도 동작)

영업일 **09:00 / 15:35**에 GitHub 서버가 자동으로 실행 → 텔레그램 알림 + 대시보드 갱신. 공휴일은 알아서 건너뜁니다.

## 1. 텔레그램 봇 만들기 (5분)
1. 텔레그램에서 **@BotFather** → `/newbot` → 이름 입력 → **토큰** 복사
2. 팀방에 봇 초대 → 방에서 아무 메시지 입력
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열기 → `"chat":{"id":-100...` 숫자가 **chat_id**

## 2. 저장소 만들고 파일 올리기
1. github.com → New repository → 이름 예: `valueup-alert` → **Private** → Create
2. `Add file → Upload files` → 이 폴더(`upload_to_github`) **안의 파일·폴더 전부** 끌어다 놓기 → Commit
   - `.github` 폴더가 안 보이면(맥: Finder에서 `Cmd+Shift+.`) `Add file → Create new file`에서
     파일명 `.github/workflows/valueup.yml` 입력 후 내용 붙여넣기
3. **Settings → Actions → General → Workflow permissions → Read and write permissions** → Save

## 3. 비밀값 넣기
**Settings → Secrets and variables → Actions → New repository secret** 으로 5개:

| Name | 값 |
|---|---|
| `DART_API_KEY` | DART 키 |
| `KIS_APP_KEY` | 한투 앱키 |
| `KIS_APP_SECRET` | 한투 앱시크릿 |
| `TELEGRAM_BOT_TOKEN` | 1번의 토큰 |
| `TELEGRAM_CHAT_ID` | 1번의 chat_id |

값은 함께 보낸 `SECRETS_입력값.txt` 참고 (이 파일은 GitHub에 올리지 마세요).

## 4. 대시보드 (GitHub Pages)
- **Settings → Pages → Source: GitHub Actions**
- 주소 `https://<아이디>.github.io/valueup-alert/` 를 **Settings → Secrets and variables → Actions → Variables** 탭에 `DASHBOARD_URL` 로 넣으면 알림에 링크가 붙습니다.
- ⚠ **무료 계정은 Private 저장소에서 Pages가 안 됩니다.**
  - GitHub Pro(유료) → Private 그대로 Pages 사용
  - Pages 없이 → 15:30 알림에 **현황 엑셀이 매일 첨부**되니 엑셀로 확인 (Pages 단계가 실패해도 알림은 정상)
  - Public 저장소로 바꾸면 무료지만 **커버리지 엑셀·발간 현황이 외부에 공개**됩니다

## 5. 첫 실행 테스트
**Actions 탭 → 밸류업 공시 알림 → Run workflow**
- slot 비우고 실행: 최근 30일 공시를 조용히 불러와 대시보드만 생성 (첫 실행 1~3분)
- slot에 `15:30` 넣고 실행: 지금 바로 알림 발송 테스트

## 평소 운영
- **커버리지 변경**: `coverage/2026_coverage.xlsx` 를 같은 이름으로 다시 업로드. 위원님 이름 변경도 엑셀만 고치면 반영
- **발간 체크**: 텔레그램 알림 아래 `계획발간 / 이행발간` 버튼 → 다음 실행 때 대시보드·엑셀에 반영 (한 번 더 누르면 취소)
- **임시 휴장일**: `data/krx_holidays.txt` 에 날짜 추가 (공휴일·대체공휴일·선거일·12/31은 자동)
- 실행 기록·오류는 Actions 탭에서 확인

## 참고
- GitHub 예약 실행은 보통 몇 분, 혼잡하면 수십 분 늦게 시작될 수 있습니다.
- GitHub 서버는 해외 IP라 KIND가 막힐 수 있습니다. 그러면 DART로 감지하고 알림에 `⚠ 첨부 미확인`을 붙여 보냅니다.
