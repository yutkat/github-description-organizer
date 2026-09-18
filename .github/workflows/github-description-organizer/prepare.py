"""Prepare public repositories with missing descriptions for the AI agent."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from github_api import (
    GitHubClient,
    GraphQLExecutor,
    ReadmeFetcher,
    paginated_repositories,
)
from plan import create_input


def prepare_input(
    client: GraphQLExecutor, readmes: ReadmeFetcher, batch_size: int
) -> dict[str, Any]:
    if not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")

    login, repositories = paginated_repositories(client)
    undescribed = [
        repo
        for repo in repositories
        if not repo["archived"]
        and not repo["fork"]
        and not (repo["description"] or "").strip()
    ]
    batch = [
        {
            "id": repo["id"],
            "name_with_owner": repo["name_with_owner"],
            "url": repo["url"],
            "description": repo["description"],
            "language": repo["language"],
            "pushed_at": repo["pushed_at"],
            "readme_head": readmes.readme_head(repo["name_with_owner"]),
        }
        for repo in undescribed[:batch_size]
    ]
    return {
        "repositories": batch,
        "stats": {
            "batch_size": batch_size,
            "remaining_after_batch": max(0, len(undescribed) - batch_size),
            "undescribed_total": len(undescribed),
        },
        "viewer_login": login,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--batch-size", type=int, default=50)
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    client = GitHubClient(os.environ.get("COPILOT_GITHUB_TOKEN", ""))
    source = prepare_input(client, client, args.batch_size)
    write_json(args.output, create_input(source))


if __name__ == "__main__":
    main()
