# 두 제공사 API 연결과 실험 실행

확인일: 2026-09-09

OpenAI Responses와 Anthropic Messages에 대한 텍스트 응답 어댑터를 추가했습니다. 실험 호출은 `config/providers.json`의 `api_calls_enabled: false`로 중지되어 있습니다. 연결 확인 요청은 별도 명령으로 실행하며 실험 입력이나 E/C/R 평가를 사용하지 않습니다.

선택한 API 모델 ID는 `gpt-5.6-sol`과 `claude-sonnet-5`입니다. API 키 값은 코드나 결과 파일에 넣지 않습니다. Python 표준 라이브러리로 HTTPS 요청을 보내므로 제공사 SDK나 httpx를 추가 설치하지 않아도 됩니다.

## 확인한 현재 위치

2026-09-09에 D: 드라이브가 다시 연결되었습니다. 원래 프로젝트는 D:\mcp-annotation-lab입니다. 요청문을 Tool별 한 문장으로 고정한 현재 데이터셋은 `datasets/20260909T100049_309346Z/cases.jsonl`이며 SHA-256은 `50093ec2d22761b286d0c7307d0a9322da21d4a726cefa227ae49480a0293480`입니다. 앞선 데이터셋은 보존하지만 실행 설정에서 제외했습니다.

## 먼저 확정할 값

- 모델별 추론/생성 설정과 본실험 예산. 현재 4096 출력 토큰 한도는 미실행 설정안입니다.
- API 계정에서 선택한 모델의 사용 권한과 할당량. 키가 있다는 사실만으로 사용 가능 여부가 보장되지는 않습니다.

현재 실행 설정은 `config/providers.json`입니다. 두 모델의 sampling 및 thinking/reasoning 옵션은 전송하지 않고 제공사 기본값을 사용하도록 작성했습니다. 기본값이 모델 간 동일한 추론량이나 같은 temperature를 뜻하지 않습니다. Sonnet 5는 기본값 이외의 sampling 값을 지원하지 않으므로 일괄 temperature=0을 설정하지 않았습니다. 본실험 전에 최종 설정을 확정하고 논문에 기록해야 합니다.

## 키 입력

키는 채팅이나 논문/결과 파일에 넣지 않습니다. 프로젝트 루트의 `.env`는 `.gitignore`에서 제외되어 있습니다.

파일 탐색기에서 `D:\mcp-annotation-lab\enter-api-keys.cmd`를 더블클릭합니다. 로컬 입력창에서 OpenAI와 Claude(Anthropic) 키를 순서대로 입력하고 각 입력 후 Enter를 누릅니다. 입력창은 화면에 키 값을 표시하지 않습니다.

또는 로컬 PowerShell에서 이 프로젝트 폴더로 이동한 후 다음 스크립트를 직접 실행합니다.

```powershell
& '.\scripts\set-api-keys.ps1'
```

스크립트가 차례대로 묻는 값은 OPENAI_API_KEY와 ANTHROPIC_API_KEY입니다. 입력은 화면에 표시되지 않고 로컬 `.env` 파일에 저장됩니다. 기존 `.env`가 있으면 덮어쓰지 않습니다. 스크립트 실행만으로 API 요청이 나가지는 않습니다. `.env`는 로컬의 평문 비밀 파일이므로 공유하지 않습니다.

`scripts/check-key-setup.ps1`은 각 키가 저장되어 있는지만 표시하며 값 자체는 출력하지 않습니다. 입력을 마치면 채팅에는 키 값 대신 입력 완료 여부와 사용할 정확한 모델 ID만 알려줍니다.

## 연결만 확인하기

다음 명령은 네트워크 요청 없이 키 존재 여부, 모델 설정, 데이터셋 해시와 시행 수를 확인합니다. 키 값은 표시하지 않습니다.

```powershell
& 'D:\mcp-annotation-lab\.venv\Scripts\python.exe' -m mcp_annotation_lab.runner plan
```

다음 명령은 두 모델에 각각 1회씩 `Reply OK.`라는 짧은 요청을 순서대로 보냅니다. **실제 API 호출이므로 소량의 사용량이 발생할 수 있습니다.** 실험 데이터셋은 읽지 않으며, E/C/R 응답을 요청하거나 집계하지 않습니다. 이 연결 점검 명령은 실험 비활성 설정과 별개입니다.

```powershell
& 'D:\mcp-annotation-lab\.venv\Scripts\python.exe' -m mcp_annotation_lab.check_connections --live
```

연결 점검 결과는 `reports/connection_check_*.json`에 저장합니다. 요청한 모델, 반환 모델, HTTP 상태, 응답 텍스트, 토큰 사용량을 기록하며 API 키는 제외합니다. 자동 재시도는 하지 않습니다.

## 향후 본실험 실행 흐름

1. 데이터셋 해시와 모델별 설정을 확정합니다.
2. API 연결 코드에 대해 네트워크 없는 검증을 수행합니다.
3. 위 연결 확인과 별개로, 본실험 전에 소수의 판단 응답으로 최종 출력 설정을 점검합니다. 사용자 요청에 따라 현재는 이 단계도 실행하지 않습니다.
4. 모델별 180회, 두 모델 총 360개 논리적 시행을 순차 실행합니다. 재시도는 별도 API 시도로 기록합니다.
5. 원문 응답과 모델·요청·토큰 사용량을 기록하고 시행표·집계표·효과량표·Markdown 보고서를 생성합니다.

관측값은 E/C/R 판단입니다. 실제 MCP 도구 실행은 측정하지 않습니다. API 오류와 형식 오류를 REFUSE로 바꾸지 않습니다.

수집기는 `python -m mcp_annotation_lab.runner run`으로 실행할 수 있습니다. 실험 활성화 설정, `--live`, 명시적 `--max-calls`가 모두 필요합니다. 현재는 실험 설정을 비활성화해 두었으므로 실행하지 않습니다.

시행과 개별 API 시도마다 저널을 저장합니다. 429·502·503·504와 명확한 연결 실패만 최대 2회 재시도합니다. 401·403, 타임아웃, 차단, 잘림, 형식 오류는 재시도하지 않습니다. 최종 오류가 발생하면 중단 결과도 모든 산출물에 저장합니다. 응답이 없는 모델은 누락에 포함하고 제공사 차단을 연구 판단 R로 변환하지 않습니다.

재개에는 `--resume`과 동일한 모델·설정·데이터·코드가 필요합니다. 시작만 기록된 요청은 이미 과금되었을 가능성이 있으므로 자동 재전송하지 않고 `interrupted_unknown`으로 남깁니다. 프로세스가 강제 종료되어 `.active.lock`이 남거나 저널이 손상된 경우에는 상태를 확인한 뒤 복구해야 합니다. 호출 수 제한은 금액 예산 제한이 아니며, 제공사별 사용량/청구액은 별도로 확인해야 합니다. RPD 장부는 `work/request-limit-ledger.json`에 저장하며 API 키 값이나 해시는 기록하지 않습니다.

## 공식 문서

- OpenAI 인증: https://developers.openai.com/api/reference/overview#authentication
- Claude API: https://platform.claude.com/docs/en/api/overview
- OpenAI Responses: https://developers.openai.com/api/reference/python/resources/responses/methods/create
- GPT-5.6 Sol: https://developers.openai.com/api/docs/models/gpt-5.6-sol
- Claude Sonnet 5: https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5
