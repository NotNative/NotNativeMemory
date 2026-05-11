# Firewall configuration helpers for install_linux.sh.
#
# Two functions:
#   nnm_firewall_detect_tool  -- returns "ufw" | "firewall-cmd" | "none"
#                                based on what is installed and active.
#   nnm_firewall_build_cmd    -- pure: builds the command string that
#                                would open the given port. Caller decides
#                                whether to eval it or print it.
#   nnm_firewall_rule_exists  -- detects whether the rule is already in place.
#                                Tools differ; this wraps the per-tool check.
#
# Returns "skipped-exists" / "would-create" / "created" / "failed" via
# echo so callers can branch on it.

nnm_firewall_detect_tool() {
    if command -v ufw >/dev/null 2>&1; then
        # ufw must be active for the rule to mean anything. If it is
        # installed but inactive, treat it as "none" so we do not silently
        # add a rule that has no effect.
        if ufw status 2>/dev/null | grep -q "Status: active"; then
            echo "ufw"
            return 0
        fi
    fi

    if command -v firewall-cmd >/dev/null 2>&1; then
        if firewall-cmd --state >/dev/null 2>&1; then
            echo "firewall-cmd"
            return 0
        fi
    fi

    echo "none"
}

nnm_firewall_build_cmd() {
    # $1: tool ("ufw" | "firewall-cmd")
    # $2: port (integer)
    local tool="$1"
    local port="$2"

    case "$tool" in
        ufw)
            echo "ufw allow ${port}/tcp"
            ;;
        firewall-cmd)
            # --permanent so it survives reboots; the caller may also run
            # --reload after.
            echo "firewall-cmd --permanent --add-port=${port}/tcp"
            ;;
        *)
            echo ""
            ;;
    esac
}

nnm_firewall_rule_exists() {
    # $1: tool, $2: port. Returns 0 if rule already present, 1 otherwise.
    local tool="$1"
    local port="$2"

    case "$tool" in
        ufw)
            ufw status 2>/dev/null | grep -E "^${port}(/tcp)?\s+ALLOW" >/dev/null
            return $?
            ;;
        firewall-cmd)
            firewall-cmd --query-port="${port}/tcp" >/dev/null 2>&1
            return $?
            ;;
        *)
            return 1
            ;;
    esac
}

nnm_firewall_apply() {
    # $1: tool, $2: port, $3: dry_run ("1" to skip execution).
    # Echoes one of: skipped-exists / would-create / created / failed / none.
    local tool="$1"
    local port="$2"
    local dry_run="${3:-0}"

    if [ "$tool" = "none" ]; then
        echo "none"
        return 0
    fi

    if nnm_firewall_rule_exists "$tool" "$port"; then
        echo "skipped-exists"
        return 0
    fi

    if [ "$dry_run" = "1" ]; then
        echo "would-create"
        return 0
    fi

    local cmd
    cmd="$(nnm_firewall_build_cmd "$tool" "$port")"
    if [ -z "$cmd" ]; then
        echo "failed"
        return 1
    fi

    if eval "$cmd" >/dev/null 2>&1; then
        # firewall-cmd needs a reload to activate the --permanent rule.
        if [ "$tool" = "firewall-cmd" ]; then
            firewall-cmd --reload >/dev/null 2>&1 || true
        fi
        echo "created"
        return 0
    fi
    echo "failed"
    return 1
}
