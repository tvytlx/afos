# Shared check harness. Sourced, never executed.
#
# Every acceptance check prints one line and moves on -- a run that stops at the
# first failure tells you less than one that shows you all four things that
# broke.

AFOS_PASS=0
AFOS_FAIL=0

_green() { printf '\033[32m%s\033[0m' "$1"; }
_red()   { printf '\033[31m%s\033[0m' "$1"; }
_dim()   { printf '\033[2m%s\033[0m' "$1"; }

section() { printf '\n%s\n' "$(_dim "-- $* ")"; }

# ok <description> <command...>   -- passes if the command exits 0
ok() {
    local desc="$1"; shift
    if out=$("$@" 2>&1); then
        printf '  %s %s\n' "$(_green PASS)" "$desc"
        AFOS_PASS=$((AFOS_PASS + 1))
    else
        printf '  %s %s\n' "$(_red FAIL)" "$desc"
        printf '%s\n' "$out" | sed 's/^/         /'
        AFOS_FAIL=$((AFOS_FAIL + 1))
    fi
}

# no <description> <command...>   -- passes if the command exits non-zero
no() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        printf '  %s %s\n' "$(_red FAIL)" "$desc"
        AFOS_FAIL=$((AFOS_FAIL + 1))
    else
        printf '  %s %s\n' "$(_green PASS)" "$desc"
        AFOS_PASS=$((AFOS_PASS + 1))
    fi
}

verdict() {
    printf '\n  %d passed, %d failed\n\n' "$AFOS_PASS" "$AFOS_FAIL"
    [[ "$AFOS_FAIL" -eq 0 ]]
}
