#!/bin/bash
# Update to a released version: check out the code, rebuild the images, regenerate the
# server configs and firewall from config/site.json, and restart everything. Settings,
# users, passwords and programs are kept. Run it when no machine is transferring:
# services restart (about a minute), which cuts a drip-feed in progress.
#
#   sudo ./host/update.sh              the newest release
#   sudo ./host/update.sh v0.9         that release (or any branch or commit)
#   sudo ./host/update.sh main         follow the branch, as this did before releases
#   ./host/update.sh --list            the releases, and where this Pi is now
#   ./host/update.sh --dry-run [ref]   say what it would do, change nothing
#
# To go back, run it again with the version you were on; --list prints it.
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_DIR"

usage() {
    cat <<USAGE
Update PunchTape to a released version.

  sudo $0              the newest release
  sudo $0 v0.9         that release (or any branch or commit)
  sudo $0 main         follow the branch, as this did before releases
  $0 --list            the releases, and where this Pi is now
  $0 --dry-run [ref]   say what it would do, change nothing
USAGE
}

REF=""
DRY=0
LIST=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --list) LIST=1 ;;
        -h|--help) usage; exit 0 ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ -n "$REF" ]; then
                echo "Give only one version (got '$REF' and '$1')" >&2
                exit 2
            fi
            REF=$1
            ;;
    esac
    shift
done

# --- where this Pi is now ---------------------------------------------------

current_name() {  # the release this Pi is on, or the branch, or a bare commit
    local tag branch
    if tag=$(git describe --tags --exact-match 2>/dev/null); then
        echo "$tag"
        return 0
    fi
    branch=$(git rev-parse --abbrev-ref HEAD)
    if [ "$branch" != "HEAD" ]; then
        echo "$branch"
    else
        git rev-parse --short HEAD
    fi
}

WAS_COMMIT=$(git rev-parse HEAD)
WAS_NAME=$(current_name)

# --- fetch ------------------------------------------------------------------

if ! git fetch --quiet --tags origin 2>/dev/null; then
    if [ "$LIST" = 1 ]; then
        echo "Warning: could not reach the git remote; showing what is already on this Pi." >&2
    else
        echo "Could not reach the git remote. Check the network (the office side needs a" >&2
        echo "route to the internet), then run this again." >&2
        exit 1
    fi
fi

releases() { git tag --list 'v*' --sort=-v:refname; }

if [ "$LIST" = 1 ]; then
    echo "This Pi is on: $WAS_NAME ($(git rev-parse --short HEAD))"
    echo
    echo "Releases (newest first):"
    if [ -z "$(releases)" ]; then
        echo "  (none yet - 'sudo ./host/update.sh main' follows the branch instead)"
    else
        releases | sed 's/^/  /'
    fi
    exit 0
fi

# --- which version ----------------------------------------------------------

if [ -z "$REF" ]; then
    REF=$(releases | head -n 1)
    if [ -z "$REF" ]; then
        echo "No releases exist yet. Name a branch instead, for example:" >&2
        echo "  sudo $0 main" >&2
        exit 1
    fi
fi

KIND=""
if git show-ref --verify --quiet "refs/tags/$REF"; then
    KIND=release
elif git show-ref --verify --quiet "refs/remotes/origin/$REF"; then
    KIND=branch
elif git rev-parse --verify --quiet "$REF^{commit}" >/dev/null; then
    KIND=commit
else
    echo "No release, branch or commit called '$REF'." >&2
    echo "Run '$0 --list' to see the releases." >&2
    exit 1
fi

if [ "$KIND" = branch ]; then
    TARGET=$(git rev-parse "origin/$REF")
else
    TARGET=$(git rev-parse "$REF^{commit}")
fi

if [ "$DRY" = 1 ]; then
    echo "Now:    $WAS_NAME ($(git rev-parse --short "$WAS_COMMIT"))"
    echo "Would:  check out $KIND $REF ($(git rev-parse --short "$TARGET"))"
    if [ "$TARGET" = "$WAS_COMMIT" ]; then
        echo "        - already on this code; the images and configs would still be rebuilt"
    fi
    echo "        - rebuild the images, regenerate the configs, reload the firewall"
    echo "        - restart the services (about a minute)"
    exit 0
fi

# --- checks before changing anything ----------------------------------------

if [ -n "$(git status --porcelain)" ]; then
    echo "This copy of PunchTape has local changes, so it cannot be updated safely:" >&2
    git status --short >&2
    echo >&2
    echo "Keep them somewhere else, or throw them away with 'git checkout -- .', then" >&2
    echo "run this again. (config/, generated/ and .env are not part of the code and" >&2
    echo "are never touched by an update.)" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 $REF" >&2
    exit 1
fi

# --- update -----------------------------------------------------------------

# Everything up to "point of no return" only writes files the running containers are not
# reading yet, so a failure there can put the Pi back exactly as it was.
APPLYING=0
rollback() {
    local code=$?
    if [ "$APPLYING" = 1 ]; then
        echo >&2
        echo "The update failed while applying it. The Pi may be part-way between" >&2
        echo "$WAS_NAME and $REF. Check the Status page, then either run this again," >&2
        echo "or go back with:  sudo $0 $WAS_NAME" >&2
    else
        echo >&2
        echo "The update failed before anything was applied; putting the code back to" >&2
        echo "$WAS_NAME. The services were not restarted and are still running." >&2
        if git show-ref --verify --quiet "refs/heads/$WAS_NAME"; then
            # It was following a branch, which may already have been fast-forwarded.
            { git checkout --quiet "$WAS_NAME" && git reset --quiet --hard "$WAS_COMMIT"; } || true
        else
            git -c advice.detachedHead=false checkout --quiet --force "$WAS_COMMIT" || true
        fi
    fi
    exit $code
}
trap rollback ERR

echo "Updating from $WAS_NAME to $KIND $REF ..."
if [ "$KIND" = branch ]; then
    git checkout --quiet "$REF"
    git merge --quiet --ff-only "origin/$REF"
else
    git -c advice.detachedHead=false checkout --quiet "$REF"
fi

./host/build-images.sh
docker compose run --rm --no-deps punchtape-admin python -m punchtape.cli render
nft -c -f /etc/nftables.conf

APPLYING=1
nft -f /etc/nftables.conf
docker compose up -d --force-recreate --remove-orphans
trap - ERR

echo
echo "Updated to $REF ($(git log -1 --format='%h %s'))."
echo "Was on $WAS_NAME; 'sudo $0 $WAS_NAME' goes back."
echo "Check the Status page: services running, machines reachable."
