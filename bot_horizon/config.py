from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_channel_id: int
    github_token: str
    github_project_id: str
    discord_notification_channel_id: int = 0
    notification_language: str = "en"
    github_repository_id: str = ""
    github_org_name: str = "play-horizon"
    github_project_number: int = 5
    github_priority_field_name: str = "Priority"
    github_assignee_field_name: str = "Assignees"
    github_poll_interval_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN"),
            "DISCORD_CHANNEL_ID": os.getenv("DISCORD_CHANNEL_ID"),
            "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN"),
            "GITHUB_PROJECT_ID": os.getenv("GITHUB_PROJECT_ID"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")

        return cls(
            discord_token=required["DISCORD_TOKEN"],
            discord_channel_id=int(required["DISCORD_CHANNEL_ID"]),
            github_token=required["GITHUB_TOKEN"],
            github_project_id=required["GITHUB_PROJECT_ID"],
            discord_notification_channel_id=int(
                os.getenv("DISCORD_NOTIFICATION_CHANNEL_ID") or 0
            ),
            notification_language=os.getenv("NOTIFICATION_LANGUAGE", "en"),
            github_repository_id=os.getenv("GITHUB_REPOSITORY_ID", ""),
            github_org_name=os.getenv("GITHUB_ORG_NAME", "play-horizon"),
            github_project_number=int(os.getenv("GITHUB_PROJECT_NUMBER", "5")),
            github_priority_field_name=os.getenv("GITHUB_PRIORITY_FIELD_NAME", "Priority"),
            github_assignee_field_name=os.getenv("GITHUB_ASSIGNEE_FIELD_NAME", "Assignees"),
            github_poll_interval_seconds=int(
                os.getenv("GITHUB_POLL_INTERVAL_SECONDS", "30")
            ),
        )
