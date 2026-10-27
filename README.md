# GitHub Description Organizer

This repository uses GitHub Agentic Workflows to fill missing descriptions
on the authenticated user's public repositories, once a week.

It follows the same prepare, generate, validate, and apply workflow as
[GitHub Topic Organizer](https://github.com/yutkat/github-topic-organizer).
It reads repositories through GitHub's GraphQL API and README heads through
the REST API. The AI proposes a short English description per repository,
and a deterministic Python step validates the complete plan before calling
the [`updateRepository` mutation](https://docs.github.com/en/graphql/reference/repos#updaterepository).

## Install

Install the package into any GitHub repository with the guided wizard:

```bash
gh aw add-wizard yutkat/github-description-organizer
```

The installer adds the workflow, compiles its lock file, copies the companion
uv runtime under `.github/workflows/github-description-organizer/`, and
records its source for future updates. Pull upstream releases later with
`gh aw update`.

## Setup

1. Enable GitHub Actions in the repository where the package was installed
   (or in this repository if you run it directly).
2. Create a fine-grained personal access token owned by the user whose
   repositories will be described. Give it access to the repositories you
   want to update and grant `Copilot Requests`, `Administration: Read and
   write`, and `Contents: Read-only`. To include newly created repositories,
   select `All repositories`; the workflow itself only targets public ones.
3. Save the token as the Actions secret `COPILOT_GITHUB_TOKEN`.
4. Run `Assign repository descriptions` manually from the Actions tab once.

The same token used by GitHub Topic Organizer can be reused if it has these
permissions and repository access. When using selected repositories, include
the repository hosting the workflow as well as the target repositories.

Failed or incomplete runs and weekly plan issues are opened without an
assignee. After each run, the `assign-issues.yml` workflow assigns every
unassigned open issue to the repository owner (or the user in the
`ISSUE_ASSIGNEE` Actions variable) so they trigger notifications. It is not
part of the `gh aw add` package; copy it manually into installed repositories.

## Behavior

- Runs weekly on Monday and supports manual dispatch with `batch_size` (1-50).
- Targets the token owner's public repositories with null, empty, or
  whitespace-only descriptions; forks and archived repositories are excluded.
- Sends repository metadata plus the first 2 KB of each README to the AI.
- Generates concise English descriptions, preferably 50-160 characters.
- Validates that every input repository appears exactly once, the input
  digest matches, and each description is 1-350 characters of single-line
  text without surrounding whitespace or control characters.
- Rechecks each repository immediately before updating it. Skips repositories
  that gained a description, became private or archived, were transferred to
  another owner, or are no longer accessible.
- Processes up to 50 repositories per run; repeat manual runs to complete
  the initial backfill. An empty batch performs no updates.

The recheck and update are separate API requests, so a manual edit made
between them can still race with the update. Descriptions are AI-generated;
repositories with little README content may receive a general description.

If you edit the Agentic Workflow source, run `gh aw compile` and include the
generated `.github/workflows/assign-descriptions.lock.yml` with your changes.

## Development

Python is managed with uv. Run the checks with:

```bash
uv run --frozen python -m unittest -v
ruff check .
ruff format --check .
gh aw compile
```

To collect input locally, provide `COPILOT_GITHUB_TOKEN` in your environment:

```bash
uv run --frozen --project .github/workflows/github-description-organizer \
  python .github/workflows/github-description-organizer/prepare.py \
  --batch-size 5 --output .github/aw-input/description-input.json
```

To validate and preview a generated plan without a token or API calls:

```bash
uv run --frozen --project .github/workflows/github-description-organizer \
  python .github/workflows/github-description-organizer/apply.py \
  --plan .github/aw-input/description-plan.json --dry-run
```

The plan and its prepared `description-input.json` must be in the same
directory. The dry run prints the proposed descriptions by repository ID.
Omit `--dry-run` to apply the plan using `COPILOT_GITHUB_TOKEN`.
