---
name: finishing-feature
description: Commit all changes and open a Merge/Pull Request (supports GitLab and GitHub)
disable-model-invocation: true
allowed-tools: Bash
---

You are finishing a development task. Follow these steps without asking for confirmation:

## Step 0 — Detect platform

Run:
```bash
git remote get-url origin
```

- If the URL contains `github.com` → use **GitHub** mode (use `gh pr create`)
- Otherwise → use **GitLab** mode (use `glab mr create`)

## Step 1 — Commit

1. Run `git status` to see what changed.
2. Stage everything: `git add -A`
3. Write a concise, meaningful commit message that explains *why* the change was made (not just what). Use the format:

   ```
   <type>: <summary>

   <optional body explaining motivation or key decisions>
   ```

   Common types: `feat`, `fix`, `refactor`, `docs`, `chore`.

4. Commit: `git commit -m "<message>"`

## Step 2 — Create MR/PR

Use the correct CLI for the detected platform.

### GitLab — `glab mr create`

Requirements:
- `--title`: one-line summary (same as the commit subject)
- `--description`: brief description of what changed and why
- Include any `--label` flags passed to this skill as additional arguments
- `--assignee @me`
- Do **not** use `--draft`

Example:
```bash
glab mr create \
  --title "feat: add user avatar upload" \
  --description "Implements avatar upload endpoint and wires it to the profile page." \
  --assignee @me
```

### GitHub — `gh pr create`

Requirements:
- `--title`: one-line summary (same as the commit subject)
- `--body`: brief description of what changed and why
- Include any `--label` flags passed to this skill as additional arguments
- Do **not** use `--draft`

Example:
```bash
gh pr create \
  --title "feat: add user avatar upload" \
  --body "Implements avatar upload endpoint and wires it to the profile page."
```

After the MR/PR is created, print the MR/PR URL on its own line so it can be detected by the task runner.
