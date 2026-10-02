import asyncio
from typing import Any

import httpx


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
      assignee: str = "",
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
        if assignee:
            await self.assign_issue(issue["id"], assignee)
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
    ) -> None:
        items = await self.list_items()
        linked_item = next(
            (
                item
                for item in items
                if source_url in ((item.get("content") or {}).get("body") or "")
            ),
            None,
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
        self, title: str, body: str, status: str, priority: str, assignee: str
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
        if assignee:
            await self.update_project_text(item_id, self.assignee_field_name, assignee)
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

    async def assign_issue(self, issue_id: str, login: str) -> None:
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
        user_query = """
        query($login: String!) { user(login: $login) { id } }
        """
        user = await self._query(user_query, {"login": login})
        user_id = user["user"]["id"]
        await self._query(query, {"issueId": issue_id, "assigneeIds": [user_id]})

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
                    ... on DraftIssue { title body createdAt }
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
