"""Basic agent class. See https://mini-swe-agent.com/latest/advanced/control_flow/ for visual explanation
or https://minimal-agent.com for a tutorial on the basic building principles.
"""

import json
import logging
import time
import traceback
from pathlib import Path

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel

from karox.engine import Environment, Model, __version__
from karox.engine.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted, TimeExceeded
from karox.engine.utils.serialize import recursive_merge


class AgentConfig(BaseModel):
    """Check the config files in minisweagent/config for example settings."""

    system_template: str
    """Template for the system message (the first message)."""
    instance_template: str
    """Template for the first user message specifying the task (the second message overall)."""
    step_limit: int = 0
    """Maximum number of steps the agent can take."""
    cost_limit: float = 3.0
    """Stop agent after exceeding (!) this cost."""
    wall_time_limit_seconds: int = 0
    """Stop agent after this many seconds of wall-clock time. 0 means no limit."""
    max_consecutive_format_errors: int = 3
    """Exit after this many format errors in a row (0 = no limit)."""
    submit_on_final_answer: bool = True
    """Treat a prose final answer as a submission instead of killing the run.

    karo-core port 3 of the KaroX task loop. The protocol asks for exactly one
    bash tool call per response, but a model that considers the task finished
    answers in prose -- the stage-A smoke and 19 of 25 stage-0..v2 runs ended in
    RepeatedFormatError with the finished patch already in the working tree
    (plopp a1/a2: solution complete, killed after three "solution is complete"
    replies). After the format-error budget is spent, a non-empty answer that
    was not truncated (finish_reason == "stop") is accepted as the final answer
    and the run exits as Submitted. The nudge messages still give the model its
    chances to continue with a real action first.
    """
    no_progress_streak: int = 4
    """Identical action+observation pairs in a row before the run is submitted.

    karo-core port 4 of the KaroX task loop. A finished model can keep looping
    on a no-op marker instead of submitting: goodconf v2a2 spent 512 of its 641
    calls on `echo "DONE"` / `echo "Task complete."` with identical output, hit
    the wall clock and never submitted. Four identical steps in a row carry no
    new information, so the workspace is final and the run exits as Submitted
    (exit_reason=no-progress). 0 disables the check.
    """
    output_path: Path | None = None
    """Save the trajectory to this path."""
    # karo-core port 2 of the KaroX context economy: deterministic history
    # compaction before every model call. The stock loop re-sends the whole
    # linear history each step, so a losing run burns the full context every
    # call until the wall clock ends it (stage-0: goodconf 121M, plopp 130M
    # input tokens). No model calls are spent: old observations are clipped,
    # then dropped, oldest first; the system message, the task message and the
    # most recent messages are never touched. Token estimates are chars//4.
    context_token_budget: int = 160_000
    """0 disables compaction; otherwise clip/drop history above this estimate.

    v2 (stage A): 400k never fired inside the task walls — long loops kept a
    history just under it and re-paid it every call (nestedpd 803 calls /
    128.9M input). 160k est rides ~4x cheaper and still leaves the loop more
    context than stock mini uses on tasks it solves.
    """
    keep_recent_messages: int = 24
    """Messages at the tail of the history that compaction never touches."""
    old_observation_chars: int = 400
    """Clip older message bodies to this many characters before dropping them."""
    tail_observation_chars: int = 2_000
    """Longest body a protected tail message may keep when the budget is over.

    The tail stays protected from *dropping*, not from *measurement*: 24 recent
    messages at the 50k-char tool ceiling are ~300k tokens that were resent on
    every call forever, and a short history was never compacted at all. When
    the middle has nothing left to free, the oldest tail bodies are clipped to
    fit, in place, keeping role and tool-call ids so the transcript stays valid.
    """
    tail_min_chars: int = 200
    """Floor for a clipped tail body; below this the text stops being useful."""


#: Fence markers that mean "this text is an attempted action, not a finish".
#: Kept local so the agent layer does not import the litellm model layer.
_ACTION_FENCE_MARKERS = ("```mswea_bash_command", "```bash", "```sh", "```shell")


class DefaultAgent:
    def __init__(self, model: Model, env: Environment, *, config_class: type = AgentConfig, **kwargs):
        """See the `AgentConfig` class for permitted keyword arguments."""
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env
        self.extra_template_vars = {}
        self.logger = logging.getLogger("agent")
        self.cost = 0.0
        self.n_calls = 0
        self.n_consecutive_format_errors = 0
        self.n_context_events = 0
        #: Tokens still over budget after compaction gave up; 0 means the sent
        #: history fits. The product reports this instead of claiming success.
        self.context_overflow_tokens = 0
        self._last_action_key: str = ""
        self._same_step_streak = 0
        self._start_time = time.time()

    def get_template_vars(self, **kwargs) -> dict:
        return recursive_merge(
            self.config.model_dump(),
            self.env.get_template_vars(),
            self.model.get_template_vars(),
            {
                "n_model_calls": self.n_calls,
                "model_cost": self.cost,
                "elapsed_seconds": int(time.time() - self._start_time),
            },
            self.extra_template_vars,
            kwargs,
        )

    def _render_template(self, template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**self.get_template_vars())

    def add_messages(self, *messages: dict) -> list[dict]:
        self.logger.debug(messages)  # set log level to debug to see
        self.messages.extend(messages)
        return list(messages)

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=str(e),
                extra={
                    "exit_status": type(e).__name__,
                    "submission": "",
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )

    def run(self, task: str = "", **kwargs) -> dict:
        """Run step() until agent is finished. Returns dictionary with exit_status, submission keys."""
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0  # reset on any clean step
            except FormatError as e:
                # The call was billed before parsing failed, so query() never got to charge it.
                self.cost += float(e.messages[0].get("extra", {}).get("cost") or 0.0)
                self.n_consecutive_format_errors += 1
                if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                    self.add_messages(*e.messages)
                    final_text = self._final_answer_text(e) if self.config.submit_on_final_answer else ""
                    if final_text:
                        self.logger.info(
                            "karo-core agent: prose final answer after %d format errors -> submit",
                            self.n_consecutive_format_errors,
                        )
                        self.add_messages(
                            {
                                "role": "exit",
                                "content": final_text,
                                "extra": {"exit_status": "Submitted", "submission": final_text},
                            }
                        )
                    else:
                        self.add_messages(
                            {
                                "role": "exit",
                                "content": "RepeatedFormatError",
                                "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                            }
                        )
                else:
                    self.add_messages(*e.messages)
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    def step(self) -> list[dict]:
        """Query the LM, execute actions."""
        return self.execute_actions(self.query())

    def query(self) -> dict:
        """Query the model and return model messages. Override to add hooks."""
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )
        self.n_calls += 1
        self.manage_context()
        message = self.model.query(self.messages)
        self.cost += float(message.get("extra", {}).get("cost") or 0.0)
        self.add_messages(message)
        return message

    # ------------------------------------------------------------------
    # karo-core finish contract (see AgentConfig for the why)
    # ------------------------------------------------------------------

    @staticmethod
    def _final_answer_text(error: FormatError) -> str:
        """Return the model's prose answer when this format error is one.

        A no-tool-call response is only accepted as a final answer when it was
        not truncated (finish_reason == "stop") and carries non-empty text: a
        cut-off or empty response is a real format failure, not a submission.
        Text that carries a bash code fence is an attempted action, not a
        finish (sentry-4179 v3: "Let me look at the tests..." + a command fence
        was accepted as final and the run ended at step 6 with nothing done);
        port 5 in the model layer executes those instead.
        """
        if not error.messages:
            return ""
        response = (error.messages[0].get("extra") or {}).get("response")
        if not isinstance(response, dict):
            return ""
        choices = response.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            return ""
        message = choice.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            return ""
        text = content.strip()
        if not text or any(marker in text for marker in _ACTION_FENCE_MARKERS):
            return ""
        return text

    # ------------------------------------------------------------------
    # karo-core context economy (see AgentConfig for the why)
    # ------------------------------------------------------------------

    _CLIP_MARKER = "[karo-core context manager: clipped"
    _TAIL_CLIP_MARKER = "[karo-core context manager: tail clipped"
    _ARGS_MARKER = "[karo-core context manager: tool arguments elided]"
    _DROP_MARKER = "[dropped by the karo-core context manager]"

    def _estimate_tokens(self) -> int:
        """Estimate what the provider is actually sent, not just message bodies.

        ``content`` alone undercounted every assistant turn that carried a
        ``repo.write_file`` body or a reasoning block: those fields ride in
        ``tool_calls``/``reasoning_blocks`` and are re-sent on every call.
        """

        total = 0
        for message in self.messages:
            total += len(str(message.get("content") or ""))
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                total += len(str(call.get("raw_arguments") or ""))
                total += len(str(call.get("name") or "")) + 16
            for block in message.get("reasoning_blocks") or ():
                if not isinstance(block, dict):
                    continue
                total += len(str(block.get("text") or ""))
                total += len(str(block.get("data") or ""))
                total += len(str(block.get("signature") or ""))
        return total // 4

    def _elide_tool_arguments(self, message: dict) -> bool:
        """Replace a large historical tool-call argument with a marker.

        The call keeps its id, name and position, and its tool result keeps the
        matching ``tool_call_id``, so the transcript remains valid for every
        provider while a 1 MB ``write_file`` body stops being re-sent forever.
        """

        changed = False
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            raw = call.get("raw_arguments")
            if isinstance(raw, str) and len(raw) > 400 and self._ARGS_MARKER not in raw:
                call["raw_arguments"] = self._ARGS_MARKER
                changed = True
        return changed

    def _clip_message(self, message: dict, chars: int) -> bool:
        """Clip one message body to ~chars; returns True when it changed."""
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= chars or self._CLIP_MARKER in content:
            return self._elide_tool_arguments(message)
        head = max(0, chars - len(self._CLIP_MARKER) - 24)
        message["content"] = f"{content[:head]}\n... {self._CLIP_MARKER} {len(content) - head} chars] ..."
        self._elide_tool_arguments(message)
        return True

    def _shrink_tail(self, budget: int) -> bool:
        """Clip protected tail bodies until the estimate fits; never drop one.

        Oldest first, newest last. The tail keeps its roles, order and tool-call
        ids; only long bodies are shortened, so a transcript that was valid
        before compaction stays valid. Returns True when anything changed.
        """

        changed = False
        floor = max(0, int(self.config.tail_min_chars))
        ceiling = max(floor, int(self.config.tail_observation_chars))
        for message in self.messages[2:]:
            if self._estimate_tokens() <= budget:
                break
            content = message.get("content")
            if isinstance(content, str) and len(content) > floor:
                excess_chars = max(0, self._estimate_tokens() - budget) * 4
                target = max(floor, min(ceiling, len(content) - excess_chars))
                if target < len(content):
                    head = max(0, target - len(self._TAIL_CLIP_MARKER) - 24)
                    message["content"] = (
                        f"{content[:head]}\n... {self._TAIL_CLIP_MARKER} "
                        f"{len(content) - head} chars] ..."
                    )
                    self._elide_tool_arguments(message)
                    changed = True
                    continue
            # A long write body rides in tool_calls, not content; elide it when
            # the bodies alone cannot bring the estimate down.
            if self._estimate_tokens() > budget and self._elide_tool_arguments(message):
                changed = True
        return changed

    def manage_context(self) -> None:
        """Keep the history under the token budget, oldest-first.

        Both passes are deterministic and model-free. The middle is clipped to
        ``old_observation_chars`` and then dropped one message at a time. When
        the middle has nothing left to free -- a short history, or a tail full
        of huge tool results -- the protected tail is clipped in place to
        ``tail_observation_chars`` instead of being resent forever. The system
        message and the task message are never modified. Terminates: every
        iteration shrinks the history or the loop breaks.
        """

        budget = self.config.context_token_budget
        if budget <= 0 or len(self.messages) <= 2:
            self.context_overflow_tokens = 0
            return
        guard = max(2, self.config.keep_recent_messages)
        changed = False
        while self._estimate_tokens() > budget:
            middle = self.messages[2 : len(self.messages) - guard]
            if middle:
                if any(self._clip_message(m, self.config.old_observation_chars) for m in middle):
                    changed = True
                    continue
                target = next((m for m in middle if m.get("content") != self._DROP_MARKER), None)
                if target is not None:
                    target["content"] = self._DROP_MARKER
                    self._elide_tool_arguments(target)
                    changed = True
                    continue
            if self._shrink_tail(budget):
                changed = True
                continue
            break
        self.context_overflow_tokens = max(0, self._estimate_tokens() - budget)
        if changed:
            self.n_context_events += 1
            self.logger.info(
                "karo-core context manager: history compacted to ~%d tokens (event %d, over budget by %d)",
                self._estimate_tokens(),
                self.n_context_events,
                self.context_overflow_tokens,
            )

    def execute_actions(self, message: dict) -> list[dict]:
        """Execute actions in message, add observation messages, return them."""
        outputs = [self.env.execute(action) for action in message.get("extra", {}).get("actions", [])]
        self._check_no_progress(message, outputs)
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def _check_no_progress(self, message: dict, outputs: list[dict]) -> None:
        """Submit when the same action produces the same observation N times.

        See ``AgentConfig.no_progress_streak``: a model that believes it is
        done may keep issuing a no-op command instead of finishing. The check
        is deterministic and costs no model calls.
        """
        threshold = self.config.no_progress_streak
        if threshold <= 0:
            return
        commands = [str(a.get("command", "")) for a in message.get("extra", {}).get("actions", [])]
        results = [(o.get("returncode"), str(o.get("output", ""))) for o in outputs]
        key = repr((commands, results))
        self._same_step_streak = self._same_step_streak + 1 if key == self._last_action_key else 1
        self._last_action_key = key
        if self._same_step_streak < threshold:
            return
        submission = str(message.get("content") or "").strip() or "no progress: repeated identical step"
        raise Submitted(
            {
                "role": "exit",
                "content": submission,
                "extra": {
                    "exit_status": "Submitted",
                    "exit_reason": "no-progress",
                    "submission": submission,
                },
            }
        )

    def serialize(self, *extra_dicts) -> dict:
        """Serialize agent state to a json-compatible nested dictionary for saving."""
        last_message = self.messages[-1] if self.messages else {}
        last_extra = last_message.get("extra", {})
        agent_data = {
            "info": {
                "model_stats": {
                    "instance_cost": self.cost,
                    "api_calls": self.n_calls,
                    "context_events": self.n_context_events,
                },
                "config": {
                    "agent": self.config.model_dump(mode="json"),
                    "agent_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
                "mini_version": __version__,
                "exit_status": last_extra.get("exit_status", ""),
                "submission": last_extra.get("submission", ""),
            },
            "messages": self.messages,
            "trajectory_format": "mini-swe-agent-1.1",
        }
        return recursive_merge(agent_data, self.model.serialize(), self.env.serialize(), *extra_dicts)

    def save(self, path: Path | None, *extra_dicts) -> dict:
        """Save the trajectory of the agent to a file if path is given. Returns full serialized data.
        You can pass additional dictionaries with extra data to be (recursively) merged into the output data.
        """
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        return data
