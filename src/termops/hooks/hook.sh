# Termops — Bash/Zsh terminal hook
# Source this in your .bashrc or .zshrc to auto-capture command errors.
#
# Install:  termops hook install
# Status:   termops hook status
# Uninstall: termops hook uninstall
#
# Or manually add to your shell config:
#   source "$TERMOPS_HOME/hook.sh"

__termops_last_exit=0
__termops_last_command=""

__termops_preexec() {
    __termops_last_command="$1"
}

__termops_precmd() {
    __termops_last_exit=$?

    if [ "$__termops_last_exit" -ge 1 ]; then
        hook_file="${TERMOPS_HOME:-$HOME/.termops}/hook_status.txt"
        if [ -f "$hook_file" ] && [ "$(cat "$hook_file")" = "enabled" ]; then
            # Capture the last error message from the command
            # The actual error output is captured by the agent's daemon
            termops analyze --text "Command '$__termops_last_command' exited with code $__termops_last_exit" --source "sh-hook" 2>/dev/null &
        fi
    fi
}

# Install hooks based on shell
case "$(basename "$SHELL")" in
    zsh)
        autoload -Uz add-zsh-hook
        add-zsh-hook preexec __termops_preexec
        add-zsh-hook precmd __termops_precmd
        ;;
    bash)
        trap '__termops_preexec "$BASH_COMMAND"' DEBUG
        PROMPT_COMMAND="__termops_precmd;${PROMPT_COMMAND:+$PROMPT_COMMAND}"
        ;;
esac