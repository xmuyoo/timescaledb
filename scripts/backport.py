#!/usr/bin/env python3

import os
import re
import string
import subprocess
import sys

from github import Github  # This is PyGithub.
import requests


def run_query(query):
    """A simple function to use requests.post to make the GraphQL API call."""

    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query},
        headers={"Authorization": f'Bearer {os.environ.get("GITHUB_TOKEN")}'},
        timeout=20,
    )
    response = request.json()

    # Have to work around the unique GraphQL convention of returning 200 for errors.
    if request.status_code != 200 or "errors" in response:
        raise ValueError(
            f"Query failed to run by returning code of {request.status_code}."
            f"\nQuery: '{query}'"
            f"\nResponse: '{request.json()}'"
        )

    return response


def get_referenced_issue(pr_number):
    """Get the number of issue fixed by the given pull request.
    Returns None if no issue is fixed, or more than one issue"""

    ref_result = run_query(
        string.Template(
            """
        query {
            repository(owner: "timescale", name: "timescaledb") {
              pullRequest(number: $pr_number) {
                closingIssuesReferences(first: 1) {
                  edges {
                    node {
                      number
                    }
                  }
                }
              }
            }
          }
          """
        ).substitute({"pr_number": pr_number})
    )

    # The above returns {'data': {'repository': {'pullRequest': {'closingIssuesReferences': {'edges': [{'node': {'number': 4944}}]}}}}}

    ref_edges = ref_result["data"]["repository"]["pullRequest"][
        "closingIssuesReferences"
    ]["edges"]

    if ref_edges and len(ref_edges) == 1:
        return ref_edges[0]["node"]["number"]

    return None


def set_auto_merge(pr_number):
    """Enable auto-merge for the given PR"""

    owner, name = target_repo_name.split("/")
    # We first have to find out the PR id, which is some base64 string, different
    # from its number.
    query = string.Template(
        """query {
          repository(owner: "$owner", name: "$name") {
            pullRequest(number: $pr_number) {
              id
            }
          }
        }"""
    ).substitute(pr_number=pr_number, owner=owner, name=name)
    result = run_query(query)
    pr_id = result["data"]["repository"]["pullRequest"]["id"]

    query = string.Template(
        """mutation {
            enablePullRequestAutoMerge(
                input: {
                    pullRequestId: "$pr_id",
                    mergeMethod: REBASE
                }
            ) {
                clientMutationId
            }
        }"""
    ).substitute(pr_id=pr_id)
    run_query(query)


def git_output(command):
    """Get output from the git command, checking for the successful exit code"""
    return subprocess.check_output(f"git {command}", shell=True, text=True)


def git_check(command):
    """Run a git command, checking for the successful exit code"""
    subprocess.run(f"git {command}", shell=True, check=True)


def git_returncode(command):
    """Run a git command, returning the exit code"""
    return subprocess.run(f"git {command}", shell=True, check=False).returncode


# The token has to have the "access public repositories" permission, or else creating a PR returns 404.
github = Github(os.environ.get("GITHUB_TOKEN"))

# If we are running inside Github Action, will modify the main repo.
source_remote = "origin"
source_repo_name = os.environ.get("GITHUB_REPOSITORY")
target_remote = source_remote
target_repo_name = source_repo_name

if not source_repo_name:
    # We are running manually for debugging, probably want to modify a fork.
    source_repo_name = "timescale/timescaledb"
    target_repo_name = os.environ.get("BACKPORT_TARGET_REPO")
    target_remote = os.environ.get("BACKPORT_TARGET_REMOTE")
    if not target_repo_name or not target_remote:
        print(
            "Please specify the target repositories for debugging, using the "
            "environment variables BACKPORT_TARGET_REPO (e.g. `timescale/timescaledb`) "
            "and BACKPORT_TARGET_REMOTE (e.g. `origin`).",
            file=sys.stderr,
        )
        sys.exit(1)

print(
    f"Will look at '{source_repo_name}' (git remote '{source_remote}') for bug fixes, "
    f"and create the backport PRs in '{target_repo_name}' (git remote '{target_remote}')."
)

source_repo = github.get_repo(source_repo_name)
target_repo = github.get_repo(target_repo_name)

# Set git name and email corresponding to the token user.
token_user = github.get_user()
os.environ["GIT_COMMITTER_NAME"] = token_user.name

# This is an email that is used by Github when you opt to hide your real email
# address. It is required so that the commits are recognized by Github as made
# by the user. That is, if you use a wrong e-mail, there won't be a clickable
# profile picture next to the commit in the Github interface.
os.environ[
    "GIT_COMMITTER_EMAIL"
] = f"{token_user.id}+{token_user.login}@users.noreply.github.com"
print(
    f"Will commit as {os.environ['GIT_COMMITTER_NAME']} <{os.environ['GIT_COMMITTER_EMAIL']}>"
)

# Fetch the sources
git_check(f"fetch {source_remote}")
git_check(f"fetch {target_remote}")

# Find out what is the branch corresponding to the previous version compared to
# main. We will backport to that branch.
version_config = dict(
    [
        re.match(r"^(.+)\s+=\s+(.+)$", line).group(1, 2)
        for line in git_output(f"show {source_remote}/main:version.config").splitlines()
        if line
    ]
)

previous_version = version_config["update_from_version"]
previous_version_parts = previous_version.split(".")
previous_version_parts[-1] = "x"
backport_target = ".".join(previous_version_parts)

print(f"Will backport to {backport_target}")

# Find out which commits are unique to main and target branch. Also build sets of
# the titles of these commits. We will compare the titles to check whether a
# commit was backported.
main_commits = [
    line.split("\t")
    for line in git_output(
        f'log -1000 --pretty="format:%h\t%s" {source_remote}/{backport_target}..{source_remote}/main'
    ).splitlines()
    if line
]
main_titles = {x[1] for x in main_commits}

branch_commits = [
    line.split("\t")
    for line in git_output(
        f'log -1000 --pretty="format:%h\t%s" {source_remote}/main..{source_remote}/{backport_target}'
    ).splitlines()
    if line
]
branch_commit_titles = {x[1] for x in branch_commits}

# We will do backports per-PR, because one PR, though not often, might contain
# many commits. So as the first step, go through the commits unique to main, find
# out which of them have to be backported, and remember the corresponding PRs.
# We also have to remember which commits to backport. The list from PR itself is
# not what we need, these are the original commits from the PR branch, and we
# need the resulting commits in master. Store them as dict(pr number -> PRInfo).
prs_to_backport = {}


class PRInfo:
    """Information about the PR to be backported."""

    def __init__(self, pygithub_pr_, issue_number_):
        self.pygithub_pr = pygithub_pr_
        self.pygithub_commits = []
        self.issue_number = issue_number_


for commit_sha, commit_title in main_commits:
    if commit_title in branch_commit_titles:
        print(f"{commit_sha[:9]} '{commit_title}' is already in the branch.")
        continue

    pygithub_commit = source_repo.get_commit(sha=commit_sha)

    pulls = pygithub_commit.get_pulls()
    if not pulls:
        print(f"{commit_sha[:9]} '{commit_title}' does not belong to a PR.")
        continue

    pull = pulls[0]
    issue_number = get_referenced_issue(pull.number)
    if not issue_number:
        print(
            f"{commit_sha[:9]} belongs to the PR #{pull.number} '{pull.title}' that does not close an issue."
        )
        continue

    pr_labels = {label.name for label in pull.labels}
    stopper_labels = pr_labels.intersection(["no-backport", "pr-auto-backport-failed"])
    if stopper_labels:
        print(
            f"{commit_sha[:9]} belongs to the PR #{pull.number} '{pull.title}' labeled as '{list(stopper_labels)[0]}' which prevents automated backporting."
        )
        continue

    changed_files = {file.filename for file in pull.get_files()}
    stopper_files = changed_files.intersection(
        ["sql/updates/latest-dev.sql", "sql/updates/reverse-dev.sql"]
    )
    if stopper_files:
        print(
            f"{commit_sha[:9]} belongs to the PR #{pull.number} '{pull.title}' that modifies '{list(stopper_files)[0]}' which prevents automated backporting."
        )
        continue

    issue = source_repo.get_issue(number=issue_number)
    issue_labels = {label.name for label in issue.labels}

    if "bug" not in issue_labels:
        print(
            f"{commit_sha[:9]} fixes the issue #{issue.number} '{issue.title}' that is not labeled as 'bug'."
        )
        continue

    if "no-backport" in issue_labels:
        print(
            f"{commit_sha[:9]} fixes the issue #{issue.number} '{issue.title}' that is labeled as 'no-backport'."
        )
        continue

    print(
        f"{commit_sha[:9]} '{commit_title}' from PR #{pull.number} '{pull.title}' "
        f"that fixes the issue #{issue.number} '{issue.title}' will be considered for backporting."
    )

    # Remember the PR and the corresponding resulting commit in main.
    if pull.number not in prs_to_backport:
        prs_to_backport[pull.number] = PRInfo(pull, issue_number)

    # We're traversing the history backwards, and want to have the list of
    # commits in forward order.
    prs_to_backport[pull.number].pygithub_commits.insert(0, pygithub_commit)


# Now, go over the list of PRs that we have collected, and try to backport
# each of them.
for pr_info in prs_to_backport.values():
    original_pr = pr_info.pygithub_pr
    backport_branch = f"backport/{backport_target}/{original_pr.number}"

    # If there is already a backport branch for this PR, just skip it.
    if (
        git_returncode(f"rev-parse {target_remote}/{backport_branch} > /dev/null 2>&1")
        == 0
    ):
        print(
            f'Backport branch {backport_branch} for PR #{original_pr.number}: "{original_pr.title}" already exists. Skipping.'
        )
        continue

    # Our general notion is that there should be one commit per PR, and if there
    # are several, they might be fixing different issues, and we can't figure
    # figure out which commit fixes which, and which of the fixes should be
    # backported, so we just play safe here and refuse to backport them.
    commit_shas = [commit.sha for commit in pr_info.pygithub_commits]
    if len(commit_shas) > 1:
        print(
            f'Will not backport the PR #{original_pr.number}: "{original_pr.title}" because it contains multiple commits.'
        )
        original_pr.add_to_labels("pr-auto-backport-failed")
        continue

    # Try to cherry-pick the commits.
    git_check(
        f"checkout --quiet --detach {target_remote}/{backport_target} > /dev/null"
    )

    if git_returncode(f"cherry-pick --quiet -m 1 -x {' '.join(commit_shas)}") != 0:
        git_check("cherry-pick --abort")
        print(
            f'Conflict while backporting pull request #{original_pr.number}: "{original_pr.title}"'
        )
        original_pr.add_to_labels("pr-auto-backport-failed")
        continue

    # Push the branch and create the backport PR
    git_check(
        f"push --quiet {target_remote} @:refs/heads/{backport_branch} > /dev/null 2>&1"
    )

    original_description = original_pr.body
    # Comment out the Github issue reference keywords like 'Fixes #1234', to
    # avoid having multiple PRs saying they fix a given issue. The backport PR
    # is going to reference the fixed issue as "Original issue #xxxx".
    original_description = re.sub(
        r"((fix|clos|resolv)[esd]+)(\s+#[0-9]+)",
        r"`\1`\3",
        original_description,
        flags=re.IGNORECASE,
    )
    backport_description = (
        f"""This is an automated backport of #{original_pr.number}: {original_pr.title}.
The original issue is #{pr_info.issue_number}.
"""
        "\n"
        "This PR will be merged automatically after all the relevant CI checks pass. "
        "If this fix should not be backported, or will be backported manually, "
        "just close this PR. You can use the backport branch to add your "
        "changes, it won't be modified automatically anymore."
        "\n"
        "\n"
        "## Original description"
        "\n"
        f"### {original_pr.title}"
        "\n"
        f"{original_description}"
    )

    backport_pr = target_repo.create_pull(
        title=f"Backport to {backport_target}: #{original_pr.number}: {original_pr.title}",
        body=backport_description,
        head=backport_branch,
        base=backport_target,
    )
    backport_pr.add_to_labels("pr-backport")
    backport_pr.add_to_assignees(original_pr.user.login)
    set_auto_merge(backport_pr.number)

    print(
        f"Created backport PR #{backport_pr.number} for #{original_pr.number}: {original_pr.title}"
    )
