#!/usr/bin/env bash
# after_each hook (eval/local/code-rhai): run the fullsend review agent on the PR the
# code agent opened, without posting anything, and keep its findings for the
# quality judge. Runs after capture-pr-artifacts.sh and before teardown.
#
# The review step is held constant across arms, whatever the code agent ran on
# (EVAL_RUNTIME/EVAL_MODEL), and by default it is the review agent as the
# agents repo configures it for itself (EVAL_REVIEW_CONFIG=production): the
# pi runtime, the parent on the `sonnet` alias, and the persona sub-agents on
# the models the repo's own .fullsend/config.yaml `subagents` block gives them,
# with that file's Claude aliases. Those are read from the production config
# at run time, so there is no second copy to drift, and applied to a scratch
# copy of FULLSEND_DIR whose eval-only review overlay loses its persona pins.
# A project that does not serve the production generation pins the aliases
# with EVAL_CLAUDE_GENERATION (e.g. 5 -> claude-opus-5 / claude-sonnet-5) or
# EVAL_ALIAS_OPUS / EVAL_ALIAS_SONNET; that is the one deviation the recorded
# runs made. EVAL_REVIEW_CONFIG=eval runs under the repo-root config.yaml as
# is (a Claude-only review, no persona routing). EVAL_REVIEW_RUNTIME /
# EVAL_REVIEW_MODEL / EVAL_REVIEW_EFFORT override the parent's runtime, model
# and effort. output/review-metrics.json records the mode, the aliases and
# the routing that were applied, and per_model_usage records what ran.
#
# Writes:
#   output/review-result.json   the review agent's agent-result.json
#                               (findings, risk_assessment, body, action) or
#                               {review_ran: false, reason}
#   output/review-metrics.json  the review run's metrics.json (kept apart from
#                               the code run's metrics.json so cost judges are
#                               not polluted); includes the agents-repo commit
#                               so the review agent version is on record
#   output/review-run/          the review run's own output tree
set -euo pipefail

CASE_WORKSPACE="${CASE_WORKSPACE:?CASE_WORKSPACE is required}"
EPHEMERAL_REPO="${EPHEMERAL_REPO:?EPHEMERAL_REPO is required}"
FULLSEND_DIR="${FULLSEND_DIR:?FULLSEND_DIR is required}"
OUTPUT_DIR="${CASE_WORKSPACE}/output"
JUDGE_DIR="${CASE_WORKSPACE}/judge"
PR_JSON="${OUTPUT_DIR}/pr.json"
mkdir -p "$OUTPUT_DIR" "$JUDGE_DIR"

# Plain-text rendering of the findings for the agent judge (judge/review.md).
render_review() {
  local f="$OUTPUT_DIR/review-result.json"
  {
    echo "# Independent automated code review of the PR"; echo
    echo "Produced by the fullsend review agent, which read the repository, the diff and"
    echo "the linked issue. Evidence to verify, not a verdict."; echo
    if [[ "$(jq -r '.review_ran // false' "$f")" != "true" ]]; then
      echo "The review did not run: $(jq -r '.reason // "unknown"' "$f")"
    else
      echo "Recommended action: $(jq -r '.action // "n/a"' "$f")"
      echo "Risk assessment: $(jq -c '.risk_assessment // "n/a"' "$f")"; echo
      echo "## Findings ($(jq '.findings // [] | length' "$f"))"; echo
      jq -r '(.findings // []) | to_entries[] | .value as $x | "\(.key+1). [\($x.severity // "?")/\($x.category // "-")] \($x.file // $x.path // "")\(if ($x.line // null) then ":" + ($x.line|tostring) else "" end)\n   \($x.description // $x.body // $x.message // "")\n   Suggested fix: \($x.remediation // "n/a")\n"' "$f"
      echo "## Review summary"; echo
      jq -r '.body // "(none)"' "$f"
    fi
  } > "$JUDGE_DIR/review.md"
}

skip() { jq -n --arg r "$1" '{review_ran: false, reason: $r}' > "$OUTPUT_DIR/review-result.json"; render_review; echo "review agent skipped: $1"; exit 0; }
[[ "${EVAL_SKIP_REVIEW:-}" == "1" ]] && skip "EVAL_SKIP_REVIEW=1"
[[ -f "$PR_JSON" ]] || skip "pr.json missing; capture-pr-artifacts.sh did not run"
[[ "$(jq -r '.pr_found // false' "$PR_JSON")" == "true" ]] || skip "no PR to review"
num=$(jq -r .number "$PR_JSON"); url=$(jq -r .url "$PR_JSON")

# --- Review configuration: production routing on a scratch copy, or the eval config as is ---
REVIEW_MODE="${EVAL_REVIEW_CONFIG:-production}"
REVIEW_FULLSEND_DIR="$FULLSEND_DIR"
review_aliases='{}'; review_subagents='{}'
case "$REVIEW_MODE" in
  eval) ;;
  production)
    prod_cfg="$FULLSEND_DIR/.fullsend/config.yaml"
    [[ -f "$prod_cfg" ]] || skip "EVAL_REVIEW_CONFIG=production but $prod_cfg is missing"
    [[ "$(yq '[.agents[] | select(.name == "review")] | length' "$prod_cfg")" == "1" ]] \
      || skip "EVAL_REVIEW_CONFIG=production but $prod_cfg has no review agent entry"
    REVIEW_FULLSEND_DIR=$(mktemp -d "${TMPDIR:-/tmp}/eval-review-cfg-XXXXXX")
    cleanup_review_cfg() {
      # shellcheck disable=SC2317 # invoked indirectly via trap
      rm -rf "$REVIEW_FULLSEND_DIR"
    }
    trap cleanup_review_cfg EXIT
    rsync -a --exclude .git --exclude 'eval/runs' --exclude 'eval/local/*/cases/*/repo' "$FULLSEND_DIR/" "$REVIEW_FULLSEND_DIR/"
    cfg="$REVIEW_FULLSEND_DIR/config.yaml"
    # Production's routing and aliases, from production's own file.
    yq '.agents[] | select(.name == "review") | .subagents // {}' "$prod_cfg" > "$REVIEW_FULLSEND_DIR/.review-subagents.yaml"
    yq '.models.aliases // {}' "$prod_cfg" > "$REVIEW_FULLSEND_DIR/.review-aliases.yaml"
    SUB="$REVIEW_FULLSEND_DIR/.review-subagents.yaml" ALI="$REVIEW_FULLSEND_DIR/.review-aliases.yaml" \
      yq -i '(.agents[] | select(.source == "eval/harness/review.yaml")).subagents = load(strenv(SUB))
             | .models.aliases = (.models.aliases // {}) * load(strenv(ALI))' "$cfg"
    # The eval-only overlay pins the personas' Claude models; production has no such pin.
    [[ -f "$REVIEW_FULLSEND_DIR/eval/harness/review.yaml" ]] \
      && yq -i 'del(.env.sandbox.ANTHROPIC_DEFAULT_OPUS_MODEL) | del(.env.sandbox.ANTHROPIC_DEFAULT_SONNET_MODEL)' "$REVIEW_FULLSEND_DIR/eval/harness/review.yaml"
    # The one supported deviation: a project that does not serve production's
    # Claude generation pins the aliases to one it does.
    if [[ -n "${EVAL_CLAUDE_GENERATION:-}" ]]; then
      [[ "$EVAL_CLAUDE_GENERATION" =~ ^[0-9]+(-[0-9]+)?$ ]] || { echo "ERROR: EVAL_CLAUDE_GENERATION must look like 5 or 5-5, got '$EVAL_CLAUDE_GENERATION'" >&2; exit 1; }
      G="$EVAL_CLAUDE_GENERATION" yq -i '.models.aliases.opus = "claude-opus-" + env(G) | .models.aliases.sonnet = "claude-sonnet-" + env(G)' "$cfg"
    fi
    [[ -n "${EVAL_ALIAS_OPUS:-}" ]] && A="$EVAL_ALIAS_OPUS" yq -i '.models.aliases.opus = env(A)' "$cfg"
    [[ -n "${EVAL_ALIAS_SONNET:-}" ]] && A="$EVAL_ALIAS_SONNET" yq -i '.models.aliases.sonnet = env(A)' "$cfg"
    rm -f "$REVIEW_FULLSEND_DIR/.review-subagents.yaml" "$REVIEW_FULLSEND_DIR/.review-aliases.yaml"
    review_aliases=$(yq -o=json -I=0 '.models.aliases // {}' "$cfg")
    review_subagents=$(yq -o=json -I=0 '(.agents[] | select(.source == "eval/harness/review.yaml")).subagents // {}' "$cfg")
    if echo "$review_subagents" | grep -q '"openai/' && [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "WARNING: the production routing sends personas to OpenAI models but OPENAI_API_KEY is unset; those personas will fail (zero tokens in review-metrics.json)" >&2
    fi
    echo "review config: production routing from $prod_cfg; aliases $review_aliases"
    ;;
  *) echo "ERROR: EVAL_REVIEW_CONFIG must be 'production' or 'eval', got '$REVIEW_MODE'" >&2; exit 1 ;;
esac

REVIEW_OUT="${OUTPUT_DIR}/review-run"
mkdir -p "$REVIEW_OUT"
rc=0
FIXTURE_TYPE=pull_request FIXTURE_URL="$url" FIXTURE_NUMBER="$num" \
EPHEMERAL_REPO="$EPHEMERAL_REPO" EVAL_NO_POST_SCRIPT=1 FULLSEND_DIR="$REVIEW_FULLSEND_DIR" \
EVAL_RUNTIME="${EVAL_REVIEW_RUNTIME:-pi}" \
EVAL_MODEL="${EVAL_REVIEW_MODEL:-sonnet}" EVAL_EFFORT="${EVAL_REVIEW_EFFORT:-}" \
EVAL_TIMEOUT="${EVAL_REVIEW_TIMEOUT:-2700}" \
  run-fullsend.sh review "$CASE_WORKSPACE" "$REVIEW_OUT" || rc=$?
echo "review agent run exit: $rc"

result=$(find "$REVIEW_OUT" -maxdepth 4 -path '*/iteration-*/output/agent-result.json' | sort -V | tail -1)
if [[ -n "$result" ]]; then
  # A result file is not a review: the schema's action enum includes "failure".
  jq --argjson rc "$rc" '. + {exit_code: $rc,
        review_ran: ((.action // "") as $a | ($a != "" and $a != "failure")),
        reason: (if (.action // "") == "failure" then "review agent reported failure"
                 elif (.action // "") == "" then "agent-result.json has no action" else null end)}' \
    "$result" > "$OUTPUT_DIR/review-result.json"
else
  jq -n --argjson rc "$rc" '{review_ran: false, reason: "no agent-result.json from the review run", exit_code: $rc}' > "$OUTPUT_DIR/review-result.json"
fi
agents_commit=$(git -C "$FULLSEND_DIR" rev-parse HEAD 2>/dev/null || echo unknown)
review_config=$(jq -n -c --arg m "$REVIEW_MODE" --argjson a "$review_aliases" --argjson s "$review_subagents" \
  --arg rt "${EVAL_REVIEW_RUNTIME:-pi}" --arg pm "${EVAL_REVIEW_MODEL:-sonnet}" \
  '{mode: $m, parent_runtime: $rt, parent_model: $pm, aliases: $a, subagents: $s}')
if [[ -f "$REVIEW_OUT/metrics.json" ]]; then
  jq --arg c "$agents_commit" --argjson rc "$review_config" '. + {agents_repo_commit: $c, review_config: $rc}' "$REVIEW_OUT/metrics.json" > "$OUTPUT_DIR/review-metrics.json"
else
  jq -n --arg c "$agents_commit" --argjson rc "$rc" --argjson cfg "$review_config" '{agents_repo_commit: $c, exit_code: $rc, metrics_missing: true, review_config: $cfg}' > "$OUTPUT_DIR/review-metrics.json"
fi
render_review
echo "Saved review findings -> ${OUTPUT_DIR}/review-result.json and ${JUDGE_DIR}/review.md"
