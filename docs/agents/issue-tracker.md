# Issue tracker: GitHub

Engineering skills track issues in the GitHub repository for this project (not yet created; set it here when it is).
Run `./scripts/github_bootstrap.sh --apply` once to create the triage labels and protect `main`.
Use the `gh` CLI.
Tracker mutations follow repository authorization rules.

## Operations

- Read: `gh issue view <number> --comments`.
- List: `gh issue list --state open --json number,title,labels`.
- Create: `gh issue create --title "..." --body-file <path>`.
- Comment: `gh issue comment <number> --body-file <path>`.
- Add labels: `gh issue edit <number> --add-label "..."`.
- Remove labels: `gh issue edit <number> --remove-label "..."`.
- Close: `gh issue close <number>`.

Run inside this clone, or supply `--repo <owner>/<repo>`.
Use files containing actual newlines for multiline issue bodies and comments.

## Skill instructions

“Publish to the issue tracker” means create a GitHub issue.
“Fetch the relevant ticket” means read the issue and its comments.

## Pull requests as a triage surface

**PRs as a request surface: no.**
