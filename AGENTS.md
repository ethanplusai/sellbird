# WorkiDigital Sellbird fork — notes for agents and contributors

This repository is a fork of Sellbird (formerly Harvey). Read this file before changing code or updating the fork. It describes this fork's Git workflow; for product setup and contribution rules, also read `README.md`, `CLAUDE.md`, and `CONTRIBUTING.md`.

## Repositories and branches

| Name | Purpose |
| --- | --- |
| `ethanplusai/sellbird` | Original project (upstream): https://github.com/ethanplusai/sellbird |
| `WorkiDigital/sellbird` | WorkiDigital's fork (origin): https://github.com/WorkiDigital/sellbird |
| `main` | Keep aligned with the original project's `main`. Do not make WorkiDigital-specific edits here. |
| `minha-versao` | WorkiDigital's editable branch. Put custom code changes here. |

The fork and `minha-versao` were created on 2026-11-19. At setup, `main` and `minha-versao` both pointed to commit `b212385259da92246a56311d790d936e75dd70dd`. This is a historical starting point, not a claim that they remain current.

## Starting in a new clone

Git remotes are local settings: the `upstream` remote configured in one workspace does **not** appear automatically in another person's clone. Set it up there:

```bash
git clone https://github.com/WorkiDigital/sellbird.git
cd sellbird
git remote add upstream https://github.com/ethanplusai/sellbird.git
git switch minha-versao
git remote -v
```

If `upstream` already exists, inspect `git remote -v` instead of adding it again. `origin` should be WorkiDigital's fork; `upstream` should be Ethan's original project. The clone created during initial setup was in a temporary agent workspace, not automatically on the user's own computer or server.

## Making WorkiDigital changes

Work on `minha-versao`; commit and push to `origin/minha-versao`. Do not push to `upstream`. Avoid force pushes and resets that discard branch history. Keep credentials, contact data, `.env`, local configuration, and the runtime database out of Git. Prefer `sellbird.local.yaml` for personal configuration; see the project's documentation for the current configuration behavior.

## Bringing in original-project updates

Make sure the working tree is clean (`git status --short`), commit or stash any pending edits, and review the upstream changes. Then:

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main

git switch minha-versao
git merge main
# If Git reports conflicts, resolve them, test the affected behavior,
# then git add the resolved files and git commit.
git push origin minha-versao
```

The `--ff-only` step will stop if `main` has diverged; inspect the history rather than forcing it. The GitHub **Sync fork** button can update the fork's `main`, but `minha-versao` still needs a separate merge. Updates are **not automatic** in this fork. Conflicts are possible when both projects change the same code; resolve and test them before pushing.

If a future agent is asked to change the sync strategy or automate it, discuss the effect on `main` and `minha-versao` and implement that as a separate, reviewable change.
