"""Linear GraphQL client — polls issues, updates state, posts comments."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class IssueRef:
    """Lightweight reference to another issue — used for blocker relations."""

    identifier: str
    state: str


@dataclass
class LinearIssue:
    """Minimal issue representation from Linear."""

    id: str
    identifier: str
    title: str
    status: str
    priority: int = 0
    labels: list[str] = field(default_factory=list)
    assignee_id: str | None = None
    description: str | None = None
    blocked_by: list[IssueRef] = field(default_factory=list)

    @property
    def is_low_risk(self) -> bool:
        return "risk:low" in self.labels


@dataclass
class LinearComment:
    """A comment on a Linear issue."""

    id: str
    issue_id: str
    body: str
    created_at: str
    user_id: str | None = None


class LinearClient:
    """Async Linear API client.

    Uses a single GraphQL query per poll tick to fetch all issues in
    actionable states for the configured project.
    """

    API_URL = "https://api.linear.app/graphql"

    def __init__(self, api_key: str, project_name: str, assignee_filter: str | None = None) -> None:
        self.project_name = project_name
        self._project_id: str | None = None  # resolved lazily on first poll
        self._assignee_filter = assignee_filter  # "me" or raw user ID
        self._viewer_id: str | None = None  # resolved lazily if assignee_filter="me"
        # Resolve env var references like $LINEAR_API_KEY
        resolved_key = api_key
        if api_key.startswith("$"):
            resolved_key = os.environ.get(api_key[1:], "")
            if not resolved_key:
                logger.warning("API key env var %s is not set — Linear requests will fail", api_key)
        self._api_key = resolved_key
        # LINEAR_API_URL env var lets test harnesses redirect to a stub server.
        # Undocumented for end users; production should use the default.
        self.api_url = os.environ.get("LINEAR_API_URL", self.API_URL)
        # httpx's AsyncClient binds its transport to the event loop on first
        # use. Reusing one client across multiple asyncio.run() invocations
        # crashes with "Event loop is closed" when httpcore tries to close
        # pooled connections. Defer creation and rebind per-loop.
        self._client_headers = {
            "Authorization": resolved_key,
            "Content-Type": "application/json",
        }
        self._client_for_loop: dict[int, httpx.AsyncClient] = {}

    @property
    def is_configured(self) -> bool:
        """True if the API key is set."""
        return bool(self._api_key)

    @property
    def _client(self) -> httpx.AsyncClient:
        """Per-event-loop httpx client.

        Each running event loop gets its own client. This keeps the wizard
        (which calls ``asyncio.run`` once per step) and the long-running
        orchestrator (one loop, one client) both correct without callers
        needing to think about loop lifecycle.
        """
        loop = asyncio.get_running_loop()
        client = self._client_for_loop.get(id(loop))
        if client is None:
            client = httpx.AsyncClient(
                headers=self._client_headers,
                timeout=30.0,
            )
            self._client_for_loop[id(loop)] = client
        return client

    async def resolve_project(self) -> str:
        """Resolve project name to ID (case-insensitive). Cached after first call."""
        if self._project_id:
            return self._project_id

        # Fetch all projects and match case-insensitively, since Linear's
        # GraphQL eq filter is case-sensitive.
        query = """
        query {
            projects(first: 50) {
                nodes { id name }
            }
        }
        """
        data = await self._query(query)
        nodes = data.get("projects", {}).get("nodes", [])
        target = self.project_name.lower()
        for node in nodes:
            if node["name"].lower() == target:
                self._project_id = node["id"]
                logger.info("Resolved project '%s' → %s (matched '%s')",
                            self.project_name, self._project_id, node["name"])
                return self._project_id

        raise RuntimeError(f"No Linear project named '{self.project_name}' found")

    async def resolve_viewer(self) -> str:
        """Resolve the current user's ID via the viewer query. Cached after first call."""
        if self._viewer_id:
            return self._viewer_id
        query = "query { viewer { id name } }"
        data = await self._query(query)
        viewer = data.get("viewer", {})
        self._viewer_id = viewer.get("id")
        if not self._viewer_id:
            raise RuntimeError("Could not resolve Linear viewer — check API key")
        logger.info("Resolved viewer → %s (%s)", viewer.get("name"), self._viewer_id)
        return self._viewer_id

    async def resolve_assignee_filter(self) -> str | None:
        """Resolve assignee_filter to a user ID. Returns None if no filter."""
        if not self._assignee_filter:
            return None
        if self._assignee_filter == "me":
            return await self.resolve_viewer()
        return self._assignee_filter  # raw user ID

    MAX_RETRIES = 3
    RETRY_BACKOFF = (1.0, 3.0, 10.0)  # seconds

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query with retry on transient errors (429, 5xx)."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self._client.post(self.api_url, json=payload)

                if resp.status_code == 429 or resp.status_code >= 500:
                    backoff = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "Linear %d on attempt %d — retrying in %.0fs",
                        resp.status_code, attempt + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code >= 400:
                    body = resp.text[:500]
                    logger.error("Linear %d: %s", resp.status_code, body)

                resp.raise_for_status()
                result = resp.json()

                if "errors" in result:
                    raise RuntimeError(f"Linear GraphQL errors: {result['errors']}")

                return result.get("data", {})

            except httpx.HTTPStatusError:
                raise  # Non-retryable HTTP errors (4xx except 429)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                backoff = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Linear connection error on attempt %d — retrying in %.0fs: %s",
                    attempt + 1, backoff, e,
                )
                await asyncio.sleep(backoff)

        raise RuntimeError(f"Linear API failed after {self.MAX_RETRIES} retries") from last_exc

    async def fetch_actionable_issues(
        self,
        status_names: list[str],
        assignee_id: str | None = None,
        terminal_statuses: list[str] | None = None,
    ) -> list[LinearIssue]:
        """Fetch all project issues in any of the given statuses.

        If assignee_id is provided, only fetches issues assigned to that user.
        Single query per tick.

        If `terminal_statuses` is provided, any issue with a blocker not in a terminal
        state is filtered out of the returned list. Blockers are resolved from Linear's
        `inverseRelations` (type = "blocks"). Each skipped issue is logged with the
        blocker identifier and current state so stalled chains are visible in telemetry.

        Passing `terminal_statuses=None` disables the filter (backwards-compat path).
        """
        project_id = await self.resolve_project()

        # Build filter dynamically based on whether assignee filtering is active
        if assignee_id:
            query = """
            query($projectId: ID!, $statuses: [String!]!, $assigneeId: ID!) {
                issues(
                    filter: {
                        project: { id: { eq: $projectId } }
                        state: { name: { in: $statuses } }
                        assignee: { id: { eq: $assigneeId } }
                    }
                    orderBy: updatedAt
                    first: 50
                ) {
                    nodes {
                        id
                        identifier
                        title
                        priority
                        description
                        assignee { id }
                        state { name }
                        labels { nodes { name } }
                        inverseRelations {
                            nodes {
                                type
                                issue {
                                    identifier
                                    state { name }
                                }
                            }
                        }
                    }
                }
            }
            """
            variables = {
                "projectId": project_id,
                "statuses": status_names,
                "assigneeId": assignee_id,
            }
        else:
            query = """
            query($projectId: ID!, $statuses: [String!]!) {
                issues(
                    filter: {
                        project: { id: { eq: $projectId } }
                        state: { name: { in: $statuses } }
                    }
                    orderBy: updatedAt
                    first: 50
                ) {
                    nodes {
                        id
                        identifier
                        title
                        priority
                        description
                        assignee { id }
                        state { name }
                        labels { nodes { name } }
                        inverseRelations {
                            nodes {
                                type
                                issue {
                                    identifier
                                    state { name }
                                }
                            }
                        }
                    }
                }
            }
            """
            variables = {
                "projectId": project_id,
                "statuses": status_names,
            }
        data = await self._query(query, variables)

        all_issues: list[LinearIssue] = []
        for node in data.get("issues", {}).get("nodes", []):
            blockers: list[IssueRef] = []
            for rel in node.get("inverseRelations", {}).get("nodes", []):
                if rel.get("type") != "blocks":
                    continue
                blocker = rel.get("issue") or {}
                blocker_id = blocker.get("identifier")
                if not blocker_id:
                    continue
                if blocker_id == node["identifier"]:
                    logger.warning("%s appears to block itself — ignoring self-block", blocker_id)
                    continue
                blocker_state = (blocker.get("state") or {}).get("name", "")
                blockers.append(IssueRef(identifier=blocker_id, state=blocker_state))

            all_issues.append(LinearIssue(
                id=node["id"],
                identifier=node["identifier"],
                title=node["title"],
                status=node["state"]["name"],
                priority=node.get("priority", 0),
                labels=[label["name"] for label in node.get("labels", {}).get("nodes", [])],
                assignee_id=node.get("assignee", {}).get("id") if node.get("assignee") else None,
                description=node.get("description"),
                blocked_by=blockers,
            ))

        # Treat None and empty list as "filter disabled". An empty list would
        # otherwise activate the filter with an empty terminal set, causing every
        # blocked issue to be skipped permanently — a deadlock on misconfigured
        # `linear_states.terminal: []`.
        if not terminal_statuses:
            logger.info("Fetched %d issues in statuses %s", len(all_issues), status_names)
            return all_issues

        terminal_set = set(terminal_statuses)
        actionable: list[LinearIssue] = []
        skipped = 0
        for issue in all_issues:
            live_blockers = [b for b in issue.blocked_by if b.state not in terminal_set]
            if live_blockers:
                logger.info(
                    "Skipping %s — blocked by %s",
                    issue.identifier,
                    ", ".join(f"{b.identifier}({b.state or 'unknown'})" for b in live_blockers),
                )
                skipped += 1
                continue
            actionable.append(issue)

        logger.info(
            "Fetched %d issues in statuses %s (%d actionable, %d blocked)",
            len(all_issues), status_names, len(actionable), skipped,
        )
        return actionable

    async def fetch_recent_comments(self, issue_id: str, since: str | None = None) -> list[LinearComment]:
        """Fetch recent comments on an issue, optionally since a timestamp."""
        query = """
        query($issueId: String!) {
            issue(id: $issueId) {
                comments(orderBy: createdAt, first: 20) {
                    nodes {
                        id
                        body
                        createdAt
                        user { id }
                    }
                }
            }
        }
        """
        data = await self._query(query, {"issueId": issue_id})
        comments = []
        for node in data.get("issue", {}).get("comments", {}).get("nodes", []):
            comment = LinearComment(
                id=node["id"],
                issue_id=issue_id,
                body=node["body"],
                created_at=node["createdAt"],
                user_id=node.get("user", {}).get("id") if node.get("user") else None,
            )
            if since is None or comment.created_at > since:
                comments.append(comment)
        return comments

    async def resolve_team_for_project(self) -> str:
        """Resolve the team_id associated with this project.

        Used by autosymph.diagnostics.check_linear_states for startup validation.
        Linear projects can span multiple teams; we return the first team listed
        on the project (matches what `transition_issue` already implicitly uses
        when reading `issue.team`).
        """
        project_id = await self.resolve_project()
        query = """
        query($projectId: String!) {
            project(id: $projectId) {
                teams(first: 5) { nodes { id name } }
            }
        }
        """
        data = await self._query(query, {"projectId": project_id})
        teams = data.get("project", {}).get("teams", {}).get("nodes", [])
        if not teams:
            raise RuntimeError(
                f"Linear project '{self.project_name}' has no associated teams"
            )
        team_id = teams[0]["id"]
        logger.info(
            "Resolved team for project '%s' → %s (%s)",
            self.project_name, team_id, teams[0].get("name"),
        )
        return team_id

    async def fetch_workspace_states(self, team_id: str) -> list[str]:
        """List workflow state names available on a Linear team.

        Used by startup validation to verify configured states exist.
        """
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                states(first: 50) { nodes { id name } }
            }
        }
        """
        data = await self._query(query, {"teamId": team_id})
        nodes = data.get("team", {}).get("states", {}).get("nodes", [])
        return [n["name"] for n in nodes]

    async def fetch_workspace_labels(self) -> list[str]:
        """List issue label names available in the workspace.

        Workspace-scoped — Linear labels can be team-bound or workspace-wide;
        we list both.
        """
        query = """
        query {
            issueLabels(first: 250) { nodes { id name } }
        }
        """
        data = await self._query(query)
        nodes = data.get("issueLabels", {}).get("nodes", [])
        return [n["name"] for n in nodes]

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all projects visible to the API key.

        Returns a list of dicts with keys: id, name, slugId, teamIds. The
        onboarding wizard uses this to render a numbered project picker.
        """
        projects: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = """
            query($after: String) {
                projects(first: 50, after: $after) {
                    nodes {
                        id
                        name
                        slugId
                        teams(first: 5) { nodes { id name } }
                    }
                    pageInfo { hasNextPage endCursor }
                }
            }
            """
            data = await self._query(query, {"after": cursor})
            page = data.get("projects", {})
            for node in page.get("nodes", []):
                team_nodes = node.get("teams", {}).get("nodes", [])
                projects.append({
                    "id": node["id"],
                    "name": node["name"],
                    "slug_id": node.get("slugId"),
                    "team_ids": [t["id"] for t in team_nodes],
                    "team_names": [t["name"] for t in team_nodes],
                })
            page_info = page.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return projects

    async def create_workflow_state(
        self,
        team_id: str,
        name: str,
        color: str,
        position: float,
        state_type: str = "started",
    ) -> str:
        """Create a workflow state on the given Linear team via ``workflowStateCreate``.

        ``state_type`` follows Linear's enum: ``triage``, ``backlog``,
        ``unstarted``, ``started``, ``completed``, ``canceled``. Defaults to
        ``started`` since the wizard's required-state set is dominated by
        in-flight states; callers should override for terminals.

        Returns the new state's id. Used by the onboarding wizard to provision
        autosymph-required workflow states.
        """
        mutation = """
        mutation($input: WorkflowStateCreateInput!) {
            workflowStateCreate(input: $input) {
                success
                workflowState { id name }
            }
        }
        """
        variables = {
            "input": {
                "teamId": team_id,
                "name": name,
                "color": color,
                "position": position,
                "type": state_type,
            }
        }
        data = await self._query(mutation, variables)
        result = data.get("workflowStateCreate", {})
        if not result.get("success"):
            raise RuntimeError(
                f"Linear workflowStateCreate failed for {name!r}: {result!r}"
            )
        state = result.get("workflowState") or {}
        state_id = state.get("id")
        if not state_id:
            raise RuntimeError(
                f"Linear workflowStateCreate returned no id for {name!r}: {result!r}"
            )
        logger.info("Created Linear state %s on team %s → %s", name, team_id, state_id)
        return state_id

    async def create_label(
        self,
        name: str,
        color: str,
        team_id: str | None = None,
    ) -> str:
        """Create an issue label via ``issueLabelCreate``.

        Workspace-scoped when ``team_id`` is None; otherwise team-scoped.
        Returns the label id.
        """
        mutation = """
        mutation($input: IssueLabelCreateInput!) {
            issueLabelCreate(input: $input) {
                success
                issueLabel { id name }
            }
        }
        """
        input_obj: dict[str, Any] = {"name": name, "color": color}
        if team_id is not None:
            input_obj["teamId"] = team_id
        data = await self._query(mutation, {"input": input_obj})
        result = data.get("issueLabelCreate", {})
        if not result.get("success"):
            raise RuntimeError(
                f"Linear issueLabelCreate failed for {name!r}: {result!r}"
            )
        label = result.get("issueLabel") or {}
        label_id = label.get("id")
        if not label_id:
            raise RuntimeError(
                f"Linear issueLabelCreate returned no id for {name!r}: {result!r}"
            )
        logger.info("Created Linear label %s → %s", name, label_id)
        return label_id

    async def transition_issue(self, issue_id: str, target_status: str) -> None:
        """Move an issue to a new status by name."""
        # First resolve the status name to a state ID
        query = """
        query($issueId: String!) {
            issue(id: $issueId) {
                team {
                    states { nodes { id name } }
                }
            }
        }
        """
        data = await self._query(query, {"issueId": issue_id})
        states = data.get("issue", {}).get("team", {}).get("states", {}).get("nodes", [])
        state_id = None
        for s in states:
            if s["name"] == target_status:
                state_id = s["id"]
                break

        if not state_id:
            raise ValueError(f"No Linear state named '{target_status}' found for issue {issue_id}")

        mutation = """
        mutation($issueId: String!, $stateId: String!) {
            issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                success
            }
        }
        """
        await self._query(mutation, {"issueId": issue_id, "stateId": state_id})
        logger.info("Transitioned issue %s to '%s'", issue_id, target_status)

    async def post_comment(self, issue_id: str, body: str) -> None:
        """Post a markdown comment on an issue."""
        mutation = """
        mutation($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) {
                success
            }
        }
        """
        await self._query(mutation, {"issueId": issue_id, "body": body})
        logger.info("Posted comment on %s", issue_id)

    async def upload_file(self, filepath: str, filename: str | None = None) -> str:
        """Upload a file to Linear's CDN. Returns the permanent asset URL.

        1. Call fileUpload mutation → get pre-signed upload URL + asset URL
        2. PUT the file content to the upload URL
        3. Return the asset URL for embedding in comments
        """
        from pathlib import Path
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        file_size = path.stat().st_size
        name = filename or path.name
        content_type = "application/x-ndjson" if name.endswith(".ndjson") else "application/octet-stream"

        # Step 1: Get upload URL
        mutation = """
        mutation($size: Int!, $filename: String!, $contentType: String!) {
            fileUpload(size: $size, filename: $filename, contentType: $contentType) {
                uploadFile {
                    uploadUrl
                    assetUrl
                    headers {
                        key
                        value
                    }
                }
            }
        }
        """
        data = await self._query(mutation, {
            "size": file_size,
            "filename": name,
            "contentType": content_type,
        })

        upload_info = data.get("fileUpload", {}).get("uploadFile", {})
        upload_url = upload_info.get("uploadUrl")
        asset_url = upload_info.get("assetUrl")
        headers = {h["key"]: h["value"] for h in upload_info.get("headers", [])}

        if not upload_url or not asset_url:
            raise RuntimeError("fileUpload mutation did not return URLs")

        # Step 2: PUT file content
        file_content = path.read_bytes()
        headers["Content-Type"] = content_type
        resp = await self._client.put(upload_url, content=file_content, headers=headers)
        resp.raise_for_status()

        logger.info("Uploaded %s (%d bytes) → %s", name, file_size, asset_url)
        return asset_url

    async def close(self) -> None:
        """Close the client bound to the current event loop, if any.

        Clients bound to other (no-longer-running) loops are dropped without
        an explicit close — httpcore's ``aclose`` would crash against their
        dead loops. Their connection pools are garbage-collected.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._client_for_loop.clear()
            return
        client = self._client_for_loop.pop(id(loop), None)
        if client is not None:
            await client.aclose()
        # Drop references to clients on other loops; they're unusable now.
        self._client_for_loop.clear()
