# Line endings are handled by this repo's .gitattributes. Bootstrap intentionally
# avoids changing user-level Git configuration.

# Parse flags and positional args:
#   [UPSTREAM]          positional, user/repo form (overrides env and persisted)
#   --rule-packs PACK   dry helper: print agent-config.yaml snippet and exit
#   --no-cache          force rule-pack refetch on this run (opt-in path only)
#   --help | -h         show usage
_POS_UPSTREAM=""
RULE_PACKS_DRY=""
NO_CACHE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rule-packs)
      if [ -z "${2:-}" ]; then
        echo "error: --rule-packs requires a pack name" >&2
        exit 1
      fi
      if [ -n "$RULE_PACKS_DRY" ]; then
        echo "warning: --rule-packs specified multiple times; last value wins" >&2
      fi
      RULE_PACKS_DRY="$2"
      shift 2
      ;;
    --rule-packs=*)
      _rp_val="${1#--rule-packs=}"
      if [ -z "$_rp_val" ]; then
        echo "error: --rule-packs requires a pack name" >&2
        exit 1
      fi
      if [ -n "$RULE_PACKS_DRY" ]; then
        echo "warning: --rule-packs specified multiple times; last value wins" >&2
      fi
      RULE_PACKS_DRY="$_rp_val"
      shift
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --help|-h)
      cat <<'EOF'
Usage: bash bootstrap.sh [UPSTREAM] [--rule-packs PACK] [--no-cache]
  UPSTREAM        user/repo form; overrides AGENT_CONFIG_UPSTREAM env and persisted file
  --rule-packs P  print agent-config.yaml snippet for pack P and exit (dry helper)
  --no-cache      force refetch of rule-pack content on this run
  -h, --help      show this help
EOF
      exit 0
      ;;
    --*)
      echo "error: unknown flag: $1" >&2
      echo "supported: --rule-packs PACK, --no-cache, --help" >&2
      exit 1
      ;;
    *)
      if [ -z "$_POS_UPSTREAM" ]; then
        _POS_UPSTREAM="$1"
      fi
      shift
      ;;
  esac
done

# Dry helper: --rule-packs prints a YAML snippet and exits without running
# bootstrap. The flag wins when both --rule-packs and AGENT_CONFIG_RULE_PACKS
# are set simultaneously.
if [ -n "$RULE_PACKS_DRY" ]; then
  if [ -n "${AGENT_CONFIG_RULE_PACKS:-}" ]; then
    echo "notice: --rule-packs is a dry helper; AGENT_CONFIG_RULE_PACKS env var is ignored in this mode" >&2
  fi
  cat <<EOF
Add the following to agent-config.yaml at your project root, then run bootstrap again to apply:

  rule_packs:
    - name: $RULE_PACKS_DRY
      # Optional: pin to a specific ref (defaults to manifest's default-ref)
      # ref: v0.3.5

After committing agent-config.yaml, run:

  bash bootstrap.sh
EOF
  exit 0
fi


# Find a real Python interpreter, avoiding the Windows Store App Execution
# Alias shim under %LOCALAPPDATA%\Microsoft\WindowsApps\ (which prints
# "Python was not found; install from Store" and exits non-zero on call).
# Order: env override > deployed wrapper > sparse-clone wrapper > PATH lookup
# with shim skip. See https://github.com/yzhao062/anywhere-agents/issues/2.
# Exit status alone is not proof that Python ran. A command that ignores its
# arguments and exits 0, such as `true` or a .cmd containing only `exit /b 0`,
# passes an exit-status probe while executing nothing. Require the interpreter
# to echo a sentinel, so only something that actually evaluated the -c program
# is accepted.
_python_runs() {
  _probe_output="$("$1" -c 'import sys; sys.stdout.write("__ANYWHERE_AGENTS_PY3__" if sys.version_info[0] >= 3 else "")' 2>/dev/null)" || return 1
  [ "$_probe_output" = "__ANYWHERE_AGENTS_PY3__" ]
}

_find_python() {
  if [ -n "${ANYWHERE_AGENTS_PYTHON:-}" ]; then
    if [ -x "$ANYWHERE_AGENTS_PYTHON" ] && _python_runs "$ANYWHERE_AGENTS_PYTHON"; then
      echo "$ANYWHERE_AGENTS_PYTHON"; return 0
    fi
    printf '%s\n' "[anywhere-agents] ANYWHERE_AGENTS_PYTHON did not execute Python 3 successfully: $ANYWHERE_AGENTS_PYTHON; trying automatic discovery." >&2
  fi
  if [ -x "$HOME/.claude/hooks/_python" ] && _python_runs "$HOME/.claude/hooks/_python"; then
    echo "$HOME/.claude/hooks/_python"; return 0
  fi
  if [ -x ".agent-config/repo/scripts/_python" ] && _python_runs ".agent-config/repo/scripts/_python"; then
    echo ".agent-config/repo/scripts/_python"; return 0
  fi
  for cmd in python3 python; do
    while IFS= read -r candidate; do
      [ -n "$candidate" ] || continue
      resolved="$candidate"
      if command -v readlink >/dev/null 2>&1; then
        maybe=$(readlink -f "$candidate" 2>/dev/null || true)
        [ -n "$maybe" ] && resolved="$maybe"
      fi
      case "$resolved" in
        *WindowsApps*|*windowsapps*) continue ;;
      esac
      if _python_runs "$candidate"; then
        echo "$candidate"; return 0
      fi
    done < <(type -a -p "$cmd" 2>/dev/null || true)
  done
  return 1
}

# Read the passive-pack selection from one config layer without Python or
# PyYAML. The return value is one of: none, empty, nonempty.
#
# `packs:` is the canonical key and `rule_packs:` the deprecated alias, and
# within one file the resolver prefers the canonical one. A pre-parser that
# knew only the alias read a consumer on the canonical key as having no
# selection at all, which is the answer that both deletes composed pack blocks
# and freezes opted-out ones. See scripts/packs/config.py.
_rule_packs_config_state() {
  _rp_state_path=$1
  [ -f "$_rp_state_path" ] || { printf '%s' none; return 0; }
  _rp_canonical=$(_rule_packs_key_state "$_rp_state_path" packs)
  if [ "$_rp_canonical" != none ]; then
    printf '%s' "$_rp_canonical"
    return 0
  fi
  _rule_packs_key_state "$_rp_state_path" rule_packs
}

# Print the value that follows a top-level `<key>:` on this line and return 0,
# for every spelling YAML gives that key: bare, single- or double-quoted, and
# with whitespace before the colon. Return 1 otherwise.
#
# Matching only the bare spelling answered `none` for `"packs": [agent-style]`
# and `packs : [agent-style]`, which the resolver reads as a selection. After a
# clear in an earlier layer that answer deleted the block the later layer had
# just asked for. A leading space means a nested key, which is a different key.
_rule_packs_key_tail() {
  _kt_line=$1
  _kt_key=$2
  case "$_kt_line" in
    [[:space:]]*) return 1 ;;
    *:*) ;;
    *) return 1 ;;
  esac
  _kt_head=${_kt_line%%:*}
  _kt_head=${_kt_head//[[:space:]]/}
  case "$_kt_head" in
    '"'*'"') _kt_head=${_kt_head#\"}; _kt_head=${_kt_head%\"} ;;
    "'"*"'") _kt_head=${_kt_head#\'}; _kt_head=${_kt_head%\'} ;;
  esac
  [ "$_kt_head" = "$_kt_key" ] || return 1
  printf '%s' "${_kt_line#*:}"
}

# The single-key scanner behind _rule_packs_config_state.
_rule_packs_key_state() {
  _rp_state_path=$1
  _rp_key=$2
  [ -f "$_rp_state_path" ] || { printf '%s' none; return 0; }
  _rp_state_found=false
  _rp_state_in_list=false
  while IFS= read -r _rp_state_line || [ -n "$_rp_state_line" ]; do
    _rp_state_line=${_rp_state_line%$'\r'}
    if _rp_state_tail=$(_rule_packs_key_tail "$_rp_state_line" "$_rp_key"); then
      _rp_state_found=true
      _rp_state_in_list=true
      _rp_state_tail=${_rp_state_tail%%#*}
      _rp_state_compact=${_rp_state_tail//[[:space:]]/}
      case "$_rp_state_compact" in
        ''|'[]'|[Nn][Uu][Ll][Ll]|'~') ;;
        *) printf '%s' nonempty; return 0 ;;
      esac
    elif $_rp_state_in_list; then
      # A block sequence may sit at the same indentation as its key. That is
      # what PyYAML's safe_dump emits, and anywhere-agents writes this file
      # with safe_dump, so the zero-indent shape is the common one rather than
      # an edge case. Requiring indentation here read every such file as an
      # empty list, which is the explicit opt-out.
      _rp_state_compact=${_rp_state_line//[[:space:]]/}
      case "$_rp_state_compact" in
        '') continue ;;
        '#'*) continue ;;
        -*) printf '%s' nonempty; return 0 ;;
        # The three spellings of an empty value, which the resolver reads as an
        # explicit clear wherever they sit. The key line already accepts them;
        # an indented node has to as well, or the opt-out stops working the
        # moment it is written on its own line.
        '[]'|[Nn][Uu][Ll][Ll]|'~') continue ;;
      esac
      case "$_rp_state_line" in
        # An indented node belongs to the key. `[agent-style]` on its own line
        # is valid YAML and resolves to a nonempty list, and skipping it read
        # the file as the explicit opt-out and deleted every pack block, under
        # all three entry points. Anything indented that is not a proven empty
        # list therefore counts as a selection: this parser runs only when the
        # real YAML reader is unavailable, so an uncertain answer has to
        # preserve rather than delete.
        [[:space:]]*) printf '%s' nonempty; return 0 ;;
        *) break ;;
      esac
    fi
  done < "$_rp_state_path"
  if $_rp_state_found; then
    printf '%s' empty
  else
    printf '%s' none
  fi
  return 0
}

# Report whether the AGENTS.md already on disk is a composed artifact. The
# composer stamps `<!-- rule-pack:<name>:begin ... -->` above each pack block,
# so that marker is the one signal available without re-running composition.
# The skipped-composition marker reads `rule-pack composition skipped`, with a
# space rather than a colon after `rule-pack`, so it does not match here.
_agents_md_is_composed() {
  [ -f AGENTS.md ] || return 1
  # A complete marker line, not a prefix. `[^ :]*` rejected a pack name
  # carrying a space or a colon, which the manifest allows, so an authentic
  # artifact was discarded and its packs deleted. It also accepted
  # `:begin-fake` and a truncated `:begin`, which froze a consumer on an
  # un-composed file. The trailing space class tolerates CRLF.
  #
  # The version field is any non-whitespace run. `[A-Za-z0-9._/-]+` was copied
  # from the legacy raw-URL validator, but the v2 schema accepts any nonempty
  # `source.ref` and the composer formats it straight into the marker. A ref
  # carrying SemVer build metadata, `v1.2.3+build.7`, is valid and produced a
  # marker this predicate called plain, which deletes the pack block it was
  # written to protect.
  grep -qE '^<!-- rule-pack:.+:begin version=[^[:space:]]+ sha256=[[:xdigit:]]{64} -->[[:space:]]*$' AGENTS.md 2>/dev/null
}

# Append one entry to .gitignore, once. A file whose last byte is not a newline
# gets one first: `echo x >> f` would otherwise glue the entry onto the last
# existing rule, breaking that rule and leaving the new entry unmatchable.
_gitignore_append() {
  if [ -s .gitignore ] && [ -n "$(tail -c 1 .gitignore)" ]; then
    printf '\n' >> .gitignore
  fi
  printf '%s\n' "$1" >> .gitignore
}

_gitignore_add() {
  _gi_pattern=$1
  _gi_line=$2
  if [ -f .gitignore ] && grep -qE "$_gi_pattern" .gitignore; then
    return 0
  fi
  _gitignore_append "$_gi_line"
}

# Report whether git already tracks a path in this repo.
_git_tracks() {
  git ls-files --error-unmatch -- "$1" >/dev/null 2>&1
}

# Where the user-level config layer lives, mirroring config.user_config_home.
# That function branches on the platform rather than cascading: Windows reads
# %APPDATA% and stops, POSIX reads $XDG_CONFIG_HOME then $HOME/.config and
# never looks at %APPDATA%. A cascade over all three disagreed with it in six
# of fourteen environment shapes, and pointed this layer at a file the resolver
# does not read. Git Bash sets both %APPDATA% and $HOME, so the disagreement
# was reachable on the one platform both entry points share.
#
# The branch reads $OSTYPE, which bash sets itself, rather than shelling out to
# `uname`. A missing PATH entry is exactly how the earlier `tr` defect reached
# a consumer, and a `uname` that cannot run would drop Windows onto the POSIX
# branch silently. `msys` is Git Bash, whose Python reports win32. `cygwin` and
# WSL stay on the POSIX branch, because their Python reports cygwin and linux
# and the resolver reads $XDG_CONFIG_HOME for both. Prints nothing when nothing
# resolves, which the caller treats as no layer.
_user_config_path() {
  # Windows when the shell is an MSYS, MinGW or Cygwin build, or when the
  # process carries the environment Windows installs. $OSTYPE alone is not
  # enough: bash sets it with set_if_not, so an OSTYPE already exported in the
  # environment survives, and one of GitHub's Windows runners exports a value
  # that is not msys, which sent every case of the layer-path test down the
  # POSIX branch. $SYSTEMROOT and $WINDIR come from Windows rather than from
  # the shell, and neither is set on Linux, on macOS, or under WSL, where the
  # POSIX answer is the correct one anyway.
  _ucp_windows=false
  case "${OSTYPE:-}" in
    msys*|mingw*|cygwin*|win32*) _ucp_windows=true ;;
  esac
  if [ -n "${SYSTEMROOT:-}" ] || [ -n "${WINDIR:-}" ]; then
    _ucp_windows=true
  fi
  if $_ucp_windows; then
    if [ -n "${APPDATA:-}" ]; then
      printf '%s' "$APPDATA/anywhere-agents/config.yaml"
    fi
    return 0
  fi
  if [ -n "${XDG_CONFIG_HOME:-}" ]; then
    printf '%s' "$XDG_CONFIG_HOME/anywhere-agents/config.yaml"
  elif [ -n "${HOME:-}" ]; then
    printf '%s' "$HOME/.config/anywhere-agents/config.yaml"
  fi
}

# True when the environment overlay names at least one pack to ADD. The overlay
# grammar is additive with a `-name` subtract form, so a value made only of
# subtractions adds nothing and must not be read as a selection.
#
# config.parse_env_var splits on commas alone and strips each token, while this
# splits on whitespace too, so `-a b` is one subtraction there and one
# subtraction plus one addition here. Trimming a token without an external
# utility is what the `tr` defect was made of, and the difference only ever
# reads an overlay as adding when the resolver would not, which preserves a
# file rather than deleting one.
_env_pack_selection_adds() {
  _rp_env=${AGENT_CONFIG_PACKS:-}
  if [ -z "$_rp_env" ]; then
    _rp_env=${AGENT_CONFIG_RULE_PACKS:-}
  fi
  [ -n "$_rp_env" ] || return 1
  _rp_env=${_rp_env//,/ }
  for _rp_entry in $_rp_env; do
    case "$_rp_entry" in
      # config.parse_env_var rejects a bare `-` and any entry carrying `/`, `@`
      # or `:`, so with Python present the whole run fails and the artifact is
      # untouched. Read as a plain subtraction here, the same value cleared the
      # selection and deleted the artifact instead. An invalid overlay is
      # uncertainty, and uncertainty preserves.
      -|-*[/@:]*) return 0 ;;
      -*)
        # A whitelist rather than a longer reject list. The resolver strips
        # each token with Python's str.strip() before validating, so `-\r`
        # arrives there as a bare `-` and is rejected while reaching this loop
        # intact. Enumerating separators loses that race every time one is
        # missed: bash does not split on \r at all, and the two PowerShell
        # editions disagree about whether `\s` covers U+001C. Anything outside
        # the characters a pack name is made of counts as uncertainty.
        _rp_name=${_rp_entry#-}
        case "$_rp_name" in
          *[!A-Za-z0-9._-]*) return 0 ;;
        esac
        ;;
      '') ;;
      *) return 0 ;;
    esac
  done
  return 1
}

# True when a line in this file is one the scanner above cannot classify. That
# is what the layer fold means by uncertainty, and file length is not it: a
# file of comments, or one holding only keys that are not this one, says
# nothing this scanner misread, and counting those overrode opt-outs the
# operator meant. Four shapes are readable, and everything else is not:
#
#   blank or comment            nothing to read
#   indented                    a continuation of the line above
#   zero-indent `- `            a block sequence item
#   zero-indent `<name>:`       a top-level key, quoted or not
#
# The head test is what leaves `{packs: [agent-style]}` unreadable. A root-level
# flow mapping is a selection to the resolver and not a key to this, so it has
# to land on the preserving side rather than be waved through as a key line.
_file_has_unreadable_line() {
  [ -s "$1" ] || return 1
  _fu_seen_top_level=false
  while IFS= read -r _fu_line || [ -n "$_fu_line" ]; do
    _fu_line=${_fu_line%$'\r'}
    _fu_compact=${_fu_line//[[:space:]]/}
    case "$_fu_compact" in
      ''|'#'*) continue ;;
    esac
    case "$_fu_line" in
      # A continuation of the line above, which needs a line above it. A file
      # whose first content line is indented is a mapping to YAML and a nested
      # key to this scanner, so it is unreadable rather than a continuation.
      [[:space:]]*)
        if $_fu_seen_top_level; then
          continue
        fi
        return 0
        ;;
    esac
    _fu_seen_top_level=true
    case "$_fu_compact" in
      -*) continue ;;
    esac
    case "$_fu_line" in
      *:*) ;;
      *) return 0 ;;
    esac
    _fu_head=${_fu_line%%:*}
    _fu_head=${_fu_head//[[:space:]]/}
    case "$_fu_head" in
      ''|*[!A-Za-z0-9_.\"\'-]*) return 0 ;;
    esac
  done < "$1"
  return 1
}

# The four layers, in the resolver's precedence order: user-level, tracked,
# project-local, environment overlay. Within the three file layers an explicit
# empty list clears everything earlier, and a nonempty list selects; a file
# with no key at all leaves the running answer alone. The overlay is additive
# and so can only turn the answer on.
#
# The seed is `configured`, because the composer's default selection includes
# agent-style, so a project that has said nothing still gets a passive pack.
#
# This answers "is anything selected", not "which packs are selected". Naming
# them would mean pulling names out of YAML without a YAML parser, which is the
# fragility that produced this release; the resolver does that job whenever
# Python is present, and this runs only when it is not. Four consequences are
# known, and every one of them keeps a file that the resolver would have
# replaced, rather than deleting one it would have kept:
#
#   1. An overlay made only of subtractions cannot be evaluated without
#      resolving names, so `AGENT_CONFIG_PACKS=-agent-style` still reads as
#      configured and preserves a file the operator has opted out of.
#   2. An overlay whose additions and subtractions cancel, such as
#      `agent-style,-agent-style`, reads as one addition here and as no
#      selection in the resolver.
#   3. Marker names are never compared with selected names, so a cleared base
#      plus a project-local selection of some other pack preserves a composed
#      file carrying only the old pack's block.
#   4. The overlay is split on whitespace as well as commas; see
#      _env_pack_selection_adds.
#   5. A later layer this scanner cannot read counts as uncertainty once a
#      clear is in force, so a file holding only unrelated keys preserves where
#      the resolver would leave the clear standing. See the `none` arm below.
_passive_rule_pack_configured() {
  _rp_configured=true
  _rp_user_config=$(_user_config_path)
  for _rp_layer in "$_rp_user_config" agent-config.yaml agent-config.local.yaml; do
    [ -n "$_rp_layer" ] || continue
    case "$(_rule_packs_config_state "$_rp_layer")" in
      nonempty) _rp_configured=true ;;
      empty) _rp_configured=false ;;
      none)
        # `none` means this scanner found no key it recognizes, which is not
        # the same as the file having no selection. The key match is the
        # literal `packs:` spelling, and YAML also allows `"packs":` and
        # `packs :`; both read as `none` here and as a selection in the
        # resolver. After a proven clear that answer deleted the artifact the
        # later layer had just asked for. Recognizing every spelling means
        # writing a YAML parser, so treat a nonempty later layer as uncertainty
        # instead, and let uncertainty preserve. An absent or empty file is not
        # uncertain and leaves the clear standing.
        if ! $_rp_configured && _file_has_unreadable_line "$_rp_layer"; then
          _rp_configured=true
        fi
        ;;
    esac
  done
  if _env_pack_selection_adds; then
    _rp_configured=true
  fi
  $_rp_configured
}

# Stage beside the destination, then rename over it. Readers therefore see a
# complete old helper or a complete new helper, never a truncated copy.
_atomic_deploy_helper() {
  _atomic_source=$1
  _atomic_target=$2
  _atomic_executable=${3:-false}
  _atomic_dir=$(dirname "$_atomic_target")
  _atomic_base=$(basename "$_atomic_target")
  mkdir -p "$_atomic_dir" || return 1
  # Deploying identical bytes is a rename that changes nothing, and on Windows a
  # rename over a file a live session holds open is refused. That refusal exits
  # this phase and records the whole run incomplete over a no-op (#44). The
  # executable bit is part of "already deployed": skipping while the target is
  # not executable would leave a helper the caller asked to be runnable sitting
  # there unrunnable. A missing cmp falls through to the normal path.
  if [ -f "$_atomic_target" ] && command -v cmp >/dev/null 2>&1; then
    if cmp -s "$_atomic_source" "$_atomic_target"; then
      if ! $_atomic_executable || [ -x "$_atomic_target" ]; then
        return 0
      fi
    fi
  fi
  _atomic_temp=$(mktemp "$_atomic_dir/.${_atomic_base}.XXXXXX") || return 1
  if ! cp -f "$_atomic_source" "$_atomic_temp"; then
    rm -f "$_atomic_temp"
    return 1
  fi
  if $_atomic_executable && ! chmod +x "$_atomic_temp" 2>/dev/null; then
    rm -f "$_atomic_temp"
    return 1
  fi
  if ! mv -f "$_atomic_temp" "$_atomic_target"; then
    rm -f "$_atomic_temp"
    return 1
  fi
  return 0
}

# Best-effort bootstrap ledger. This script runs without set -e, so every
# helper must finish successfully rather than relying on error propagation.
_ledger_esc() {
  local LC_ALL=C
  local _ledger_input=$1
  local _ledger_output=""
  local _ledger_char _ledger_code _ledger_encoded
  local _ledger_index=0
  while [ "$_ledger_index" -lt "${#_ledger_input}" ]; do
    _ledger_char=${_ledger_input:$_ledger_index:1}
    case "$_ledger_char" in
      $'\b') _ledger_output="${_ledger_output}\\b" ;;
      $'\t') _ledger_output="${_ledger_output}\\t" ;;
      $'\n') _ledger_output="${_ledger_output}\\n" ;;
      $'\f') _ledger_output="${_ledger_output}\\f" ;;
      $'\r') _ledger_output="${_ledger_output}\\r" ;;
      '"') _ledger_output="${_ledger_output}\\\"" ;;
      '\') _ledger_output="${_ledger_output}\\\\" ;;
      *)
        if ! printf -v _ledger_code '%d' "'$_ledger_char" 2>/dev/null; then
          _ledger_output=""
          break
        fi
        if [ "$_ledger_code" -lt 32 ]; then
          if ! printf -v _ledger_encoded '\\u%04x' "$_ledger_code" 2>/dev/null; then
            _ledger_output=""
            break
          fi
          _ledger_output="${_ledger_output}${_ledger_encoded}"
        else
          _ledger_output="${_ledger_output}${_ledger_char}"
        fi
        ;;
    esac
    _ledger_index=$((_ledger_index + 1))
  done
  printf '%s' "$_ledger_output" 2>/dev/null || true
  return 0
}

_ledger_write() {
  _ledger_last_phase=$1
  _ledger_completed=$2
  mkdir -p .agent-config 2>/dev/null || return 0
  _ledger_upstream=$(_ledger_esc "${_LEDGER_UPSTREAM:-}")
  _ledger_run_id=$(_ledger_esc "${_LEDGER_RUN_ID:-}")
  _ledger_started=$(_ledger_esc "${_LEDGER_STARTED:-}")
  _ledger_phase=$(_ledger_esc "$_ledger_last_phase")
  printf '{"schema":1,"emitted_by":"bootstrap.sh","run_id":"%s","started_at":"%s","upstream":"%s","completed":%s,"last_phase":"%s","steps":[%s]}\n' \
    "$_ledger_run_id" "$_ledger_started" "$_ledger_upstream" "$_ledger_completed" \
    "$_ledger_phase" "${_LEDGER_STEPS:-}" > .agent-config/last-run.json.tmp 2>/dev/null &&
    mv -f .agent-config/last-run.json.tmp .agent-config/last-run.json 2>/dev/null || true
}

_ledger_init() {
  _LEDGER_RUN_ID="$$-$(date -u +%s 2>/dev/null || echo 0)"
  _LEDGER_STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
  _LEDGER_UPSTREAM="${_POS_UPSTREAM:-${AGENT_CONFIG_UPSTREAM:-}}"
  _LEDGER_STEPS=""
  _LEDGER_TARGETS=""
  _LEDGER_INCOMPLETE=false
  _ledger_write start false
  return 0
}

_ledger_target() {
  _ledger_target_value=$(_ledger_esc "$1")
  if [ -n "${_LEDGER_TARGETS:-}" ]; then
    _LEDGER_TARGETS="${_LEDGER_TARGETS},"
  fi
  _LEDGER_TARGETS="${_LEDGER_TARGETS}\"${_ledger_target_value}\""
  return 0
}

_ledger_step() {
  _ledger_step_phase=$1
  _ledger_step_scope=$2
  _ledger_step_status=$3
  _ledger_step_rc=${4:-null}
  _ledger_step_reason=${5:-}
  _ledger_step_phase_json=$(_ledger_esc "$_ledger_step_phase")
  _ledger_step_scope_json=$(_ledger_esc "$_ledger_step_scope")
  _ledger_step_status_json=$(_ledger_esc "$_ledger_step_status")
  _ledger_step_value="{\"phase\":\"${_ledger_step_phase_json}\",\"scope\":\"${_ledger_step_scope_json}\",\"status\":\"${_ledger_step_status_json}\",\"rc\":${_ledger_step_rc},\"targets\":[${_LEDGER_TARGETS:-}]"
  if [ -n "$_ledger_step_reason" ]; then
    _ledger_step_reason_json=$(_ledger_esc "$_ledger_step_reason")
    _ledger_step_value="${_ledger_step_value},\"reason\":\"${_ledger_step_reason_json}\""
  fi
  _ledger_step_value="${_ledger_step_value}}"
  if [ -n "${_LEDGER_STEPS:-}" ]; then
    _LEDGER_STEPS="${_LEDGER_STEPS},"
  fi
  _LEDGER_STEPS="${_LEDGER_STEPS}${_ledger_step_value}"
  _LEDGER_TARGETS=""
  _ledger_write "$_ledger_step_phase" false
  return 0
}

_generator_status=skipped
_generator_rc=null

_run_generator() {
  _generator_python=$1
  [ -n "$_generator_python" ] || return 0
  [ -f .agent-config/repo/scripts/generate_agent_configs.py ] || return 0
  "$_generator_python" .agent-config/repo/scripts/generate_agent_configs.py --root . --quiet
  _generator_rc=$?
  if [ "$_generator_rc" -eq 0 ]; then
    _generator_status=ok
  else
    _generator_status=failed
  fi
  return 0
}

_record_generator_step() {
  case "$_generator_status" in
    ok)
      _ledger_target CLAUDE.md
      _ledger_target agents/codex.md
      _ledger_step generate repo ok
      ;;
    failed)
      _ledger_step generate repo failed "$_generator_rc"
      ;;
    *)
      _ledger_step generate repo skipped
      ;;
  esac
  return 0
}

# Detect git binary and reject pre-2.25 versions before any git invocation
# below. Sparse clone uses `git clone --filter=blob:none --sparse`; `--sparse`
# is the Git 2.25 floor (2020-01-13), while `--filter=blob:none` is the older
# partial-clone option (Git 2.19+). macOS shipped pre-2.25 system git as late
# as Big Sur, and a real consumer hit `unknown option 'sparse'` on 2026-05-18.
# On parse failure default-pass with a stderr warning so unexpected version
# strings (alpha builds, distro suffixes like `2.30.1.windows.1` or
# `(Apple Git-141)`) do not block already-modern systems.
_git_install_hint() {
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'install: brew install git' ;;
    Linux)  printf 'install: sudo apt update && sudo apt install -y git (or your distro package manager)' ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) printf 'install: https://git-scm.com/download/win' ;;
    *)      printf 'install: see https://git-scm.com/downloads' ;;
  esac
}

check_git_preflight() {
  if [ -n "${AGENT_CONFIG_SKIP_GIT_PREFLIGHT:-}" ]; then
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    printf '[anywhere-agents] git is not installed or not on PATH; bootstrap needs git >= 2.25 for sparse clone.\n' >&2
    printf '[anywhere-agents] %s\n' "$(_git_install_hint)" >&2
    exit 1
  fi
  _ver_line=$(git --version 2>/dev/null || true)
  _ver_str=${_ver_line#git version }
  _major=""
  _minor=""
  if [ -n "$_ver_str" ] && [ "$_ver_str" != "$_ver_line" ]; then
    _major=$(printf '%s' "$_ver_str" | sed -n 's/^\([0-9][0-9]*\)\..*/\1/p')
    _minor=$(printf '%s' "$_ver_str" | sed -n 's/^[0-9][0-9]*\.\([0-9][0-9]*\).*/\1/p')
  fi
  if [ -z "$_major" ] || [ -z "$_minor" ]; then
    printf '[anywhere-agents] could not parse git version from %s; assuming OK.\n' "${_ver_line:-<empty>}" >&2
    return 0
  fi
  if [ "$_major" -lt 2 ] || { [ "$_major" -eq 2 ] && [ "$_minor" -lt 25 ]; }; then
    printf '[anywhere-agents] git %s.%s is too old; bootstrap needs git >= 2.25 for sparse clone.\n' "$_major" "$_minor" >&2
    printf '[anywhere-agents] %s\n' "$(_git_install_hint)" >&2
    exit 1
  fi
  return 0
}

_ledger_init

# Publish the session banner report when this run ends, whatever ended it.
# The renderer reads the ledger this run wrote, so a failed or degraded
# refresh publishes a report naming the phase it stopped at instead of
# leaving an older all-clear report in place. The exit status is preserved.
# A sparse clone without the renderer (a first install whose fetch failed)
# publishes nothing, and the agent's freshness check then selects the fixed
# fallback banner. The renderer is best effort: its own failure never
# changes this run's result.
_render_banner_report() {
  _rb_rc=$1
  [ -f .agent-config/repo/scripts/render_banner.py ] || return 0
  _rb_py="${_py:-}"
  [ -n "$_rb_py" ] || _rb_py=$(_find_python 2>/dev/null || true)
  [ -n "$_rb_py" ] || return 0
  "$_rb_py" .agent-config/repo/scripts/render_banner.py --root . --bootstrap-rc "$_rb_rc" >/dev/null 2>&1 || true
  return 0
}
trap '_bootstrap_rc=$?; _render_banner_report "$_bootstrap_rc"; exit $_bootstrap_rc' EXIT

check_git_preflight
[ -n "${AGENT_CONFIG_PREFLIGHT_TEST:-}" ] && exit 0

_codex_auto_update_disabled() {
  _v=$(printf '%s' "${ANYWHERE_AGENTS_CODEX_AUTO_UPDATE:-}" | tr '[:upper:]' '[:lower:]')
  case "$_v" in
    off|0|disabled|false|no) return 0 ;;
    *) return 1 ;;
  esac
}

maybe_update_codex_cli() {
  _codex_auto_update_disabled && return 0
  command -v npm >/dev/null 2>&1 || return 0
  _prefix=$(npm prefix -g 2>/dev/null || true)
  [ -n "$_prefix" ] || return 0
  [ -f "$_prefix/node_modules/@openai/codex/package.json" ] || return 0
  _outdated=$(npm outdated -g @openai/codex --json 2>/dev/null || true)
  [ -n "$_outdated" ] && [ "$_outdated" != "{}" ] || return 0
  _current=$(printf '%s' "$_outdated" | sed -n 's/.*"current"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  _latest=$(printf '%s' "$_outdated" | sed -n 's/.*"latest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$_latest" ] && [ "$_latest" != "$_current" ] || return 0
  printf '[anywhere-agents] updating Codex CLI @openai/codex %s -> %s\n' "${_current:-?}" "$_latest" >&2
  if ! npm install -g @openai/codex@latest --silent; then
    printf '%s\n' '[anywhere-agents] Codex CLI auto-update failed; run `npm install -g @openai/codex@latest`' >&2
  fi
}

# Legacy AC -> AA migration for direct shell-bootstrap runs. If the
# persisted upstream or cached repo origin still points at agent-config
# and the caller did not pass an explicit upstream, delete the old cache
# so the normal clone path below re-clones anywhere-agents.
_legacy_ac=0
if [ -z "$_POS_UPSTREAM" ] && [ -z "${AGENT_CONFIG_UPSTREAM:-}" ]; then
  if [ -f .agent-config/upstream ] && [ "$(tr -d '\r\n\t ' < .agent-config/upstream)" = "yzhao062/agent-config" ]; then
    _legacy_ac=1
  elif [ -f .agent-config/repo/.git/config ] && git -C .agent-config/repo remote get-url origin 2>/dev/null | grep -Eiq '(^|[:/])yzhao062/agent-config(\.git)?/?$'; then
    _legacy_ac=1
  fi
fi
if [ "$_legacy_ac" = "1" ]; then
  printf '%s\n' '[anywhere-agents] Migrating from agent-config bootstrap to anywhere-agents...' >&2
  rm -rf .agent-config/repo .agent-config/upstream .agent-config/bootstrap.sh .agent-config/bootstrap.ps1
fi

# Resolve Python once before the network-backed fetch and this run's user-level
# helper deployment. Every later Python-backed phase reuses this snapshot.
_py=$(_find_python || true)

# Upstream cascade: argv > env var > persisted file > hardcoded default.
# Forkers can persist a different default in their fork; consumers can pass
# upstream via `bash .agent-config/bootstrap.sh <user>/<repo>` or the
# $AGENT_CONFIG_UPSTREAM environment variable.
UPSTREAM=""
if [ -n "$_POS_UPSTREAM" ]; then
  UPSTREAM="$_POS_UPSTREAM"
elif [ -n "${AGENT_CONFIG_UPSTREAM:-}" ]; then
  UPSTREAM="$AGENT_CONFIG_UPSTREAM"
elif [ -f .agent-config/upstream ]; then
  UPSTREAM="$(tr -d '\r\n' < .agent-config/upstream)"
fi
UPSTREAM="${UPSTREAM:-yzhao062/anywhere-agents}"
mkdir -p .agent-config
printf '%s' "$UPSTREAM" > .agent-config/upstream
_LEDGER_UPSTREAM="$UPSTREAM"
_ledger_target .agent-config/upstream
_ledger_step preflight repo ok

mkdir -p .agent-config .claude/commands
curl -sfL "https://raw.githubusercontent.com/$UPSTREAM/main/AGENTS.md" -o .agent-config/AGENTS.md

# Sparse clone moved up (before composing the root AGENTS.md): the rule-pack
# manifest and composer helper live inside .agent-config/repo/ and must be
# present before we branch on opt-in.
REPO_URL="https://github.com/$UPSTREAM.git"
if [ -d .agent-config/repo/.git ]; then
  git -C .agent-config/repo remote set-url origin "$REPO_URL"
  git -C .agent-config/repo pull --ff-only
else
  git clone --depth 1 --filter=blob:none --sparse "$REPO_URL" .agent-config/repo
fi
git -C .agent-config/repo sparse-checkout set skills .claude scripts user bootstrap
_ledger_target .agent-config/AGENTS.md
_ledger_target .agent-config/repo
_ledger_step fetch repo ok

# Compose root AGENTS.md. Default-on: every aa consumer gets the agent-style
# writing rule pack unless they explicitly opt out via `rule_packs: []` in
# agent-config.yaml. Composition requires Python 3 + PyYAML; when PyYAML is
# missing we attempt a best-effort `pip install --user pyyaml`. If Python or
# PyYAML still are not available, we fall back to a marked upstream
# AGENTS.md and print a one-line tip unless the consumer has explicitly
# referenced rule_packs themselves.
_compose_ok=false
_compose_skip_reason=""
if [ -z "$_py" ]; then
  _compose_skip_reason="no Python 3 interpreter found"
else
  if ! "$_py" -c "import yaml" >/dev/null 2>&1; then
    printf 'installing PyYAML (enables agent-style rule-pack composition)...\n' >&2
    "$_py" -m pip install --user --quiet pyyaml || true
  fi
  if "$_py" -c "import yaml" >/dev/null 2>&1; then
    _compose_ok=true
  else
    _compose_skip_reason="Python 3 interpreter has no PyYAML after install attempt"
  fi
fi

if $_compose_ok && [ ! -f .agent-config/repo/scripts/compose_packs.py ] && [ ! -f .agent-config/repo/scripts/compose_rule_packs.py ]; then
  # Upstream sparse clone has no composer script (e.g. the ac source repo
  # itself, which intentionally ships only generate_agent_configs.py and
  # not the v0.4.0 unified composer). Fall through to the marked-AGENTS.md
  # path instead of crashing on a non-existent Python file.
  printf '%s\n' '[anywhere-agents] no composer script in .agent-config/repo/scripts/; falling back to verbatim AGENTS.md' >&2
  _compose_ok=false
  _compose_skip_reason="no composer script in sparse clone"
fi

if $_compose_ok; then
  _NO_CACHE_FLAG=""
  [ -n "$NO_CACHE" ] && _NO_CACHE_FLAG="--no-cache"
  # Prefer the v0.4.0 unified composer. Fall back to the v0.3.x rule-pack
  # composer on pre-v0.4.0 sparse clones that predate compose_packs.py.
  if [ -f .agent-config/repo/scripts/compose_packs.py ]; then
    _composer=.agent-config/repo/scripts/compose_packs.py
  else
    _composer=.agent-config/repo/scripts/compose_rule_packs.py
  fi
  # shellcheck disable=SC2086
  # v0.5.8: capture composer rc and always run generator so CLAUDE.md stays
  # coherent even when composition aborts (e.g. DriftAbort, OSError).
  "$_py" "$_composer" --root . $_NO_CACHE_FLAG
  _composer_rc=$?
  _run_generator "$_py"
  if [ "$_composer_rc" -ne 0 ]; then
    printf '%s\n' "[anywhere-agents] pack composition did not complete (rc=${_composer_rc}); generated files (CLAUDE.md, agents/codex.md) refreshed from current AGENTS.md. Re-run \`anywhere-agents\` after addressing the failure." >&2
    _ledger_target AGENTS.md
    _ledger_step compose repo failed "$_composer_rc"
    _record_generator_step
    exit "$_composer_rc"
  fi
  _ledger_target AGENTS.md
  _ledger_step compose repo ok
else
  _compose_preserved=false
  _passive_configured=false
  if _passive_rule_pack_configured; then
    _passive_configured=true
  fi
  # Preservation is gated on a configured selection. Testing the artifact alone
  # kept the old packs even when the consumer had set `rule_packs: []`, which
  # contradicts the documented opt-out and could freeze a removed pack forever.
  # This makes the pre-parser load-bearing again, which is why the `tr`
  # dependency had to go in the same change.
  if $_passive_configured && _agents_md_is_composed; then
    # Composition cannot run, and the AGENTS.md on disk is a composed artifact.
    # Replacing it with the un-composed upstream copy deletes every pack block,
    # and where the file is tracked git then records that deletion as intent.
    # Keep the last good artifact. This check reads the file rather than the
    # configuration, so it still holds when the configuration is misread, which
    # is how the pack blocks were lost in the first place.
    _compose_preserved=true
  elif $_passive_configured; then
    {
      printf '%s\n' "<!-- rule-pack composition skipped: $_compose_skip_reason; run anywhere-agents to compose -->"
      cat .agent-config/AGENTS.md
    } > AGENTS.md
    # The marker goes on every skip, because the artifact must never be
    # mistakable for a composed one. `completed: false` is narrower: it means
    # the run did not do its job and someone can act. Missing Python or PyYAML
    # is actionable. An upstream that ships no composer is a property of that
    # upstream, and agent-config deliberately ships only the generator, so
    # flagging it would mark every bootstrap from an ac-shaped remote as
    # incomplete forever with nothing to fix. pack verify still reports those
    # packs as registered rather than composed.
    if [ "$_compose_skip_reason" != "no composer script in sparse clone" ]; then
      _LEDGER_INCOMPLETE=true
    fi
  else
    cp -f .agent-config/AGENTS.md AGENTS.md
  fi
  if $_compose_preserved; then
    printf '\n' >&2
    printf 'warning: composition was skipped (%s) and the AGENTS.md on disk is a\n' "$_compose_skip_reason" >&2
    printf '         composed artifact, so this run left it untouched rather than\n' >&2
    printf '         replacing it with the un-composed upstream copy.\n' >&2
    printf '         Its pack blocks are whatever the last successful composition\n' >&2
    printf '         produced; upstream changes reach this file only once\n' >&2
    printf '         composition runs again.\n' >&2
    if [ "$_compose_skip_reason" != "no composer script in sparse clone" ]; then
      _LEDGER_INCOMPLETE=true
    fi
  fi
  # Awareness is a different question from selection: `packs: []` is an opt-out
  # and still means the operator knows about packs. It reads the same four
  # layers the selection gate does, so a consumer whose only mention is
  # user-level, or who uses the canonical env var, is not told that packs were
  # skipped when they were never asked for.
  _rp_aware=false
  _rp_tracked_state=$(_rule_packs_config_state agent-config.yaml)
  _rp_local_state=$(_rule_packs_config_state agent-config.local.yaml)
  _rp_user_state=$(_rule_packs_config_state "$(_user_config_path)")
  if [ "$_rp_tracked_state" != none ]; then
    _rp_aware=true
  elif [ "$_rp_local_state" != none ]; then
    _rp_aware=true
  elif [ "$_rp_user_state" != none ]; then
    _rp_aware=true
  elif [ -n "${AGENT_CONFIG_PACKS:-}${AGENT_CONFIG_RULE_PACKS:-}" ]; then
    _rp_aware=true
  fi
  # The tip tells the operator the writing rules are absent. When the composed
  # artifact was preserved they are present, so the tip would be wrong.
  if $_compose_preserved; then
    _rp_aware=true
  fi
  if ! $_rp_aware; then
    printf '\n' >&2
    printf 'tip: anywhere-agents ships with agent-style writing rules enabled by default,\n' >&2
    printf '     but this run skipped them (%s).\n' "$_compose_skip_reason" >&2
    printf "     install Python + PyYAML to enable, or silence with 'rule_packs: []' in agent-config.yaml.\n" >&2
  fi
  _ledger_target AGENTS.md
  if $_compose_preserved; then
    _ledger_step compose repo skipped null "$_compose_skip_reason; existing composed AGENTS.md preserved"
  else
    _ledger_step compose repo skipped null "$_compose_skip_reason"
  fi
fi
# Generate per-agent config files (CLAUDE.md, agents/codex.md) from AGENTS.md.
# Generator preserves hand-authored files (no GENERATED header) and warns loudly.
# v0.5.8: in the compose-ok path the generator already ran inside the if block
# above. Only run here for the fallback path (Python/PyYAML unavailable) where
# $_compose_ok is false.
if ! $_compose_ok; then
  _run_generator "$_py"
fi
_record_generator_step
if [ -d .agent-config/repo/.claude/commands ]; then
  cp -f .agent-config/repo/.claude/commands/*.md .claude/commands/
  _ledger_target .claude/commands
fi
if [ -f .agent-config/repo/.claude/settings.json ]; then
  if [ -f .claude/settings.json ]; then
    # Both entry points run this one helper, so the merge semantics and the
    # on-disk format have a single implementation. The guard mirrors the
    # composer guard above: a sparse clone predating this release does not
    # carry the file, and that must not fail the run.
    if [ -n "$_py" ] && [ -f .agent-config/repo/scripts/merge_settings.py ]; then
      "$_py" .agent-config/repo/scripts/merge_settings.py \
        .claude/settings.json .agent-config/repo/.claude/settings.json
    fi
  else
    cp -f .agent-config/repo/.claude/settings.json .claude/settings.json
  fi
  _ledger_target .claude/settings.json
fi
_ledger_step project_files repo ok
# --- User-level setup: hooks and settings ---
# This section modifies ~/.claude/ (user-level, not project-level).
# It deploys a PreToolUse hook guard and merges shared permission settings.
# Remove this section if you do not want bootstrap to modify user-level config.
if [ -f .agent-config/repo/scripts/_python ]; then
  if ! _atomic_deploy_helper .agent-config/repo/scripts/_python "$HOME/.claude/hooks/_python" true; then
    printf '%s\n' 'error: could not atomically deploy ~/.claude/hooks/_python' >&2
    exit 1
  fi
  _ledger_target '~/.claude/hooks/_python'
fi
if [ -f .agent-config/repo/scripts/guard.py ]; then
  if ! _atomic_deploy_helper .agent-config/repo/scripts/guard.py "$HOME/.claude/hooks/guard.py"; then
    printf '%s\n' 'error: could not atomically deploy ~/.claude/hooks/guard.py' >&2
    exit 1
  fi
  _ledger_target '~/.claude/hooks/guard.py'
  # Codex runs the same guard from its own hooks file, once the user trusts
  # the entry. A machine without a Codex home is skipped.
  if [ -n "$_py" ] && [ -d "${CODEX_HOME:-$HOME/.codex}" ] \
    && [ -f .agent-config/repo/scripts/merge_codex_hooks.py ]; then
    "$_py" .agent-config/repo/scripts/merge_codex_hooks.py
    _ledger_target '~/.codex/hooks.json'
  fi
fi
if [ -f .agent-config/repo/scripts/session_bootstrap.py ]; then
  if ! _atomic_deploy_helper .agent-config/repo/scripts/session_bootstrap.py "$HOME/.claude/hooks/session_bootstrap.py"; then
    printf '%s\n' 'error: could not atomically deploy ~/.claude/hooks/session_bootstrap.py' >&2
    exit 1
  fi
  _ledger_target '~/.claude/hooks/session_bootstrap.py'
fi
if [ -f .agent-config/repo/scripts/statusline.py ]; then
  if ! _atomic_deploy_helper .agent-config/repo/scripts/statusline.py "$HOME/.claude/statusline.py"; then
    printf '%s\n' 'error: could not atomically deploy ~/.claude/statusline.py' >&2
    exit 1
  fi
  _ledger_target '~/.claude/statusline.py'
fi
if [ -f .agent-config/repo/scripts/agent-quota.py ]; then
  if ! _atomic_deploy_helper .agent-config/repo/scripts/agent-quota.py "$HOME/.claude/agent-quota.py"; then
    printf '%s\n' 'error: could not atomically deploy ~/.claude/agent-quota.py' >&2
    exit 1
  fi
  _ledger_target '~/.claude/agent-quota.py'
fi
if [ -f .agent-config/repo/user/settings.json ]; then
  mkdir -p "$HOME/.claude"
  # The helper creates an absent target as well as merging an existing one, and
  # it does both inside its lock. Testing for existence out here and installing
  # separately is what let a concurrent composer commit be overwritten (#32).
  if [ -n "$_py" ] && [ -f .agent-config/repo/scripts/merge_settings.py ]; then
    # The helper publishes by rename and takes a sibling lock, so a refusal
    # leaves the target holding its previous content. Read the code rather
    # than carrying on regardless: this file registers the PreToolUse guard
    # for every consumer on the machine, and a run that silently skipped the
    # merge used to look exactly like one that applied it (#32).
    "$_py" .agent-config/repo/scripts/merge_settings.py \
      "$HOME/.claude/settings.json" .agent-config/repo/user/settings.json
    _settings_rc=$?
    if [ "$_settings_rc" -eq 3 ]; then
      _SETTINGS_MERGE_REASON='another bootstrap held the settings lock; target left unchanged'
    elif [ "$_settings_rc" -ne 0 ]; then
      _SETTINGS_MERGE_REASON="settings merge helper exited ${_settings_rc}; target left unchanged"
    fi
    if [ -n "${_SETTINGS_MERGE_REASON:-}" ]; then
      printf 'warning: %s\n' "$_SETTINGS_MERGE_REASON" >&2
    fi
  elif [ -f "$HOME/.claude/settings.json" ]; then
    # Merging into an existing file is exactly what needs the helper, so there
    # is nothing this shell can safely do. Say so: a run that skipped the merge
    # used to be indistinguishable from one that applied it (#32).
    _SETTINGS_MERGE_REASON='no Python for the settings merge helper; target left unchanged'
    printf 'warning: %s\n' "$_SETTINGS_MERGE_REASON" >&2
  else
    # First install with no helper and therefore no lock. ln(1) fails when the
    # destination exists, so create-if-absent is one atomic step rather than a
    # test followed by a rename that would overwrite a file another writer
    # created in between. That is the interleaving that replaced a committed
    # composer permission in a Round 2 probe (#32). Installing still matters
    # here: this file is what registers the PreToolUse guard, and refusing
    # would cost every such machine its guard to avoid a race that ln closes.
    _settings_temp=$(mktemp "$HOME/.claude/.settings.json.XXXXXX" 2>/dev/null) || _settings_temp=''
    if [ -z "$_settings_temp" ] || ! cp -f .agent-config/repo/user/settings.json "$_settings_temp"; then
      [ -n "$_settings_temp" ] && rm -f "$_settings_temp"
      _SETTINGS_MERGE_REASON='could not install ~/.claude/settings.json'
      printf 'warning: %s\n' "$_SETTINGS_MERGE_REASON" >&2
    elif ln "$_settings_temp" "$HOME/.claude/settings.json" 2>/dev/null; then
      rm -f "$_settings_temp"
      printf 'warning: installed ~/.claude/settings.json without the merge helper; no Python found\n' >&2
    elif [ -f "$HOME/.claude/settings.json" ]; then
      rm -f "$_settings_temp"
      _SETTINGS_MERGE_REASON='another writer created ~/.claude/settings.json first; merging into it needs Python'
      printf 'warning: %s\n' "$_SETTINGS_MERGE_REASON" >&2
    else
      # The atomic create was not available: no ln on PATH, or a filesystem
      # that does not do hard links. Publishing with a rename here was tried
      # and withdrawn: a negative existence check does not reserve the name, so
      # the rename replaced a permission the composer committed inside that
      # window, returned 0, and recorded nothing. Refusing is loud and cannot
      # destroy a finished write, which is the whole point of #32.
      rm -f "$_settings_temp"
      _SETTINGS_MERGE_REASON='could not create ~/.claude/settings.json atomically; target left absent'
      printf 'warning: %s\n' "$_SETTINGS_MERGE_REASON" >&2
    fi
  fi
  _ledger_target '~/.claude/settings.json'
fi
# Heal legacy autoUpdates: false in ~/.claude.json. See bootstrap.ps1 comment
# for the why. To genuinely disable auto-updates, set DISABLE_AUTOUPDATER=1
# via the env block in ~/.claude/settings.json.
if [ -f "$HOME/.claude.json" ]; then
  if [ -n "$_py" ]; then
    "$_py" -c "
import json, os, pathlib as P, tempfile
p = P.Path.home() / '.claude.json'
try:
    # read_bytes plus an explicit decode: text mode picks the locale codepage
    # on Windows, which is cp1252 on a default install, and this file carries
    # non-ASCII. utf-8-sig also heals a copy some earlier writer left with a BOM.
    d = json.loads(p.read_bytes().decode('utf-8-sig'))
    if d.get('autoUpdates') is False:
        d['autoUpdates'] = True
        # Best-effort heal. Atomic replace (tempfile.mkstemp + os.replace)
        # prevents a truncated config on interrupt but is NOT a cross-process
        # lock: a concurrent Claude Code write landing between our read and
        # replace will be clobbered by our older snapshot. Healed flag
        # reappears on the next session if that happens.
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix='.claude.json.', suffix='.tmp')
        try:
            # Binary write: text mode also translates '\n' to '\r\n' on
            # Windows, which rewrites every line of a 125 KB file that the
            # other entry point writes with LF.
            with os.fdopen(fd, 'wb') as f:
                f.write((json.dumps(d, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
            os.replace(tmp, str(p))
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
except Exception:
    pass
"
  fi
  _ledger_target '~/.claude.json'
fi
if [ -n "${_SETTINGS_MERGE_REASON:-}" ]; then
  _ledger_step user_files user failed null "$_SETTINGS_MERGE_REASON"
else
  _ledger_step user_files user ok
fi

# Codex CLI has no native updater like Claude Code. If Codex is installed as
# the global npm package that this config recommends, keep it current during
# bootstrap. Set ANYWHERE_AGENTS_CODEX_AUTO_UPDATE=off to disable.
maybe_update_codex_cli
# No target here: maybe_update_codex_cli no-ops when Codex is not the global
# npm install or when ANYWHERE_AGENTS_CODEX_AUTO_UPDATE=off, so naming the
# package would imply an update that may not have happened.
_ledger_step external external ok

_gitignore_add '^\/?\.agent-config/' '.agent-config/'
# Rule-pack opt-in writes agent-config.local.yaml as a machine-local override
# that must not be committed. Auto-ignore it idempotently alongside .agent-config/.
_gitignore_add '^\/?agent-config\.local\.yaml$' 'agent-config.local.yaml'
# AGENTS.md, CLAUDE.md and agents/codex.md are regenerated on every run. Their
# bytes depend on which packs this machine resolved and on whether composition
# ran, so two machines that are both up to date produce different content and
# each sees the other's as a diff to commit. Worse, a run that degraded the
# artifact records the loss as an intentional deletion in a tracked file.
#
# A path git already tracks cannot be untracked by .gitignore, so adding an
# entry for one would be inert and misleading. Skip those and leave the repo
# as it is; moving an already-tracked file out of the index is an operator
# decision, because the resulting commit removes it for every other clone.
# Set AGENT_CONFIG_TRACK_GENERATED to keep all three out of .gitignore.
#
# The entries are anchored with a leading slash so a monorepo's
# packages/foo/AGENTS.md is not caught, and codex.md is named rather than the
# agents/ directory so agents/codex.local.md, the documented per-agent
# override, stays visible.
if [ -z "${AGENT_CONFIG_TRACK_GENERATED:-}" ]; then
  for _gi_generated in AGENTS.md CLAUDE.md agents/codex.md; do
    if ! _git_tracks "$_gi_generated"; then
      # Escape the dot for the ERE probe; unescaped it also matches AGENTSxmd.
      # The leading slash is optional in the probe only: a consumer who already
      # ignores an unanchored `AGENTS.md` has the broader rule, so appending the
      # narrower `/AGENTS.md` under it would add a line that changes nothing.
      # The entry written is still anchored.
      _gi_escaped=${_gi_generated//./\\.}
      _gitignore_add "^/?${_gi_escaped}\$" "/${_gi_generated}"
    fi
  done
fi
# The todo/ drop box. A person copies a file in, points an agent at it, and the
# agent files it where it belongs or deletes it; the resting state is empty. The
# convention spread by hand-copying until now, which is why it reached neither a
# new repo nor two of the existing ones.
#
# Seeded only when absent. An existing README is never rewritten: one consumer
# carries a version written around its own filing rules, and replacing that with
# the upstream copy is the failure this release series exists to remove.
#
# `todo/*` rather than `todo/`, because git cannot re-include a file whose parent
# directory is excluded: the directory form silently drops the README and the
# folder then disappears from fresh clones.
#
# A linked todo belongs to whoever linked it. Seeding through the link writes
# the README into a directory outside this repo, which is not a place a session
# start has any business creating files, and the ignore rules would then name a
# path git does not index here anyway. Measured on a Windows junction, which
# Git Bash also reports as -L, so one predicate covers both link kinds.
if [ -z "${AGENT_CONFIG_NO_TODO_DROPBOX:-}" ]; then
  if [ ! -L todo ] &&
     [ ! -e todo/README.md ] &&
     [ -f .agent-config/repo/bootstrap/todo-readme.md ]; then
    mkdir -p todo
    cp -f .agent-config/repo/bootstrap/todo-readme.md todo/README.md || \
      printf '%s\n' 'warning: could not seed todo/README.md' >&2
  fi
  # Gated on the README rather than on the directory. A path named todo that
  # is not a directory, or a seed that failed, must not leave this run claiming
  # an ignore rule and a ledger target for a file that is not there. The link
  # test repeats here so a pre-existing README behind a link is left alone too.
  # The PowerShell half gates on the same postcondition.
  if [ ! -L todo ] && [ -f todo/README.md ]; then
    _gitignore_add '^/?todo/\*$' 'todo/*'
    _gitignore_add '^!/?todo/README\.md$' '!todo/README.md'
    # git applies the last matching rule, so a negation above its exclusion
    # does nothing. A .gitignore that already carried a lone negation lands in
    # exactly that state once the exclusion is appended below it, so append one
    # canonical negation underneath when that has happened. A second run finds
    # the order already valid and writes nothing.
    # The line number is taken with parameter expansion rather than `cut`.
    # Nothing else in this script needs cut, and a shell that has grep and tail
    # but not cut is exactly what the hermetic test fixture builds; there the
    # pipeline yielded an empty string, the guards below skipped the repair,
    # and the run still exited 0.
    _gi_last_exclude=$(grep -nE '^/?todo/\*$' .gitignore 2>/dev/null | tail -1)
    _gi_last_exclude=${_gi_last_exclude%%:*}
    _gi_last_negate=$(grep -nE '^!/?todo/README\.md$' .gitignore 2>/dev/null | tail -1)
    _gi_last_negate=${_gi_last_negate%%:*}
    if [ -n "$_gi_last_exclude" ] && [ -n "$_gi_last_negate" ] &&
       [ "$_gi_last_negate" -lt "$_gi_last_exclude" ]; then
      _gitignore_append '!todo/README.md'
    fi
    _ledger_target todo/README.md
  fi
fi

# Self-update: copy the latest bootstrap script from the sparse clone over this
# one. Without this, a consumer that initially fetched an older bootstrap.sh
# stays on that version forever; future bootstrap improvements added upstream
# would never reach them automatically.
#
# v0.5.2 cross-OS fix: copy BOTH bootstrap.sh AND bootstrap.ps1. Previously
# the .sh entry only refreshed itself, so a Windows user running this bash
# entry on Git Bash / WSL would never land bootstrap.ps1 at all. Symmetric
# in bootstrap.ps1 (copies both). Cheap and covers cross-OS dev workflows.
if [ -f .agent-config/repo/bootstrap/bootstrap.sh ]; then
  if ! _atomic_deploy_helper .agent-config/repo/bootstrap/bootstrap.sh .agent-config/bootstrap.sh true; then
    printf '%s\n' 'error: could not atomically self-update .agent-config/bootstrap.sh' >&2
    exit 1
  fi
fi
if [ -f .agent-config/repo/bootstrap/bootstrap.ps1 ]; then
  cp -f .agent-config/repo/bootstrap/bootstrap.ps1 .agent-config/bootstrap.ps1 || \
    printf '%s\n' 'warning: could not copy .agent-config/bootstrap.ps1' >&2
fi
_ledger_target .gitignore
_ledger_target .agent-config/bootstrap.sh
_ledger_target .agent-config/bootstrap.ps1
_ledger_step finalize repo ok
if [ "${_LEDGER_INCOMPLETE:-false}" = true ]; then
  _ledger_write finalize false
else
  _ledger_write finalize true
fi
