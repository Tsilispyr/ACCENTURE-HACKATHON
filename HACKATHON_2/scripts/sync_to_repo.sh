#!/usr/bin/env bash
# Copy this project into the ACCENTURE-HACKATHON clone, under HACKATHON_2/.
#
# TWO REPOSITORIES, ON PURPOSE. The private working repository holds everything and
# keeps everything: the plan, the status log, the defect log, the team split.
# `ACCENTURE-HACKATHON` is the DELIVERABLE, one folder per hackathon beside
# HACKATHON_1, and it carries only what a judge or a new engineer needs to run
# and understand the app. INTERNAL_DOCS below is that filter.
#
# WHY THIS EXISTS. Development happens here, in C:/projects/hackathon2, because
# this is where .venv, .env and the Chroma index live. The git history lives in
# a different repo that keeps hackathon2/ as a folder beside HACKATHON_1/. Two
# copies of a tree that must agree is a recipe for committing a stale one, so
# the copy is a command rather than a habit.
#
# What it copies: exactly the files `git ls-files -co --exclude-standard`
# reports, which is the set git would commit - so .env, .venv, .chroma,
# __pycache__ and the rest are excluded by the SAME rules, in one place, rather
# than by a second list in this script that would drift from .gitignore.
#
# What it removes: files in the target that no longer exist here. Without that,
# a renamed or deleted file lingers in the repo forever and the two trees
# silently disagree in the one direction nobody checks.
#
#   bash scripts/sync_to_repo.sh            # copy, then show git status
#   bash scripts/sync_to_repo.sh --dry-run  # show what would change

if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
    echo "ERROR: this script needs bash." >&2
    exit 1
fi

set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${SYNC_TARGET:-$(cd "$SOURCE/.." && pwd)/ACCENTURE-HACKATHON-main/HACKATHON_2}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

REPO_ROOT="$(dirname "$TARGET")"
if [ ! -d "$REPO_ROOT/.git" ]; then
    echo "ERROR: $REPO_ROOT is not a git repository." >&2
    echo "       Set SYNC_TARGET to the hackathon2 folder inside your clone." >&2
    exit 1
fi

cd "$SOURCE"

# The file list git itself would use. Tracked files plus untracked ones that
# are not ignored - never the ignored ones.
# Working documents that stay in the development repo. Not secrets and not
# embarrassing - just not part of the deliverable. A judge wants to know how
# the app works, not how the team divided the week or what the plan was on
# Tuesday. The handout's section 15 lists what the repository must contain and
# none of these is on it.
INTERNAL_DOCS=(
    PLAN.md          # what we intended to build
    TEAM_PLAN.md     # who owns which part
    STATUS.md        # the running work log
    HANDOVER.md      # notes for the next session
    PROBLEMS.md      # the defect log
    DECISIONS.md     # the decision log: why, at length, for us
    CORPUS.md        # the playbook for re-deriving numbers after a corpus change
    RUNBOOK.md       # operating the stack: the team removed it from the deliverable
)

# Whole directories that stay behind. Same reasoning as INTERNAL_DOCS, but a
# prefix match, because these are trees rather than single files.
INTERNAL_DIRS=(
    course-material/   # the provider's course content: reference, not ours to ship
    team-docs/         # role reports and the contribution record: source for the deck
)

is_internal() {
    local candidate="$1" name prefix
    for name in "${INTERNAL_DOCS[@]}"; do
        [ "$candidate" = "$name" ] && return 0
    done
    for prefix in "${INTERNAL_DIRS[@]}"; do
        case "$candidate" in "$prefix"*) return 0 ;; esac
    done
    return 1
}

mapfile -t ALL_FILES < <(git ls-files -co --exclude-standard)
FILES=()
for file in "${ALL_FILES[@]}"; do
    is_internal "$file" || FILES+=("$file")
done
if [ "${#FILES[@]}" -eq 0 ]; then
    echo "ERROR: no files to sync. Is $SOURCE a git repository?" >&2
    exit 1
fi

# Refuse to sync anything that looks like a real secret, whatever .gitignore
# says. A guard that only runs when the config is right is not a guard.
for file in "${FILES[@]}"; do
    case "$file" in
        .env|*/.env|*.pem|*.key|*_rsa)
            echo "REFUSING to sync $file - that looks like a secret." >&2
            echo "       Check .gitignore before running this again." >&2
            exit 1
            ;;
    esac
done

echo "source : $SOURCE"
echo "target : $TARGET"
echo "files  : ${#FILES[@]}"
[ "$DRY_RUN" -eq 1 ] && echo "MODE   : dry run, nothing will be written"
echo

# Compare with line endings normalised. This tree is checked out CRLF on
# Windows; the target is normalised to LF by .gitattributes the moment git
# touches it. A byte comparison therefore reports every text file as changed on
# every run, which would make the output useless for seeing what ACTUALLY
# changed - and the churn it copies is undone by git on the next add anyway.
same_content() {
    [ -f "$2" ] || return 1
    cmp -s <(tr -d '\r' < "$1") <(tr -d '\r' < "$2")
}

copied=0
for file in "${FILES[@]}"; do
    [ -f "$file" ] || continue
    destination="$TARGET/$file"
    if same_content "$file" "$destination"; then
        continue
    fi
    echo "  update  $file"
    if [ "$DRY_RUN" -eq 0 ]; then
        mkdir -p "$(dirname "$destination")"
        cp -p "$file" "$destination"
    fi
    copied=$((copied + 1))
done

# Anything in the target that is no longer here. Compared against the same
# list, so a file that became ignored is removed too, which is correct: it
# would not be committed from here either.
removed=0
if [ -d "$TARGET" ]; then
    wanted="$(printf '%s\n' "${FILES[@]}" | sort)"
    while IFS= read -r existing; do
        relative="${existing#"$TARGET"/}"
        case "$relative" in .git|.git/*) continue ;; esac
        if ! printf '%s\n' "$wanted" | grep -qxF "$relative"; then
            echo "  remove  $relative"
            [ "$DRY_RUN" -eq 0 ] && rm -f "$existing"
            removed=$((removed + 1))
        fi
    done < <(find "$TARGET" -type f 2>/dev/null)
fi

echo
echo "$copied updated, $removed removed"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry run: nothing written"
    exit 0
fi

echo
echo "git status in $REPO_ROOT (HACKATHON_2/ only):"
git -C "$REPO_ROOT" status --short hackathon2/ | head -30
echo
echo "next:  git -C '$REPO_ROOT' add HACKATHON_2/ && git -C '$REPO_ROOT' commit"
