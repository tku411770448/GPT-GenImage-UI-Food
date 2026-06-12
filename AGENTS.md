# GPTUI70 Codex Working Rules

## Git / Version Control Rules

This project uses local Git version control only.

Do not run:
- git push
- git pull
- git remote add
- git remote remove
- Publish Branch

Before editing:
- Run `git status`.
- Confirm the working tree is clean, unless the user explicitly asks to continue from existing changes.

After editing:
- Run `git status`.
- Run `git diff --stat`.
- Review whether generated files, logs, exports, zip files, API keys, `.env`, or temporary images are accidentally included.
- Do not commit files ignored by `.gitignore`.

Validation:
- If Python files were changed, run a reasonable validation command, such as:
  `python -m compileall .`

Commit rule:
- If the task is complete and validation passes, create a local commit.
- Use:
  `git add -A`
  `git commit -m "<clear commit message>"`

Commit message style:
- Use clear English commit messages.
- Examples:
  - Fix Step sidebar responsive layout
  - Add Step 2 drag and drop upload
  - Update export artifact structure
  - Fix API key loading on Windows

Final response:
- Report changed files.
- Report validation command and result.
- Report local commit hash.

將目前專案的內容對 README.md 做同步的修正