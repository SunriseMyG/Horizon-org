import logging
from typing import Any

import httpx

from .sync import github_item_key, parse_assignees


logger = logging.getLogger(__name__)


class GitHubClient:
    endpoint = "https://api.github.com/graphql"

    def __init__(
        self,
        token: str,
        project_id: str,
        repository_id: str,
        priority_field_name: str = "Priority",
        assignee_field_name: str = "Assignee",
        org_name: str = "play-horizon",
        project_number: int = 5,
    ) -> None:
        self.project_id = project_id
        self.repository_id = repository_id
        self.priority_field_name = priority_field_name
        self.assignee_field_name = assignee_field_name
        self.org_name = org_name
        self.project_number = project_number
        self.client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=20,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            self.endpoint, json={"query": query, "variables": variables}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(payload["errors"])
        return payload["data"]

    async def create_issue(
      self,
      title: str,
      body: str,
      status: str = "Backlog",
      priority: str = "Low",
      assignees: list[str] | str = "",
    ) -> str:
        query = """
        mutation($repositoryId: ID!, $title: String!, $body: String!) {
          createIssue(input: {repositoryId: $repositoryId, title: $title, body: $body}) {
            issue { id databaseId number url }
          }
        }
        """
        data = await self._query(
            query,
            {
                "repositoryId": self.repository_id,
                "title": title,
                "body": body,
            },
        )
        issue = data["createIssue"]["issue"]
        if priority:
            await self.add_label(issue["id"], priority)
        if assignees:
            # Never let an assignee problem leave the issue out of the Project.
            try:
                await self.assign_issue(issue["id"], assignees)
            except Exception:
                logger.warning(
                    "Could not assign %s on issue %s",
                    assignees,
                    issue["url"],
                    exc_info=True,
                )
        item_id = await self.add_issue_to_project(issue["id"])
        await self.update_project_status(item_id, status)
        await self.update_native_priority(issue["id"], priority)
        return issue["url"]

    async def update_issue_from_discord(
        self,
        source_url: str,
        title: str,
        status: str,
        priority: str,
        previous_title: str | None = None,
    ) -> str | None:
        """Push a Discord change to GitHub, returning the key of the item hit."""
        linked_item = await self.find_linked_item(
            source_url, title=previous_title or title
        )
        if not linked_item:
            raise RuntimeError(f"No GitHub issue linked to Discord thread: {source_url}")
        issue = linked_item["content"]
        update_issue = """
        mutation($issueId: ID!, $title: String!) {
          updateIssue(input: {id: $issueId, title: $title}) {
            issue { id url }
          }
        }
        """
        await self._query(update_issue, {"issueId": issue["id"], "title": title})
        priority_label_ids = [
            label["id"]
            for label in issue.get("labels", {}).get("nodes", [])
            if label.get("name", "").casefold()
            in {"low", "medium", "high", "urgent"}
        ]
        if priority_label_ids:
            remove_labels = """
            mutation($issueId: ID!, $labelIds: [ID!]!) {
              removeLabelsFromLabelable(input: {
                labelableId: $issueId,
                labelIds: $labelIds
              }) { clientMutationId }
            }
            """
            await self._query(
                remove_labels,
                {"issueId": issue["id"], "labelIds": priority_label_ids},
            )
        await self.add_label(issue["id"], priority)
        await self.update_project_status(linked_item["id"], status)
        await self.update_native_priority(issue["id"], priority)
        return github_item_key(linked_item)

    async def update_native_priority(self, issue_id: str, priority: str) -> None:
        fields_query = """
        query($repositoryId: ID!) {
          node(id: $repositoryId) {
            ... on Repository {
              issueFields(first: 100) {
                nodes {
                  ... on IssueFieldSingleSelect {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
        """
        fields_data = await self._query(
            fields_query, {"repositoryId": self.repository_id}
        )
        priority_field = next(
            field
            for field in fields_data["node"]["issueFields"]["nodes"]
            if field.get("name") == self.priority_field_name
        )
        option = next(
            option
            for option in priority_field.get("options", [])
            if option["name"].casefold() == priority.casefold()
        )
        query = """
        mutation($issueId: ID!, $fieldId: ID!, $optionId: ID!) {
          updateIssueFieldValue(input: {
            issueId: $issueId,
            issueField: {
              fieldId: $fieldId,
              singleSelectOptionId: $optionId
            }
          }) { issue { id } }
        }
        """
        await self._query(
            query,
            {
                "issueId": issue_id,
                "fieldId": priority_field["id"],
                "optionId": option["id"],
            },
        )

    async def create_project_item(
        self,
        title: str,
        body: str,
        status: str,
        priority: str,
        assignees: list[str] | str = "",
    ) -> str:
        query = """
        mutation($projectId: ID!, $title: String!, $body: String!) {
          addProjectV2DraftIssue(input: {
            projectId: $projectId,
            title: $title,
            body: $body
          }) {
            projectItem { id }
          }
        }
        """
        data = await self._query(
            query,
            {"projectId": self.project_id, "title": title, "body": body},
        )
        item_id = data["addProjectV2DraftIssue"]["projectItem"]["id"]
        await self.update_project_select(item_id, "Status", status)
        await self.update_project_select(item_id, self.priority_field_name, priority)
        if isinstance(assignees, str):
            assignees = parse_assignees(assignees)
        if assignees:
            await self.update_project_text(
                item_id, self.assignee_field_name, ", ".join(assignees)
            )
        return item_id

    async def add_issue_to_project(self, issue_id: str) -> str:
        query = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }
        """
        data = await self._query(query, {"projectId": self.project_id, "contentId": issue_id})
        return data["addProjectV2ItemById"]["item"]["id"]

    async def add_label(self, issue_id: str, label_name: str) -> None:
        query = """
        query($repositoryId: ID!) {
          node(id: $repositoryId) {
            ... on Repository {
              labels(first: 100) { nodes { id name } }
            }
          }
        }
        """
        data = await self._query(query, {"repositoryId": self.repository_id})
        labels = data["node"]["labels"]["nodes"]
        label = next(
            (label for label in labels if label["name"].casefold() == label_name.casefold()),
            None,
        )
        if not label:
          colors = {
            "low": "0E8A16",
            "medium": "FBCA04",
            "high": "D93F0B",
            "urgent": "B60205",
          }
          create_label = """
          mutation($repositoryId: ID!, $name: String!, $color: String!) {
            createLabel(input: {
            repositoryId: $repositoryId,
            name: $name,
            color: $color
            }) { label { id name } }
          }
          """
          created = await self._query(
            create_label,
            {
              "repositoryId": self.repository_id,
              "name": label_name,
              "color": colors.get(label_name.casefold(), "6E7781"),
            },
          )
          label = created["createLabel"]["label"]
        mutation = """
        mutation($issueId: ID!, $labelIds: [ID!]!) {
          addLabelsToLabelable(input: {labelableId: $issueId, labelIds: $labelIds}) {
            labelable {
              ... on Issue { id }
              ... on PullRequest { id }
            }
          }
        }
        """
        await self._query(mutation, {"issueId": issue_id, "labelIds": [label["id"]]})

    async def assign_issue(self, issue_id: str, logins: list[str] | str) -> None:
        """Assign every known login, warning about the ones GitHub does not know."""
        if isinstance(logins, str):
            logins = parse_assignees(logins)
        user_query = """
        query($login: String!) { user(login: $login) { id } }
        """
        user_ids = []
        for login in logins:
            try:
                user = await self._query(user_query, {"login": login})
                user_id = (user.get("user") or {}).get("id")
            except Exception:
                user_id = None
            if user_id:
                user_ids.append(user_id)
            else:
                logger.warning(
                    "No GitHub user named %r, that assignee is skipped", login
                )
        if not user_ids:
            return
        query = """
        mutation($issueId: ID!, $assigneeIds: [ID!]!) {
          addAssigneesToAssignable(input: {assignableId: $issueId, assigneeIds: $assigneeIds}) {
            assignable {
              ... on Issue { id }
              ... on PullRequest { id }
            }
          }
        }
        """
        await self._query(query, {"issueId": issue_id, "assigneeIds": user_ids})

    async def update_project_status(self, item_id: str, status: str) -> None:
        await self.update_project_select(item_id, "Status", status)

    async def update_project_select(
        self, item_id: str, field_name: str, option_name: str
    ) -> None:
        metadata = await self.project_metadata()
        field = next(
            (field for field in metadata if field.get("name") == field_name), None
        )
        if not field:
            raise RuntimeError(f"The GitHub Project has no {field_name} field")
        option = next(
            (
                option
                for option in field.get("options", [])
                if option["name"].casefold() == option_name.casefold()
            ),
            None,
        )
        if not option:
            raise RuntimeError(
                f"Option {option_name} not found in GitHub Project field {field_name}"
            )
        query = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: {singleSelectOptionId: $optionId}
          }) { projectV2Item { id } }
        }
        """
        await self._query(
            query,
            {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": field["id"],
                "optionId": option["id"],
            },
        )

    async def update_project_text(
        self, item_id: str, field_name: str, text: str
    ) -> None:
        metadata = await self.project_metadata()
        field = next(
            (field for field in metadata if field.get("name") == field_name), None
        )
        if not field:
            raise RuntimeError(f"The GitHub Project has no {field_name} field")
        query = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $text: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: {text: $text}
          }) { projectV2Item { id } }
        }
        """
        await self._query(
            query,
            {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": field["id"],
                "text": text,
            },
        )

    async def find_linked_item(
        self,
        source_url: str,
        item_key: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the Project item matching this Discord thread.

        The source line of the task body is the reliable link. The item key and
        the post name are fallbacks for a task that is not linked yet; a title is
        only trusted when it identifies a single task.
        """
        items = await self.list_items()
        linked_item = next(
            (
                item
                for item in items
                if source_url in ((item.get("content") or {}).get("body") or "")
            ),
            None,
        )
        if linked_item is None and item_key:
            linked_item = next(
                (item for item in items if github_item_key(item) == item_key), None
            )
        if linked_item is None and title:
            matches = [
                item
                for item in items
                if ((item.get("content") or {}).get("title") or "")[:100].casefold()
                == title[:100].casefold()
            ]
            if len(matches) == 1:
                linked_item = matches[0]
        return linked_item

    async def set_discord_source(self, item: dict[str, Any], source_url: str) -> None:
        """Write the Discord thread url in the task body so the link survives restarts."""
        content = item.get("content") or {}
        body = content.get("body") or ""
        if source_url in body:
            return
        body = f"{body.rstrip()}\n\nSource: {source_url}".lstrip()
        content_id = content.get("id")
        if not content_id:
            raise RuntimeError("The GitHub task has no node id to write the source into")
        if content.get("url"):
            query = """
            mutation($issueId: ID!, $body: String!) {
              updateIssue(input: {id: $issueId, body: $body}) { issue { id } }
            }
            """
            await self._query(query, {"issueId": content_id, "body": body})
        else:
            query = """
            mutation($draftIssueId: ID!, $body: String!) {
              updateProjectV2DraftIssue(input: {
                draftIssueId: $draftIssueId,
                body: $body
              }) { draftIssue { id } }
            }
            """
            await self._query(query, {"draftIssueId": content_id, "body": body})

    async def delete_task_from_discord(
        self,
        source_url: str,
        item_key: str | None = None,
        title: str | None = None,
    ) -> str | None:
        """Delete the task linked to a Discord thread, returning its item key."""
        linked_item = await self.find_linked_item(source_url, item_key, title)
        if not linked_item:
            return None
        key = github_item_key(linked_item)
        content = linked_item.get("content") or {}
        # Only issues and pull requests carry a url; a draft item has none.
        issue_id = content["id"] if content.get("url") else None
        if issue_id:
            try:
                await self.delete_issue(issue_id)
            except Exception:
                logger.warning(
                    "Could not delete issue %s, removing it from the Project instead",
                    issue_id,
                    exc_info=True,
                )
                await self.delete_project_item(linked_item["id"])
        else:
            await self.delete_project_item(linked_item["id"])
        return key

    async def delete_issue(self, issue_id: str) -> None:
        query = """
        mutation($issueId: ID!) {
          deleteIssue(input: {issueId: $issueId}) {
            repository { id }
          }
        }
        """
        await self._query(query, {"issueId": issue_id})

    async def delete_project_item(self, item_id: str) -> None:
        query = """
        mutation($projectId: ID!, $itemId: ID!) {
          deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
            deletedItemId
          }
        }
        """
        await self._query(
            query, {"projectId": self.project_id, "itemId": item_id}
        )

    async def project_metadata(self) -> list[dict[str, Any]]:
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 100) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id name options { id name }
                  }
                  ... on ProjectV2Field { id name }
                }
              }
            }
          }
        }
        """
        data = await self._query(query, {"projectId": self.project_id})
        return data["node"]["fields"]["nodes"]

    async def list_items(self) -> list[dict[str, Any]]:
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100) {
                nodes {
                  id
                  content {
                    ... on DraftIssue { id title body createdAt }
                    ... on Issue {
                          id databaseId number title body url createdAt
                      labels(first: 20) { nodes { id name } }
                      assignees(first: 10) { nodes { login } }
                    }
                    ... on PullRequest {
                      id databaseId number title body url createdAt
                      labels(first: 20) { nodes { id name } }
                      assignees(first: 10) { nodes { login } }
                    }
                  }
                  fieldValues(first: 50) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name optionId field {
                          ... on ProjectV2SingleSelectField { name }
                        }
                      }
                      ... on ProjectV2ItemFieldTextValue {
                        text field {
                          ... on ProjectV2Field { name }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._query(query, {"projectId": self.project_id})
        return data["node"]["items"]["nodes"]
