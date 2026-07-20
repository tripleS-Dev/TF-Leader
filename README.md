# TF-Leader

Embark의 THE FINALS 리더보드 페이지에서 시즌 데이터 10,000명을 가져와 SQLite에 검증된 증분 스냅샷으로 저장하고, 유저 검색과 점수/순위 이력 그래프를 제공하는 Python 백엔드입니다.

## 설치

이 프로젝트는 [uv](https://docs.astral.sh/uv/)로 의존성과 가상환경을 관리합니다.

```powershell
git clone https://github.com/tripleS-Dev/TF-Leader.git
cd TF-Leader
uv sync --dev
```

## Python에서 사용

```python
from tf_leader import TFLeaderboard

app = TFLeaderboard("data/leaderboard.sqlite3")

# 원본 10,000명 동기화. 같은 원본 업데이트는 중복 저장하지 않습니다.
result = app.sync("s11")

# Embark 표시 이름, Steam, PSN, Xbox 이름을 부분 검색합니다.
users = app.search_user("Balise", season="s11")
player = users[0].player
print(player.rank, player.display_name, player.score, player.league_name)

# 정확한 이름으로 누적 이력을 조회합니다.
history = app.user_history(player.display_name, season="s11")

# PNG 파일 생성
app.score_graph(player.display_name, output="outputs/score.png")
app.rank_graph(player.display_name, output="outputs/rank.png")
```

한 번 수집하면 그래프에 한 점이 표시됩니다. 페이지의 `lastUpdatedAt`이 바뀐 뒤 `sync()`를 다시 호출하면 새 스냅샷이 추가되어 시간에 따른 점수와 순위가 이어집니다.

## CLI에서 사용

```powershell
uv run tf-leader sync --season s11
uv run tf-leader search "Balise" --season s11
uv run tf-leader history "Balise#2431" --season s11
uv run tf-leader graph "Balise#2431" --season s11 --kind score --output outputs\score.png
uv run tf-leader graph "Balise#2431" --season s11 --kind rank --output outputs\rank.png
```

다른 위치의 DB를 쓰려면 하위 명령 앞에 `--db`를 지정합니다.

```powershell
uv run tf-leader --db D:\data\tf.sqlite3 search "Player"
```

## 구조

- `LeaderboardClient`: 공개 페이지의 `__NEXT_DATA__`를 읽어 10,000개 행으로 변환
- `LeaderboardStore`: SQLite 증분 저장, 최신 상태 구체화, 무결성 검증, 유저 검색과 이력 복원
- `TFLeaderboard`: 앱에서 바로 호출하기 위한 고수준 facade
- `plot_history`: matplotlib 기반 점수/순위 PNG 생성

데이터 필드는 순위, 24시간 순위 변동, Embark 표시 이름, 리그, 점수, Steam/PSN/Xbox 이름, 클럽 태그와 클럽 ID를 저장합니다.

### 증분 저장과 무결성

- 매 수집 시 원본 10,000행과 순위 `1..10000`의 연속성, 유저 키 중복을 먼저 검사합니다.
- 점수·리그·플랫폼 이름·클럽처럼 실제 상태가 바뀐 유저만 `player_state_events`에 저장합니다.
- 점수가 그대로인 유저의 순위 이동은 저장하지 않습니다. 이전 순서를 유지한 뒤 점수 변경·신규 유저를 `order_events`의 공식 위치에 삽입해 순위를 복원합니다.
- 동점자 상대 순서가 예상과 다르면 LIS 기반의 최소 `order_correction`만 추가하므로 임의의 동점 정렬에도 원본 순서를 정확히 재현합니다.
- API의 `change` 필드는 값이 바뀐 경우에만 좁은 `rank_change_events` 정수 행으로 저장합니다.
- 순위권에서 빠진 유저는 tombstone으로 기록합니다.
- `current_entries`에는 검증된 최신 10,000행을 유지하므로 `/leaderboard`와 검색 API는 전체 스냅샷 때와 같은 결과를 제공합니다.
- 12개 스냅샷마다 검증된 전체 상태 체크포인트를 저장합니다. 세션 이력 조회는 세션 직전 체크포인트부터 필요한 구간만 재생합니다.
- `/api/users/history`는 마지막 점수 변경 뒤 5회 연속 점수가 같으면 세션을 닫고, 기본적으로 최신 세션만 반환합니다. `session=2`부터 이전 세션을 조회할 수 있습니다.
- 복원된 매 스냅샷의 10,000행 전체 순서와 모든 필드를 원본 및 SHA-256과 대조합니다. 완전히 일치한 경우에만 하나의 SQLite 트랜잭션으로 커밋합니다.
- 동일 시각의 서로 다른 데이터와 최신보다 오래된 데이터는 기존 상태를 훼손하지 않도록 거부합니다.

기존 DB는 첫 실행 때 자동 변환됩니다. v1은 `data/leaderboard.sqlite3-pre-delta-v1.bak`, v2는 `data/leaderboard.sqlite3-pre-reconstruction-v2.bak`에 SQLite 온라인 백업을 만든 후 복원·검증합니다. v3 DB는 원본 이벤트를 변경하지 않고 v4 체크포인트를 한 번 생성합니다. 검증이 끝난 v1/v2 구형 중복 행은 운영 DB에서 제거하고 `VACUUM`으로 압축하지만 원본 백업에는 그대로 남습니다.

## 테스트

```powershell
uv run pytest
```

## 24/7 localhost 서버

`live.py`는 시작 즉시 데이터를 수집하고, 성공 시 20분 뒤 다시 수집합니다. Embark 요청이 실패하면 기존 SQLite 데이터를 계속 제공하면서 2분 뒤 재시도합니다.

```powershell
uv run python live.py
```

기본 주소는 `http://127.0.0.1:3000`이며, OpenAPI 문서는 `http://127.0.0.1:3000/docs`에서 확인할 수 있습니다.

주요 API:

- `GET /leaderboard`: Clubweb 호환 최신 10,000명 데이터
- `GET /api/users/search?q=Balise`: 최신 유저 검색
- `GET /api/users/history?q=Balise%232431`: 최신 점수·순위 세션과 전체 세션 수
- `GET /api/users/history?q=Balise%232431&session=2`: 바로 이전 세션
- `GET /api/graphs/score.png?q=Balise%232431`: 점수 PNG
- `GET /api/graphs/rank.png?q=Balise%232431`: 순위 PNG
- `GET /health`: 수집기, DB, 다음 갱신 상태

실행 옵션:

```powershell
uv run python live.py --season s11 --port 3000 `
  --refresh-seconds 1200 --retry-seconds 120 `
  --db data\leaderboard.sqlite3 --log-file logs\live.log
```

서버는 보안을 위해 `127.0.0.1`에만 바인딩됩니다. `Ctrl+C`로 정상 종료할 수 있으며, 로그는 10MB 단위로 회전해 최대 5개 백업을 유지합니다. 스냅샷은 자동 삭제하지 않습니다.

복원 DB의 용량은 실제 점수·프로필·멤버십 변경률과 `change` 필드 변경률에 좌우됩니다. v4는 여기에 12개 스냅샷마다 10,000행 체크포인트 하나를 추가합니다. 점수가 그대로인 유저의 일반 순위 이동은 이벤트로 중복 저장하지 않으며, 조회 시 가장 가까운 체크포인트와 이후 이벤트로 정확한 순위를 복원합니다.
