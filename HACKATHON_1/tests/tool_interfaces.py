"""PLACEHOLDERS: Will be populated after tools are ready.

TODO(tool-integration): After names, arguments, return schemas, and sync/async
behavior are ready, adapt or replace this.
"""

from typing import Any


class PendingToolInterfaces:
    """Signature templates for mocks; every unimplemented call fails explicitly."""

    # TODO(logs): Bind the real log-search tool and provide scenario log records.
    async def search_logs(self, service: str) -> list[dict[str, Any]]:
        """Proposed input: service name.

        Proposed output: records with evidence_id, timestamp, service, level,
        and message. Include relevant errors and normal observations.
        """
        raise NotImplementedError("PLACEHOLDER: log-search tool is not integrated")

    # TODO(metrics): Bind the real metrics tool and provide before/after snapshots.
    async def get_service_metrics(self, service: str) -> list[dict[str, Any]]:
        """Proposed input: service name.

        Proposed output: records with evidence_id, timestamp, service, name,
        value, and unit. Scenario state determines the current snapshot.
        """
        raise NotImplementedError("PLACEHOLDER: metrics tool is not integrated")

    # TODO(knowledge-base): Bind the real search tool and supply searchable articles.
    async def search_knowledge_base(self, query: str) -> list[dict[str, Any]]:
        """Proposed input: a diagnostic search query.

        Proposed output: articles with evidence_id, title, symptoms, and
        remediation_guidance. Keep test expectations separate from articles.
        """
        raise NotImplementedError("PLACEHOLDER: knowledge-base tool is not integrated")

    # TODO(remediation): Bind the simulated executor and define scenario transitions.
    # Approval belongs in the workflow: tests must assert this tool is not called
    # while a required approval is pending or rejected. No real actions here.
    async def execute_remediation(
        self, service: str, action: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Proposed input: service, action name, and optional action parameters.

        Proposed output: execution_id, service, action, status, and details.
        Execution success does not imply recovery; verification decides that.
        """
        raise NotImplementedError("PLACEHOLDER: simulated remediation is not integrated")

    # TODO(health): Bind the verification tool and provide recovery/failure outcomes.
    async def check_service_health(self, service: str) -> dict[str, Any]:
        """Proposed input: service name.

        Proposed output: service, timestamp, healthy, and evidence_ids.
        Derive the result from scenario state after remediation; include
        failed verification so tests can exercise replanning.
        """
        raise NotImplementedError("PLACEHOLDER: health-check tool is not integrated")
