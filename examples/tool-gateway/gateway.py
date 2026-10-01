# SPDX-License-Identifier: Apache-2.0
"""A tool gateway that speaks Mem2A for the agents behind it.

The gateway sits between agents and the tools they call, and holds the
credentials those tools need. For every governed tool call it:

1. turns the call into a Mem2A intent and negotiates with memory, reusing the
   open task when the agent retries the same action;
2. reads the current dossier right before acting (spec 8.3.7);
3. holds the call while a `must` constraint applies, and tells the agent why;
4. otherwise runs the tool, and commits what happened, with the tool's
   answer as evidence (spec 8.4).

The agent needs no Mem2A code of its own. To memory, the gateway is an agent
acting for the user whose connection runs the tool (spec 10.2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from a2a.types import Task

from mem2a import Mem2AClient, dossier_of, error_of, receipt_of, state_of
from mem2a.models import Constraint, Dossier, Receipt


Tool = Callable[..., Awaitable[Any]]
OPEN = 'TASK_STATE_INPUT_REQUIRED'


@dataclass(frozen=True)
class Governed:
    """How one tool's calls become Mem2A intents and reports."""

    action: str
    summary: Callable[[Mapping[str, Any]], str]
    entities: Callable[[Mapping[str, Any]], list[str]]
    claim: Callable[[Mapping[str, Any], Any], str]
    evidence: Callable[[Mapping[str, Any], Any], dict[str, str]] | None = None


class Held(Exception):
    """A tool call the gateway did not run, because a `must` constraint applies."""

    def __init__(self, tool: str, task_id: str, dossier: Dossier, rules: list[Constraint]) -> None:
        self.tool, self.task_id, self.dossier, self.rules = tool, task_id, dossier, rules
        reasons = '; '.join(f'{rule.statement} ({rule.id})' for rule in rules)
        super().__init__(f'{tool} held by company policy: {reasons}')


@dataclass
class Outcome:
    """What a call produced. `receipt` is None for tools memory doesn't govern."""

    result: Any
    receipt: Receipt | None = None
    dossier: Dossier | None = None
    conflicts: list[str] = field(default_factory=list)


class MemoryGateway:
    """Runs tools on an agent's behalf, consulting memory first.

    Args:
        memory: A client authenticated as the gateway, acting for `user`.
        user: The principal whose connection runs the tools, e.g. ``user:tom``.
        tools: The tools the gateway can run, by name.
        governed: Which tools memory governs, and how.
        on_must: ``hold`` refuses a call while a `must` constraint applies.
            ``report`` runs it anyway and lists the constraints as conflicts,
            for a person to review (spec 8.4.6).
    """

    def __init__(
        self,
        memory: Mem2AClient,
        user: str,
        tools: Mapping[str, Tool],
        governed: Mapping[str, Governed],
        on_must: Literal['hold', 'report'] = 'hold',
    ) -> None:
        self.memory, self.user, self.tools = memory, user, tools
        self.governed, self.on_must = governed, on_must
        self._open: dict[tuple[str, tuple[str, ...]], str] = {}  # action -> task id

    async def call(self, tool: str, **args: Any) -> Outcome:
        rule = self.governed.get(tool)
        if rule is None:
            return Outcome(await self.tools[tool](**args))  # not governed: run it
        task = await self._current_task(rule, args)  # read right before acting
        dossier = dossier_of(task)
        assert dossier is not None
        must = [c for c in dossier.constraints if c.level == 'must']
        if must and self.on_must == 'hold':
            raise Held(tool, task.id, dossier, must)
        result = await self.tools[tool](**args)
        return await self._commit(task, rule, args, result, dossier, [c.id for c in must])

    async def _current_task(self, rule: Governed, args: Mapping[str, Any]) -> Task:
        """The action's open task as memory has it now, or a new one.

        One task per action (spec 8): a retry of the same call reuses the task
        and its watch, so memory's answer reflects every change since.
        """
        entities = rule.entities(args)
        key = (rule.action, tuple(entities))
        if key in self._open:
            task = await self.memory.get(self._open[key])
            if state_of(task) == OPEN:
                return task
        intent = {
            'action': rule.action,
            'summary': rule.summary(args),
            'entities': entities,
            'onBehalfOf': self.user,
        }
        task = await self.memory.negotiate(intent)
        self._open[key] = task.id
        return task

    async def _commit(
        self,
        task: Task,
        rule: Governed,
        args: Mapping[str, Any],
        result: Any,
        dossier: Dossier,
        conflicts: list[str],
    ) -> Outcome:
        claim: dict[str, Any] = {
            'statement': rule.claim(args, result),
            'entities': rule.entities(args),
        }
        if rule.evidence is not None:
            claim['evidence'] = [rule.evidence(args, result)]
        report: dict[str, Any] = {
            'basedOn': dossier.version,
            'action': rule.action,
            'outcome': 'done',
            'summary': claim['statement'],
            'claims': [claim],
        }
        why = 'The gateway ran the call while this applied.'
        if conflicts:
            report['conflicts'] = [{'id': c, 'explanation': why} for c in conflicts]
        done = await self.memory.commit(task, report)
        error = error_of(done)
        if error is not None and error.code == 'stale-dossier':
            # Memory changed while the tool ran, and the action already
            # happened: commit against the current dossier, naming what the
            # action went against (spec 8.4.4).
            current = dossier_of(done)
            assert current is not None
            conflicts = [c.id for c in current.constraints if c.level == 'must']
            why = 'The tool ran before the gateway saw this.'
            report['basedOn'] = current.version
            report.pop('conflicts', None)
            if conflicts:
                report['conflicts'] = [{'id': c, 'explanation': why} for c in conflicts]
            done, dossier = await self.memory.commit(task, report), current
        receipt = receipt_of(done)
        if receipt is None:
            raise RuntimeError(f'Memory did not record the report: {error_of(done)}')
        self._open = {k: v for k, v in self._open.items() if v != task.id}
        return Outcome(result, receipt, dossier, conflicts)
