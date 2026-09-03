#!/bin/sh
# install.sh — install personal skills into a Claude Code skills directory.
#
# Copy-based by default: installs an immutable snapshot rather than a symlink
# into a live checkout. A symlinked checkout breaks when it sits under a synced
# folder, when a branch switch silently mutates every installed skill, or when
# the checkout is mid-update. Use --mode link only for skill development.
#
# Usage:
#   ./install.sh [options]
#     --prefix DIR     install root (default: $HOME/.claude/skills)
#     --mode copy|link copy (default) or symlink for development
#     --only NAME      install just this skill; repeatable
#     --dry-run        print what would happen, change nothing
#     --force          replace a directory even without our ownership marker
#     --uninstall      remove skills this repo installed, then exit
#
# POSIX sh. Works with git 2.23 (macOS) and 2.34+ (clusters). No GNU-only flags.

set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${HOME}/.claude/skills"
MODE=copy
DRY=0
FORCE=0
ALLOW_ORG_SHADOW=0
UNINSTALL=0
ONLY=""
MARKER=".installed-by-multi-agent-skills"

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)    PREFIX="$2"; shift 2 ;;
    --mode)      MODE="$2"; shift 2 ;;
    --only)      ONLY="$ONLY $2"; shift 2 ;;
    --dry-run)   DRY=1; shift ;;
    --force)     FORCE=1; shift ;;
    --allow-org-shadow) ALLOW_ORG_SHADOW=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$MODE" in copy|link) ;; *) echo "--mode must be copy or link" >&2; exit 2 ;; esac

umask 077

say()  { printf '%s\n' "$*"; }
step() { [ "$DRY" -eq 1 ] && printf 'would: %s\n' "$*" || printf '%s\n' "$*"; }

# --- source version ---------------------------------------------------------

VERSION="unknown"
# ASK git whether this is a checkout; do not stat a path. `.git` is a DIRECTORY
# in a clone and a FILE in a worktree, so `-d` answered "not a checkout" for
# every worktree -- and installing from an attempt worktree, to try a change
# before merging it, is the normal case here. The marker then recorded
# version=unknown, which reads like a value rather than a failure, and doctor
# reported provenance it had been handed for free.
if command -v git >/dev/null 2>&1 &&
   git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  VERSION=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)
  if [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ]; then
    VERSION="${VERSION}-dirty"
  fi
fi

# --- uninstall --------------------------------------------------------------

if [ "$UNINSTALL" -eq 1 ]; then
  [ -d "$PREFIX" ] || { say "nothing installed at $PREFIX"; exit 0; }
  removed=0
  kept=0
  for d in "$PREFIX"/*; do
    [ -e "$d" ] || [ -L "$d" ] || continue
    name=$(basename "$d")
    ours=0
    # OURS means one of exactly two things:
    #   - a real directory carrying our marker file, or
    #   - a symlink that points INTO THIS CHECKOUT (a --mode link install).
    #
    # It used to mean "a marker OR ANY SYMLINK", and that removed two skills
    # belonging to someone else, because they happened to be symlinks to
    # elsewhere. rm -rf on a symlink takes only the link, but the skills were
    # gone from the skills directory just the same. Ownership is a property we
    # record, never a shape we infer.
    if [ -L "$d" ]; then
      target=$(cd "$(dirname "$d")" && cd "$(readlink "$d")" 2>/dev/null && pwd) || target=""
      case "$target" in
        "$REPO"/skills/*) ours=1 ;;
      esac
    elif [ -f "$d/$MARKER" ]; then
      ours=1
    fi
    if [ "$ours" -eq 1 ]; then
      step "remove $d"
      [ "$DRY" -eq 1 ] || rm -rf "$d"
      removed=$((removed + 1))
    else
      kept=$((kept + 1))
    fi
  done
  say "removed $removed skill(s) from $PREFIX"
  [ "$kept" -gt 0 ] && say "left $kept skill(s) alone: not installed by this repo"
  exit 0
fi

# --- validate before touching the destination -------------------------------

[ -d "$REPO/skills" ] || { echo "error: no skills/ in $REPO" >&2; exit 1; }

problems=""
for src in "$REPO"/skills/*; do
  [ -d "$src" ] || continue
  name=$(basename "$src")
  case "$name" in .*|*.bak|*.bak.*)
    problems="$problems\n  $name: not a valid skill directory name"; continue ;;
  esac
  [ -f "$src/SKILL.md" ] || problems="$problems\n  $name: missing SKILL.md"
  head -n 1 "$src/SKILL.md" 2>/dev/null | grep -q '^---$' \
    || problems="$problems\n  $name: SKILL.md lacks YAML frontmatter"
  grep -q '^name:' "$src/SKILL.md" 2>/dev/null \
    || problems="$problems\n  $name: SKILL.md has no name: field"
  # Scripts must parse with the interpreter that will run them.
  for py in "$src"/scripts/*.py; do
    [ -f "$py" ] || continue
    python3 -m py_compile "$py" 2>/dev/null \
      || problems="$problems\n  $name: $(basename "$py") fails to compile"
  done
  for sh_ in "$src"/scripts/*.sh; do
    [ -f "$sh_" ] || continue
    sh -n "$sh_" 2>/dev/null \
      || problems="$problems\n  $name: $(basename "$sh_") has a syntax error"
  done
done

if [ -n "$problems" ]; then
  printf 'error: refusing to install:%b\n' "$problems" >&2
  exit 1
fi

# --- collision check against an org-managed skill store ----------------------
# The comment here used to say "personal skills are prefixed hanig-* precisely
# so this never fires". That premise is simply wrong: a copy of these skills is
# maintained on Claude Science, so all five arrive in the Arc org store through
# catalog sync. It is deliberate and permanent, not an accident to be cleaned
# up, and while the guard was a hard exit 1 the repo was uninstallable and the
# documented Quick Start printed an error.
#
# The guard is still right about the hazard. Which store wins is not
# guaranteed, so installing a skill whose name already exists there can mean
# the copy that loads is not the copy you just installed, silently.
#
# What it cannot do is tell "an unrelated Arc skill that happens to share a
# name" from "a stale snapshot of the very thing I am installing". Both look
# identical from here, and only the first is dangerous. So it stops being a
# hard refusal and becomes a refusal WITH AN ACTION: shadowing is allowed, and
# has to be asked for.

shadowed=""
for orgdir in "$HOME"/.claude-science/orgs/*/skills; do
  [ -d "$orgdir" ] || continue
  for src in "$REPO"/skills/*; do
    [ -d "$src" ] || continue
    name=$(basename "$src")
    if [ -e "$orgdir/$name" ]; then
      shadowed="$shadowed $name"
    fi
  done
done

if [ -n "$shadowed" ]; then
  if [ "$ALLOW_ORG_SHADOW" -eq 1 ]; then
    for name in $shadowed; do
      echo "note: '$name' also exists in an org-managed store; installing ""anyway (--allow-org-shadow). Which copy loads is not guaranteed." >&2
    done
  else
    echo "error: these skills also exist in an org-managed skill store:" >&2
    for name in $shadowed; do echo "         $name" >&2; done
    echo "       Which copy the loader picks is NOT guaranteed, so the one" >&2
    echo "       that loads may not be the one you just installed." >&2
    echo "       If those org copies are older snapshots of THIS repo, that" >&2
    echo "       is expected and harmless: pass --allow-org-shadow." >&2
    echo "       If they are unrelated Arc skills, rename ours instead." >&2
    exit 1
  fi
fi

# --- install ----------------------------------------------------------------

step "mkdir -p $PREFIX"
[ "$DRY" -eq 1 ] || mkdir -p "$PREFIX"

installed=0
skipped=0
for src in "$REPO"/skills/*; do
  [ -d "$src" ] || continue
  name=$(basename "$src")
  case "$name" in .*|*.bak|*.bak.*) continue ;; esac

  if [ -n "$ONLY" ]; then
    case " $ONLY " in *" $name "*) ;; *) continue ;; esac
  fi

  dest="$PREFIX/$name"

  # Never clobber something we did not put there.
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    # A symlink is NOT proof of ownership here either; see the uninstall
    # comment above. Only our marker, a link into this checkout, or an
    # explicit --force may replace what is already there.
    ours_dest=0
    if [ -L "$dest" ]; then
      dtarget=$(cd "$(dirname "$dest")" && cd "$(readlink "$dest")" 2>/dev/null && pwd) || dtarget=""
      case "$dtarget" in "$REPO"/skills/*) ours_dest=1 ;; esac
    elif [ -f "$dest/$MARKER" ]; then
      ours_dest=1
    fi
    if [ "$ours_dest" -eq 1 ] || [ "$FORCE" -eq 1 ]; then
      step "replace $dest"
      [ "$DRY" -eq 1 ] || rm -rf "$dest"
    else
      say "skip $name: $dest exists and was not installed by this repo (--force to override)"
      skipped=$((skipped + 1))
      continue
    fi
  fi

  if [ "$MODE" = "link" ]; then
    step "link $dest -> $src"
    [ "$DRY" -eq 1 ] || ln -s "$src" "$dest"
  else
    # Stage on the destination filesystem, then move into place, so a failed
    # copy never leaves a half-installed skill behind.
    tmp="$PREFIX/.tmp-$name-$$"
    [ "$DRY" -eq 1 ] || {
      rm -rf "$tmp"
      cp -R "$src" "$tmp"
      printf 'repo=multi-agent-skills\nversion=%s\ninstalled_at=%s\n' \
        "$VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp/$MARKER"
      find "$tmp" -name '*.py' -exec chmod +x {} + 2>/dev/null || true
      find "$tmp" -name '*.sh' -exec chmod +x {} + 2>/dev/null || true
      mv "$tmp" "$dest"
    }
    step "install $dest (copy, $VERSION)"
  fi
  installed=$((installed + 1))
done

# --- prune skills this repo used to ship and no longer does -----------------
#
# Re-running install did NOT remove a skill deleted upstream, so a machine that
# installed an older version kept loading it forever. Not hypothetical: this
# repo has deleted two skills, and both were still sitting on all three
# clusters. Claude Code loads whatever is in the directory, so a deleted skill
# stays live until someone notices.
#
# Only ever removes a directory carrying OUR marker, so a skill somebody else
# installed is never touched. Skipped entirely under --only, where the caller
# has deliberately narrowed the set.
pruned=0
if [ -z "$ONLY" ] && [ -d "$PREFIX" ]; then
  for d in "$PREFIX"/*; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ -f "$d/$MARKER" ] || continue          # not ours: leave it alone
    [ -d "$REPO/skills/$name" ] && continue  # still shipped
    step "prune $d (no longer in this repo)"
    [ "$DRY" -eq 1 ] || rm -rf "$d"
    pruned=$((pruned + 1))
  done
fi

say ""
say "installed $installed skill(s) to $PREFIX  [mode=$MODE version=$VERSION]"
[ "$pruned" -gt 0 ] && say "pruned $pruned skill(s) this repo no longer ships"
[ "$skipped" -gt 0 ] && say "skipped $skipped (pre-existing, not ours)"
[ "$DRY" -eq 1 ] && say "(dry run — nothing changed)"

say ""
say "verify with: sh $REPO/bin/doctor"
exit 0
