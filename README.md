# MCP Tool Execution

12개 모의 도구의 메타데이터를 사용하는 **판단만 수집하는 연구 환경**입니다. 본실험의 관측값은 EXECUTE / CONFIRM / REFUSE입니다. 실제 Tool은 실행하지 않습니다. 2026-09-10 GPT와 Claude의 파일럿 및 본실험을 완료했으며, 최종 분석에는 두 모델의 유효 응답 360개를 사용했습니다. 기본 API 실행 설정은 비활성화되어 있습니다.

## 최종 결과

- GPT-5.6 Sol: E=158, C=22, R=0 (유효 응답 180개)
- Claude Sonnet 5: E=169, C=11, R=0 (유효 응답 180개)
- 통합 보고서: [results/two-model-results.md](results/two-model-results.md)
- 집계 CSV: [results/two-model-results.csv](results/two-model-results.csv)
- 효과량 CSV: [results/two-model-results.effects.csv](results/two-model-results.effects.csv)
- 통합 시행별 응답: [results/two-model-responses.jsonl](results/two-model-responses.jsonl)

`runs/`에는 GPT와 Claude의 파일럿 및 본실험 원시 기록을 보존했습니다.

## 현재 구성

- Python 3.12, 공식 MCP Python SDK 2.1.1, pytest 9.1.1.
- 도구: 파일·메모·일정·DB 네 도메인에 읽기/추가/파괴적 변경 각 1개, 총 12개.
- 로컬 stdio 통신. 서버를 시작하면 클라이언트의 입력을 기다립니다.
- 도구가 다루는 파일은 SQLite에 저장된 **가상 파일**입니다. 메모·일정·레코드도 같은 방식의 합성 데이터입니다.
- SQLite는 서버 프로세스 전용 메모리 DB입니다. 각 실행은 동일한 시드로 시작하고 종료되면 변경 상태가 사라집니다.
- 개발용 MCP 서버의 Tool Annotation은 실제 동작과 일치하는 기준값입니다. 본실험용 텍스트 사례에서는 RO와 DH를 모두 제시한 일치·상충 프로필을 사용합니다.
- 추가 도구는 기존 ID에 대해 오류를 반환하며 덮어쓰지 않습니다.
- 12개 도구 × Annotation 3조건 × 고정 요청문 × 반복 5회 = 모델당 180개 판단용 사례를 오프라인으로 생성합니다.
- GPT-5.6 Sol과 Claude Sonnet 5의 본실험 응답을 각각 180개 수집했습니다. 연결 점검과 기술 파일럿은 최종 결과에 포함하지 않습니다.
- 코드는 OpenAI와 Anthropic의 텍스트 응답 어댑터를 제공하며, 연구 결과도 두 모델을 대상으로 합니다.

## 순서대로 실행하기

PowerShell에서 다음 명령을 한 번에 하나씩 실행합니다. 가상환경 활성화는 필수가 아닙니다.

```powershell
Set-Location 'MCP_Tool_Execution'
```

1. 환경 확인:

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli doctor
```

2. MCP 연결을 통해 전달된 도구 목록과 실제 JSON 스키마 확인:

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli catalog
```

3. 판단용 사례 180개 생성 (도구 호출 없음, 모델 호출 없음):

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli prepare
```

datasets/ 아래 새 디렉터리에 cases.jsonl과 manifest.json을 저장합니다. 분석용 condition, case_id 등의 필드는 모델 입력에 포함하지 않으며, 향후 사용할 입력은 각 행의 model_input뿐입니다.

4. 개발용 모의 도구 동작 점검과 기록 저장 (본실험과 별개):

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli smoke
```

5. 전체 자동 검증:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
```

MCP 서버만 시작하려면 아래 명령을 사용합니다. stdio 서버이므로 화면에 웹페이지나 메뉴가 뜨지 않고 프로토콜 입력을 기다립니다. 일반 사용 시에는 위 catalog/smoke 명령이 서버를 시작하고 종료합니다.

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.server
```

## 검증 기록

각 smoke 실행은 runs/ 아래에 고유한 새 디렉터리를 만듭니다.

| 파일 | 내용 |
|---|---|
| manifest.json | 시작·종료 시각, Python/SDK/패키지 버전, 소스 해시, 상태, 초기 데이터 해시 |
| catalogue.json | MCP tools/list에서 실제로 받은 이름·Description·Annotation·입출력 스키마 |
| events.jsonl | 도구 요청·원본 응답·소요 시간·예상 오류 여부를 순서대로 저장 |
| report.json | 성공한 전체 검증의 상세 결과 |
| summary.json | 검증한 도구 수·호출 수·결과 요약 |
| summary.csv | 도구별 호출 수, 예상 오류 수, 통과 수 |

검증 중 실패해도 이미 받은 이벤트와 failed 상태의 manifest를 보존합니다. 중복 생성 4회, 삭제 후 조회 3회의 오류는 의도한 정상 검증 항목입니다. 실패 횟수나 LLM 거부 횟수로 해석하지 않습니다.

## 판단 결과 집계

실제 모델 응답은 JSONL로 보관합니다. 각 행에는 case_id, model_id, decision을 기록합니다. decision은 EXECUTE, CONFIRM, REFUSE 또는 E, C, R을 받으며 대소문자와 양끝 공백을 정규화합니다. 형식은 config/decision-response.schema.json에 정의했습니다. 고정된 180개 사례는 집계 과정에서 변경하지 않습니다.

집계 변수는 **E=EXECUTE, C=CONFIRM, R=REFUSE**입니다. 유효 응답 수는 E+C+R이며, 비율은 각 변수 / 유효 응답 수로 계산합니다.

사용자가 준비한 입력·응답 경로를 지정합니다. 아래 INPUT_CASES.jsonl과 OBSERVED_RESPONSES.jsonl은 경로 자리표시자입니다.

```powershell
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli summarize --cases 'INPUT_CASES.jsonl' --responses 'OBSERVED_RESPONSES.jsonl' --output 'reports\decision-report'
```

한 번의 명령으로 **reports/decision-report.md, reports/decision-report.csv, reports/decision-report.effects.csv**를 생성합니다. --output에 .csv 또는 .md를 지정해도 같은 basename을 사용합니다. 하나라도 이미 있으면 새 파일을 만들지 않습니다.

- Markdown: 집계 기준, 모델별 전체 결과, 조건별 결과, 조건·도구 유형별 결과, 개별 도구 결과, 입력 파일 경로와 해시.
- CSV: 같은 집계 결과를 level 열로 구분합니다. overall, condition, condition_behavior, tool_condition 네 수준은 서로 합산하지 않습니다.
- effects CSV: Tool 군집 기반 상충−일치 차이, 95% 부트스트랩 신뢰구간, 순열 p값과 Holm 보정값을 기록합니다.
- E/C/R은 횟수, E_rate/C_rate/R_rate는 0~1 비율입니다. Markdown에서는 백분율로 표시합니다.
- 누락(missing)과 형식 오류(invalid)는 별도로 집계합니다. 유효 응답이 없으면 비율은 빈 CSV 셀 및 Markdown의 ‘—’로 표시합니다.
- 같은 모델·사례의 중복 응답과 알 수 없는 사례 ID는 오류로 처리합니다. 도구 실행이나 정답 정확도는 계산하지 않습니다.
- 합성 예시는 --demo로 표시합니다. data_kind=synthetic_demo 또는 DEMO_ONLY로 시작하는 모델명이 응답에 있어도 자동으로 예시 표시를 붙입니다.

상세 출력 명세는 docs/result-report-format.md에 있습니다.

## 12개 도구

| 도메인 | 읽기 | 추가 | 파괴적 변경 |
|---|---|---|---|
| 파일 | read_file | create_file | delete_file |
| 메모 | get_note | create_note | overwrite_note |
| 일정 | get_event | create_event | delete_event |
| DB | get_record | insert_record | delete_record |

초기 ID는 file_demo, note_demo, event_demo, record_demo입니다. 논리 ID만 받고 실제 경로, 임의 SQL, URL은 입력으로 받지 않습니다. 일정 생성 시 ISO 8601 시간대 오프셋을 명시합니다.

## 재설치

현재 설치 패키지는 requirements.lock에 고정되어 있습니다. Python 3.12와 uv를 준비한 다음, 프로젝트 루트에서 순서대로 실행합니다.

API 연결을 다시 확인할 때는 `.env.example`을 `.env`로 복사하고 자리표시자를 실제 키로 바꿉니다. `.env`는 `.gitignore`에 포함되어 Git에 추가되지 않습니다.

```powershell
uv venv .venv --python 3.12
uv pip sync --python .venv\Scripts\python.exe requirements.lock
uv pip install --python .venv\Scripts\python.exe --editable . --no-deps
```

현재 가상환경은 Codex에 포함된 Python 3.12.14를 사용합니다. 그 기반 Python 설치가 제거되면 위 방법으로 가상환경을 재생성해야 합니다. 시스템 Python, 다른 프로젝트의 환경, Codex의 MCP 설정은 이 프로젝트 설치 대상이 아닙니다.

## 최종 실험 상태

- 요청문·반복 횟수·조건·분석 설정은 [실험 계획서](docs/experiment-plan-20260909.md)에 고정했습니다.
- GPT와 Claude의 기술 파일럿 및 본실험을 완료했습니다. 두 모델 모두 180/180개의 유효 응답을 확보했습니다.
- `api_calls_enabled=false`이며 저장소의 결과 재집계에는 API 호출이 필요하지 않습니다.

MCP 서버는 고정된 일치 Annotation만 제공합니다. 판단용 데이터셋의 미표기/상충 조건은 **비실행형 사례 텍스트**로만 생성합니다. 서버 도구에 그 값을 등록하거나 모델에게 실행 가능한 함수를 제공하지 않습니다. 외부 전송 도구와 모델 자동 실행은 포함하지 않습니다.

조건 구성과 해석의 한계는 docs/experiment-design.md, 비용의 가정과 출처는 docs/cost-estimate.md에 정리했습니다.

## 근거

- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Tool Annotation 의미: https://modelcontextprotocol.io/specification/2025-11-25/schema#toolannotations

실제 협상된 프로토콜 버전은 실행마다 manifest.json에 기록합니다. SDK 2.1.1은 이전 명세의 Annotation도 지원하지만, 문서에 인용한 명세 날짜와 실행 중 협상된 프로토콜 버전을 같은 것으로 가정하지 않습니다.
