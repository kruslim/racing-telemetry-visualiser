# RTV v2 Pitwall - Headless Build Runner

Six staged Claude Code prompts that evolve racing-telemetry-visualiser from
v1 (post-hoc analysis + coach) into v2 (live multi-agent AI pitwall).

## Contents
- CLAUDE.md ................ shared context, auto-read by every Claude Code session
- prompts/01..06 ........... one stage per file, run in order
- run_pitwall.ps1 .......... Windows runner (use this one)
- run_pitwall.sh ........... bash runner (Git Bash / WSL)

All files are ASCII-only with CRLF so Windows PowerShell 5.1 cannot mangle them.

## Setup (from repo root)

    git checkout -b pitwall-v2
    git status                      # must be clean

Copy CLAUDE.md, prompts\, and run_pitwall.ps1 into the repo root, then:

    git add CLAUDE.md prompts run_pitwall.ps1
    git commit -m "pitwall: headless build scaffolding"

## Auth check (uses your subscription, NOT an API key)

    claude
    /status                         # confirm subscription auth, then exit
    # if it shows an API key and you want the subscription instead:
    #   Remove-Item Env:\ANTHROPIC_API_KEY

## Run

    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    .\.venv\Scripts\Activate.ps1
    .\run_pitwall.ps1

## Behaviour
- Each stage: fresh headless session -> full pytest gate -> auto-commit.
- Completed stages move to prompts\done\, so re-running resumes where it stopped.
- Any failure stops the run immediately; nothing builds on a broken stage.

## If it stops
- 401 auth error:  run `claude`, then /login, then re-run the script.
- pytest red:      `claude "pytest is failing after <stage>. Read docs/PITWALL.md
                    and the failures, then fix until green."` then re-run.
- bad stage:       `git reset --hard <last good commit>`, move the prompt back
                    out of prompts\done\, re-run.

## API key
Not needed for the build. Only needed to actually run the pitwall live
(ANTHROPIC_API_KEY) - the agents call the Anthropic API at race time.
