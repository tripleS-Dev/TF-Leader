# `live.py` HTTP API 상세 가이드

이 문서는 `live.py`가 제공하는 localhost HTTP API의 실행 방법, 요청 형식, 응답 필드, 상태 코드, 오류 처리 및 클라이언트 사용 예를 설명합니다. 문서의 예시 데이터는 응답 구조를 보여 주기 위한 가상 데이터이므로 실제 순위, 점수, 스냅샷 ID, 시각 및 해시는 실행할 때마다 달라집니다.

현재 애플리케이션 정보는 다음과 같습니다.

| 항목 | 값 |
| --- | --- |
| API 이름 | `TF-Leader Live API` |
| API 버전 | `0.4.1` |
| 기본 주소 | `http://127.0.0.1:3000` |
| 기본 시즌 | `s11` |
| 기본 수집 주기 | 성공 후 1,200초(20분) |
| 기본 재시도 주기 | 실패 후 120초(2분) |
| 응답 데이터 형식 | JSON 또는 PNG |
| 인증 | 없음 |
| 네트워크 공개 범위 | 로컬 루프백 `127.0.0.1`만 사용 |

## 1. 빠른 시작

### 1.1 설치 및 서버 실행

프로젝트 루트에서 다음 명령을 실행합니다.

```powershell
uv sync
uv run python live.py
```

정상적으로 시작되면 기본 주소는 다음과 같습니다.

```text
http://127.0.0.1:3000
```

서버가 시작되면 백그라운드 수집기가 즉시 Embark 리더보드 동기화를 시도합니다.

- 로컬 DB에 기존 스냅샷이 있으면 첫 동기화가 끝나기 전에도 기존 데이터를 조회할 수 있습니다.
- 기존 스냅샷이 없으면 첫 동기화가 끝날 때까지 데이터 API는 주로 `503 Service Unavailable`을 반환합니다.
- 동기화에 성공하면 기본적으로 20분 뒤 다시 수집합니다.
- 동기화에 실패하면 기존 DB 데이터는 계속 제공하며 기본적으로 2분 뒤 재시도합니다.
- 종료할 때는 서버를 실행한 터미널에서 `Ctrl+C`를 누릅니다.

### 1.2 실행 옵션

```powershell
uv run python live.py `
  --season s11 `
  --port 3000 `
  --refresh-seconds 1200 `
  --retry-seconds 120 `
  --db data\leaderboard.sqlite3 `
  --log-file logs\live.log
```

| 옵션 | 기본값 | 의미 | 제약 |
| --- | --- | --- | --- |
| `--season` | `s11` | 자동 수집 및 `/leaderboard`, `/health`에서 사용할 시즌 | 공백 제거와 소문자 변환 후 `s` + 숫자 형식이어야 함 |
| `--port` | `3000` | localhost HTTP 포트 | `1`~`65535` |
| `--refresh-seconds` | `1200` | 수집 성공 후 다음 수집까지 대기할 초 | `0`보다 큰 실수 |
| `--retry-seconds` | `120` | 수집 실패 후 재시도까지 대기할 초 | `0`보다 큰 실수 |
| `--db` | `data/leaderboard.sqlite3` | SQLite DB 경로 | 상대 경로이면 실행 시 절대 경로로 해석됨 |
| `--log-file` | `logs/live.log` | 로그 파일 경로 | 부모 디렉터리는 자동 생성됨 |

로그는 콘솔과 파일에 동시에 기록됩니다. 파일 로그는 10 MiB에 도달하면 회전하며 최대 5개의 백업 파일을 보존합니다.

### 1.3 API 문서 UI

서버 실행 중 다음 자동 문서를 브라우저에서 사용할 수 있습니다.

| 경로 | 용도 |
| --- | --- |
| `GET /docs` | Swagger UI에서 요청을 직접 시험 |
| `GET /redoc` | ReDoc 형식의 읽기 전용 API 문서 |
| `GET /openapi.json` | OpenAPI 스키마 원문 |
| `GET /docs/oauth2-redirect` | Swagger UI가 자동 등록하는 OAuth 리다이렉트 보조 경로. 현재 API는 인증을 사용하지 않음 |

예:

```text
http://127.0.0.1:3000/docs
```

## 2. API 전체 목록

`live.py`가 정의하는 데이터 API는 모두 `GET`입니다.

| 메서드와 경로 | 응답 | 설명 |
| --- | --- | --- |
| `GET /leaderboard` | JSON | 서버 기본 시즌의 최신 리더보드 최대 10,000명 |
| `GET /api/users/search` | JSON | 최신 스냅샷에서 표시 이름 또는 플랫폼 이름 검색 |
| `GET /api/users/history` | JSON | 정확한 이름에 대응하는 최신/이전 세션 점수·순위 이력 |
| `GET /api/graphs/score.png` | PNG | 유저 점수 이력 그래프 |
| `GET /api/graphs/rank.png` | PNG | 유저 순위 이력 그래프 |
| `GET /health` | JSON | 수집기, 최신 DB 스냅샷 및 무결성 상태 |

루트 경로 `/`는 별도로 정의되어 있지 않으므로 요청하면 일반적인 `404 Not Found` 응답을 받습니다. 수동 동기화, 데이터 수정 또는 삭제를 수행하는 API도 제공하지 않습니다.

## 3. 공통 요청 규칙

### 3.1 URL과 쿼리 문자열 인코딩

유저 이름에는 `#`, 공백 또는 비 ASCII 문자가 들어갈 수 있으므로 쿼리 값은 URL 인코딩해야 합니다. 예를 들어 `Player#1234`의 `#`는 `%23`으로 전송해야 합니다.

```text
GET /api/users/history?q=Player%231234
```

`curl.exe -G --data-urlencode`를 사용하면 직접 인코딩할 필요가 없습니다.

```powershell
curl.exe -G "http://127.0.0.1:3000/api/users/history" `
  --data-urlencode "q=Player#1234"
```

PowerShell의 `Invoke-RestMethod`를 사용할 때는 다음과 같이 인코딩할 수 있습니다.

```powershell
$name = [uri]::EscapeDataString("Player#1234")
Invoke-RestMethod "http://127.0.0.1:3000/api/users/history?q=$name"
```

### 3.2 시즌 형식

API 쿼리의 `season`은 정규식 `^s\d+$`를 만족해야 합니다.

- 올바른 예: `s11`, `s12`
- 잘못된 예: `S11`, `season11`, `11`, 빈 문자열
- 형식이 올바르지만 DB에 해당 시즌의 스냅샷이 없으면 `503`을 반환합니다.

CLI의 `--season`과 `LiveSettings.season`은 앞뒤 공백을 제거하고 소문자로 변환하지만, HTTP 쿼리 파라미터는 소문자로 자동 변환하지 않습니다. 따라서 HTTP 요청에서는 `s11`처럼 소문자를 사용해야 합니다.

### 3.3 시각 형식

API에는 두 가지 시각 표현이 있습니다.

| 형식 | 사용 필드 | 예 | 설명 |
| --- | --- | --- | --- |
| Unix timestamp 정수 | `timestamp`, `lastCheck` | `1784247600` | UTC 기준 1970-01-01 이후 초. 소수점 이하는 버림 |
| ISO 8601 문자열 | `updatedAt`, `serverTime`, `latestDataAt` 등 | `2026-07-17T00:20:00+00:00` | UTC 오프셋을 포함한 문자열 |

JavaScript에서 Unix timestamp를 변환하는 예는 다음과 같습니다.

```javascript
const utcDate = new Date(response.timestamp * 1000);
console.log(utcDate.toISOString());
```

### 3.4 CORS

브라우저 교차 출처 요청은 다음 Origin에만 허용됩니다.

```text
http://localhost[:port]
https://localhost[:port]
http://127.0.0.1[:port]
https://127.0.0.1[:port]
```

세부 규칙은 다음과 같습니다.

- 허용 메서드: `GET`, CORS 사전 요청용 `OPTIONS`
- 허용 요청 헤더: 모든 헤더
- 자격 증명: 허용하지 않음. 즉, CORS 응답에 credential 허용을 설정하지 않음
- `file://`, LAN IP, 임의 도메인은 허용하지 않음
- 서버 자체가 `127.0.0.1`에만 바인딩되므로 다른 PC에서 직접 접속할 수 없음

허용되는 브라우저 프런트엔드의 예:

```javascript
const response = await fetch(
  "http://127.0.0.1:3000/api/users/search?q=Player"
);
const payload = await response.json();
```

### 3.5 압축과 캐시

- 응답 본문 크기가 1,000바이트 이상이고 클라이언트가 지원하면 GZip 미들웨어가 응답을 압축할 수 있습니다.
- `/leaderboard`는 다음 자동 갱신 예상 시점까지의 `Cache-Control`과 `Expires` 헤더를 설정합니다.
- 두 PNG 그래프 API는 항상 `Cache-Control: no-store`를 설정합니다.
- 검색, 이력, 상태 API에는 `live.py`가 별도의 `Cache-Control` 헤더를 추가하지 않습니다.

## 4. 공통 리더보드 필드

`/leaderboard`와 `/api/users/search`의 각 `data` 항목에는 다음 필드가 들어갑니다.

| 필드 | 형식 | null 가능 | 의미 |
| --- | --- | --- | --- |
| `rank` | 정수 | 아니요 | 해당 스냅샷의 순위. 1이 최상위 |
| `change` | 정수 | 아니요 | Embark 원본의 24시간 순위 변동 값 |
| `name` | 문자열 | 아니요 | Embark 표시 이름 |
| `steamName` | 문자열 또는 `null` | 예 | Steam 이름. 원본 값이 비어 있으면 `null` |
| `psnName` | 문자열 또는 `null` | 예 | PlayStation Network 이름. 비어 있으면 `null` |
| `xboxName` | 문자열 또는 `null` | 예 | Xbox 이름. 비어 있으면 `null` |
| `clubTag` | 문자열 또는 `null` | 예 | 클럽 태그. 비어 있으면 `null` |
| `clubId` | 문자열 또는 `null` | 예 | 클럽 ID. 비어 있으면 `null` |
| `leagueNumber` | 정수 | 아니요 | 리그의 숫자 코드 |
| `rankScore` | 정수 | 아니요 | 해당 스냅샷의 랭크 점수 |

`change`의 부호 표시는 원본 Embark 리더보드 값에 따릅니다. API는 이 값을 별도로 재해석하지 않고 정수로 전달합니다.

### 4.1 리그 코드 표

`/api/users/search`와 이력 API는 숫자 코드 외에 사람이 읽을 수 있는 리그 이름도 제공합니다.

| `leagueNumber` | `league` |
| ---: | --- |
| 0 | `Unranked` |
| 1~4 | `Bronze 4` ~ `Bronze 1` |
| 5~8 | `Silver 4` ~ `Silver 1` |
| 9~12 | `Gold 4` ~ `Gold 1` |
| 13~16 | `Platinum 4` ~ `Platinum 1` |
| 17~20 | `Diamond 4` ~ `Diamond 1` |
| 21 | `Ruby` |

정의되지 않은 숫자는 `Unknown (숫자)` 형식으로 표시됩니다. 예를 들어 코드 `22`는 `"Unknown (22)"`가 됩니다.

## 5. `GET /leaderboard`

서버 실행 옵션으로 지정한 기본 시즌의 최신 리더보드를 반환합니다. Clubweb 호환 응답을 목적으로 하며, 별도 쿼리 파라미터는 없습니다.

### 5.1 요청

```http
GET /leaderboard HTTP/1.1
Host: 127.0.0.1:3000
Accept: application/json
```

```powershell
curl.exe "http://127.0.0.1:3000/leaderboard"
```

### 5.2 정상 응답

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: public, max-age=1200
Expires: Fri, 17 Jul 2026 02:22:07 GMT
```

```json
{
  "data": [
    {
      "rank": 1,
      "change": 1,
      "name": "Player#1234",
      "steamName": "SteamPlayer",
      "psnName": "ConsolePlayer",
      "xboxName": null,
      "clubTag": "TST",
      "clubId": "club-id",
      "leagueNumber": 20,
      "rankScore": 52000
    }
  ],
  "timestamp": 1784247600,
  "lastCheck": 1784247600,
  "source": "local-live"
}
```

### 5.3 최상위 응답 필드

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `data` | 객체 배열 | 최신 스냅샷의 선수 목록. `rank` 오름차순. 최대 10,000명 |
| `timestamp` | 정수 | 최신 원본 데이터의 갱신 시각을 Unix 초로 변환한 값 |
| `lastCheck` | 정수 | 서버가 마지막으로 동기화를 시도한 시각. 서버 실행 후 아직 시도 기록이 없으면 `timestamp`와 같음 |
| `source` | 문자열 | `local-live` 또는 `local-stale-fallback` |

`source`의 의미는 다음과 같습니다.

| 값 | 의미 |
| --- | --- |
| `local-live` | 마지막 동기화 오류가 없는 로컬 DB 데이터 |
| `local-stale-fallback` | 최근 동기화가 실패하여 기존 로컬 DB 스냅샷을 대신 제공 중 |

`local-live`는 요청할 때 Embark 서버를 직접 조회했다는 뜻이 아닙니다. 모든 응답은 로컬 SQLite의 최신 저장 데이터를 사용합니다.

### 5.4 캐시 헤더 계산

`max-age`는 현재 시각부터 다음 예정 갱신 시각까지 남은 초입니다.

- 다음 갱신 시각이 있으면 그 시각을 사용합니다.
- 스케줄러가 꺼져 있거나 다음 시각이 아직 없으면 현재 시각 + `refresh_seconds`를 사용합니다.
- 계산 결과가 0 이하더라도 최소 `max-age=1`을 사용합니다.

따라서 `max-age`는 항상 정확히 `1200`인 것이 아니라 요청 시점에 따라 감소할 수 있습니다.

### 5.5 데이터가 없는 응답

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json
```

```json
{
  "detail": "리더보드 데이터가 아직 준비되지 않았습니다."
}
```

최신 전체 리더보드의 행 수나 SHA-256 내용 해시가 검증된 스냅샷과 일치하지 않으면 공통 무결성 오류 `503`이 반환될 수 있습니다. 해당 형식은 11.2절을 참고합니다.

## 6. `GET /api/users/search`

특정 시즌의 **최신 스냅샷만** 검색합니다. 과거 이력 전체를 검색하는 API가 아닙니다.

### 6.1 쿼리 파라미터

| 이름 | 필수 | 기본값 | 형식과 범위 | 의미 |
| --- | --- | --- | --- | --- |
| `q` | 예 | 없음 | 문자열 1~100자 | 검색어 |
| `season` | 아니요 | 서버 설정 시즌 | `^s\d+$` | 검색할 시즌 |
| `exact` | 아니요 | `false` | 불리언 | `false`: 부분 일치, `true`: 전체 값 일치 |
| `limit` | 아니요 | `20` | 정수 1~100 | 최대 결과 수 |

검색 대상 필드는 다음 네 가지입니다.

- Embark 표시 이름 `name`
- Steam 이름 `steamName`
- PSN 이름 `psnName`
- Xbox 이름 `xboxName`

검색은 SQLite의 `NOCASE` 비교를 사용하고 결과를 `rank` 오름차순으로 반환합니다. 영문 ASCII의 대소문자는 구분하지 않지만, 모든 비 ASCII 문자의 유니코드 대소문자 변환까지 보장하는 검색은 아닙니다. `exact=false`일 때 `%`와 `_`는 SQL 와일드카드가 아니라 입력한 문자 자체로 처리됩니다.

서버는 `q`의 앞뒤 공백을 제거한 뒤 검색합니다. 공백만 있는 `q`는 길이 검증 자체는 통과할 수 있지만 검색 결과는 빈 배열입니다.

### 6.2 부분 검색 요청과 응답

```powershell
curl.exe -G "http://127.0.0.1:3000/api/users/search" `
  --data-urlencode "q=Steam" `
  --data-urlencode "season=s11" `
  --data-urlencode "limit=20"
```

```json
{
  "season": "s11",
  "snapshotId": 2,
  "updatedAt": "2026-07-17T00:20:00+00:00",
  "count": 1,
  "data": [
    {
      "rank": 1,
      "change": 1,
      "name": "Player#1234",
      "steamName": "SteamPlayer",
      "psnName": "ConsolePlayer",
      "xboxName": null,
      "clubTag": "TST",
      "clubId": "club-id",
      "leagueNumber": 20,
      "rankScore": 52000,
      "snapshotId": 2,
      "season": "s11",
      "updatedAt": "2026-07-17T00:20:00+00:00",
      "league": "Diamond 1"
    }
  ]
}
```

### 6.3 정확 검색

```powershell
curl.exe -G "http://127.0.0.1:3000/api/users/search" `
  --data-urlencode "q=Player#1234" `
  --data-urlencode "exact=true" `
  --data-urlencode "limit=1"
```

`exact=true`는 네 검색 대상 필드 중 하나의 전체 값이 `q`와 같을 때만 일치합니다. 예를 들어 `q=Player`는 `Player#1234`와 일치하지 않습니다.

### 6.4 최상위 응답 필드

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `season` | 문자열 | 실제 조회한 시즌 |
| `snapshotId` | 정수 | 검색 대상인 최신 로컬 스냅샷의 ID |
| `updatedAt` | ISO 8601 문자열 | 해당 스냅샷의 원본 갱신 시각 |
| `count` | 정수 | 이번 응답의 `data` 항목 수. 전체 후보 수가 아니라 `limit` 적용 후의 수 |
| `data` | 객체 배열 | 검색된 선수 목록 |

각 `data` 항목에는 4절의 공통 필드와 다음 필드가 추가됩니다.

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `snapshotId` | 정수 | 이 레코드가 속한 최신 스냅샷 ID |
| `season` | 문자열 | 레코드의 시즌 |
| `updatedAt` | ISO 8601 문자열 | 원본 데이터 갱신 시각 |
| `league` | 문자열 | `leagueNumber`에 대응하는 표시 이름 |

### 6.5 검색 결과가 없는 경우

검색 결과가 없어도 오류가 아니며 `200 OK`입니다.

```json
{
  "season": "s11",
  "snapshotId": 2,
  "updatedAt": "2026-07-17T00:20:00+00:00",
  "count": 0,
  "data": []
}
```

해당 시즌의 스냅샷 자체가 없으면 빈 배열 대신 `503`과 `"리더보드 데이터가 아직 준비되지 않았습니다."`를 반환합니다.

## 7. `GET /api/users/history`

선수의 점수·순위 이력을 점수 변화 세션 단위로 반환합니다. 파라미터가 없으면 가장 최근 세션을 반환하고, `totalSessions`로 조회 가능한 전체 세션 수를 알려줍니다. 순위 변화는 세션 판정에 사용하지 않습니다.

### 7.1 세션 판정 규칙

- 이전 fetch와 비교해 점수가 한 번이라도 실제로 바뀐 경우에만 세션을 생성합니다. 변화가 전혀 없는 관측 구간은 세션이 아닙니다.
- 두 점수 변화 사이에 점수가 같은 fetch가 8회 이상 연속되면 두 변화를 서로 다른 세션으로 나눕니다.
- 8회 미만의 정체 사이에 발생한 여러 점수 변화는 같은 세션으로 묶습니다.
- 반환 범위에는 첫 점수 변화 직전의 평행 포인트 최대 2개와 마지막 점수 변화 직후의 평행 포인트 최대 2개만 문맥으로 포함합니다.

### 7.2 쿼리 파라미터

| 이름 | 필수 | 기본값 | 형식과 범위 | 의미 |
| --- | --- | --- | --- | --- |
| `q` | 예 | 없음 | 문자열 1~100자 | 정확히 찾을 표시 이름 또는 플랫폼 이름 |
| `season` | 아니요 | 서버 설정 시즌 | `^s\d+$` | 이력을 조회할 시즌 |
| `session` | 아니요 | `1` | 1 이상의 정수 | `1`은 최신 세션, `2`는 바로 이전 세션 |

`session=0` 또는 음수는 `422`입니다. 전체 세션 수보다 큰 값을 요청하면 `200 OK`, 빈 `data`, 실제 `totalSessions`를 반환합니다.

### 7.3 검색 API와 다른 일치 규칙

이력 조회는 **부분 일치를 지원하지 않습니다**. `q`는 표시 이름, Steam 이름, PSN 이름 또는 Xbox 이름 중 하나와 대소문자 구분 없이 전체가 일치해야 합니다.

권장 흐름은 다음과 같습니다.

1. `/api/users/search?q=이름일부`로 최신 후보를 찾습니다.
2. 후보의 정확한 `name` 또는 플랫폼 이름을 가져옵니다.
3. 그 값을 `/api/users/history?q=정확한이름`에 전달합니다.

이력 조회는 과거 상태 이벤트에서도 이름을 찾습니다. 여러 선수 키가 같은 이름과 연결된 경우 세션은 선수 키별로 계산한 뒤 최신순으로 선택합니다.

### 7.4 요청

최신 세션:

```powershell
curl.exe -G "http://127.0.0.1:3000/api/users/history" `
  --data-urlencode "q=Player#1234" `
  --data-urlencode "season=s11"
```

바로 이전 세션:

```powershell
curl.exe -G "http://127.0.0.1:3000/api/users/history" `
  --data-urlencode "q=Player#1234" `
  --data-urlencode "season=s11" `
  --data-urlencode "session=2"
```

### 7.5 정상 응답

```json
{
  "query": "Player#1234",
  "season": "s11",
  "session": 1,
  "totalSessions": 3,
  "count": 2,
  "data": [
    {
      "snapshotId": 21,
      "season": "s11",
      "updatedAt": "2026-07-17T00:00:00+00:00",
      "rank": 2,
      "change": 1,
      "score": 50000,
      "leagueNumber": 20,
      "league": "Diamond 1",
      "name": "Player#1234"
    },
    {
      "snapshotId": 22,
      "season": "s11",
      "updatedAt": "2026-07-17T00:20:00+00:00",
      "rank": 1,
      "change": 1,
      "score": 52000,
      "leagueNumber": 20,
      "league": "Diamond 1",
      "name": "Player#1234"
    }
  ]
}
```

### 7.6 응답 필드

최상위 필드:

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `query` | 문자열 | 요청에서 받은 `q` 원문 |
| `season` | 문자열 | 조회한 시즌 |
| `session` | 정수 | 요청한 세션 번호. `1`이 최신 |
| `totalSessions` | 정수 | 조회 가능한 전체 세션 수 |
| `count` | 정수 | 선택한 세션의 이력 포인트 수 |
| `data` | 객체 배열 | 선택한 세션 범위의 갱신 시각 오름차순 이력 |

각 이력 포인트:

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| `snapshotId` | 정수 | 이 포인트를 만든 로컬 스냅샷 ID |
| `season` | 문자열 | 시즌 |
| `updatedAt` | ISO 8601 문자열 | Embark 원본 리더보드 갱신 시각 |
| `rank` | 정수 | 당시 순위 |
| `change` | 정수 | 당시 원본의 24시간 순위 변동 값 |
| `score` | 정수 | 당시 랭크 점수 |
| `leagueNumber` | 정수 | 당시 리그 코드 |
| `league` | 문자열 | 당시 리그 이름 |
| `name` | 문자열 | 당시 표시 이름 |

### 7.7 이력이 없는 경우

스냅샷은 있지만 정확히 일치하는 선수를 찾지 못하면 `200 OK`, `totalSessions: 0`, 빈 배열을 반환합니다.

```json
{
  "query": "UnknownPlayer#0000",
  "season": "s11",
  "session": 1,
  "totalSessions": 0,
  "count": 0,
  "data": []
}
```

`/api/users/history`에는 페이지네이션이나 개수 제한 파라미터가 없으며 `session`으로 세션 단위 이력을 선택합니다.

## 8. `GET /api/graphs/score.png`

`/api/users/history`와 같은 정확 일치 규칙으로 선수를 찾고 저장된 전체 점수 이력을 PNG 그래프로 반환합니다.

### 8.1 쿼리 파라미터

| 이름 | 필수 | 기본값 | 형식과 범위 |
| --- | --- | --- | --- |
| `q` | 예 | 없음 | 정확한 이름, 1~100자 |
| `season` | 아니요 | 서버 설정 시즌 | `^s\d+$` |

### 8.2 파일 저장 예

```powershell
curl.exe -G "http://127.0.0.1:3000/api/graphs/score.png" `
  --data-urlencode "q=Player#1234" `
  --output "score-history.png"
```

```powershell
$name = [uri]::EscapeDataString("Player#1234")
Invoke-WebRequest `
  "http://127.0.0.1:3000/api/graphs/score.png?q=$name" `
  -OutFile "score-history.png"
```

### 8.3 정상 응답

```http
HTTP/1.1 200 OK
Content-Type: image/png
Cache-Control: no-store
Content-Length: <생성된 PNG 바이트 수>

<PNG 바이너리 데이터>
```

PNG 파일의 선두 8바이트는 표준 PNG 시그니처입니다.

```text
89 50 4E 47 0D 0A 1A 0A
```

그래프의 구성은 다음과 같습니다.

- 기본 이미지 크기: 1,600 × 880 픽셀(10 × 5.5인치, 160 DPI)
- 제목: `<최근 표시 이름> - Score History`
- X축: 원본 리더보드 갱신 시각(UTC)
- Y축: 랭크 점수
- 각 데이터 포인트 위에 천 단위 구분 기호를 사용한 점수 표시
- 값이 모두 같아도 그래프가 보이도록 Y축에 자동 여백 추가

### 8.4 오류 응답

시즌 스냅샷이 없으면 `503`입니다.

```json
{
  "detail": "리더보드 데이터가 아직 준비되지 않았습니다."
}
```

스냅샷은 있지만 일치하는 이력이 없으면 `404`입니다.

```json
{
  "detail": "해당 유저의 이력을 찾지 못했습니다."
}
```

## 9. `GET /api/graphs/rank.png`

순위 이력을 PNG로 반환합니다. 요청 파라미터, 이름 일치 방식, `404`와 `503` 조건 및 캐시 정책은 점수 그래프 API와 같습니다.

### 9.1 요청 및 저장

```powershell
curl.exe -G "http://127.0.0.1:3000/api/graphs/rank.png" `
  --data-urlencode "q=Player#1234" `
  --data-urlencode "season=s11" `
  --output "rank-history.png"
```

### 9.2 정상 응답

```http
HTTP/1.1 200 OK
Content-Type: image/png
Cache-Control: no-store

<PNG 바이너리 데이터>
```

점수 그래프와 다른 부분은 다음과 같습니다.

- 제목: `<최근 표시 이름> - Rank History`
- Y축: 정수 순위
- 순위 축을 반전하여 1위가 그래프 위쪽에 표시됨
- 각 포인트 위에 당시 순위 표시
- 모든 순위가 같아도 선이 보이도록 자동 여백 추가

서버는 Matplotlib 렌더링을 한 번에 하나씩 수행하도록 잠금을 사용합니다. 여러 그래프 요청이 동시에 들어오면 이미지 렌더링 구간은 순차 처리될 수 있습니다.

## 10. `GET /health`

서버 프로세스의 수집 상태와 기본 시즌 DB의 최신 스냅샷 상태를 반환합니다. 모니터링 도구는 HTTP 상태 코드와 본문의 `status`를 함께 확인하는 것이 좋습니다.

### 10.1 요청

```powershell
curl.exe "http://127.0.0.1:3000/health"
```

### 10.2 스냅샷이 있는 응답 예

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "status": "healthy",
  "season": "s11",
  "serverTime": "2026-07-17T02:20:00.123456+00:00",
  "snapshotCount": 2,
  "entryCount": 10000,
  "contentHash": "7ebcff7e4e908d836c337e1dee59195b500f3ee900e1e9b0e60eb40e5fef9f63",
  "changedEntryCount": 1007,
  "removedEntryCount": 0,
  "rankChangeEventCount": 438,
  "orderEventCount": 927,
  "orderCorrectionCount": 0,
  "integrityVerified": true,
  "latestDataAt": "2026-07-17T00:20:00+00:00",
  "lastAttemptAt": "2026-07-17T02:00:00.000000+00:00",
  "lastSuccessAt": "2026-07-17T02:00:01.250000+00:00",
  "nextRefreshAt": "2026-07-17T02:20:01.250000+00:00",
  "lastError": null
}
```

### 10.3 필드 설명

| 필드 | 형식 | null 가능 | 의미 |
| --- | --- | --- | --- |
| `status` | 문자열 | 아니요 | `starting`, `healthy`, `degraded`, `unavailable` 중 하나 |
| `season` | 문자열 | 아니요 | 서버 설정의 기본 시즌. 쿼리로 변경할 수 없음 |
| `serverTime` | ISO 8601 문자열 | 아니요 | 상태 응답을 만든 서버의 현재 UTC 시각 |
| `snapshotCount` | 정수 | 아니요 | 기본 시즌에 저장된 총 스냅샷 수 |
| `entryCount` | 정수 | 아니요 | 최신 스냅샷의 선수 수. 스냅샷이 없으면 `0` |
| `contentHash` | 문자열 또는 `null` | 예 | 최신 전체 상태의 SHA-256 해시. 스냅샷이 없으면 `null` |
| `changedEntryCount` | 정수 | 아니요 | 최신 스냅샷에서 저장된 선수 상태 변경 이벤트 수 |
| `removedEntryCount` | 정수 | 아니요 | 최신 스냅샷에서 사라진 선수 수 |
| `rankChangeEventCount` | 정수 | 아니요 | 최신 스냅샷의 24시간 순위 변동 값 변경 이벤트 수 |
| `orderEventCount` | 정수 | 아니요 | 최신 순서 복원에 저장된 순위 배치 이벤트 수 |
| `orderCorrectionCount` | 정수 | 아니요 | 순위 순서 복원 중 기록된 보정 이벤트 수 |
| `integrityVerified` | 불리언 | 아니요 | 재구성 행 수와 현재 상태 행 수가 최신 스냅샷 행 수와 모두 일치하는지 여부 |
| `latestDataAt` | ISO 8601 문자열 또는 `null` | 예 | 최신 Embark 원본 데이터 갱신 시각 |
| `lastAttemptAt` | ISO 8601 문자열 또는 `null` | 예 | 가장 최근 자동 동기화 시도 시작 시각 |
| `lastSuccessAt` | ISO 8601 문자열 또는 `null` | 예 | 가장 최근 성공한 자동 동기화 완료 시각 |
| `nextRefreshAt` | ISO 8601 문자열 또는 `null` | 예 | 다음 자동 동기화 예정 시각 |
| `lastError` | 문자열 또는 `null` | 예 | 최근 동기화 오류의 클래스 이름과 메시지. 오류가 없으면 `null` |

변경 이벤트 수 필드들은 최신 스냅샷 한 건에 대한 값이며, 모든 스냅샷의 누계가 아닙니다.

### 10.4 `status` 결정 규칙

아래 표는 코드의 판정 우선순위를 나타냅니다.

| 조건 | 본문 `status` | HTTP 상태 |
| --- | --- | --- |
| 스냅샷 없음, 마지막 오류 없음 | `starting` | `503` |
| 스냅샷 없음, 마지막 오류 있음 | `unavailable` | `503` |
| 스냅샷 있음, `integrityVerified=false` | `degraded` | `200` |
| 무결한 스냅샷 있음, 아직 동기화 시도 기록 없음 | `starting` | `200` |
| 무결한 스냅샷 있음, 최근 동기화 오류 있음 | `degraded` | `200` |
| 무결한 스냅샷 있음, 동기화를 시도했고 최근 오류 없음 | `healthy` | `200` |

중요한 차이는 다음과 같습니다.

- 스냅샷만 존재하면 상태가 `starting` 또는 `degraded`여도 HTTP 응답은 `200`입니다.
- 스냅샷이 전혀 없을 때만 `/health`의 HTTP 응답이 `503`입니다.
- `degraded`는 기존 데이터를 제공할 수 있지만 무결성 또는 최신 동기화에 문제가 있다는 뜻입니다.

### 10.5 초기 상태 응답 예

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json
```

```json
{
  "status": "starting",
  "season": "s11",
  "serverTime": "2026-07-17T02:01:49.274676+00:00",
  "snapshotCount": 0,
  "entryCount": 0,
  "contentHash": null,
  "changedEntryCount": 0,
  "removedEntryCount": 0,
  "rankChangeEventCount": 0,
  "orderEventCount": 0,
  "orderCorrectionCount": 0,
  "integrityVerified": false,
  "latestDataAt": null,
  "lastAttemptAt": null,
  "lastSuccessAt": null,
  "nextRefreshAt": null,
  "lastError": null
}
```

## 11. 오류 응답

### 11.1 상태 코드 요약

| 상태 코드 | 발생 조건 | 일반적인 응답 |
| ---: | --- | --- |
| `200` | 정상 JSON/PNG, 검색 또는 이력 결과가 0건, 스냅샷이 있는 `/health` | 엔드포인트별 응답 |
| `404` | 그래프 API에서 해당 유저 이력이 없음, 정의되지 않은 경로 | `detail`이 있는 JSON |
| `422` | 필수 쿼리 누락, 길이·정규식·숫자 범위·불리언 파싱 실패 | FastAPI 검증 오류 배열 |
| `503` | 요청 시즌 스냅샷 없음, `/health`에 스냅샷 없음, 데이터 무결성 오류 | 조건별 JSON |
| `500` | 처리되지 않은 예상 밖의 서버 오류 | 서버 오류 응답, 상세 내용은 로그 확인 |

### 11.2 데이터 무결성 오류

DB에서 복원한 데이터가 검증된 스냅샷과 일치하지 않아 `DataIntegrityError`가 발생하면 다음 형식입니다.

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json
```

```json
{
  "detail": "리더보드 무결성 검증에 실패했습니다.",
  "error": "최신 리더보드 내용이 검증된 스냅샷 해시와 일치하지 않습니다."
}
```

`error`에는 실제 검증 실패 원인이 들어가므로 문구가 달라질 수 있습니다. 이 오류가 나오면 API 재시도만 반복하기보다 `logs/live.log`와 SQLite 파일 상태를 확인해야 합니다.

### 11.3 쿼리 검증 오류 예

필수 `q`를 누락한 요청:

```text
GET /api/users/search
```

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["query", "q"],
      "msg": "Field required",
      "input": null
    }
  ]
}
```

잘못된 대문자 시즌 `S11`:

```text
GET /api/users/search?q=Player&season=S11
```

```json
{
  "detail": [
    {
      "type": "string_pattern_mismatch",
      "loc": ["query", "season"],
      "msg": "String should match pattern '^s\\d+$'",
      "input": "S11",
      "ctx": {
        "pattern": "^s\\d+$"
      }
    }
  ]
}
```

범위를 벗어난 `limit=0`:

```text
GET /api/users/search?q=Player&limit=0
```

```json
{
  "detail": [
    {
      "type": "greater_than_equal",
      "loc": ["query", "limit"],
      "msg": "Input should be greater than or equal to 1",
      "input": "0",
      "ctx": {
        "ge": 1
      }
    }
  ]
}
```

검증 오류의 영문 메시지와 세부 키는 설치된 FastAPI/Pydantic 버전에 따라 달라질 수 있으므로 클라이언트 로직은 `detail[*].loc`와 HTTP 상태를 중심으로 처리하는 것이 안전합니다.

## 12. 클라이언트별 사용 예

### 12.1 Python `httpx`

프로젝트 의존성에 포함된 `httpx`를 사용하는 예입니다.

```python
from pathlib import Path

import httpx


BASE_URL = "http://127.0.0.1:3000"

with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
    search_response = client.get(
        "/api/users/search",
        params={
            "q": "Player",
            "season": "s11",
            "exact": False,
            "limit": 10,
        },
    )
    search_response.raise_for_status()
    search_payload = search_response.json()

    for player in search_payload["data"]:
        print(player["rank"], player["name"], player["rankScore"])

    if search_payload["data"]:
        exact_name = search_payload["data"][0]["name"]
        history_response = client.get(
            "/api/users/history",
            params={"q": exact_name, "season": "s11"},
        )
        history_response.raise_for_status()
        print(history_response.json())

        graph_response = client.get(
            "/api/graphs/score.png",
            params={"q": exact_name, "season": "s11"},
        )
        graph_response.raise_for_status()
        Path("score-history.png").write_bytes(graph_response.content)
```

HTTP 오류 본문까지 확인하려면 다음처럼 처리할 수 있습니다.

```python
try:
    response = httpx.get(
        "http://127.0.0.1:3000/api/users/history",
        params={"q": "Player#1234"},
        timeout=30.0,
    )
    response.raise_for_status()
except httpx.HTTPStatusError as exc:
    print(exc.response.status_code)
    print(exc.response.json())
```

### 12.2 브라우저 JavaScript

```javascript
const baseUrl = "http://127.0.0.1:3000";

async function searchPlayers(query) {
  const url = new URL("/api/users/search", baseUrl);
  url.searchParams.set("q", query);
  url.searchParams.set("season", "s11");
  url.searchParams.set("limit", "20");

  const response = await fetch(url);
  const payload = await response.json();

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${JSON.stringify(payload)}`);
  }

  return payload.data;
}

searchPlayers("Player")
  .then((players) => console.table(players))
  .catch(console.error);
```

그래프를 `<img>`에 표시하는 예입니다.

```javascript
const name = "Player#1234";
const graphUrl = new URL(
  "/api/graphs/rank.png",
  "http://127.0.0.1:3000"
);
graphUrl.searchParams.set("q", name);
graphUrl.searchParams.set("season", "s11");

document.querySelector("#rank-graph").src = graphUrl.toString();
```

```html
<img id="rank-graph" alt="Rank history" />
```

JavaScript 페이지가 다른 포트에서 실행되어도 Origin이 `localhost` 또는 `127.0.0.1`이면 CORS가 허용됩니다. 예를 들어 Vite 개발 서버 `http://localhost:5173`은 허용됩니다.

### 12.3 PowerShell 상태 확인

```powershell
$health = Invoke-RestMethod "http://127.0.0.1:3000/health"

if ($health.status -eq "healthy") {
    Write-Host "정상: 최신 데이터 시각 $($health.latestDataAt)"
} elseif ($health.status -eq "degraded") {
    Write-Warning "저하 상태: $($health.lastError)"
} else {
    Write-Warning "현재 상태: $($health.status)"
}
```

`Invoke-RestMethod`는 `503` 응답에서 예외를 발생시키므로 초기 상태까지 자동 처리하는 모니터링 스크립트에서는 예외의 HTTP 응답 본문도 함께 읽어야 합니다.

## 13. 운영 동작과 주의점

### 13.1 자동 수집 상태 흐름

```text
서버 시작
  -> 즉시 동기화 시도
     -> 성공: lastSuccessAt 갱신, lastError 제거, refresh_seconds 뒤 재시도
     -> 실패: lastError 기록, 기존 DB 유지, retry_seconds 뒤 재시도
```

중복 동기화가 이미 실행 중이면 새 동기화 시도는 건너뛰고 경고 로그를 남깁니다. HTTP API에는 동기화를 강제로 실행하는 엔드포인트가 없습니다.

### 13.2 데이터 신선도 확인

데이터가 최신인지 판단할 때는 다음 값을 함께 사용합니다.

- `/leaderboard.timestamp`: 실제 원본 데이터의 갱신 시각
- `/leaderboard.lastCheck`: 마지막 동기화 시도 시각
- `/leaderboard.source`: 최근 수집 실패로 기존 데이터를 제공 중인지 여부
- `/health.latestDataAt`: 최신 원본 데이터 시각의 ISO 8601 표현
- `/health.lastSuccessAt`: 로컬 수집기가 마지막으로 성공한 시각
- `/health.lastError`: 최근 수집 오류

`lastSuccessAt`과 `latestDataAt`은 서로 다른 개념입니다. 전자는 로컬 수집 완료 시각이고 후자는 Embark가 제공한 원본 리더보드 갱신 시각입니다.

### 13.3 보안 범위

- 인증과 권한 확인이 없으므로 서버는 의도적으로 `127.0.0.1`에만 바인딩됩니다.
- 외부 공개가 필요하더라도 단순히 바인딩 주소만 변경하기보다 인증, 접근 제어, TLS, 요청 제한 및 CORS 정책을 먼저 설계해야 합니다.
- 현재 API는 조회 전용이며 브라우저 CORS 허용 메서드도 `GET`과 `OPTIONS`뿐입니다.

### 13.4 일반적인 문제 해결

| 증상 | 확인할 항목 |
| --- | --- |
| 모든 데이터 API가 `503` | `/health`, 네트워크 연결, 첫 수집 완료 여부, `logs/live.log` |
| `/health`가 `unavailable` | `lastError`, Embark 접근 가능 여부, 프록시·방화벽·DNS |
| `/health`가 `degraded` | `integrityVerified`, `lastError`, SQLite 파일 무결성 |
| 검색은 되지만 이력이 0건 | 이력 API는 부분 검색이 아님. 검색 응답의 정확한 이름 사용 |
| 이름에 `#` 뒤 숫자가 사라짐 | URL에서 `#`를 `%23`으로 인코딩하거나 `--data-urlencode` 사용 |
| 브라우저에서 CORS 오류 | 페이지 Origin이 정확히 localhost/127.0.0.1인지 확인 |
| 다른 PC에서 연결되지 않음 | 정상 동작. 서버가 `127.0.0.1`에만 바인딩됨 |
| PNG 대신 JSON 파일이 저장됨 | 저장한 파일 내용을 확인. `404`, `422`, `503` 오류 JSON일 수 있음 |

## 14. Python 코드에서 앱 구성하기

CLI 대신 테스트, 임베딩 또는 별도 Uvicorn 설정에서 앱 팩토리를 직접 사용할 수 있습니다.

```python
from pathlib import Path

import uvicorn

from live import LiveSettings, configure_logging, create_app


settings = LiveSettings(
    db_path=Path("data/custom.sqlite3"),
    season="s11",
    port=3100,
    refresh_seconds=600,
    retry_seconds=60,
    log_path=Path("logs/custom-live.log"),
    scheduler_enabled=True,
)

app = create_app(settings)
configure_logging(settings.log_path)

uvicorn.run(
    app,
    host="127.0.0.1",
    port=settings.port,
    log_config=None,
    access_log=False,
)
```

`LiveSettings.log_path`는 경로 설정일 뿐이며 그 자체로 로깅 핸들러를 등록하지 않습니다. CLI의 `main()`은 `configure_logging(settings.log_path)`를 자동 호출하지만, 직접 앱을 실행하는 코드는 위 예처럼 명시적으로 호출해야 같은 회전 로그 구성을 사용합니다.

### 14.1 `LiveSettings`

```python
LiveSettings(
    db_path=PROJECT_ROOT / "data" / "leaderboard.sqlite3",
    season="s11",
    port=3000,
    refresh_seconds=1200,
    retry_seconds=120,
    log_path=PROJECT_ROOT / "logs" / "live.log",
    scheduler_enabled=True,
)
```

생성 시 다음 정규화를 수행합니다.

- `season`: 앞뒤 공백 제거 후 소문자로 변환
- `db_path`, `log_path`: `~` 확장 후 절대 경로로 변환
- 잘못된 시즌, 포트 또는 0 이하 주기: `ValueError`

`scheduler_enabled=False`는 주로 테스트나 외부 스케줄러가 동기화를 담당할 때 사용합니다. CLI에는 이 값을 끄는 옵션이 없으며 Python에서만 설정할 수 있습니다.

### 14.2 `create_app`

```python
def create_app(
    settings: LiveSettings | None = None,
    leaderboard: TFLeaderboard | None = None,
) -> FastAPI: ...
```

- `settings`를 생략하면 기본 `LiveSettings()`를 사용합니다.
- `leaderboard`를 생략하면 `settings.db_path`를 사용하는 `TFLeaderboard`를 생성합니다.
- 테스트에서는 준비된 `TFLeaderboard` 또는 같은 인터페이스의 대역 객체를 주입할 수 있습니다.
- 반환된 앱의 `app.state.runtime`에서 실행 상태를 읽을 수 있습니다.

스케줄러를 끈 테스트 예:

```python
from pathlib import Path

from fastapi.testclient import TestClient

from live import LiveSettings, create_app
from tf_leader import TFLeaderboard


service = TFLeaderboard("data/test.sqlite3")
settings = LiveSettings(
    db_path=service.store.db_path,
    log_path=Path("logs/test-live.log"),
    scheduler_enabled=False,
)
app = create_app(settings, service)

with TestClient(app) as client:
    response = client.get("/health")
    print(response.status_code, response.json())
```

### 14.3 `LiveRuntime`의 프로그램용 메서드

`create_app` 내부에서 생성하는 `LiveRuntime`에는 다음 비동기 메서드가 있습니다.

| 메서드 | 반환값 | 동작 |
| --- | --- | --- |
| `await runtime.start()` | `None` | 자동 수집 태스크 시작. 이미 실행 중이면 아무 작업도 하지 않음 |
| `await runtime.stop()` | `None` | 중지 이벤트를 설정하고 현재 태스크가 끝날 때까지 대기 |
| `await runtime.refresh_once()` | `bool` | 한 번 동기화. 성공하면 `True`, 실패 또는 중복 실행이면 `False` |

한 번만 수동 동기화하는 프로그램 예:

```python
import asyncio

from live import LiveSettings, LiveRuntime
from tf_leader import TFLeaderboard


async def refresh() -> None:
    settings = LiveSettings(scheduler_enabled=False)
    service = TFLeaderboard(settings.db_path)
    runtime = LiveRuntime(settings, service)

    succeeded = await runtime.refresh_once()
    print("success:", succeeded)
    print("last attempt:", runtime.state.last_attempt_at)
    print("last success:", runtime.state.last_success_at)
    print("last error:", runtime.state.last_error)


asyncio.run(refresh())
```

`refresh_once()`는 내부 예외를 호출자에게 다시 던지는 대신 `lastError`에 `예외클래스: 메시지` 형식으로 저장하고 `False`를 반환합니다. 데이터 무결성 검증 실패를 포함한 모든 일반 예외가 이 방식으로 기록됩니다.

### 14.4 내부 전용 도우미

이름이 밑줄로 시작하는 `_clubweb_player`, `_api_player`, `_history_point`, `_set_cache_headers`, `_iso`, `_utcnow`, `_positive_float`는 HTTP 응답 직렬화와 CLI 검증을 위한 내부 구현입니다. 안정적인 외부 Python API로 간주하지 말고 HTTP 엔드포인트, `LiveSettings`, `create_app`, 필요 시 `LiveRuntime`을 사용해야 합니다.

## 15. 구현 기준 한계

현재 `live.py` API에는 다음 기능이 없습니다.

- `/leaderboard`의 시즌 선택, 페이지네이션 또는 반환 개수 지정
- 이력 API의 임의 기간 필터와 페이지네이션
- 검색 결과의 전체 후보 수(`count`는 반환 배열의 길이만 의미)
- JSON 그래프 데이터 외의 SVG/JPEG 출력
- 수동 갱신 HTTP 엔드포인트
- 인증, API 키, 사용자별 권한 및 요청 속도 제한
- 외부 네트워크 인터페이스 바인딩

이 기능이 필요하면 기존 응답 호환성을 유지하면서 별도 엔드포인트나 쿼리 파라미터를 추가하는 것이 안전합니다.
