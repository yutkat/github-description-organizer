"""Validate an AI description plan and apply it to GitHub repositories."""

from __future__ import annotations

import argparse
import json
import os
import unicodedata
from pathlib import Path
from typing import Any

from github_api import (
    UPDATE_DESCRIPTION_MUTATION,
    GitHubClient,
    GraphQLExecutor,
    current_repository,
)
from plan import verified_source

INPUT_FILENAME = "description-input.json"
MAX_DESCRIPTION_LENGTH = 350


def validate_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, str]:
    prepared = verified_source(prepared_input, plan)
    assignments = plan.get("assignments")
    if not isinstance(assignments, list):
        raise TypeError("assignments must be an array")

    required_ids = {repository["id"] for repository in prepared["repositories"]}
    assigned: dict[str, str] = {}
    for item in assignments:
        if not isinstance(item, dict):
            raise TypeError("assignments entries must be objects")
        repository_id = item.get("repository_id")
        description = item.get("description")
        if not isinstance(repository_id, str) or not repository_id:
            raise ValueError("repository_id must be a non-empty string")
        if repository_id in assigned:
            raise ValueError(f"duplicate assignment for {repository_id}")
        if not isinstance(description, str):
            raise TypeError(f"description must be a string for {repository_id}")
        if not 1 <= len(description) <= MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"invalid description length for {repository_id}")
        if description != description.strip():
            raise ValueError(f"description has surrounding whitespace: {repository_id}")
        if any(
            unicodedata.category(character).startswith("C")
            or character in "\u2028\u2029"
            for character in description
        ):
            raise ValueError(
                f"description must be plain single-line text: {repository_id}"
            )
        assigned[repository_id] = description

    if set(assigned) != required_ids:
        missing = sorted(required_ids - set(assigned))
        extra = sorted(set(assigned) - required_ids)
        raise ValueError(f"assignment mismatch; missing={missing}, extra={extra}")
    return assigned


def preview_plan(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    assigned = validate_plan(prepared_input, plan)
    return {
        "repositories": len(assigned),
        "descriptions_by_repository": dict(sorted(assigned.items())),
    }


def apply_plan(
    client: GraphQLExecutor, prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, int]:
    assigned = validate_plan(prepared_input, plan)
    applied = 0
    skipped = 0
    for repository_id, description in sorted(assigned.items()):
        # Recheck each repository immediately before its mutation.
        existing = current_repository(client, repository_id)
        if (
            existing is None
            or (existing["description"] or "").strip()
            or existing["isPrivate"]
            or existing["isArchived"]
            or existing["isFork"]
            or existing["owner"]["login"].casefold()
            != prepared_input["viewer_login"].casefold()
        ):
            skipped += 1
            continue
        client.execute(
            UPDATE_DESCRIPTION_MUTATION,
            {"input": {"repositoryId": repository_id, "description": description}},
        )
        applied += 1
    return {"applied": applied, "skipped": skipped}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--dry-run", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    input_path = args.plan.with_name(INPUT_FILENAME)
    with input_path.open(encoding="utf-8") as handle:
        prepared_input = json.load(handle)
    with args.plan.open(encoding="utf-8") as handle:
        plan = json.load(handle)
    if args.dry_run:
        print(json.dumps(preview_plan(prepared_input, plan), sort_keys=True))
        return
    client = GitHubClient(os.environ.get("COPILOT_GITHUB_TOKEN", ""))
    print(json.dumps(apply_plan(client, prepared_input, plan), sort_keys=True))


if __name__ == "__main__":
    main()
