from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from importlib import util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

RUNTIME_PATH = (
    Path(__file__).parents[1] / ".github" / "workflows" / "github-description-organizer"
)


def load_runtime_module(name: str) -> ModuleType:
    module_path = RUNTIME_PATH / f"{name}.py"
    spec = util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {module_path}")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plan_envelope = load_runtime_module("plan")
github_api = load_runtime_module("github_api")

create_input = plan_envelope.create_input
verified_source = plan_envelope.verified_source

REPOSITORIES_QUERY = github_api.REPOSITORIES_QUERY
REPOSITORY_STATE_QUERY = github_api.REPOSITORY_STATE_QUERY
UPDATE_DESCRIPTION_MUTATION = github_api.UPDATE_DESCRIPTION_MUTATION
GitHubClient = github_api.GitHubClient
current_repository = github_api.current_repository
paginated_repositories = github_api.paginated_repositories

prepare = load_runtime_module("prepare")

prepare_input = prepare.prepare_input

apply = load_runtime_module("apply")

apply_plan = apply.apply_plan
preview_plan = apply.preview_plan
validate_plan = apply.validate_plan


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com", code, "Bad Gateway", None, io.BytesIO(b"<html>")
    )


class RetryTests(unittest.TestCase):
    def patched(self, responses: list[Any], sleeps: list[float]):
        def fake_urlopen(request, timeout=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return (
            mock.patch.object(github_api.urllib.request, "urlopen", fake_urlopen),
            mock.patch.object(github_api.time, "sleep", sleeps.append),
        )

    def test_execute_retries_transient_server_errors(self):
        client = GitHubClient("github_pat_example")
        success = io.BytesIO(json.dumps({"data": {"ok": True}}).encode())
        sleeps: list[float] = []
        urlopen_patch, sleep_patch = self.patched(
            [http_error(502), http_error(503), success], sleeps
        )

        with urlopen_patch, sleep_patch:
            data = client.execute("query", {})

        self.assertEqual({"ok": True}, data)
        self.assertEqual([2, 4], sleeps)

    def test_execute_gives_up_after_max_attempts(self):
        client = GitHubClient("github_pat_example")
        sleeps: list[float] = []
        urlopen_patch, sleep_patch = self.patched([http_error(502)] * 4, sleeps)

        with (
            urlopen_patch,
            sleep_patch,
            self.assertRaisesRegex(RuntimeError, "HTTP 502"),
        ):
            client.execute("query", {})

        self.assertEqual([2, 4, 8], sleeps)

    def test_readme_head_retries_and_keeps_404_semantics(self):
        client = GitHubClient("github_pat_example")
        sleeps: list[float] = []
        urlopen_patch, sleep_patch = self.patched(
            [http_error(502), io.BytesIO(b"# Title")], sleeps
        )

        with urlopen_patch, sleep_patch:
            head = client.readme_head("octocat/tool")

        self.assertEqual("# Title", head)
        self.assertEqual([2], sleeps)

        missing_patch, missing_sleep = self.patched([http_error(404)], sleeps)
        with missing_patch, missing_sleep:
            self.assertIsNone(client.readme_head("octocat/none"))


def repo_node(
    repository_id: str,
    name: str,
    *,
    archived: bool = False,
    fork: bool = False,
    language: str | None = "Lua",
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "id": repository_id,
        "nameWithOwner": name,
        "url": f"https://github.com/{name}",
        "description": description,
        "isArchived": archived,
        "isFork": fork,
        "isPrivate": False,
        "owner": {"login": name.split("/")[0]},
        "pushedAt": "2026-08-01T00:00:00Z",
        "primaryLanguage": {"name": language} if language else None,
    }


class FakeGraphQL:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.repositories: list[dict[str, Any]] = []
        self.repository_pages: list[list[dict[str, Any]]] | None = None
        self.current: dict[str, dict[str, Any]] = {}

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if query == REPOSITORIES_QUERY:
            pages = self.repository_pages or [self.repositories]
            index = int(variables["cursor"]) if variables["cursor"] else 0
            return {
                "viewer": {
                    "login": "octocat",
                    "repositories": {
                        "nodes": pages[index],
                        "pageInfo": {
                            "hasNextPage": index + 1 < len(pages),
                            "endCursor": str(index + 1),
                        },
                    },
                }
            }
        if query == REPOSITORY_STATE_QUERY:
            return {"node": self.current.get(variables["id"])}
        if query == UPDATE_DESCRIPTION_MUTATION:
            repository_id = variables["input"]["repositoryId"]
            self.current[repository_id]["description"] = variables["input"][
                "description"
            ]
            return {"updateRepository": {"repository": {"id": repository_id}}}
        raise AssertionError("unexpected query")


class FakeReadmes:
    def __init__(self, readmes: dict[str, str] | None = None):
        self.readmes = readmes or {}
        self.calls: list[str] = []

    def readme_head(self, name_with_owner: str) -> str | None:
        self.calls.append(name_with_owner)
        return self.readmes.get(name_with_owner)


def prepared_single_repo() -> dict[str, Any]:
    client = FakeGraphQL()
    client.repositories = [repo_node("R_a", "octocat/tool")]
    return create_input(prepare_input(client, FakeReadmes(), 50))


def plan_for(prepared: dict[str, Any], assignments: list[dict[str, Any]]):
    return {"source_sha256": prepared["source_sha256"], "assignments": assignments}


def single_plan(prepared: dict[str, Any], description: Any = "A Lua plugin for Neovim"):
    return plan_for(prepared, [{"repository_id": "R_a", "description": description}])


class DigestTests(unittest.TestCase):
    def test_create_input_adds_digest(self):
        prepared = create_input({"repositories": []})
        self.assertEqual(64, len(prepared["source_sha256"]))

    def test_rejects_modified_input(self):
        prepared = prepared_single_repo()
        plan = single_plan(prepared)
        prepared["repositories"][0]["readme_head"] = "Changed after preparation"
        with self.assertRaisesRegex(ValueError, "modified"):
            verified_source(prepared, plan)

    def test_rejects_plan_for_different_input(self):
        prepared = prepared_single_repo()
        plan = single_plan(prepared)
        plan["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            verified_source(prepared, plan)

    def test_rejects_missing_digest(self):
        prepared = prepared_single_repo()
        with self.assertRaisesRegex(TypeError, "source_sha256"):
            verified_source(prepared, {"assignments": []})
        plan = single_plan(prepared)
        del prepared["source_sha256"]
        with self.assertRaisesRegex(TypeError, "source_sha256"):
            verified_source(prepared, plan)


class GitHubApiTests(unittest.TestCase):
    def test_rejects_missing_and_non_fine_grained_tokens(self):
        for token in ("", "ghu_example"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                GitHubClient(token)

    def test_current_repository_returns_state(self):
        client = FakeGraphQL()
        node = repo_node("R_a", "octocat/tool", description="Written by hand")
        client.current = {"R_a": node}
        self.assertEqual(node, current_repository(client, "R_a"))

    def test_current_repository_handles_deleted_repository(self):
        self.assertIsNone(current_repository(FakeGraphQL(), "R_gone"))

    def test_current_repository_rejects_unexpected_id(self):
        client = FakeGraphQL()
        client.current = {"R_a": repo_node("R_b", "octocat/other")}
        with self.assertRaisesRegex(ValueError, "unexpected repository"):
            current_repository(client, "R_a")

    def test_paginated_repositories_normalizes_nodes(self):
        client = FakeGraphQL()
        client.repositories = [repo_node("R_a", "octocat/tool", language=None)]
        login, repositories = paginated_repositories(client)
        self.assertEqual("octocat", login)
        self.assertEqual(
            [
                {
                    "id": "R_a",
                    "name_with_owner": "octocat/tool",
                    "url": "https://github.com/octocat/tool",
                    "description": None,
                    "language": None,
                    "pushed_at": "2026-08-01T00:00:00Z",
                    "archived": False,
                    "fork": False,
                }
            ],
            repositories,
        )

    def test_paginated_repositories_follows_cursors(self):
        client = FakeGraphQL()
        client.repository_pages = [
            [repo_node("R_a", "octocat/a")],
            [repo_node("R_b", "octocat/b")],
        ]
        login, repositories = paginated_repositories(client)
        self.assertEqual("octocat", login)
        self.assertEqual(["R_a", "R_b"], [repo["id"] for repo in repositories])
        self.assertEqual(
            [None, "1"], [variables["cursor"] for _, variables in client.calls]
        )

    def test_readme_is_bounded_and_handles_split_utf8(self):
        body = b"a" * 2047 + "あ".encode() + b"ignored"
        with mock.patch.object(
            github_api.urllib.request, "urlopen", return_value=io.BytesIO(body)
        ):
            head = GitHubClient("github_pat_example").readme_head("octocat/tool")
        self.assertEqual("a" * 2047 + "\ufffd", head)

    def test_graphql_errors_fail(self):
        response = io.BytesIO(json.dumps({"errors": [{"message": "Denied"}]}).encode())
        with (
            mock.patch.object(
                github_api.urllib.request, "urlopen", return_value=response
            ),
            self.assertRaisesRegex(RuntimeError, "Denied"),
        ):
            GitHubClient("github_pat_example").execute("query", {})

    def test_permission_errors_are_not_retried(self):
        with (
            mock.patch.object(
                github_api.urllib.request, "urlopen", side_effect=http_error(403)
            ) as request,
            self.assertRaisesRegex(RuntimeError, "HTTP 403"),
        ):
            GitHubClient("github_pat_example").execute("query", {})
        self.assertEqual(1, request.call_count)


class PrepareTests(unittest.TestCase):
    def test_prepare_skips_forks_archived_and_described(self):
        client = FakeGraphQL()
        client.repositories = [
            repo_node("R_plain", "octocat/plain"),
            repo_node("R_empty", "octocat/empty", description=""),
            repo_node("R_blank", "octocat/blank", description=" \t "),
            repo_node("R_fork", "octocat/fork", fork=True),
            repo_node("R_archived", "octocat/archived", archived=True),
            repo_node("R_described", "octocat/described", description="Keep this"),
        ]
        readmes = FakeReadmes()
        prepared = prepare_input(client, readmes, 50)
        self.assertEqual("octocat", prepared["viewer_login"])
        self.assertEqual(
            ["R_plain", "R_empty", "R_blank"],
            [repo["id"] for repo in prepared["repositories"]],
        )
        self.assertEqual(3, prepared["stats"]["undescribed_total"])
        self.assertEqual(
            ["octocat/plain", "octocat/empty", "octocat/blank"], readmes.calls
        )

    def test_prepare_caps_batch_and_only_fetches_selected_readmes(self):
        client = FakeGraphQL()
        client.repositories = [
            repo_node(f"R_{index}", f"octocat/repo{index}") for index in range(5)
        ]
        readmes = FakeReadmes()
        prepared = prepare_input(client, readmes, 2)
        self.assertEqual(2, len(prepared["repositories"]))
        self.assertEqual(3, prepared["stats"]["remaining_after_batch"])
        self.assertEqual(5, prepared["stats"]["undescribed_total"])
        self.assertEqual(["octocat/repo0", "octocat/repo1"], readmes.calls)

    def test_prepare_attaches_readme_head(self):
        client = FakeGraphQL()
        client.repositories = [
            repo_node("R_a", "octocat/a"),
            repo_node("R_b", "octocat/b"),
        ]
        prepared = prepare_input(
            client, FakeReadmes({"octocat/a": "# Title\nIntro."}), 50
        )
        by_id = {repo["id"]: repo for repo in prepared["repositories"]}
        self.assertEqual("# Title\nIntro.", by_id["R_a"]["readme_head"])
        self.assertIsNone(by_id["R_b"]["readme_head"])

    def test_prepare_rejects_out_of_range_batch_size(self):
        for size in (-1, 0, 51):
            client = FakeGraphQL()
            with (
                self.subTest(size=size),
                self.assertRaisesRegex(ValueError, "batch_size"),
            ):
                prepare_input(client, FakeReadmes(), size)
            self.assertEqual([], client.calls)

    def test_prepare_empty_batch(self):
        prepared = prepare_input(FakeGraphQL(), FakeReadmes(), 50)
        self.assertEqual([], prepared["repositories"])
        self.assertEqual(0, prepared["stats"]["undescribed_total"])
        self.assertEqual(0, prepared["stats"]["remaining_after_batch"])


class ApplyTests(unittest.TestCase):
    def test_plan_requires_each_repository_exactly_once(self):
        prepared = prepared_single_repo()
        for assignments in (
            [],
            [{"repository_id": "R_other", "description": "Another project"}],
        ):
            with (
                self.subTest(assignments=assignments),
                self.assertRaisesRegex(ValueError, "assignment mismatch"),
            ):
                validate_plan(prepared, plan_for(prepared, assignments))

    def test_rejects_invalid_description(self):
        prepared = prepared_single_repo()
        invalid = [
            None,
            42,
            [],
            {},
            "",
            " " * 10,
            "x" * 351,
            " leading",
            "trailing ",
            "two\nlines",
            "two\rlines",
            "a\tb",
            "a\x00b",
            "a\x7fb",
            "a\u2028b",
            "a\u2029b",
            "a\u200bb",
        ]
        for description in invalid:
            with (
                self.subTest(description=description),
                self.assertRaises((TypeError, ValueError)),
            ):
                validate_plan(prepared, single_plan(prepared, description))

    def test_accepts_description_length_boundaries_and_unicode(self):
        prepared = prepared_single_repo()
        for description in ("x", "x" * 350, "A café menu editor"):
            with self.subTest(description=description):
                self.assertEqual(
                    {"R_a": description},
                    validate_plan(prepared, single_plan(prepared, description)),
                )

    def test_rejects_invalid_assignment_structure(self):
        prepared = prepared_single_repo()
        for assignments in (
            None,
            {},
            [None],
            [{"repository_id": [], "description": "Text"}],
            [{"description": "Text"}],
        ):
            with (
                self.subTest(assignments=assignments),
                self.assertRaises((TypeError, ValueError)),
            ):
                validate_plan(prepared, plan_for(prepared, assignments))

    def test_rejects_duplicate_assignment(self):
        prepared = prepared_single_repo()
        plan = single_plan(prepared)
        plan["assignments"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate assignment"):
            validate_plan(prepared, plan)

    def test_apply_updates_only_description(self):
        client = FakeGraphQL()
        client.current = {"R_a": repo_node("R_a", "octocat/tool")}
        prepared = prepared_single_repo()
        result = apply_plan(client, prepared, single_plan(prepared))
        self.assertEqual({"applied": 1, "skipped": 0}, result)
        self.assertEqual(
            "A Lua plugin for Neovim", client.current["R_a"]["description"]
        )
        self.assertEqual(
            {
                "input": {
                    "repositoryId": "R_a",
                    "description": "A Lua plugin for Neovim",
                }
            },
            client.calls[-1][1],
        )

    def test_apply_skips_changed_or_missing_repository(self):
        prepared = prepared_single_repo()
        changes = [
            {"description": "Added by hand"},
            {"isPrivate": True},
            {"isArchived": True},
            {"isFork": True},
            {"owner": {"login": "another-owner"}},
            None,
        ]
        for change in changes:
            with self.subTest(change=change):
                client = FakeGraphQL()
                if change is not None:
                    client.current = {
                        "R_a": {**repo_node("R_a", "octocat/tool"), **change}
                    }
                result = apply_plan(client, prepared, single_plan(prepared))
                self.assertEqual({"applied": 0, "skipped": 1}, result)
                self.assertEqual(
                    [REPOSITORY_STATE_QUERY], [query for query, _ in client.calls]
                )
                if change and "description" in change:
                    self.assertEqual(
                        "Added by hand", client.current["R_a"]["description"]
                    )

    def test_apply_handles_blank_description_and_owner_case(self):
        client = FakeGraphQL()
        client.current = {"R_a": repo_node("R_a", "Octocat/tool", description=" \t ")}
        prepared = prepared_single_repo()
        self.assertEqual(
            {"applied": 1, "skipped": 0},
            apply_plan(client, prepared, single_plan(prepared)),
        )

    def test_apply_is_idempotent(self):
        client = FakeGraphQL()
        client.current = {"R_a": repo_node("R_a", "octocat/tool")}
        prepared = prepared_single_repo()
        plan = single_plan(prepared)
        apply_plan(client, prepared, plan)
        self.assertEqual(
            {"applied": 0, "skipped": 1}, apply_plan(client, prepared, plan)
        )

    def test_apply_validates_whole_plan_before_any_api_call(self):
        client = FakeGraphQL()
        client.repositories = [
            repo_node("R_a", "octocat/a"),
            repo_node("R_b", "octocat/b"),
        ]
        prepared = create_input(prepare_input(client, FakeReadmes(), 50))
        client.calls.clear()
        plan = plan_for(
            prepared,
            [
                {"repository_id": "R_a", "description": "Valid description"},
                {"repository_id": "R_b", "description": ""},
            ],
        )
        with self.assertRaises(ValueError):
            apply_plan(client, prepared, plan)
        self.assertEqual([], client.calls)

    def test_apply_rechecks_each_repository_just_before_update(self):
        client = FakeGraphQL()
        client.repositories = [
            repo_node("R_a", "octocat/a"),
            repo_node("R_b", "octocat/b"),
        ]
        client.current = {node["id"]: dict(node) for node in client.repositories}
        prepared = create_input(prepare_input(client, FakeReadmes(), 50))
        client.calls.clear()
        plan = plan_for(
            prepared,
            [
                {"repository_id": key, "description": "A Lua plugin"}
                for key in ("R_a", "R_b")
            ],
        )
        apply_plan(client, prepared, plan)
        self.assertEqual(
            [REPOSITORY_STATE_QUERY, UPDATE_DESCRIPTION_MUTATION] * 2,
            [query for query, _ in client.calls],
        )

    def test_empty_plan_performs_no_api_calls(self):
        client = FakeGraphQL()
        prepared = create_input(prepare_input(client, FakeReadmes(), 50))
        client.calls.clear()
        self.assertEqual(
            {"applied": 0, "skipped": 0},
            apply_plan(client, prepared, plan_for(prepared, [])),
        )
        self.assertEqual([], client.calls)

    def test_dry_run_previews_descriptions(self):
        prepared = prepared_single_repo()
        self.assertEqual(
            {
                "repositories": 1,
                "descriptions_by_repository": {"R_a": "A Lua plugin for Neovim"},
            },
            preview_plan(prepared, single_plan(prepared)),
        )

    def test_cli_dry_run_reads_sibling_input_without_token_or_network(self):
        prepared = prepared_single_repo()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare.write_json(root / "description-input.json", prepared)
            prepare.write_json(root / "description-plan.json", single_plan(prepared))
            output = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "apply.py",
                        "--plan",
                        str(root / "description-plan.json"),
                        "--dry-run",
                    ],
                ),
                mock.patch.dict(apply.os.environ, {}, clear=True),
                mock.patch.object(
                    apply,
                    "GitHubClient",
                    side_effect=AssertionError("Unexpected API access"),
                ),
                mock.patch("sys.stdout", output),
            ):
                apply.main()
        self.assertEqual(
            {"R_a": "A Lua plugin for Neovim"},
            json.loads(output.getvalue())["descriptions_by_repository"],
        )


if __name__ == "__main__":
    unittest.main()
