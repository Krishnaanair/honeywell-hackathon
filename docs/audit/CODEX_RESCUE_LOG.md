# EcoLoop Rescue Log

This journal records the evidence-driven rescue and acceptance audit requested
for the existing hackathon repository. It supplements, rather than replaces,
the historical implementation journal in `docs/progress.md`.

## Phase 0 - Protect the current work

### Work completed

- Inspected tracked, untracked, ignored, branch, worktree, and remote state.
- Confirmed the tracked working tree was clean at
  `0fc71b18c2c37fbb5ead3319a6bb41a85ff1d2e1`.
- Preserved the existing `main` revision and created the isolated
  `codex/ecoloop-rescue` branch.
- Recorded the protected source tree and a summary of ignored runtime evidence
  in `docs/audit/ORIGINAL_REPOSITORY_TREE.txt`.
- Reviewed and strengthened the durable repository instructions in `AGENTS.md`.
- Kept all existing real run directories, SQLite evidence, generated reports,
  presentation files, weather data, and upload bundles intact.

### Commands executed

```text
git status --short
git status --ignored --short
git branch --all --verbose --no-abbrev
git worktree list --porcelain
git rev-parse HEAD
git remote -v
git ls-tree -r --name-only 0fc71b18c2c37fbb5ead3319a6bb41a85ff1d2e1
git switch -c codex/ecoloop-rescue
```

### Tests

- Not applicable to this protection-only phase.

### Decisions

- Use a rescue branch in the existing worktree because there were no tracked
  uncommitted changes to isolate or save as a patch.
- Treat ignored run/output directories as evidence, not source; preserve them
  locally and keep them excluded from source commits.
- Audit before changing runtime architecture because the repository already
  contains executed real EnergyPlus, Ollama, and MCP evidence.

### Current blockers

- None.

### Next action

- Complete the forensic component audit and requirement traceability matrix,
  then repair only evidence-backed gaps.
