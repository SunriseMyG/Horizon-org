"""Finding the Project item of a Discord post, on a faked GraphQL endpoint.

These tests need httpx, so the project `.venv`.
"""

import unittest
from unittest.mock import AsyncMock, patch

try:
    from bot_horizon.github_client import GitHubClient
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("httpx is not installed")

PROJECT_ID = "PVT_1"
ISSUE_URL = "https://github.com/org/repo/issues/1"
THREAD_URL = "https://discord.com/channels/1/200"


def resource(*project_ids: str) -> dict[str, object]:
    """An issue as `resource(url:)` returns it, with its item in each Project."""
    content = {
        "id": "I_1",
        "title": "Fix login",
        "body": f"Source: {THREAD_URL}/200",
        "url": ISSUE_URL,
        "labels": {"nodes": []},
    }
    return {
        "resource": {
            "projectItems": {
                "nodes": [
                    {
                        "id": f"PVTI_{project_id}",
                        "project": {"id": project_id},
                        "content": content,
                        "fieldValues": {"nodes": []},
                    }
                    for project_id in project_ids
                ]
            },
        }
    }


class FindLinkedItemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)

    async def test_an_issue_url_is_read_from_the_issue_not_the_listing(self) -> None:
        with patch.object(
            self.github, "_query", AsyncMock(return_value=resource("PVT_other", PROJECT_ID))
        ), patch.object(self.github, "list_items", AsyncMock()) as list_items:
            item = await self.github.find_linked_item(THREAD_URL, ISSUE_URL)
        list_items.assert_not_awaited()
        self.assertEqual(item["id"], f"PVTI_{PROJECT_ID}")
        self.assertEqual(item["content"]["id"], "I_1")
        self.assertNotIn("project", item)

    async def test_an_issue_outside_the_project_falls_back_to_the_listing(self) -> None:
        listed = {"id": "PVTI_listed", "content": {"body": f"Source: {THREAD_URL}"}}
        with patch.object(
            self.github, "_query", AsyncMock(return_value=resource("PVT_other"))
        ), patch.object(self.github, "list_items", AsyncMock(return_value=[listed])):
            item = await self.github.find_linked_item(THREAD_URL, ISSUE_URL)
        self.assertEqual(item, listed)

    async def test_a_draft_key_goes_straight_to_the_listing(self) -> None:
        listed = {"id": "PVTI_draft", "content": {"body": f"Source: {THREAD_URL}"}}
        query = AsyncMock()
        with patch.object(self.github, "_query", query), patch.object(
            self.github, "list_items", AsyncMock(return_value=[listed])
        ):
            item = await self.github.find_linked_item(THREAD_URL, "PVTI_draft")
        query.assert_not_awaited()
        self.assertEqual(item, listed)



class AddToProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)

    async def test_an_issue_the_auto_add_workflow_added_keeps_its_item(self) -> None:
        already = RuntimeError(
            [{"type": "UNPROCESSABLE", "message": "Content already exists in this project"}]
        )
        found = {
            "node": {
                "projectItems": {
                    "nodes": [{"id": "PVTI_auto", "project": {"id": PROJECT_ID}}]
                }
            }
        }
        with patch.object(
            self.github, "_query", AsyncMock(side_effect=[already, found])
        ):
            self.assertEqual(await self.github.add_issue_to_project("I_1"), "PVTI_auto")

    async def test_any_other_error_still_raises(self) -> None:
        with patch.object(
            self.github, "_query", AsyncMock(side_effect=RuntimeError("boom"))
        ), self.assertRaises(RuntimeError):
            await self.github.add_issue_to_project("I_1")

def page(*ids: str, more: bool) -> dict[str, object]:
    return {
        "node": {
            "items": {
                "pageInfo": {"hasNextPage": more, "endCursor": ids[-1]},
                "nodes": [{"id": item_id} for item_id in ids],
            }
        }
    }


def metadata(*fields: dict) -> list[dict]:
    return [
        {"id": "F_status", "name": "Status", "dataType": "SINGLE_SELECT",
         "options": [{"id": "O_ready", "name": "Ready"}, {"id": "O_done", "name": "Done"}]},
        *fields,
    ]


class ListItemsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)

    async def test_every_page_of_the_project_is_read(self) -> None:
        query = AsyncMock(side_effect=[page("A", "B", more=True), page("C", more=False)])
        with patch.object(self.github, "_query", query):
            items = await self.github.list_items()
        self.assertEqual([item["id"] for item in items], ["A", "B", "C"])
        self.assertIsNone(query.await_args_list[0].args[1]["cursor"])
        self.assertEqual(query.await_args_list[1].args[1]["cursor"], "B")


class UpdateTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)
        self.github._metadata = metadata(
            {"id": "F_target", "name": "Target date", "dataType": "DATE"},
            {"id": "F_size", "name": "Size", "dataType": "SINGLE_SELECT",
             "options": [{"id": "O_m", "name": "M"}]},
        )
        # No issue field of these names: the Project holds them.
        self.github._issue_fields = []
        self.query = AsyncMock(return_value={})
        patcher = patch.object(self.github, "_query", self.query)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.item = {
            "id": "PVTI_1",
            "content": {
                "id": "I_1",
                "url": ISSUE_URL,
                "body": f"Old\n\nSource: {THREAD_URL}",
                "assignees": {"nodes": [{"login": "alice"}]},
            },
        }

    async def test_only_the_given_fields_are_written(self) -> None:
        await self.github.update_task(self.item, target_date="2026-10-31")
        [call] = self.query.await_args_list
        self.assertEqual(call.args[1]["value"], {"date": "2026-10-31"})
        self.assertEqual(call.args[1]["fieldId"], "F_target")

    async def test_an_empty_date_clears_the_field(self) -> None:
        await self.github.update_task(self.item, target_date="")
        [call] = self.query.await_args_list
        self.assertIn("clearProjectV2ItemFieldValue", call.args[0])

    async def test_a_field_the_project_lacks_is_skipped(self) -> None:
        fields = self.github._metadata
        self.query.return_value = {"node": {"fields": {"nodes": fields}}}
        await self.github.update_task(self.item, start_date="2026-10-20")
        # Read the fields again to be sure, then write nothing.
        self.assertTrue(all("mutation" not in call.args[0] for call in self.query.await_args_list))

    async def test_the_size_is_a_select_option(self) -> None:
        await self.github.update_task(self.item, size="M")
        [call] = self.query.await_args_list
        self.assertEqual(call.args[1]["value"], {"singleSelectOptionId": "O_m"})

    async def test_a_new_description_keeps_the_link_to_the_post(self) -> None:
        await self.github.update_task(self.item, description="New")
        [call] = self.query.await_args_list
        self.assertEqual(call.args[1]["body"], f"New\n\nSource: {THREAD_URL}")
        self.assertNotIn("title", call.args[1])

    async def test_closing_and_reopening(self) -> None:
        await self.github.update_task(self.item, closed=True)
        await self.github.update_task(self.item, closed=False)
        self.assertIn("closeIssue", self.query.await_args_list[0].args[0])
        self.assertIn("reopenIssue", self.query.await_args_list[1].args[0])

    async def test_the_assignees_become_exactly_the_given_ones(self) -> None:
        self.query.side_effect = lambda query, variables: (
            {"user": {"id": f"U_{variables['login']}"}} if "user(login" in query else {}
        )
        await self.github.update_task(self.item, assignees=["bob"])
        written = [
            (call.args[0], call.args[1].get("assigneeIds"))
            for call in self.query.await_args_list
            if "Assignable" in call.args[0]
        ]
        self.assertIn("removeAssigneesFromAssignable", written[0][0])
        self.assertEqual(written[0][1], ["U_alice"])
        self.assertIn("addAssigneesToAssignable", written[1][0])
        self.assertEqual(written[1][1], ["U_bob"])


class IssueFieldTests(unittest.IsolatedAsyncioTestCase):
    """Fields of the organisation, which only `updateIssueFieldValue` writes."""

    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)
        self.github._metadata = metadata()
        self.github._issue_fields = [
            {"__typename": "IssueFieldDate", "id": "IFD_target", "name": "Target date"},
            {"__typename": "IssueFieldSingleSelect", "id": "IFSS_priority", "name": "Priority",
             "options": [{"id": "O_high", "name": "High"}]},
        ]
        self.query = AsyncMock(return_value={})
        patcher = patch.object(self.github, "_query", self.query)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.item = {"id": "PVTI_1", "content": {"id": "I_1", "url": ISSUE_URL}}

    async def test_a_date_issue_field_is_written_on_the_issue(self) -> None:
        await self.github.update_task(self.item, target_date="2026-10-31")
        [call] = self.query.await_args_list
        self.assertIn("updateIssueFieldValue", call.args[0])
        self.assertEqual(
            call.args[1],
            {"issueId": "I_1", "field": {"fieldId": "IFD_target", "dateValue": "2026-10-31"}},
        )

    async def test_an_empty_date_deletes_the_value(self) -> None:
        await self.github.update_task(self.item, target_date="")
        [call] = self.query.await_args_list
        self.assertIn("deleteIssueFieldValue", call.args[0])
        self.assertEqual(call.args[1], {"issueId": "I_1", "fieldId": "IFD_target"})

    async def test_the_native_priority_is_an_option_of_its_issue_field(self) -> None:
        await self.github.update_native_priority("I_1", "high")
        [call] = self.query.await_args_list
        self.assertEqual(
            call.args[1]["field"], {"fieldId": "IFSS_priority", "singleSelectOptionId": "O_high"}
        )

    async def test_a_draft_has_no_issue_fields(self) -> None:
        draft = {"id": "PVTI_2", "content": {"id": "DI_1"}}
        self.github._metadata = metadata({"id": "F_target", "name": "Target date", "dataType": "DATE"})
        await self.github.update_task(draft, target_date="2026-10-31")
        [call] = self.query.await_args_list
        self.assertIn("updateProjectV2ItemFieldValue", call.args[0])


class CommentSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.github = GitHubClient("token", PROJECT_ID, "R_1")
        self.addAsyncCleanup(self.github.close)
        self.message_url = f"{THREAD_URL}/201"
        self.comments = [
            {"id": "C_other", "url": "u0", "body": "by someone on GitHub"},
            {"id": "C_1", "url": "u1",
             "body": f"<!-- horizon-github -->\n**Alice** on [Discord]({self.message_url}):\n\nOld"},
        ]
        patchers = (
            patch.object(self.github, "find_linked_item",
                         AsyncMock(return_value={"content": {"id": "I_1", "url": ISSUE_URL}})),
            patch.object(self.github, "issue_comments", AsyncMock(return_value=self.comments)),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_an_edited_message_rewrites_its_own_comment(self) -> None:
        with patch.object(self.github, "_query", AsyncMock(return_value={})) as query:
            url = await self.github.edit_comment_from_discord(THREAD_URL, self.message_url, "New")
        self.assertEqual(url, "u1")
        self.assertEqual(query.await_args.args[1], {"id": "C_1", "body": "New"})

    async def test_a_deleted_message_deletes_its_own_comment(self) -> None:
        with patch.object(self.github, "_query", AsyncMock(return_value={})) as query:
            await self.github.delete_comment_from_discord(THREAD_URL, self.message_url)
        self.assertIn("deleteIssueComment", query.await_args.args[0])
        self.assertEqual(query.await_args.args[1], {"id": "C_1"})

    async def test_a_message_never_copied_touches_nothing(self) -> None:
        with patch.object(self.github, "_query", AsyncMock()) as query:
            url = await self.github.edit_comment_from_discord(THREAD_URL, f"{THREAD_URL}/999", "x")
        self.assertIsNone(url)
        query.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
