# Sentient Sands — 개조판 (RAG Custom Edition)

> Kenshi의 모든 NPC를 LLM으로 살아 움직이게 하는 AI 대화 모드 — **기억 3계층 · 팩션 RAG · 프롬프트 최적화 · 한국어 현지화 · SQLite 하이브리드 스토리지 · 벡터 기억 회상** 개조판

[![RE_Kenshi](https://img.shields.io/badge/Requires-RE__Kenshi-blue)](https://github.com/BFrizzleFoShizzle/RE_Kenshi/releases)
[![KenshiLib](https://img.shields.io/badge/Requires-KenshiLib-blue)](https://github.com/KenshiReclaimer/KenshiLib/releases)
[![Python](https://img.shields.io/badge/Python-3.10%20embedded-green)](#설치)

이 저장소는 **Antigravity의 원작 Sentient Sands** (Steam / itch.io / Player2 배포)를 기반으로 한 개조판입니다.
원작의 C++ 플러그인(`SentientSands.dll`)과 게임 연동 구조는 그대로 유지하면서, Python 서버를
대폭 확장했습니다. 원작자 크레딧은 [크레딧](#크레딧--라이선스) 참조.

---

## 소개

Kenshi의 대화 시스템을 통째로 LLM에 연결합니다. 게임 내 **누구에게나 직접 타이핑으로 말을 걸 수
있고**, NPC는 자신의 종족·팩션·부상 상태·주변 상황·과거 대화를 근거로 실시간 응답합니다.

**원작 핵심 기능**

- **자유 대화** — 모든 NPC와 어떤 주제로든 대화. LLM이 대화 흐름에 따라 실제 게임 행동(영입·공격·거래·세력 관계 변화·석방 등)을 실행
- **음성 모드 3종** — Talk(주변이 엿들음) / Whisper(1:1 비밀 대화) / Yell(군중 연설, 여러 NPC가 동시에 응답)
- **영속 기억** — 처음 말을 건 순간 고유 이름·배경·성격이 생성되어 캠페인 폴더에 영구 저장. 며칠 뒤 다른 마을에서 만나도 기억함
- **자동 개명** — Dust Bandit 같은 제네릭 NPC에게 5,000+ 이름 풀에서 고유 이름 부여 (예: *Takao the Bold*)
- **앰비언트 대화** — 플레이어가 없어도 NPC끼리 허기·날씨·전투 등에 반응하며 잡담
- **월드 루머** — 전투·정복·살인 같은 사건이 주기적으로 루머로 합성되어 NPC들 사이에 퍼짐
- **멀티 캠페인** — 캠페인별로 NPC 기억·루머가 완전히 분리, F8 허브에서 전환

**개조판 추가 기능** (상세는 [개조판 추가 기능](#개조판-추가-기능) 참조)

- **기억 3계층 강화** — 단기(20줄) + 중기 Digest(자동 요약) + 아카이브 압축 + 장기 Durable Memory(NPC 자율 기록)
- **팩션 RAG** — 대화 내용에서 팩션을 퍼지+시맨틱 매칭으로 감지해 해당 로어만 주입. 바닐라 50개 + UWE 23개 = **총 73개 팩션** 수록. UWE 팩션은 `[UWE]` 레이블로 출처 구분. 커스텀 모드 팩션도 JSON 드롭인으로 추가 가능
- **월드 로어 청크 RAG** — world_lore.txt를 주제별 청크로 분할, 대화 관련 청크만 선택 주입 (~50% 토큰 절감). 기본 **8개 청크** (바닐라 5 + UWE 3)
- **SQLite 하이브리드 스토리지** — 프로파일은 JSON(사용자 직접 편집 가능) / 대화 이력·기억은 SQLite DB로 분리 관리
- **벡터 기반 기억 회상** — sqlite-vec KNN으로 키워드 없이 의미 기반으로 장기 기억 회상
- **프롬프트 최적화** — 첫 대화 프롬프트 약 3,300~3,600토큰 (한글 기준 실제 API ~5,000토큰 이내)
- **한국어 현지화** — `Language=Korean` 설정 시 NPC 응답·프로필·루머·요약 전부 한국어 + F8 UI 번역
- **원본 버그 4건 수정** — BOM JSON 거부, 컨텍스트 덮어쓰기, 액션 태그 인자 중복, 이벤트 중복 주입

---

## 설치

### 요구 사항

1. **[RE_Kenshi](https://github.com/BFrizzleFoShizzle/RE_Kenshi/releases)** — C++ 코드 주입용 스크립트 익스텐더
2. **[KenshiLib](https://github.com/KenshiReclaimer/KenshiLib/releases)** — C++ 훅이 사용하는 라이브러리
3. OpenAI 호환 LLM API (OpenRouter, NanoGPT, 로컬 Ollama/LM Studio, Player2 등)
4. 인터넷 연결 — 최초 1회 Python 런타임 + 패키지 + 임베딩 모델 자동 다운로드 (~700MB)

### 절차

1. `RE_Kenshi`를 Kenshi 루트 폴더(`kenshi_x64.exe`가 있는 곳)에 압축 해제
2. **이 모드 폴더 전체를 `Kenshi/mods/SentientSands/` 로 복사**
   - ⚠️ 폴더 이름은 반드시 `SentientSands` 여야 합니다. DLL이
     `mods/SentientSands/server/python/python.exe` 경로를 직접 참조합니다.
3. **Python 환경 설치 — 최초 1회만 실행**

   ```
   mods/SentientSands/server/setup_embedded_python.py
   ```

   시스템 Python(3.x)으로 이 스크립트를 실행하면:
   - 임베디드 Python 3.10 런타임을 `server/python/`에 자동 설치
   - flask · requests · model2vec · numpy · **sqlite-vec** · rapidfuzz 등 패키지 설치
   - 임베딩 모델(`potion-multilingual-128M`, ~512MB)을 HuggingFace에서 자동 다운로드

   모델이 이미 있으면 다운로드를 건너뜁니다. 이후 업데이트 시 재실행해도 안전합니다.

   > SSL 인증서 문제가 있는 환경(사내망 등)에서도 자동으로 우회하여 설치됩니다.

4. 🚨 **반드시 `RE_Kenshi.exe`로 게임을 실행**하세요. 일반 실행 파일이나 Steam 직접 실행으로는
   모드가 로드되지 않고 AI 서버도 뜨지 않습니다.
5. Kenshi 모드 런처에서 **SentientSands**와 **KenshiLib**를 체크

게임이 시작되면 DLL이 백그라운드 Flask 서버(`127.0.0.1:5000`)를 자동 기동하고, 게임 종료 시 함께 종료됩니다.

---

## 빠른 시작

1. **API 키 입력** — `server/config/providers.json`에서 사용할 제공자의 `api_key`를 채웁니다.

   ```json
   {
       "openrouter": {
           "api_key": "sk-or-여기에-키-입력",
           "base_url": "https://openrouter.ai/api/v1"
       },
       "ollama": {
           "api_key": "ollama",
           "base_url": "http://localhost:11434/v1"
       }
   }
   ```

2. **모델 등록** — `server/config/models.json`에 UI 표시명 → 제공자/모델 ID 매핑을 추가합니다.
   `provider` 값은 providers.json의 최상위 키와 정확히 일치해야 합니다.

   ```json
   {
       "gemini-2.5-flash": { "provider": "openrouter", "model": "google/gemini-2.5-flash" },
       "ollama-llama3":    { "provider": "ollama",     "model": "llama3" }
   }
   ```

3. **모델 선택** — 게임 내 **F8 → AI Settings**의 *LLM Model* 드롭다운에서 선택 후 Save.
   또는 게임 실행 전 `SentientSands_Config.ini`의 `currentmodel = 모델표시명`을 직접 수정해도 됩니다.

4. **연결 테스트** — AI Settings 창의 **Test** 버튼으로 LLM 응답을 확인합니다.

5. (선택) 한국어 플레이: AI Settings의 *Language*를 **Korean**으로 (INI `language = Korean`).

---

## 게임 내 사용법

### 대화 시작

- **`\` (백슬래시) 키로 채팅 창**을 열고, **F8 키로 AI 허브 패널**을 엽니다 (시작 시 환영 창에도 안내).
- NPC를 선택한 상태에서 채팅 창에 입력하고 Send. 채팅 창에서 음성 모드(Talk/Whisper/Yell)를 선택할 수 있고, **Trigger Radiant** 버튼으로 주변 NPC끼리의 앰비언트 대화를 즉시 발동시킬 수 있습니다.

### 음성 모드

| 모드 | 범위 (기본) | 동작 |
|---|---|---|
| **Talk** | 100 | 일반 대화. 범위 내 다른 NPC가 엿듣고 기억을 갱신 |
| **Whisper** | 대상 1인 | 완전한 1:1 비밀 대화. 아무도 엿듣지 못함 |
| **Yell** | 200 | 군중 연설. 여러 NPC가 각자 한 줄씩 동시에 응답 |

### F8 AI 허브

| 메뉴 | 기능 |
|---|---|
| **AI Settings** | LLM 제공자/모델 선택, Talk/Yell/Radiant 범위, 이벤트 타이머, 언어, 연결 Test, 서버 Restart |
| **Profile Editor** | 내 캐릭터 배경(Bio)과 플레이어 세력 설명을 게임 안에서 직접 편집 |
| **Dialogue Library** | 캠페인의 모든 NPC 대화 기록 열람 |
| **World Event Log** | 합성된 월드 루머 실시간 피드 + **Generate World Event**(루머 즉시 합성) |
| **Campaign Manager** | 캠페인 생성/전환 (기억·루머 분리 관리) |

### 채팅 명령어

채팅 입력창에 `/`로 시작하는 명령을 칠 수 있습니다. 두 부류입니다.

**① NPC 개명 명령** (DLL 처리, 일반 플레이용)

| 명령어 | 기능 | 예시 |
|---|---|---|
| `/name 새이름` | 대화 중인 NPC의 이름을 직접 변경 | `/name Kaelen` |

**② 디버그/테스트 명령** (서버 처리 — LLM을 거치지 않고 액션을 강제 실행. 테스트 용도)

| 명령어 | 기능 |
|---|---|
| `/help`, `/commands` | 명령어 목록 표시 |
| `/join` / `/leave` | 대상 NPC를 즉시 영입 / 이탈 |
| `/attack` | 대상 NPC가 공격 개시 |
| `/follow` / `/idle` / `/patrol` | 따라오기 / 대기 / 마을 순찰 |
| `/release` | 감금·운반 상태의 플레이어 석방 |
| `/give_cats [수량]` / `/take_cats [수량]` | NPC가 돈을 줌 / 가져감 |
| `/take` / `/take_item [아이템]` / `/drop [아이템]` | 인벤토리 첫 아이템 가져가기 / 지정 아이템 가져가기 / 버리기 |
| `/spawn [템플릿 \| 이름 \| 설명]` | 커스텀 아이템(쪽지 등) 생성 |
| `/relations [팩션] [수치]` | 팩션 관계 변경 (예: `/relations United Cities 10`) |
| `/notify [메시지]` | 시스템 메시지 표시 |
| `/task [작업명]` | 임의 TASK 태그 실행 |

### 대화로 유도 가능한 행동 (액션 태그)

아래는 명령어가 아니라, **대화 흐름에 따라 LLM이 스스로 실행하는 행동**입니다. 설득·협상·위협으로 유도할 수 있습니다.

| 행동 | 태그 | 유도 방법 |
|---|---|---|
| 영입 | `JOIN_PARTY` | 동행 설득. **단, 주요 팩션(성국·연합도시·셰크 왕국 등) 소속원은 높은 평판(75+), 생명의 은인 관계, 또는 극히 설득력 있는 서사 없이는 거절**하도록 설계됨. 커스텀 팩션도 `is_major: true`면 동일하게 저항 |
| 이탈/해산 | `LEAVE` | 동료에게 떠나라고 하거나 NPC가 스스로 결별 |
| 공격 | `ATTACK` | 도발·위협 — 대화가 험악해지면 실제 전투 발생 |
| 따라오기 / 대기 / 순찰 | `FOLLOW_PLAYER` / `IDLE` / `PATROL_TOWN` | 길 안내 요청, 자리 지키라는 부탁 등 |
| 아이템 주기/가져가기/버리기 | `GIVE_ITEM` / `TAKE_ITEM` / `DROP_ITEM` | 구두 거래·선물·강탈. 가격 합의("좋다, 200캣에 사지") 후에만 실행되는 2단계 거래 규칙 적용 |
| 돈 지불/수금 | `GIVE_CATS` / `TAKE_CATS` | 물건 판매 대금, 보상, 뇌물, 세금, 강도 |
| 아이템 생성 | `SPAWN_ITEM` | 쪽지·신문 등 고유 아이템을 만들어 건네줌 |
| 세력 관계 변화 | `FACTION_RELATIONS` | 배신·헌신 등 강한 서사적 사건 시 해당 팩션과의 평판 변동 |
| 석방 | `RELEASE_PLAYER` | 감옥·우리에 갇혔을 때 간수를 설득해 풀려나기 |
| 알림 | `NOTIFY` | 시스템 메시지 (NPC의 비언어 반응 묘사 등) |
| **장기 기억 기록** | `RECORD_MEMORY` *(개조판)* | 게임 행동이 아닌 **NPC 내면 기록** — 중요한 사건을 NPC가 스스로 장기 기억에 남김. 플레이어에게는 보이지 않음 |

---

## 커스터마이징 가이드

모든 프롬프트 재료는 평문 텍스트/JSON이며, 코드 수정 없이 편집할 수 있습니다.

### 1. 내 캐릭터·세력 설정

| 파일 | 주입 위치 | 내용 |
|---|---|---|
| `server/templates/character_bio.txt` | 시스템 프롬프트의 플레이어 소개 | 플레이어 캐릭터의 배경·성격 (1인칭 서술 권장) |
| `server/templates/player_faction_description.txt` | 〃 | 플레이어 세력에 대한 설명 |

새 캠페인이 만들어질 때 이 두 파일이 `server/campaigns/{캠페인}/`으로 복사되며, **캠페인 폴더 쪽이
우선 적용**됩니다(캠페인별로 다른 주인공 설정 가능). 게임 내 **F8 → Profile Editor**에서도 편집 가능.

> 작성 요령: NPC가 "당신이 누구인지" 판단하는 근거가 됩니다. 짧고 구체적으로 — 출신, 목표, 평판,
> 가치관 위주. 기본 동봉 예시:
> *"I am a wandering drifter, a survivor of the Great Desert and the Border Zone. … My goal is
> simple: survive another day, find enough cats to stay fed…"*

### 2. 커스텀 팩션 로어 추가 (팩션 RAG)

기본 DB에 **바닐라 50개 + UWE 23개 = 총 73개 팩션**이 수록되어 있습니다.
- `server/config/faction_lore.json` — 바닐라 메이저/마이너 33개 (원작 수록)
- `server/config/faction_lore.d/vanilla_missing_factions.json` — 바닐라 미수록 17개 (세력정보.txt 기반)
- `server/config/faction_lore.d/uwe_factions.json` — UWE 모드 신규 23개

추가 **모드 팩션**은 `server/config/faction_lore.d/` 폴더에 `*.json` 파일을 떨어뜨리면
서버 시작 시(또는 실행 중 `http://127.0.0.1:5000/lore/reload` 호출 시) 자동 병합됩니다.
`source_mod` 필드를 모드 이름으로 설정하면 프롬프트에 `[모드명]` 레이블이 자동 출력됩니다.

```jsonc
{
  "factions": [
    {
      "id": "uwe_velakoz",
      "name": "Velakoz",
      "aliases": ["Velakoz Raiders", "벨라코즈"],
      "keywords": ["Kraz", "Shek bandits", "raid"],
      "source_mod": "UWE",
      "is_major": false,
      "leader": "Kraz the Mad",
      "summary": "도시 외곽을 약탈하는 셰크 우월주의 도적단.",
      "lore": "프롬프트에 그대로 주입되는 본문 (300~600자 권장).",
      "relations": { "Shek Kingdom": "hostile" },
      "locations": ["Raiding camps"]
    }
  ]
}
```

- 허용 파일 형태: 단일 객체, 객체 배열, 또는 `{"factions": [...]}` — 템플릿은
  `faction_lore.d/example_uwe_faction.json.example` 참조
- 캠페인 전용 오버라이드: `server/campaigns/{캠페인}/faction_lore.json` (최우선 순위)
- 로드 확인: 브라우저에서 `http://127.0.0.1:5000/lore/list`

### 3. 월드 로어 청크 편집

`server/config/world_lore_chunks.json`에 세계관 정보를 주제별 청크로 정의합니다.
대화 내용과 코사인 유사도가 높은 청크만 선택해 주입하므로, 무관한 내용이 프롬프트를 채우지 않습니다.

| 필드 | 설명 |
|---|---|
| `id` | 청크 식별자 |
| `always_include` | `true`면 쿼리 무관하게 항상 주입 (세계관 기본 설명에 활용) |
| `embed_text` | 유사도 계산용 요약 키워드 (생략 시 title + text 앞부분 사용) |
| `text` | 프롬프트에 실제 주입되는 내용 |

수정 후 게임 재시작 없이 `http://127.0.0.1:5000/lore/reload`로 즉시 반영됩니다.

### 4. 고유 NPC 사전 정의 (canon_characters.json)

`server/config/canon_characters.json` 파일을 만들면(기본 미동봉, **선택 사항**) 특정 이름의 NPC에
대해 LLM 자동 생성 대신 **고정 프로필**을 사용합니다.

```json
[
  {
    "Name": "Beep",
    "Race": "Hive Prince",
    "Faction": "Western Hive",
    "Sex": "Male",
    "Personality": "천진난만하고 충성스럽다. 자신을 전설의 전사라 믿는다.",
    "Backstory": "몽그렐에서 홀로 살아남은 추방 하이브.",
    "SpeechQuirks": "자신을 3인칭 'Beep'으로 지칭."
  }
]
```

### 5. NPC 프로필 직접 편집

생성된 NPC 프로필은 `server/campaigns/{캠페인}/characters/{이름}.json`에 저장됩니다.
게임을 끈 상태에서 텍스트 에디터로 직접 편집하면 다음 대화에 즉시 반영됩니다.

> **개조판 변경점**: 대화 이력·기억 데이터는 JSON이 아닌 SQLite DB(`sentient_sands.db`)에 분리
> 저장됩니다. JSON에는 프로필 필드만 남으므로 파일이 훨씬 가볍습니다.

편집 가능한 JSON 필드: `Name`, `Race`, `Sex`, `Faction`, `Personality`, `Backstory`,
`SpeechQuirks`, `Relation`(호감도)

장기 기억을 **수동으로 심어줄** 때는 게임 종료 후 `scripts/embed_existing_memories.py`를
실행하면 해당 기억이 벡터화되어 의미 기반 회상에도 사용됩니다.

```
server\python\python.exe server\scripts\embed_existing_memories.py [캠페인명]
```

### 6. 월드 로어 · 응답 규칙 · NPC 기본 인격

`server/templates/`의 전역 템플릿 (모든 캠페인 공통, 단 캠페인 폴더에 같은 이름 파일을 두면 캠페인이 우선):

| 파일 | 역할 |
|---|---|
| `world_lore.txt` | 전체 로어 원문 — `world_lore_chunks.json`이 없는 환경의 폴백 |
| `response_rules.txt` | `## RESPONSE FORMAT RULES` — 응답 길이·금지 표현·거래 2단계·주요 팩션 영입 저항 등 |
| `npc_base.txt` | 모든 NPC의 기본 인격 지침 (황무지 생존자 톤) |
| `prompt_action_tags.txt` | 액션 태그 사용법 지침 (태그 추가/조정 시 DLL 호환에 주의) |
| `prompt_*.txt` | 프로필 생성·루머 합성·기억 요약 등 내부 파이프라인 템플릿 |

### 7. 이름 풀

`server/config/names.json`(+ `generic_names.json`)이 자동 개명에 쓰입니다 — Male/Female/Neutral
배열에 이름을 추가/삭제하면 됩니다. **현재 영문 이름 풀이며, 한글 음차 전환은 검토 중**입니다
(영문 백업: `names.json.en.bak`, 변환 스크립트: `scripts/transliterate_names.py`).

### 8. UI 번역

`server/config/localization.json` — F8 패널 등 UI 문자열 번역(English/Spanish/French/Japanese/Russian/**Korean** 96키). INI `language` 값과 연동됩니다.

---

## 개조판 추가 기능

<details>
<summary><b>기억 3계층 시스템</b> — 단기 20줄 + 자동 요약 Digest + 아카이브 압축 + 장기 Durable Memory</summary>

- **단기**: 대화 원문 최근 **20줄**을 프롬프트에 직접 주입 (`ShortTermContextCount`)
- **중기 (Digest)**: 대화가 **30줄** 누적될 때마다 백그라운드 LLM이 오래된 구간을 요약해
  `Digests`로 보관 — 요약된 구간 원문은 주입에서 제외해 토큰 이중 사용을 차단
- **아카이브 (Archive Summary)**: Digest가 3개 이상 쌓이면 가장 오래된 것들을 LLM이 단 하나의
  압축 단락으로 재요약 → `[ARCHIVE]` 블록으로 프롬프트에 고정 주입. 장기 플레이 시 토큰이
  선형 증가하지 않고 상한에 수렴
- **장기 (Durable Memory)**: LLM이 응답 끝에 `[RECORD_MEMORY: w=5 | keywords: ... | text: ...]`
  태그를 출력하면 서버가 가로채 SQLite DB에 저장 (게임에는 전달되지 않음).
  이후 대화에 키워드가 등장하면 **퍼지 매칭 또는 벡터 유사도**로 회상해 주입.
  w=5는 영구, w=3/1은 선형 감쇠로 휘발하되 회상될 때마다 수명이 연장됨

**프롬프트 주입 순서:**
```
[ARCHIVE — 오래된 대화 압축 요약]
[EARLIER EVENTS — MEMORY DIGEST]
[DURABLE MEMORIES — 장기 기억 회상]
[RECENT DIALOGUE — 최근 20줄 원문]
```

| INI 키 | 기본값 | 설명 |
|---|---|---|
| `ShortTermContextCount` | **20** | 단기 대화 주입 줄수 |
| `DigestEnabled` | 1 | 중기 요약 사용 |
| `DigestTriggerCount` / `DigestKeepRecent` | **30** / **10** | 요약 트리거 누적 줄수 / 원문 유지 줄수 |
| `DigestMaxCount` / `DigestInjectCount` | **3** / 3 | 보관 / 프롬프트 주입 개수 |
| `DigestCooldownSeconds` | 300 | NPC당 요약 최소 간격 |
| `ArchiveSummaryEnabled` | 1 | 아카이브 압축 사용 |
| `ArchiveDigestThreshold` | 3 | 아카이브 트리거 Digest 누적 수 |
| `DurableMemoryEnabled` | 1 | 장기 기억 사용 |
| `DurableMemoryMaxCount` / `DurableMemoryInjectCount` | 30 / 3 | 보관 / 주입 개수 |
| `DurableMemoryInjectTokens` | 200 | 주입 토큰 예산 |
| `DurableMemoryMatchThreshold` | 80 | 키워드 매칭 임계 (0–100, 한국어 회상이 잘 안 되면 하향) |
| `DurableMemoryDecayW3` / `DurableMemoryDecayW1` | 0.04 / 0.10 | w=3 / w=1 기억의 일일 감쇠율 |

</details>

<details>
<summary><b>팩션 RAG</b> — 대화 맥락에 맞는 팩션 정보만 골라 주입</summary>

대화 상대의 소속 팩션 + 대화 텍스트에서 감지된 팩션의 로어 블록만 `## FACTION INTEL`로 주입합니다.
감지는 ① rapidfuzz 퍼지 매칭(별칭·오타 허용, 한국어 별칭 91건 포함) ② model2vec 임베딩 시맨틱
매칭(potion-multilingual-128M)의 2단계. **임베딩 모델이나 numpy/model2vec이 없어도
퍼지 단독 모드로 완전히 동작**합니다.

| INI 키 | 기본값 | 설명 |
|---|---|---|
| `FactionRagEnabled` | 1 | 팩션 RAG 사용 |
| `FactionMatchThreshold` | 82 | 퍼지 매칭 임계 (0–100) |
| `FactionInjectCount` / `FactionInjectTokens` | 2 / 500 | 매칭 주입 블록 수 / 토큰 예산 |
| `FactionEmbeddingEnabled` | 1 | 시맨틱 매칭 사용 |
| `FactionSemanticThreshold` | 0.4 | 코사인 유사도 임계 |
| `FactionEmbeddingModel` | potion-multilingual-128M | `server/models/` 내 모델 폴더명 |

보조 도구: `scripts/build_faction_lore.py`(LLM으로 신규 팩션 로어 초안 생성),
`/lore/list`·`/lore/reload` HTTP 엔드포인트.

</details>

<details>
<summary><b>월드 로어 청크 RAG</b> — world_lore.txt를 주제별 청크로 분할, 관련 청크만 주입</summary>

`server/config/world_lore_chunks.json`에 세계관을 주제별 청크(기본 **8개** — 바닐라 5 + UWE 3)로 분리합니다.
`/chat` 요청마다 대화 쿼리와 코사인 유사도를 계산해 관련성 높은 청크(기본 top-2)만 주입합니다.
각 청크에 `"source"` 필드를 설정하면 vanilla 이외의 출처에는 `[SOURCE]` 접두사가 붙습니다.

- 임베딩 모델 로딩 전에는 `always_include: true` 청크만 주입해 안전하게 폴백
- `world_lore_chunks.json`이 없으면 기존 `world_lore.txt` 전체 주입으로 자동 폴백
- 실측 절감: ~603tk(전체) → ~298tk(청크 2개), 약 **51% 감소**

| INI 키 | 기본값 | 설명 |
|---|---|---|
| `WorldLoreRagEnabled` | 1 | 청크 RAG 사용 (0 = 전체 주입 폴백) |
| `WorldLoreTopK` | 2 | 유사도 상위 N개 청크 주입 |
| `WorldLoreChunkTokenBudget` | 300 | WORLD LORE 섹션 소프트 토큰 상한 |

</details>

<details>
<summary><b>SQLite 하이브리드 스토리지</b> — 프로파일은 JSON(편집 가능), 이력은 DB</summary>

대화 이력·기억 데이터를 캠페인당 단일 SQLite DB(`sentient_sands.db`)로 통합합니다.
**NPC 프로파일 JSON은 그대로 유지**되어 텍스트 에디터로 언제든 Personality 등을 수정할 수 있습니다.

```
campaigns/Default/
├── characters/Ruka.json         ← 프로파일만 (Name, Race, Personality 등) — 편집 가능
└── sentient_sands.db            ← 대화 100줄, 다이제스트, 장기 기억, 이벤트 이력
```

- 90일 이상 미접촉 NPC의 대화 이력은 자동 정리 (`NpcRetentionDays`)
- 기존 JSON 데이터를 DB로 이전하는 마이그레이션 스크립트: `scripts/migrate_to_sqlite.py`

| INI 키 | 기본값 | 설명 |
|---|---|---|
| `StorageBackend` | sqlite | 스토리지 방식: `sqlite` / `json` (레거시) |
| `NpcRetentionDays` | 90 | 미접촉 NPC 이력 보존 기간(인게임 일수) |

</details>

<details>
<summary><b>벡터 기반 장기 기억 회상</b> — 키워드 없이 의미로 기억 검색</summary>

`sqlite-vec` 라이브러리의 vec0 가상 테이블을 사용해 DurableMemory 회상을 벡터 KNN 검색으로 수행합니다.
기존 키워드 매칭과 달리 "당신이 나를 도와줬잖아요"처럼 키워드가 없는 표현으로도 관련 기억이 회상됩니다.

- 벡터 데이터는 `sentient_sands.db` 내 `durable_memory_index` 테이블에 저장 (배포 불필요, 플레이 중 자동 생성)
- sqlite-vec 로드 실패 시 자동으로 키워드 매칭 폴백
- 기존 기억 일괄 임베딩: `scripts/embed_existing_memories.py`

| INI 키 | 기본값 | 설명 |
|---|---|---|
| `VectorRecallEnabled` | 1 | 벡터 기반 회상 사용 (0 = 키워드 매칭만) |
| `VectorRecallThreshold` | 0.35 | 코사인 유사도 최소 임계값 (0–1) |

</details>

<details>
<summary><b>프롬프트 최적화</b></summary>

- **한글 토큰 보정**: 한글 비율 30% 초과 시 토큰 추정 로직을 `len//2` 적용 (한글은 음절당 실제 2~3토큰)
- 월드 로어 청크 RAG로 `world_lore` 섹션 ~50% 절감
- 주변 NPC 상세 정보를 **시야 반경 내 인원으로 제한** (`NearbyMaxCount`, `NearbyDetailRadius`)
- Yell 모드에서 비주연 청중은 1줄 프로필로 축약 (`YellCompactProfiles`)
- 오래된/무관 이벤트 필터 (`EventFilterEnabled`, `EventFilterDays`)
- 단기 대화 윈도우가 이미 담고 있는 사건의 이벤트 중복 주입 제거 (`DedupeChatEvents`)
- 프롬프트 섹션 구조화 + 소프트 토큰 상한에서 오래된 히스토리부터 절삭 (`MaxPromptTokens`)
- **실측 토큰**: 서버 추정 기준 ~3,300tk / 실제 API 청구 기준(한글 포함) ~5,000tk 이내

</details>

<details>
<summary><b>한국어 현지화</b></summary>

- INI `language = Korean` (또는 F8 → AI Settings → Language) 하나로 **NPC 대사·프로필 생성·루머
  합성·기억 요약·앰비언트 대화** 등 6개 프롬프트 경로 전부에 한국어 응답이 강제됩니다
- F8 패널 UI 한국어 번역 96키 (`localization.json`)
- 팩션 한국어 별칭 91건 — "성국", "연합도시" 같은 한국어 발화로도 팩션 RAG가 작동
- `Language=English`면 원작과 동일하게 동작
- 이름 풀 한글 음차는 인게임 렌더링 검증 후 적용 예정 (현재 영문 유지)

</details>

<details>
<summary><b>원본 버그 수정 4건</b></summary>

1. BOM이 포함된 캐릭터 JSON을 로더가 거부하던 문제 → 모든 읽기를 `utf-8-sig`로
2. 대화 시작 시 `/chat`의 약식 등록이 `/context`가 보낸 의료·스탯·인벤토리 정보를 덮어쓰던 문제 → 병합 방식으로
3. 액션 태그 정규화가 `[ACTION: X]`를 `[ACTION: X: X]`로 인자 중복시키던 문제
4. 같은 대화가 히스토리와 이벤트 로그로 이중 주입되던 문제 (`DedupeChatEvents`)

</details>

---

## 설정 레퍼런스 (`SentientSands_Config.ini` [Settings])

INI에 키가 없으면 기본값으로 동작합니다 (하위 호환). F8 → AI Settings에서 바꾸면 INI에 자동 저장됩니다.

<details>
<summary><b>원작 기본 키</b></summary>

| 키 | 기본값 | 설명 |
|---|---|---|
| `CurrentModel` | player2-default | models.json의 모델 표시명 |
| `ActiveCampaign` | Default | 활성 캠페인 |
| `Language` | English | 응답·UI 언어 (Korean 지원) |
| `TalkRadius` / `YellRadius` | 100 / 200 | Talk / Yell 도달 범위 |
| `RadiantRange` / `RadiantDelay` | 100 / 240 | 앰비언트 대화 범위 / 주기(초) |
| `EnableAmbientConversations` | 1 | NPC끼리 자율 대화 |
| `SynthesisIntervalMinutes` | 15 | 루머 합성 주기(분) |
| `GlobalEventsCount` | 5 | 프롬프트에 넣을 최근 이벤트 수 |
| `DialogueSpeedSeconds` / `SpeechBubbleLife` | 5 / 5.0 | 말풍선 표시 간격 / 수명(초) |
| `MinFactionRelation` / `MaxFactionRelation` | -100 / 100 | FACTION_RELATIONS 변동 한계 |
| `EnableWelcomePopup` | 1 | 시작 시 환영 창 |

</details>

<details>
<summary><b>개조판 신규 키 (30종)</b></summary>

| 키 | 기본값 | 설명 |
|---|---|---|
| `ShortTermContextCount` | 20 | 단기 대화 주입 줄수 |
| `MaxPromptTokens` | 6000 | 프롬프트 소프트 상한 (초과 시 오래된 히스토리 절삭) |
| `NearbyMaxCount` / `NearbyDetailRadius` | 8 / 10.0 | 주변 인물 목록 상한 / 상세정보 반경 |
| `YellCompactProfiles` | 1 | Yell 모드 청중 프로필 1줄 축약 |
| `EventFilterEnabled` / `EventFilterDays` | 1 / 3 | 이벤트 필터 / 유지 일수 |
| `DedupeChatEvents` | 1 | 대화-이벤트 중복 주입 제거 |
| `DigestEnabled` | 1 | 중기 요약 |
| `DigestTriggerCount` / `DigestKeepRecent` | 30 / 10 | 요약 트리거 / 원문 유지 줄수 |
| `DigestMaxCount` / `DigestInjectCount` | 3 / 3 | 보관 / 주입 개수 |
| `DigestCooldownSeconds` | 300 | NPC당 요약 최소 간격(초) |
| `ArchiveSummaryEnabled` | 1 | 아카이브 압축 |
| `ArchiveDigestThreshold` | 3 | 아카이브 트리거 Digest 수 |
| `DurableMemoryEnabled` | 1 | 장기 기억 |
| `DurableMemoryMaxCount` / `DurableMemoryInjectCount` | 30 / 3 | 보관 / 주입 개수 |
| `DurableMemoryInjectTokens` | 200 | 주입 토큰 예산 |
| `DurableMemoryMatchThreshold` | 80 | 키워드 매칭 임계 |
| `DurableMemoryDecayW3` / `DurableMemoryDecayW1` | 0.04 / 0.10 | w=3 / w=1 일일 감쇠율 |
| `FactionRagEnabled` | 1 | 팩션 RAG |
| `FactionMatchThreshold` | 82 | 퍼지 매칭 임계 |
| `FactionInjectCount` / `FactionInjectTokens` | 2 / 500 | 주입 블록 수 / 토큰 예산 |
| `FactionEmbeddingEnabled` / `FactionSemanticThreshold` | 1 / 0.4 | 시맨틱 매칭 / 코사인 임계 |
| `FactionEmbeddingModel` | potion-multilingual-128M | 모델 폴더명 |
| `WorldLoreRagEnabled` / `WorldLoreTopK` | 1 / 2 | 청크 RAG / 상위 청크 수 |
| `WorldLoreChunkTokenBudget` | 300 | 월드 로어 토큰 상한 |
| `StorageBackend` | sqlite | `sqlite` / `json` |
| `NpcRetentionDays` | 90 | 미접촉 NPC 이력 보존 기간 |
| `VectorRecallEnabled` | 1 | 벡터 기반 기억 회상 |
| `VectorRecallThreshold` | 0.35 | 코사인 유사도 임계 |

</details>

---

## 문제 해결 (FAQ)

**Q. 서버가 안 뜹니다 / NPC가 응답하지 않습니다**
- `RE_Kenshi.exe`로 실행했는지, 모드 런처에서 SentientSands + KenshiLib가 켜져 있는지 확인
- 모드 폴더 경로가 정확히 `Kenshi\mods\SentientSands\`인지 확인 (DLL이 이 경로를 하드코딩)
- `server/python/` 폴더가 없다면 `setup_embedded_python.py`를 먼저 실행하세요
- 브라우저에서 `http://127.0.0.1:5000/ping` 접속으로 서버 생존 확인
- 로그 확인: `server/logs/server.log` (요약), `server/debug.log` (프롬프트 전문 포함 상세)

**Q. LLM 연결 테스트가 실패합니다**
- `providers.json`의 `api_key`·`base_url` 오타, `models.json`의 `provider` 키 불일치가 대부분
- 로컬 Ollama/LM Studio는 해당 프로그램이 먼저 실행 중이어야 합니다

**Q. 한국어로 응답하지 않습니다**
- F8 → AI Settings → Language = Korean (INI `language = Korean`) 확인
- 소형/저가 모델은 한국어 지시를 무시할 수 있습니다 — 다국어 성능이 검증된 모델 권장

**Q. NPC JSON을 열었더니 ConversationHistory가 없어졌어요**
- 개조판에서 대화 이력은 JSON이 아닌 `sentient_sands.db`에 저장됩니다.
  JSON에는 Personality·Backstory 등 **사용자가 편집하는 프로파일 필드만** 남아 있습니다.
  대화 기록은 DB에 정상 저장되어 있으며, 게임 내 **Dialogue Library**에서 확인할 수 있습니다.

**Q. 임베딩 모델(512MB)을 지우면 어떻게 되나요?**
- `server/models/potion-multilingual-128M` 삭제 시 팩션 RAG와 월드 로어 RAG가 퍼지 매칭 단독
  모드로 자동 전환되어 정상 동작합니다. 벡터 기반 기억 회상도 키워드 매칭으로 폴백됩니다.
  시맨틱(의미 기반) 매칭 기능만 비활성화됩니다. 모델 재다운로드: `setup_embedded_python.py` 재실행.

**Q. NPC가 예전 일을 기억 못 합니다**
- 키워드 매칭 회상: 당시 키워드(이름·장소·사건)를 대화에 언급해 보세요
- 벡터 회상(`VectorRecallEnabled=1`): 의미 유사 발화로도 회상됩니다. `VectorRecallThreshold`를
  0.35에서 0.25~0.30으로 낮추면 더 너그럽게 회상합니다
- 수동으로 기억을 심은 경우 `embed_existing_memories.py`를 실행해 벡터 인덱스를 생성하세요

**Q. 설정을 INI에서 직접 바꿨는데 적용이 안 됩니다**
- 게임(서버) 실행 중 INI 직접 수정은 권장하지 않습니다 — F8 메뉴를 쓰거나 게임 재시작.
  단, 팩션 로어 JSON과 월드 로어 청크 JSON은 실행 중 수정 후 `/lore/reload`로 즉시 반영 가능합니다.

---

## 변경 이력

### 2026-06-16 — 로어 RAG 확장 (바닐라 + UWE 통합)

**팩션 DB 33 → 73개로 확장**

| Phase | 내용 |
|-------|------|
| A | `_format_faction_intel()` — `source_mod` 값이 vanilla가 아니면 `[모드명]` 레이블 자동 출력. `world_lore_chunks.json`에 `"source"` 필드 추가 |
| B | `faction_lore.d/vanilla_missing_factions.json` — 세력정보.txt(한국어 위키) 기반 바닐라 미수록 17개 팩션. 정제(게임팁 제거) → 영문 번역(150tk 이내) → 스키마화 파이프라인 적용 |
| C | `faction_lore.d/uwe_factions.json` — UWE_Info.md §6~8 기반 UWE 모드 신규 23개 팩션. biome·관계도·거점 데이터 기반 로어 작성 |
| D | `world_lore_chunks.json` — UWE 세계 청크 3개 추가: 신규 적대 세력 거점 / 신규 하이브 변종 / 팩션 외교관 위치 |

---

## 크레딧 · 라이선스

- **원작**: *Sentient Sands* by **Antigravity** — 본 저장소는 원작의 비공식 개조판이며,
  C++ 플러그인(`SentientSands.dll`)은 원작 바이너리를 그대로 사용합니다.
- **의존 모드**: [RE_Kenshi](https://github.com/BFrizzleFoShizzle/RE_Kenshi) (BFrizzleFoShizzle),
  [KenshiLib](https://github.com/KenshiReclaimer/KenshiLib) (KenshiReclaimer)
- **오픈소스**: Python 3.10 (embedded) · Flask · requests ·
  [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) ·
  [model2vec](https://github.com/MinishLab/model2vec) + numpy ·
  [potion-multilingual-128M](https://huggingface.co/minishlab/potion-multilingual-128M) (MinishLab, MIT) ·
  [sqlite-vec](https://github.com/asg017/sqlite-vec) (Alex Garcia, MIT)
- 원작자 고지: 원작 모드는 LLM 코딩 에이전트의 도움으로 제작되었습니다. 본 개조판 역시 동일합니다.
