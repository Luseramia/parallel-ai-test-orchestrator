#!/bin/sh

# Clone one exact commit while keeping the read-only Git credential inside the
# init container. The application runner receives only the populated workspace
# volume and never inherits GIT_READ_TOKEN.
set -eu

: "${GIT_CLONE_URL:?GIT_CLONE_URL is required}"
: "${GIT_SHA:?GIT_SHA is required}"
: "${GIT_READ_TOKEN:?GIT_READ_TOKEN is required}"

case "${GIT_SHA}" in
  *[!0-9a-f]*)
    echo "GIT_SHA must be a full lowercase Git SHA" >&2
    exit 2
    ;;
esac
if [ "${#GIT_SHA}" -ne 40 ]; then
  echo "GIT_SHA must be a full lowercase Git SHA" >&2
  exit 2
fi

repository_path="${PRECLONED_REPOSITORY:-/workspace/repository}"
workspace_root=$(dirname "${repository_path}")
askpass=$(mktemp /tmp/ai-test-git-askpass.XXXXXX)

cleanup() {
  rm -f -- "${askpass}"
}
trap cleanup EXIT HUP INT TERM

cat >"${askpass}" <<'EOF'
#!/bin/sh
case "${1:-}" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "${GIT_READ_TOKEN}" ;;
  *) exit 1 ;;
esac
EOF
chmod 700 "${askpass}"

mkdir -p "${workspace_root}"
if [ -e "${repository_path}" ]; then
  echo "repository workspace already exists" >&2
  exit 2
fi

export GIT_ASKPASS="${askpass}"
export GIT_TERMINAL_PROMPT=0

git init "${repository_path}"
git -C "${repository_path}" remote add origin "${GIT_CLONE_URL}"
git -C "${repository_path}" fetch --depth=1 origin "${GIT_SHA}"
git -C "${repository_path}" checkout --detach "${GIT_SHA}"

actual_sha=$(git -C "${repository_path}" rev-parse HEAD)
if [ "${actual_sha}" != "${GIT_SHA}" ]; then
  echo "checked out Git SHA does not match requested SHA" >&2
  exit 2
fi

# Do not leave the credential helper configuration or token-bearing process
# environment for the main container. EmptyDir contains Git data only.
unset GIT_ASKPASS GIT_READ_TOKEN
