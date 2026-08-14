# Termops — PowerShell terminal hook
# Source this in your PowerShell profile to auto-capture command errors.
#
# Install:  termops hook install
# Status:   termops hook status
# Uninstall: termops hook uninstall
#
# Or manually add to your $PROFILE:
#   . "$env:TERMOPS_HOME\hook.ps1"

$Global:__termops_last_command = ""
$Global:__termops_last_exit = 0

# Override the prompt function to capture last command output
$original_prompt = Get-Content Function:\prompt -ErrorAction SilentlyContinue

function prompt {
    $Global:__termops_last_exit = $LASTEXITCODE

    if ($Global:__termops_last_exit -ge 1) {
        # Only capture if hook is enabled and not already analyzing
        $hookFile = "$env:TERMOPS_HOME\hook_status.txt"
        if ((Test-Path $hookFile) -and (Get-Content $hookFile -Raw).Trim() -eq "enabled") {
            $errorText = $Error[0].Exception.Message -replace "`n", " "
            if ($errorText) {
                termops analyze --text "$errorText" --source "ps-hook" 2>$null
            }
        }
    }

    # Call original prompt function
    if ($original_prompt) {
        & $original_prompt
    } else {
        "PS $($executionContext.SessionState.Path.CurrentLocation)$('>' * ($nestedPromptLevel + 1)) "
    }
}