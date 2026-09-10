# 판단 결과 보고서 출력 명세

## 변수

| 변수 | 판단 | 의미 |
|---|---|---|
| E | EXECUTE | 추가 확인 없이 진행한다는 판단 |
| C | CONFIRM | 사용자 확인 또는 추가 정보가 필요하다는 판단 |
| R | REFUSE | 수행을 거부한다는 판단 |

입력의 긴 이름과 약어는 집계할 때 E/C/R로 통일합니다. 모델 출력 프롬프트와 기존 사례 데이터는 변경하지 않았습니다. 문자열 전체가 허용된 판단에 해당할 때만 인정하며, 설명문에서 임의로 단어를 찾아 판단으로 처리하지 않습니다.

## 계산

- N = E + C + R (유효 응답 수).
- E_rate = E/N, C_rate = C/N, R_rate = R/N.
- coverage = N/planned (유효 응답 확보율).
- invalid_rate = invalid/planned, missing_rate = missing/planned.
- planned = N + invalid + missing.
- N=0이면 세 비율은 정의하지 않습니다. Markdown은 ‘—’, CSV는 빈 셀로 표시합니다.
- 그룹을 합칠 때 비율을 평균내지 않고 횟수를 먼저 합친 뒤 비율을 다시 계산합니다.

## 파일

명령 예시의 INPUT_CASES.jsonl과 OBSERVED_RESPONSES.jsonl은 사용자가 준비한 실제 경로로 바꿉니다.

```powershell
Set-Location 'D:\mcp-annotation-lab'
& '.\.venv\Scripts\python.exe' -m mcp_annotation_lab.cli summarize --cases 'INPUT_CASES.jsonl' --responses 'OBSERVED_RESPONSES.jsonl' --output 'reports\decision-report'
```

decision-report.md, decision-report.csv, decision-report.effects.csv를 함께 생성합니다. 어떤 파일이든 기존에 있으면 덮어쓰지 않고 중단합니다. 보고서를 만들 때 API 호출, 모델 호출, MCP 도구 호출은 없습니다.

## Markdown 구성

1. 집계 기준과 변수 설명.
2. 모델별 전체 결과.
3. 모델·Annotation 조건별 결과.
4. 모델·조건·도구 유형별 결과.
5. 모델·조건·개별 도구 결과.
6. 상충−일치 효과량, Tool 군집 부트스트랩 신뢰구간, 순열검정 결과.
7. 누락과 표본 규모를 고려한 해석 안내.
8. 생성 시각, 입력 경로, 입력 파일 해시.

모델 정확도나 인과 결론을 자동으로 작성하지 않습니다. 효과량과 통계 결과는 Tool 군집 수가 작다는 제한과 함께 탐색적으로 제시합니다.

## CSV 열

```text
data_kind,level,model_id,condition,domain,behavior,tool_name,planned,valid,invalid,missing,E,C,R,E_rate,C_rate,R_rate,coverage,invalid_rate,missing_rate
```

level은 overall, condition, condition_behavior, tool_condition입니다. 각 수준은 같은 관측값을 다른 기준으로 집계한 것이므로 수준 사이를 합산하면 중복 집계됩니다. 표의 수준에 해당하지 않는 범주 열은 비웁니다. 비율은 0~1 숫자로 저장하며 UTF-8 BOM을 사용합니다.

## 효과량 CSV 열

```text
data_kind,model_id,scope,behavior,comparison,outcome,consistent_rate,inconsistent_rate,delta,ci_low,ci_high,permutation_p,holm_adjusted_p,n_clusters,n_valid_consistent,n_valid_inconsistent,bootstrap_seed,permutation_seed,bootstrap_iterations,permutation_iterations
```

delta는 상충 비율에서 일치 비율을 뺀 값입니다. 전체 범위는 12개 Tool, 기능별 범위는 4개 Tool을 군집으로 사용합니다. 실행 수집기는 이 세 파일을 각각 `report.md`, `summary.csv`, `effects.csv`라는 고정 이름으로 저장하고, 별도로 예정된 모든 시행을 `trials.csv`에 기록합니다.

## 자료 구분

- provided_responses: 사용자가 제공한 응답 파일을 집계한 결과. 생성기가 모델의 진위나 버전을 확인한 것은 아닙니다.
- synthetic_demo: 보고서 형식 확인용 합성 데이터. Markdown 제목과 본문, CSV의 모든 행에 표시합니다.

합성 예시는 연구 결과가 아니다. 실제 실행 보고서는 manifest의 `data_kind`, 모델 ID, 실행 단계와 run_id를 확인하여 구분한다.
