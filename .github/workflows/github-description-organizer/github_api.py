"""GitHub GraphQL/REST client and queries shared by preparation and application."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
REST_ENDPOINT = "https://api.github.com"
README_MAX_BYTES = 2048

RETRYABLE_STATUS = {502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2


def urlopen_with_retry(request: urllib.request.Request) -> Any:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS or attempt + 1 == MAX_ATTEMPTS:
                raise
            time.sleep(BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


REPOSITORIES_QUERY = """
query Repositories($cursor: String) {
  viewer {
    login
    repositories(
      first: 100
      after: $cursor
      ownerAffiliations: OWNER
      privacy: PUBLIC
    ) {
      nodes {
        id
        nameWithOwner
        url
        description
        isArchived
        isFork
        pushedAt
        primaryLanguage { name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPOSITORY_STATE_QUERY = """
query RepositoryState($id: ID!) {
  node(id: $id) {
    ... on Repository {
      id
      description
      isPrivate
      isArchived
      isFork
      owner { login }
    }
  }
}
"""

UPDATE_DESCRIPTION_MUTATION = """
mutation UpdateDescription($input: UpdateRepositoryInput!) {
  updateRepository(input: $input) { repository { id } }
}
"""


class GraphQLExecutor(Protocol):
    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class ReadmeFetcher(Protocol):
    def readme_head(self, name_with_owner: str) -> str | None: ...


class GitHubClient:
    def __init__(self, token: str, endpoint: str = GRAPHQL_ENDPOINT):
        if not token:
            raise ValueError("COPILOT_GITHUB_TOKEN is required")
        if not token.startswith("github_pat_"):
            raise ValueError("COPILOT_GITHUB_TOKEN must be a fine-grained PAT")
        self.token = token
        self.endpoint = endpoint

    def headers(self, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "github-description-organizer",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                **self.headers("application/vnd.github+json"),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen_with_retry(request) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            raise RuntimeError(f"GitHub GraphQL HTTP {error.code}: {body}") from error
        if result.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {result['errors']}")
        return result["data"]

    def readme_head(self, name_with_owner: str) -> str | None:
        request = urllib.request.Request(
            f"{REST_ENDPOINT}/repos/{name_with_owner}/readme",
            headers=self.headers("application/vnd.github.raw+json"),
        )
        try:
            with urlopen_with_retry(request) as response:
                body = response.read(README_MAX_BYTES)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            text = error.read().decode(errors="replace")
            raise RuntimeError(f"GitHub REST HTTP {error.code}: {text}") from error
        return body.decode("utf-8", errors="replace")


def paginated_repositories(
    client: GraphQLExecutor,
) -> tuple[str, list[dict[str, Any]]]:
    cursor = None
    login = ""
    repositories: list[dict[str, Any]] = []
    while True:
        viewer = client.execute(REPOSITORIES_QUERY, {"cursor": cursor})["viewer"]
        login = viewer["login"]
        connection = viewer["repositories"]
        for node in connection["nodes"]:
            repositories.append(
                {
                    "id": node["id"],
                    "name_with_owner": node["nameWithOwner"],
                    "url": node["url"],
                    "description": node["description"],
                    "language": (
                        node["primaryLanguage"]["name"]
                        if node["primaryLanguage"]
                        else None
                    ),
                    "pushed_at": node["pushedAt"],
                    "archived": node["isArchived"],
                    "fork": node["isFork"],
                }
            )
        if not connection["pageInfo"]["hasNextPage"]:
            return login, repositories
        cursor = connection["pageInfo"]["endCursor"]


def current_repository(
    client: GraphQLExecutor, repository_id: str
) -> dict[str, Any] | None:
    node = client.execute(REPOSITORY_STATE_QUERY, {"id": repository_id})["node"]
    if node is not None and node.get("id") != repository_id:
        raise ValueError(f"unexpected repository state: {repository_id}")
    return node
