# Contributing

## Git workflow

- `main` is the shared working version. Never commit to it directly.
- One short-lived branch per feature or fix, named like `manu/stripe-polling` or `friend/admin-commands`.
- Open a pull request when done. The other person reviews it, then merge and delete the branch.
- Before starting new work: `git switch main && git pull`, then branch from there.
- Merge small and often. Agree on who owns which module up front.

## Secrets

Keep secrets in `.env` (git-ignored). Never commit real keys or tokens. List variable names in `.env.example`.
