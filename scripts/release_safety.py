"""Explicit repository opt-in and identity checks shared by release automation."""

import os


def check_release_settings(repository):
    if not repository or os.environ.get("RELEASE_REPOSITORY") != repository:
        raise ValueError("RELEASE_REPOSITORY must explicitly equal the release repository")
    if "GITHUB_REPOSITORY" in os.environ and os.environ["GITHUB_REPOSITORY"] != repository:
        raise ValueError("GITHUB_REPOSITORY must equal the release repository")
    if (os.environ.get("GITHUB_EVENT_NAME") == "schedule"
            and os.environ.get("ENABLE_SCHEDULED_RELEASES") != "true"):
        raise ValueError("Scheduled releases require ENABLE_SCHEDULED_RELEASES=true")


def check_repository_info(repository, info):
    if not isinstance(info, dict) or info.get("full_name") != repository:
        raise ValueError("Release repository must match the configured name")
