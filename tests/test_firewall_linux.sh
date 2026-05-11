#!/usr/bin/env bash
# Tests for install_lib/firewall.sh.
#
# Verifies:
#   - nnm_firewall_build_cmd produces the right command for ufw vs firewall-cmd
#     and an empty string for unknown tools
#   - nnm_firewall_detect_tool returns "none" when neither ufw nor firewall-cmd
#     is available (we shadow them out of PATH for this case)
#   - nnm_firewall_apply with dry_run=1 returns 'would-create' / 'skipped-exists'
#     depending on the stubbed presence of the rule
#
# Runs without root; stubs the firewall tools so it never touches the host.
#
# Usage:
#   bash tests/test_firewall_linux.sh

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# shellcheck source=../install_lib/firewall.sh
. "$ROOT/install_lib/firewall.sh"

failed=0
total=0

check() {
    local label="$1"
    local cond="$2"
    total=$((total + 1))
    if [ "$cond" = "1" ]; then
        echo "  PASS  $label"
    else
        echo "  FAIL  $label"
        failed=$((failed + 1))
    fi
}

# 1. Build command for ufw.
out=$(nnm_firewall_build_cmd "ufw" 9500)
[ "$out" = "ufw allow 9500/tcp" ] && check "ufw build_cmd shape" 1 || check "ufw build_cmd shape (got: $out)" 0

# 2. Build command for firewall-cmd.
out=$(nnm_firewall_build_cmd "firewall-cmd" 9500)
expected="firewall-cmd --permanent --add-port=9500/tcp"
[ "$out" = "$expected" ] && check "firewall-cmd build_cmd shape" 1 || check "firewall-cmd build_cmd shape (got: $out)" 0

# 3. Unknown tool returns empty string.
out=$(nnm_firewall_build_cmd "iptables" 9500)
[ -z "$out" ] && check "unknown tool yields empty cmd" 1 || check "unknown tool yields empty cmd (got: $out)" 0

# 4. nnm_firewall_detect_tool with neither ufw nor firewall-cmd available.
#    We shadow them with a temp PATH that contains neither.
TMP_PATH_DIR=$(mktemp -d)
PATH_BACKUP="$PATH"
PATH="$TMP_PATH_DIR"
out=$(nnm_firewall_detect_tool)
PATH="$PATH_BACKUP"
rm -rf "$TMP_PATH_DIR"
[ "$out" = "none" ] && check "detect_tool returns 'none' when no tool present" 1 \
    || check "detect_tool returns 'none' (got: $out)" 0

# 5. nnm_firewall_apply with dry_run=1 and a stubbed rule_exists that
#    returns FALSE -> 'would-create'.
nnm_firewall_rule_exists() { return 1; }
out=$(nnm_firewall_apply "ufw" 9500 1)
[ "$out" = "would-create" ] && check "dry_run + missing rule -> would-create" 1 \
    || check "dry_run + missing rule -> would-create (got: $out)" 0

# 6. nnm_firewall_apply with dry_run=1 and a stubbed rule_exists that
#    returns TRUE -> 'skipped-exists'.
nnm_firewall_rule_exists() { return 0; }
out=$(nnm_firewall_apply "ufw" 9500 1)
[ "$out" = "skipped-exists" ] && check "dry_run + present rule -> skipped-exists" 1 \
    || check "dry_run + present rule -> skipped-exists (got: $out)" 0

# 7. nnm_firewall_apply with tool=none short-circuits.
out=$(nnm_firewall_apply "none" 9500 0)
[ "$out" = "none" ] && check "tool=none short-circuits" 1 \
    || check "tool=none short-circuits (got: $out)" 0

echo "---"
echo "$((total - failed))/$total passed"
[ $failed -eq 0 ] && exit 0 || exit 1
