#!/bin/bash
# host/update.sh picks the right version and refuses the cases that would damage a Pi.
# Runs against throwaway git repositories, so nothing here builds images or restarts
# anything: --dry-run and --list stop before the first change.
set -uo pipefail

PROJECT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fails=0

check() {  # condition already evaluated by the caller: "$1" is 0 for pass
    if [ "$1" = 0 ]; then echo "PASS $2"; else echo "FAIL $2"; fails=$((fails + 1)); fi
}

contains() {  # haystack, needle
    case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac
}

same() { [ "$1" = "$2" ]; }

line_of() { printf '%s\n' "$1" | grep -n "$2" | head -n 1 | cut -d: -f1; }

earlier_than() { [ "$(line_of "$1" "$2")" -lt "$(line_of "$1" "$3")" ]; }

git_q() { git -c user.name=t -c user.email=t@example.com -c commit.gpgsign=false "$@" >/dev/null 2>&1; }

# --- a remote with three releases and a branch ahead of them ----------------
ORIGIN="$WORK/origin"
mkdir -p "$ORIGIN"
(
    cd "$ORIGIN" || exit 1
    git_q init -b main
    echo one > file.txt; git_q add -A; git_q commit -m one; git_q tag -a v0.1 -m v0.1
    echo two > file.txt; git_q add -A; git_q commit -m two; git_q tag -a v0.2 -m v0.2
    # v0.10 must beat v0.9 and v0.2: version order, not alphabetical order
    echo nine > file.txt; git_q add -A; git_q commit -m nine; git_q tag -a v0.9 -m v0.9
    echo ten > file.txt; git_q add -A; git_q commit -m ten; git_q tag -a v0.10 -m v0.10
    echo unreleased > file.txt; git_q add -A; git_q commit -m unreleased
)

PI="$WORK/pi"
git_q clone "$ORIGIN" "$PI"
mkdir -p "$PI/host"
cp "$PROJECT/host/update.sh" "$PI/host/update.sh"
chmod +x "$PI/host/update.sh"
# The copied script is not part of the test repository's history; ignore it, the way
# config/ and generated/ are ignored on a real Pi.
echo "host/update.sh" > "$PI/.git/info/exclude"
(cd "$PI" && git_q checkout v0.1)        # start on an old release, as a stale Pi would be

U="$PI/host/update.sh"
run() { (cd "$PI" && "$U" "$@" 2>&1); }

# --- --list -----------------------------------------------------------------
out=$(run --list)
contains "$out" "This Pi is on: v0.1"; check $? "--list says which release the Pi is on"
contains "$out" "v0.10"; check $? "--list shows the releases"
earlier_than "$out" 'v0\.10' 'v0\.9'
check $? "--list puts v0.10 above v0.9 (version order, not alphabetical)"

# --- the default is the newest release, not the tip of the branch -----------
out=$(run --dry-run)
contains "$out" "check out release v0.10"; check $? "with no version it picks the newest release"
! contains "$out" "unreleased"; check $? "...not the unreleased tip of main"

# --- naming a version -------------------------------------------------------
out=$(run --dry-run v0.2)
contains "$out" "check out release v0.2"; check $? "an older release can be named (going back)"

out=$(run --dry-run main)
contains "$out" "check out branch main"; check $? "a branch still works, for following main"

sha=$(cd "$PI" && git rev-parse --short HEAD)
out=$(run --dry-run "$sha")
contains "$out" "check out commit"; check $? "a bare commit works"

out=$(run --dry-run v0.1)
contains "$out" "already on this code"; check $? "says so when the Pi is already on that version"

# --- things that must be refused --------------------------------------------
out=$(run --dry-run v9.9.9); rc=$?
[ "$rc" != 0 ] && contains "$out" "No release, branch or commit"
check $? "refuses a version that does not exist"

out=$(run --dry-run v0.1 v0.2); rc=$?
[ "$rc" != 0 ] && contains "$out" "only one version"
check $? "refuses two versions at once"

out=$(run --dry-run --nonsense); rc=$?
[ "$rc" != 0 ] && contains "$out" "Unknown option"
check $? "refuses an unknown option"

# --- a dry run changes nothing ----------------------------------------------
before=$(cd "$PI" && git rev-parse HEAD)
run --dry-run v0.10 >/dev/null
same "$before" "$(cd "$PI" && git rev-parse HEAD)"
check $? "--dry-run leaves the checked-out code alone"

# --- local edits block an update --------------------------------------------
# Not --dry-run: this check guards the real update, and must come before any change.
echo "a local fix" >> "$PI/file.txt"
out=$(cd "$PI" && "$U" v0.10 2>&1); rc=$?
[ "$rc" != 0 ] && contains "$out" "local changes"
check $? "refuses to update over local edits to the code"
contains "$out" "file.txt"; check $? "...and names the edited file"
(cd "$PI" && git_q checkout -- file.txt)

# --- without sudo it stops before touching anything --------------------------
if [ "$(id -u)" -ne 0 ]; then
    out=$(cd "$PI" && "$U" v0.10 2>&1); rc=$?
    [ "$rc" != 0 ] && contains "$out" "Run with sudo"
    check $? "a real update needs root"
    same "$before" "$(cd "$PI" && git rev-parse HEAD)"
    check $? "...and stops before checking the new code out"
fi

# --- a Pi with no releases yet ----------------------------------------------
BARE="$WORK/bare-origin"
mkdir -p "$BARE"
(
    cd "$BARE" || exit 1
    git_q init -b main
    echo x > file.txt; git_q add -A; git_q commit -m only
)
PI2="$WORK/pi2"
git_q clone "$BARE" "$PI2"
mkdir -p "$PI2/host"; cp "$PROJECT/host/update.sh" "$PI2/host/update.sh"; chmod +x "$PI2/host/update.sh"
echo "host/update.sh" > "$PI2/.git/info/exclude"
out=$(cd "$PI2" && ./host/update.sh --dry-run 2>&1); rc=$?
[ "$rc" != 0 ] && contains "$out" "No releases exist yet"
check $? "with no releases it asks for a branch rather than guessing"
out=$(cd "$PI2" && ./host/update.sh --list 2>&1)
contains "$out" "none yet"; check $? "--list says there are no releases yet"

echo
if [ "$fails" = 0 ]; then echo "ALL PASSED"; else echo "$fails FAILED"; fi
exit $((fails > 0))
