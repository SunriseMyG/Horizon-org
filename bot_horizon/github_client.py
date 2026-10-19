import logging
from typing import Any

import httpx

from .sync import (
    PRIORITY_NAMES,
    comment_of_message,
    github_item_key,
    parse_assignees,
    with_description,
)


logger = logging.getLogger(__name__)

# Past that many pages of 100 items, the listing stops and says so.
MAX_ITEM_PAGES = 50

# What the bot reads of a Project item, wherever it reads one from.
ITEM_FIELDS = """
fragment ItemFields on ProjectV2Item {
  id
  content {
    ... on DraftIssue { id title body createdAt }
    ... on Issue {
      id databaseId number title body url state createdAt
      labels(first: 20) { nodes { id name } }
      assignees(first: 10) { nodes { login } }
      comments(last: 100) { totalCount nodes { id url body author { login } } }
      issueFieldValues(first: 20) {
        nodes {
          ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } }
          ... on IssueFieldSingleSelectValue {
            name field { ... on IssueFieldSingleSelect { name } }
          }
        }
      }
    }
    ... on PullRequest {
      id databaseId number title body url state createdAt
      labels(first: 20) { nodes { id name } }
      assignees(first: 10) { nodes { login } }
      comments(last: 100) { totalCount nodes { id url body author { login } } }
    }
  }
  fieldValues(first: 50) {
    nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name optionId field { ... on ProjectV2SingleSelectField { name } }
      }
      ... on ProjectV2ItemFieldTextValue {
        text field { ... on ProjectV2Field { name } }
      }
      ... on ProjectV2ItemFieldDateValue {
        date field { ... on ProjectV2Field { name } }
      }
    }
  }
}
"""


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
        size_field_name: str = "Size",
        start_date_field_name: str = "Start date",
        target_date_field_name: str = "Target date",
    ) -> None:
        self.project_id = project_id
        self.repository_id = repository_id
        self.priority_field_name = priority_field_name
        self.assignee_field_name = assignee_field_name
        self.org_name = org_name
        self.project_number = project_number
        self.size_field_name = size_field_name
        self.start_date_field_name = start_date_field_name
        self.target_date_field_name = target_date_field_name
        self._metadata: list[dict[str, Any]] | None = None
        self._issue_fields: list[dict[str, Any]] | None = None
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
        *,
        size: str = "",
        start_date: str = "",
        target_date: str = "",
        closed: bool = False,
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
        # An empty field of a new item is already empty: nothing to clear.
        await self.update_optional_fields(
            item_id,
            issue["id"],
            size=size or None,
            start_date=start_date or None,
            target_date=target_date or None,
        )
        if closed:
            await self.close_issue(issue["id"])
        return issue["url"]

    async def update_issue_from_discord(
        self,
        source_url: str,
        title: str,
        status: str,
        priority: str,
        previous_title: str | None = None,
        item_key: str | None = None,
        closed: bool | None = None,
    ) -> str | None:
        """Push a Discord change to GitHub, returning the key of the item hit."""
        linked_item = await self.find_linked_item(
            source_url, item_key, title=previous_title or title
        )
        if not linked_item:
            raise RuntimeError(f"No GitHub issue linked to Discord thread: {source_url}")
        await self.update_task(
            linked_item, title=title, status=status, priority=priority, closed=closed
        )
        return github_item_key(linked_item)

    async def update_linked_task(
        self, source_url: str, item_key: str | None = None, **changes: Any
    ) -> str | None:
        """Write some fields of the task linked to a Discord thread."""
        linked_item = await self.find_linked_item(source_url, item_key)
        if not linked_item:
            raise RuntimeError(f"No GitHub issue linked to Discord thread: {source_url}")
        await self.update_task(linked_item, **changes)
        return github_item_key(linked_item)

    async def update_task(
        self,
        item: dict[str, Any],
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        size: str | None = None,
        start_date: str | None = None,
        target_date: str | None = None,
        assignees: list[str] | None = None,
        closed: bool | None = None,
    ) -> None:
        """Write the given fields of a task; a field left to None is untouched."""
        content = item.get("content") or {}
        issue_id = content.get("id")
        is_issue = bool(content.get("url"))
        if title is not None or description is not None:
            body = None
            if description is not None:
                body = with_description(content.get("body") or "", description)
            await self.update_content(content, title=title, body=body)
        if priority is not None and is_issue:
            await self.set_priority_label(content, priority)
            await self.update_native_priority(issue_id, priority)
        if status is not None:
            await self.update_project_status(item["id"], status)
        await self.update_optional_fields(
            item["id"],
            issue_id if is_issue else None,
            size=size,
            start_date=start_date,
            target_date=target_date,
        )
        if assignees is not None and is_issue:
            current = [
                node["login"]
                for node in (content.get("assignees") or {}).get("nodes", [])
                if node.get("login")
            ]
            await self.set_assignees(issue_id, current, assignees)
        if closed is not None and is_issue:
            if closed:
                await self.close_issue(issue_id)
            else:
                await self.reopen_issue(issue_id)

    async def update_content(
        self,
        content: dict[str, Any],
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        """Rename an issue or a draft, or rewrite its body."""
        fields = {
            name: value
            for name, value in (("title", title), ("body", body))
            if value is not None
        }
        if not fields:
            return
        if content.get("url"):
            query = """
            mutation($id: ID!, $title: String, $body: String) {
              updateIssue(input: {id: $id, title: $title, body: $body}) {
                issue { id }
              }
            }
            """
        else:
            query = """
            mutation($id: ID!, $title: String, $body: String) {
              updateProjectV2DraftIssue(input: {
                draftIssueId: $id, title: $title, body: $body
              }) { draftIssue { id } }
            }
            """
        await self._query(query, {"id": content["id"], **fields})

    async def set_priority_label(self, content: dict[str, Any], priority: str) -> None:
        """Swap the priority label of an issue for another one."""
        priority_names = {name.casefold() for name in PRIORITY_NAMES}
        priority_label_ids = [
            label["id"]
            for label in (content.get("labels") or {}).get("nodes", [])
            if label.get("name", "").casefold() in priority_names
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
                {"issueId": content["id"], "labelIds": priority_label_ids},
            )
        await self.add_label(content["id"], priority)

    async def update_optional_fields(
        self,
        item_id: str,
        issue_id: str | None = None,
        *,
        size: str | None = None,
        start_date: str | None = None,
        target_date: str | None = None,
    ) -> None:
        """Write the size and the dates, skipping a field nothing holds.

        A field is an issue field of the organisation when the repository has
        one of that name, which only `updateIssueFieldValue` writes; otherwise
        it is a field of the Project. A None value is untouched and an empty
        one clears the field.
        """
        for field_name, value in (
            (self.size_field_name, size),
            (self.start_date_field_name, start_date),
            (self.target_date_field_name, target_date),
        ):
            if value is None:
                continue
            if issue_id and (issue_field := await self.issue_field(field_name)):
                await self.set_issue_field(issue_id, issue_field, value)
                continue
            field = await self.project_field(field_name)
            if field is None:
                if value:
                    logger.warning(
                        "The GitHub Project has no %r field, %r is not written",
                        field_name,
                        value,
                    )
                continue
            if not value:
                await self.clear_project_field(item_id, field["id"])
            elif field.get("dataType") == "DATE":
                await self.update_project_value(item_id, field["id"], {"date": value})
            else:
                await self.update_project_select(item_id, field_name, value)

    async def update_project_value(
        self, item_id: str, field_id: str, value: dict[str, Any]
    ) -> None:
        query = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: ProjectV2FieldValue!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: $value
          }) { projectV2Item { id } }
        }
        """
        await self._query(
            query,
            {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "value": value,
            },
        )

    async def clear_project_field(self, item_id: str, field_id: str) -> None:
        query = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!) {
          clearProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId
          }) { projectV2Item { id } }
        }
        """
        await self._query(
            query,
            {"projectId": self.project_id, "itemId": item_id, "fieldId": field_id},
        )

    async def close_issue(self, issue_id: str) -> None:
        query = """
        mutation($issueId: ID!) {
          closeIssue(input: {issueId: $issueId, stateReason: COMPLETED}) {
            issue { id }
          }
        }
        """
        await self._query(query, {"issueId": issue_id})

    async def reopen_issue(self, issue_id: str) -> None:
        query = """
        mutation($issueId: ID!) {
          reopenIssue(input: {issueId: $issueId}) { issue { id } }
        }
        """
        await self._query(query, {"issueId": issue_id})

    async def comment_from_discord(
        self, source_url: str, body: str, item_key: str | None = None
    ) -> str | None:
        """Comment on the issue linked to a Discord thread, returning the comment url.

        No title fallback here: a reply posted on the wrong issue is worse than
        a reply not copied.
        """
        linked_item = await self.find_linked_item(source_url, item_key)
        content = (linked_item or {}).get("content") or {}
        # A draft item has no url, and no conversation to comment on.
        if not content.get("url"):
            return None
        return await self.add_comment(content["id"], body)

    async def add_comment(self, subject_id: str, body: str) -> str:
        query = """
        mutation($subjectId: ID!, $body: String!) {
          addComment(input: {subjectId: $subjectId, body: $body}) {
            commentEdge { node { id url } }
          }
        }
        """
        data = await self._query(query, {"subjectId": subject_id, "body": body})
        return data["addComment"]["commentEdge"]["node"]["url"]

    async def issue_comments(self, issue_id: str) -> list[dict[str, Any]]:
        """The latest comments of an issue, read from the issue itself."""
        query = """
        query($issueId: ID!) {
          node(id: $issueId) {
            ... on Issue { comments(last: 100) { nodes { id url body } } }
            ... on PullRequest { comments(last: 100) { nodes { id url body } } }
          }
        }
        """
        data = await self._query(query, {"issueId": issue_id})
        return ((data.get("node") or {}).get("comments") or {}).get("nodes") or []

    async def comment_copied_from(
        self, source_url: str, message_url: str, item_key: str | None
    ) -> dict[str, Any] | None:
        """The comment the bot wrote on GitHub from one Discord message."""
        linked_item = await self.find_linked_item(source_url, item_key)
        content = (linked_item or {}).get("content") or {}
        if not content.get("url"):
            return None
        return comment_of_message(await self.issue_comments(content["id"]), message_url)

    async def edit_comment_from_discord(
        self, source_url: str, message_url: str, body: str, item_key: str | None = None
    ) -> str | None:
        """Rewrite the comment copied from an edited Discord message."""
        comment = await self.comment_copied_from(source_url, message_url, item_key)
        if comment is None or comment.get("body") == body:
            return None
        query = """
        mutation($id: ID!, $body: String!) {
          updateIssueComment(input: {id: $id, body: $body}) { issueComment { id } }
        }
        """
        await self._query(query, {"id": comment["id"], "body": body})
        return comment["url"]

    async def delete_comment_from_discord(
        self, source_url: str, message_url: str, item_key: str | None = None
    ) -> str | None:
        """Delete the comment copied from a deleted Discord message."""
        comment = await self.comment_copied_from(source_url, message_url, item_key)
        if comment is None:
            return None
        query = """
        mutation($id: ID!) {
          deleteIssueComment(input: {id: $id}) { clientMutationId }
        }
        """
        await self._query(query, {"id": comment["id"]})
        return comment["url"]

    async def assignable_users(self) -> list[str]:
        """The logins an issue of the repository may be assigned to."""
        query = """
        query($repositoryId: ID!) {
          node(id: $repositoryId) {
            ... on Repository { assignableUsers(first: 100) { nodes { login } } }
          }
        }
        """
        data = await self._query(query, {"repositoryId": self.repository_id})
        nodes = ((data.get("node") or {}).get("assignableUsers") or {}).get("nodes") or []
        return [node["login"] for node in nodes if node.get("login")]

    async def update_native_priority(self, issue_id: str, priority: str) -> None:
        field = await self.issue_field(self.priority_field_name)
        option = next(
            (
                option
                for option in (field or {}).get("options", [])
                if option["name"].casefold() == priority.casefold()
            ),
            None,
        )
        # Issue fields belong to organisations: a repository of a personal
        # account has none, and keeps the priority label alone.
        if option is None:
            logger.warning(
                "The repository has no %r issue field with a %r option, "
                "the native priority is left unset",
                self.priority_field_name,
                priority,
            )
            return
        await self.set_issue_field(issue_id, field, priority)

    async def issue_fields(self, refresh: bool = False) -> list[dict[str, Any]]:
        """The issue fields of the repository, read once and kept until asked again."""
        if self._issue_fields is not None and not refresh:
            return self._issue_fields
        query = """
        query($repositoryId: ID!) {
          node(id: $repositoryId) {
            ... on Repository {
              issueFields(first: 100) {
                nodes {
                  __typename
                  ... on IssueFieldSingleSelect { id name options { id name } }
                  ... on IssueFieldDate { id name }
                  ... on IssueFieldText { id name }
                  ... on IssueFieldNumber { id name }
                }
              }
            }
          }
        }
        """
        data = await self._query(query, {"repositoryId": self.repository_id})
        nodes = ((data.get("node") or {}).get("issueFields") or {}).get("nodes") or []
        self._issue_fields = [field for field in nodes if field and field.get("id")]
        return self._issue_fields

    async def issue_field(self, field_name: str) -> dict[str, Any] | None:
        return next(
            (field for field in await self.issue_fields() if field.get("name") == field_name),
            None,
        )

    async def set_issue_field(
        self, issue_id: str, field: dict[str, Any], value: str
    ) -> None:
        """Write an issue field of an issue; an empty value clears it."""
        if not value:
            query = """
            mutation($issueId: ID!, $fieldId: ID!) {
              deleteIssueFieldValue(input: {issueId: $issueId, fieldId: $fieldId}) {
                success
              }
            }
            """
            await self._query(query, {"issueId": issue_id, "fieldId": field["id"]})
            return
        written: dict[str, Any] = {"fieldId": field["id"]}
        kind = field.get("__typename", "IssueFieldSingleSelect")
        if kind == "IssueFieldDate":
            written["dateValue"] = value
        elif kind == "IssueFieldSingleSelect":
            option = next(
                (
                    option
                    for option in field.get("options", [])
                    if option["name"].casefold() == value.casefold()
                ),
                None,
            )
            if option is None:
                raise RuntimeError(
                    f"Option {value} not found in issue field {field.get('name')}"
                )
            written["singleSelectOptionId"] = option["id"]
        elif kind == "IssueFieldNumber":
            written["numberValue"] = float(value)
        else:
            written["textValue"] = value
        query = """
        mutation($issueId: ID!, $field: IssueFieldCreateOrUpdateInput!) {
          updateIssueFieldValue(input: {issueId: $issueId, issueField: $field}) {
            issue { id }
          }
        }
        """
        await self._query(query, {"issueId": issue_id, "field": written})

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
        try:
            data = await self._query(
                query, {"projectId": self.project_id, "contentId": issue_id}
            )
        except RuntimeError as error:
            # The auto-add workflow of the Project may add the issue first.
            if "already exists" not in str(error):
                raise
            item_id = await self.project_item_id(issue_id)
            if item_id is None:
                raise
            return item_id
        return data["addProjectV2ItemById"]["item"]["id"]

    async def project_item_id(self, issue_id: str) -> str | None:
        """The id of the item an issue has in the Project, if it has one."""
        query = """
        query($issueId: ID!) {
          node(id: $issueId) {
            ... on Issue {
              projectItems(first: 20) { nodes { id project { id } } }
            }
          }
        }
        """
        data = await self._query(query, {"issueId": issue_id})
        nodes = ((data.get("node") or {}).get("projectItems") or {}).get("nodes") or []
        return next(
            (
                node["id"]
                for node in nodes
                if (node.get("project") or {}).get("id") == self.project_id
            ),
            None,
        )

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

    async def user_ids(self, logins: list[str]) -> list[str]:
        """The node ids of the logins GitHub knows, warning about the others."""
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
        return user_ids

    async def assign_issue(self, issue_id: str, logins: list[str] | str) -> None:
        """Assign every known login, warning about the ones GitHub does not know."""
        if isinstance(logins, str):
            logins = parse_assignees(logins)
        user_ids = await self.user_ids(logins)
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

    async def set_assignees(
        self, issue_id: str, current: list[str], logins: list[str]
    ) -> None:
        """Make the assignees of an issue exactly these logins."""
        wanted = {login.casefold() for login in logins}
        removed = [login for login in current if login.casefold() not in wanted]
        present = {login.casefold() for login in current}
        added = [login for login in logins if login.casefold() not in present]
        if removed and (user_ids := await self.user_ids(removed)):
            query = """
            mutation($issueId: ID!, $assigneeIds: [ID!]!) {
              removeAssigneesFromAssignable(input: {
                assignableId: $issueId, assigneeIds: $assigneeIds
              }) { assignable { ... on Issue { id } ... on PullRequest { id } } }
            }
            """
            await self._query(query, {"issueId": issue_id, "assigneeIds": user_ids})
        if added:
            await self.assign_issue(issue_id, added)

    async def update_project_status(self, item_id: str, status: str) -> None:
        await self.update_project_select(item_id, "Status", status)

    async def update_project_select(
        self, item_id: str, field_name: str, option_name: str
    ) -> None:
        field = await self.project_field(field_name)
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
        await self.update_project_value(
            item_id, field["id"], {"singleSelectOptionId": option["id"]}
        )

    async def update_project_text(
        self, item_id: str, field_name: str, text: str
    ) -> None:
        field = await self.project_field(field_name)
        if not field:
            raise RuntimeError(f"The GitHub Project has no {field_name} field")
        await self.update_project_value(item_id, field["id"], {"text": text})

    async def find_linked_item(
        self,
        source_url: str,
        item_key: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the Project item matching this Discord thread.

        An issue url as item key is read from the issue itself, which knows its
        Project item at once. The listing of the Project items lags behind, by
        up to several minutes after an issue joins it.

        In that listing, the source line of the task body is the reliable link.
        The item key and the post name are fallbacks for a task that is not
        linked yet; a title is only trusted when it identifies a single task.
        """
        if item_key and item_key.startswith("https://"):
            linked_item = await self.item_of_issue(item_key)
            if linked_item is not None:
                return linked_item
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

    async def item_of_issue(self, issue_url: str) -> dict[str, Any] | None:
        """The Project item of an issue, read from the issue, as `list_items` shapes it."""
        query = (
            """
        query($url: URI!) {
          resource(url: $url) {
            ... on Issue {
              projectItems(first: 20) { nodes { project { id } ...ItemFields } }
            }
            ... on PullRequest {
              projectItems(first: 20) { nodes { project { id } ...ItemFields } }
            }
          }
        }
        """
            + ITEM_FIELDS
        )
        data = await self._query(query, {"url": issue_url})
        nodes = ((data.get("resource") or {}).get("projectItems") or {}).get("nodes") or []
        item = next(
            (
                node
                for node in nodes
                if (node.get("project") or {}).get("id") == self.project_id
            ),
            None,
        )
        if item is None:
            return None
        return {key: value for key, value in item.items() if key != "project"}

    async def set_discord_source(self, item: dict[str, Any], source_url: str) -> None:
        """Write the Discord thread url in the task body so the link survives restarts."""
        content = item.get("content") or {}
        body = content.get("body") or ""
        if source_url in body:
            return
        body = f"{body.rstrip()}\n\nSource: {source_url}".lstrip()
        if not content.get("id"):
            raise RuntimeError("The GitHub task has no node id to write the source into")
        await self.update_content(content, body=body)

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

    async def project_metadata(self, refresh: bool = False) -> list[dict[str, Any]]:
        """The fields of the Project, read once and kept until asked again."""
        if self._metadata is not None and not refresh:
            return self._metadata
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 100) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id name dataType options { id name }
                  }
                  ... on ProjectV2Field { id name dataType }
                }
              }
            }
          }
        }
        """
        data = await self._query(query, {"projectId": self.project_id})
        self._metadata = [field for field in data["node"]["fields"]["nodes"] if field]
        return self._metadata

    async def project_field(self, field_name: str) -> dict[str, Any] | None:
        """One field of the Project, reading the fields again when it is missing.

        A field or an option added on GitHub while the bot runs is found that way.
        """
        for refresh in (False, True):
            field = next(
                (
                    field
                    for field in await self.project_metadata(refresh)
                    if field.get("name") == field_name
                ),
                None,
            )
            if field is not None:
                return field
        return None

    async def list_items(self) -> list[dict[str, Any]]:
        """Every item of the Project, page after page."""
        query = (
            """
        query($projectId: ID!, $cursor: String) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes { ...ItemFields }
              }
            }
          }
        }
        """
            + ITEM_FIELDS
        )
        items: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_ITEM_PAGES):
            data = await self._query(
                query, {"projectId": self.project_id, "cursor": cursor}
            )
            page = data["node"]["items"]
            items.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return items
            cursor = page["pageInfo"]["endCursor"]
        logger.warning(
            "The GitHub Project holds more than %d items, the rest is not read",
            MAX_ITEM_PAGES * 100,
        )
        return items
