#!/usr/bin/env bash
# Tests for run-review-agent.sh's review configuration: by default the review
# runs with the routing and aliases the repo's own .fullsend/config.yaml gives
# the review agent, on a scratch copy of FULLSEND_DIR with the eval overlay's
# persona pins dropped; EVAL_CLAUDE_GENERATION (or the eval config's
# EVAL_CLAUDE_GENERATION_DEFAULT) pins the Claude aliases;
# EVAL_REVIEW_CONFIG=eval runs under the repo-root config as is. A stub
# run-fullsend.sh records which fullsend directory it was handed and what that
# directory's config says, and fakes a review result.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="${SCRIPT_DIR}/run-review-agent.sh"
FAILURES=0
TESTS=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for tool in yq jq rsync; do
  command -v "$tool" >/dev/null || { echo "SKIP: $tool not on PATH"; exit 0; }
done

pass() { TESTS=$((TESTS + 1)); echo "PASS: $1"; }
fail() { TESTS=$((TESTS + 1)); FAILURES=$((FAILURES + 1)); echo "FAIL: $1"; }

# A fake agents checkout: production config, eval workspace config, overlay.
FS="$TMP/agents"
mkdir -p "$FS/.fullsend" "$FS/eval/harness" "$FS/harness" "$FS/eval/runs/junk"
cat > "$FS/.fullsend/config.yaml" <<'YAML'
version: "1"
models:
  aliases:
    opus: claude-opus-5-5
    sonnet: claude-sonnet-5-5
agents:
  - name: code
    model: opus
  - name: review
    runtime: pi
    model: sonnet
    subagents:
      challenger: opus
      correctness: openai/gpt-6.1-sol
      docs-currency: google-vertex/gemini-3.8-flash
YAML
cat > "$FS/config.yaml" <<'YAML'
version: "1"
models:
  aliases:
    sonnet: claude-sonnet-5-5
    opus: claude-sonnet-5-5
agents:
  - source: harness/code.yaml
    model: sonnet
  - source: eval/harness/review.yaml
    model: sonnet
YAML
cat > "$FS/eval/harness/review.yaml" <<'YAML'
base: ../../harness/review.yaml
env:
  sandbox:
    ANTHROPIC_DEFAULT_OPUS_MODEL: claude-sonnet-5-5
    ANTHROPIC_DEFAULT_SONNET_MODEL: claude-sonnet-5-5
    KEEP_ME: yes
YAML
echo 'model: sonnet' > "$FS/harness/review.yaml"
echo junk > "$FS/eval/runs/junk/file"

# Stub run-fullsend.sh: record the config it would run under, fake a result.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/run-fullsend.sh" <<'STUB'
#!/usr/bin/env bash
# <agent> <workspace> <output-dir>
out="$3"; mkdir -p "$out/fs-rev-1/iteration-1/output"
cp "$FULLSEND_DIR/config.yaml" "$out/seen-config.yaml"
cp "$FULLSEND_DIR/eval/harness/review.yaml" "$out/seen-overlay.yaml"
echo "$FULLSEND_DIR" > "$out/seen-dir"
[[ -d "$FULLSEND_DIR/eval/runs/junk" ]] && echo yes > "$out/seen-runs" || echo no > "$out/seen-runs"
echo '{"action": "comment", "findings": [], "body": "ok"}' > "$out/fs-rev-1/iteration-1/output/agent-result.json"
echo '{"cost_usd": 1.0}' > "$out/metrics.json"
STUB
chmod +x "$TMP/bin/run-fullsend.sh"

new_ws() {
  local ws="$TMP/$1"; mkdir -p "$ws/output"
  echo '{"pr_found": true, "number": 7, "url": "https://x/pull/7"}' > "$ws/output/pr.json"
  echo "$ws"
}
run_hook() { # ws [VAR=value ...]
  local ws="$1"; shift
  RC=0
  OUT=$(env -i PATH="$TMP/bin:$PATH" HOME="$TMP" TMPDIR="$TMP" CASE_WORKSPACE="$ws" EPHEMERAL_REPO=o/r FULLSEND_DIR="$FS" "$@" bash "$HOOK" 2>&1) || RC=$?
}
seen() { yq -r "$2" "$1/output/review-run/seen-config.yaml"; }
cfg_of() { jq -r "$2" "$1/output/review-metrics.json"; }

# --- production (default) ---------------------------------------------------
WS=$(new_ws prod); run_hook "$WS"
if [[ $RC -eq 0 && "$(seen "$WS" '.agents[1].subagents.correctness')" == "openai/gpt-6.1-sol" \
      && "$(seen "$WS" '.models.aliases.opus')" == "claude-opus-5-5" && "$(seen "$WS" '.models.aliases.sonnet')" == "claude-sonnet-5-5" \
      && "$(yq -r '.env.sandbox.ANTHROPIC_DEFAULT_OPUS_MODEL // "absent"' "$WS/output/review-run/seen-overlay.yaml")" == "absent" \
      && "$(yq -r '.env.sandbox.KEEP_ME' "$WS/output/review-run/seen-overlay.yaml")" == "yes" \
      && "$(cat "$WS/output/review-run/seen-dir")" != "$FS" && "$(cat "$WS/output/review-run/seen-runs")" == "no" \
      && "$(cfg_of "$WS" .review_config.mode)" == "production" && "$(cfg_of "$WS" .review_config.subagents.challenger)" == "opus" \
      && "$(cfg_of "$WS" .review_config.aliases.opus)" == "claude-opus-5-5" && "$(jq -r .review_ran "$WS/output/review-result.json")" == "true" ]]; then
  pass "production mode: production's subagents and aliases applied on a scratch copy, overlay pins dropped, runs/ not copied, metrics record the config"
else
  fail "production mode (rc=$RC, config: $(tr '\n' ' ' < "$WS/output/review-run/seen-config.yaml" 2>/dev/null), metrics: $(cat "$WS/output/review-metrics.json" 2>/dev/null), out: $OUT)"
fi
if [[ "$(yq -r '.models.aliases.opus' "$FS/config.yaml")" == "claude-sonnet-5-5" && "$(yq -r '.env.sandbox.ANTHROPIC_DEFAULT_OPUS_MODEL' "$FS/eval/harness/review.yaml")" == "claude-sonnet-5-5" ]]; then
  pass "production mode leaves the real checkout untouched"
else
  fail "production mode leaves the real checkout untouched"
fi
if [[ "$OUT" == *"OPENAI_API_KEY is unset"* ]]; then
  pass "production mode warns when the routing needs an OpenAI key that is not set"
else
  fail "production mode warns when the routing needs an OpenAI key that is not set (out: $OUT)"
fi

WS=$(new_ws prod-key); run_hook "$WS" OPENAI_API_KEY=test-key
if [[ $RC -eq 0 && "$OUT" != *"OPENAI_API_KEY is unset"* ]]; then
  pass "no key warning when OPENAI_API_KEY is set"
else
  fail "no key warning when OPENAI_API_KEY is set (rc=$RC, out: $OUT)"
fi

# --- alias pins -------------------------------------------------------------
WS=$(new_ws gen5); run_hook "$WS" EVAL_CLAUDE_GENERATION=5 OPENAI_API_KEY=k
if [[ $RC -eq 0 && "$(seen "$WS" '.models.aliases.opus')" == "claude-opus-5" && "$(seen "$WS" '.models.aliases.sonnet')" == "claude-sonnet-5" \
      && "$(seen "$WS" '.agents[1].subagents.challenger')" == "opus" && "$(cfg_of "$WS" .review_config.aliases.sonnet)" == "claude-sonnet-5" ]]; then
  pass "EVAL_CLAUDE_GENERATION=5 pins both aliases one generation back and keeps the routing"
else
  fail "EVAL_CLAUDE_GENERATION=5 pins both aliases (rc=$RC, config: $(tr '\n' ' ' < "$WS/output/review-run/seen-config.yaml" 2>/dev/null), out: $OUT)"
fi

WS=$(new_ws gendefault); run_hook "$WS" EVAL_CLAUDE_GENERATION_DEFAULT=5 OPENAI_API_KEY=k
if [[ $RC -eq 0 && "$(seen "$WS" '.models.aliases.opus')" == "claude-opus-5" && "$(cfg_of "$WS" .review_config.claude_generation_pin)" == "5" ]]; then
  pass "EVAL_CLAUDE_GENERATION_DEFAULT from the eval config pins the aliases when no explicit generation is given"
else
  fail "EVAL_CLAUDE_GENERATION_DEFAULT pins the aliases (rc=$RC, config: $(tr '\n' ' ' < "$WS/output/review-run/seen-config.yaml" 2>/dev/null), out: $OUT)"
fi

WS=$(new_ws genoverride); run_hook "$WS" EVAL_CLAUDE_GENERATION_DEFAULT=5 EVAL_CLAUDE_GENERATION=5-5 OPENAI_API_KEY=k
if [[ $RC -eq 0 && "$(seen "$WS" '.models.aliases.opus')" == "claude-opus-5-5" && "$(cfg_of "$WS" .review_config.claude_generation_pin)" == "5-5" ]]; then
  pass "an explicit EVAL_CLAUDE_GENERATION wins over the eval config's default"
else
  fail "an explicit EVAL_CLAUDE_GENERATION wins over the default (rc=$RC, config: $(tr '\n' ' ' < "$WS/output/review-run/seen-config.yaml" 2>/dev/null), out: $OUT)"
fi

cp "$FS/config.yaml" "$FS/config.yaml.keep"; yq -i '.agents[1].source = "harness/review.yaml"' "$FS/config.yaml"
WS=$(new_ws noentry); run_hook "$WS"
if [[ $RC -eq 0 && "$(jq -r .review_ran "$WS/output/review-result.json")" == "false" && "$(jq -r .reason "$WS/output/review-result.json")" == *"no agents entry with source eval/harness/review.yaml"* ]]; then
  pass "a root config that registers the review agent another way is refused rather than run unrouted"
else
  fail "a root config without the eval review entry is refused (rc=$RC, result: $(cat "$WS/output/review-result.json" 2>/dev/null), out: $OUT)"
fi
mv "$FS/config.yaml.keep" "$FS/config.yaml"

WS=$(new_ws badgen); run_hook "$WS" EVAL_CLAUDE_GENERATION='5; rm -rf x'
if [[ $RC -ne 0 && "$OUT" == *"EVAL_CLAUDE_GENERATION must look like"* ]]; then
  pass "a malformed EVAL_CLAUDE_GENERATION is refused"
else
  fail "a malformed EVAL_CLAUDE_GENERATION is refused (rc=$RC, out: $OUT)"
fi

# --- eval mode and bad modes ------------------------------------------------
WS=$(new_ws evalmode); run_hook "$WS" EVAL_REVIEW_CONFIG=eval
if [[ $RC -eq 0 && "$(cat "$WS/output/review-run/seen-dir")" == "$FS" && "$(seen "$WS" '.models.aliases.opus')" == "claude-sonnet-5-5" \
      && "$(seen "$WS" '.agents[1].subagents // "none"')" == "none" && "$(cfg_of "$WS" .review_config.mode)" == "eval" ]]; then
  pass "eval mode runs under the repo-root config as is"
else
  fail "eval mode runs under the repo-root config as is (rc=$RC, out: $OUT)"
fi

WS=$(new_ws badmode); run_hook "$WS" EVAL_REVIEW_CONFIG=whatever
if [[ $RC -ne 0 && "$OUT" == *"must be 'production' or 'eval'"* ]]; then
  pass "an unknown EVAL_REVIEW_CONFIG is refused"
else
  fail "an unknown EVAL_REVIEW_CONFIG is refused (rc=$RC, out: $OUT)"
fi

mv "$FS/.fullsend/config.yaml" "$FS/.fullsend/config.yaml.away"
WS=$(new_ws noprod); run_hook "$WS"
if [[ $RC -eq 0 && "$(jq -r .review_ran "$WS/output/review-result.json")" == "false" && "$(jq -r .reason "$WS/output/review-result.json")" == *"is missing"* ]]; then
  pass "production mode without a production config records the review as not run, with the reason"
else
  fail "production mode without a production config records the review as not run (rc=$RC, result: $(cat "$WS/output/review-result.json" 2>/dev/null), out: $OUT)"
fi
mv "$FS/.fullsend/config.yaml.away" "$FS/.fullsend/config.yaml"

# --- a capture failure skips the review --------------------------------------
WS=$(new_ws capture-failed)
echo '{"reason": "could not clone the PR head", "hook": "capture-pr-artifacts.py"}' > "$WS/output/infrastructure-failure.json"
run_hook "$WS"
if [[ $RC -eq 0 && "$(jq -r .review_ran "$WS/output/review-result.json")" == "false" \
      && "$(jq -r .reason "$WS/output/review-result.json")" == *"capture failed (could not clone the PR head)"* \
      && ! -d "$WS/output/review-run" ]] && grep -q "The review did not run" "$WS/judge/review.md"; then
  pass "a case the capture hook marked as a capture failure is not reviewed, and the reason is recorded"
else
  fail "a case the capture hook marked as a capture failure is not reviewed (rc=$RC, result=$(cat "$WS/output/review-result.json" 2>/dev/null), out: $OUT)"
fi

# A capture failure before the outcome leaves no pr.json; the marker's reason wins.
WS="$TMP/capture-failed-early"; mkdir -p "$WS/output"
echo '{"reason": "fixture-state.json missing; capture-fixture.sh did not run", "hook": "capture-pr-artifacts.py"}' \
  > "$WS/output/infrastructure-failure.json"
run_hook "$WS"
if [[ $RC -eq 0 && "$(jq -r .review_ran "$WS/output/review-result.json")" == "false" \
      && "$(jq -r .reason "$WS/output/review-result.json")" == *"capture failed (fixture-state.json missing"* ]]; then
  pass "a capture failure before the outcome (no pr.json) is recorded with the marker's reason, not as a missing pr.json"
else
  fail "a capture failure before the outcome is recorded with the marker's reason (rc=$RC, result=$(cat "$WS/output/review-result.json" 2>/dev/null), out: $OUT)"
fi

echo ""
echo "=== $TESTS tests, $FAILURES failures ==="
[[ $FAILURES -eq 0 ]]
