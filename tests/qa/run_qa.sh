#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Gymbro scan-progress QA runner — 一條指令跑晒 SCAN_PROGRESS_SPEC §4 五項
#
# Isak 2026-10-20。
#
# 用法:
#   bash /home/work/projects/gymbro/tests/qa/run_qa.sh              # 全套
#   bash .../run_qa.sh --allow-ai                                   # 加 AI 測試
#   bash .../run_qa.sh --only contract                              # 只跑某一組
#
# ⚠️ PYTHONPATH="" 一定要加 —— 否則會繼承 alonso 個 3.14 venv,
#    PIL _imaging.so ImportError。
# ⚠️ gymbro .venv 一開始冇 playwright,已由 QA 裝咗 (playwright==1.58.0)。
#    chromium browser 已經喺 ~/.cache/ms-playwright/chromium-1208。
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO="/home/work/projects/gymbro"
PY="$REPO/.venv/bin/python"
export PYTHONPATH=""

ALLOW_AI=""
ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-ai) ALLOW_AI=1 ;;
    --only) ONLY="$2"; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
  shift
done

if [[ -n "$ALLOW_AI" ]]; then
  export GYMBRO_QA_ALLOW_AI=1
  echo "⚠️  GYMBRO_QA_ALLOW_AI=1 — AI 測試會真跑 MiniMax/APiyi/PPLX。"
  echo ""
  echo "🔴 HARD STOP (Jim OOB 2026-10-20「Someone has pollute the production"
  echo "   data」): 唔准 commit 落 Jim 個 Nutrition golden copy。"
  echo "   --allow-ai 而家 REQUIRE 一個 THROWAWAY sheet copy:"
  echo ""
  echo "     GYMBRO_SHEET_ID=<copy-of-sheet-id> \\"
  echo "     bash $0 --allow-ai"
  echo ""
  echo "   冇 set GYMBRO_SHEET_ID → 唔會行 AI 測試 (會 refuse)。"
  echo ""
  # Rule 3: full commit pipeline MUST target a COPY of the tab.
  # Production sheet id = 1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag
  PROD_SHEET_ID="1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
  if [[ -z "${GYMBRO_SHEET_ID:-}" ]]; then
    echo "❌ REFUSE: 冇 GYMBRO_SHEET_ID — 唔會對住 production 跑 AI/commit 測試。"
    echo "   (想睇 would-have-written 就用 GYMBRO_WRITE_MODE=dryrun,見 tests/qa/README.md)"
    exit 2
  fi
  if [[ "$GYMBRO_SHEET_ID" == "$PROD_SHEET_ID" ]]; then
    echo "❌ REFUSE: GYMBRO_SHEET_ID 指向 production sheet — 唔准。"
    exit 2
  fi
  export GYMBRO_WRITE_MODE=throwaway
  echo "✅ GYMBRO_WRITE_MODE=throwaway + GYMBRO_SHEET_ID=$GYMBRO_SHEET_ID"
fi

cd "$REPO"

declare -a QA_GROUPS=(
  "template:tests/qa/test_scan_progress_template.py"
  "freshness:tests/qa/test_server_freshness.py"
  "contract:tests/qa/test_scan_progress_contract.py"
  "frontend:tests/qa/test_scan_progress_frontend.py"
  "pollution:tests/qa/test_no_test_pollution.py"
  "writemode:tests/qa/test_write_mode_guard.py"
  "vision:tests/qa/test_vision_failure_guard.py"
  "aifallback:tests/qa/test_ai_provider_fallback.py"
  "rule25:tests/qa/test_generic_name_guard.py"
  "manualinput:tests/qa/test_manual_food_input.py"
  "bgparallel:tests/qa/test_background_parallel_scan.py"
  "bgdynamic:tests/qa/test_v3333_dynamic.py"
  "hygiene:tests/qa/test_repo_hygiene.py"
  "whoopfresh:tests/qa/test_whoop_freshness.py"
  "thumbfresh:tests/qa/test_thumb_url_freshness.py"
  "renamemacro:tests/qa/test_rename_macro_reestimate.py"
  "thumbintegrity:tests/qa/test_thumb_url_integrity.py"
  "meta:tests/qa/baseline_diff_meta_test.py"
)

echo "════════════════════════════════════════════════════════════"
echo "  GYMBRO SCAN-PROGRESS QA — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "════════════════════════════════════════════════════════════"

FAILED_GROUPS=()
declare -A RESULT

for entry in "${QA_GROUPS[@]}"; do
  name="${entry%%:*}"
  path="${entry#*:}"

  if [[ -n "$ONLY" && "$ONLY" != "$name" ]]; then
    continue
  fi

  echo ""
  echo "────────────────────────────────────────────────────────────"
  echo "▶ [$name] $path"
  echo "────────────────────────────────────────────────────────────"

  if [[ "$name" == "meta" ]]; then
    # meta-test 唔係 pytest,係獨立 script
    "$PY" "$path" 2>&1
    rc=$?
  else
    "$PY" -m pytest "$path" -v --tb=short -p no:randomly 2>&1
    rc=$?
  fi
  if [[ $rc -eq 0 ]]; then
    RESULT[$name]="PASS"
  else
    RESULT[$name]="FAIL(rc=$rc)"
    FAILED_GROUPS+=("$name")
  fi
done

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  QA SUMMARY"
echo "════════════════════════════════════════════════════════════"
for name in "${!RESULT[@]}"; do
  printf "  %-12s %s\n" "$name" "${RESULT[$name]}"
done
echo ""

if [[ ${#FAILED_GROUPS[@]} -gt 0 ]]; then
  echo "❌ 有組失敗: ${FAILED_GROUPS[*]}"
  exit 1
fi
echo "✅ 全部通過"
exit 0
