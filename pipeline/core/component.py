import logging
import abc
import asyncio
import copy
from typing import Optional, Union, Awaitable

from dff.core.engine.core import Context, Actor

from ..service.extra import BeforeHandler, AfterHandler
from ..conditions import always_start_condition
from ..types import (
    PIPELINE_STATE_KEY,
    StartConditionCheckerFunction,
    ComponentExecutionState,
    ServiceRuntimeInfo,
    GlobalExtraHandlerType,
    ExtraHandlerFunction,
    ExtraHandlerType,
    ExtraHandlerBuilder,
)

logger = logging.getLogger(__name__)


class PipelineComponent(abc.ABC):
    """
    This class represents a pipeline component, which is a service or a service group.
    It contains some fields that they have in common.

    :param before_handler: before handler, associated with this component
    :param after_handler: after handler, associated with this component
    :param timeout: (for asynchronous only!) maximum component execution time (in seconds),
        if it exceeds this time, it is interrupted
    :param requested_async_flag: requested asynchronous property;
        if not defined, calculated_async_flag is used instead
    :param calculated_async_flag: whether the component can be asynchronous or not

        - for :py:class:`~pipeline.service.service.Service`: whether its ``handler`` is asynchronous or not
        - for :py:class:`~pipeline.service.group.ServiceGroup`: whether all its ``services`` are asynchronous or not

    :param start_condition: StartConditionCheckerFunction that is invoked before each component execution;
        component is executed only if it returns True
    :param name: component name (should be unique in single :py:class:`~pipeline.service.group.ServiceGroup`),
        should not be blank or contain `.` symbol
    :param path: separated by dots path to component, is universally unique.
    """

    def __init__(
        self,
        before_handler: Optional[ExtraHandlerBuilder] = None,
        after_handler: Optional[ExtraHandlerBuilder] = None,
        timeout: Optional[float] = None,
        requested_async_flag: Optional[bool] = None,
        calculated_async_flag: bool = False,
        start_condition: Optional[StartConditionCheckerFunction] = None,
        name: Optional[str] = None,
        path: Optional[str] = None,
    ):
        #: Maximum component execution time (in seconds),
        #: if it exceeds this time, it is interrupted (for asynchronous only!)
        self.timeout = timeout
        #: Requested asynchronous property; if not defined, :py:attr:`~requested_async_flag` is used instead
        self.requested_async_flag = requested_async_flag
        #: Calculated asynchronous property, whether the component can be asynchronous or not
        self.calculated_async_flag = calculated_async_flag
        #: Component start condition that is invoked before each component execution;
        #: component is executed only if it returns True
        self.start_condition = always_start_condition if start_condition is None else start_condition
        #: Component name (should be unique in single :py:class:`~pipeline.service.group.ServiceGroup`),
        #: should not be blank or contain '.' symbol
        self.name = name
        #: ??ot-separated path to component (should be is universally unique)
        self.path = path

        self.before_handler = BeforeHandler([] if before_handler is None else before_handler)
        self.after_handler = AfterHandler([] if after_handler is None else after_handler)

        if name is not None and (name == "" or "." in name):
            raise Exception(f"User defined service name shouldn't be blank or contain '.' (service: {name})!")

        if not calculated_async_flag and requested_async_flag:
            raise Exception(f"{type(self).__name__} '{name}' can't be asynchronous!")

    def _set_state(self, ctx: Context, value: ComponentExecutionState):
        """
        Method for component runtime state setting, state is preserved in ``ctx.framework_states`` dict,
        in subdict, dedicated to this library.

        :param ctx: context to keep state in
        :param value: state to set
        :return: None
        """
        if PIPELINE_STATE_KEY not in ctx.framework_states:
            ctx.framework_states[PIPELINE_STATE_KEY] = {}
        ctx.framework_states[PIPELINE_STATE_KEY][self.path] = value.name

    def get_state(self, ctx: Context, default: Optional[ComponentExecutionState] = None) -> ComponentExecutionState:
        """
        Method for component runtime state getting, state is preserved in `ctx.framework_states` dict,
        in subdict, dedicated to this library.

        :param ctx: context to get state from
        :param default: default to return if no record found
            (usually it's :py:attr:`~pipeline.types.ComponentExecutionState.NOT_RUN`)
        :return: :py:class:`~pipeline.types.ComponentExecutionState` of this service or default if not found
        """
        return ComponentExecutionState[
            ctx.framework_states[PIPELINE_STATE_KEY].get(self.path, default if default is not None else None)
        ]

    @property
    def asynchronous(self) -> bool:
        """
        Property, that indicates, whether this component is synchronous or asynchronous.
        It is calculated according to following rule:

           1. If component **can** be asynchronous and :py:attr:`~requested_async_flag` is set,
           it returns :py:attr:`~requested_async_flag`

           2. If component **can** be asynchronous and :py:attr:`~requested_async_flag` isn't set,
           it returns True

           3. If component **can't** be asynchronous and :py:attr:`~requested_async_flag` is False or not set,
           it returns False

           4. If component **can't** be asynchronous and :py:attr:`~requested_async_flag` is True,
           an Exception is thrown in constructor

        :return: bool
        """
        return self.calculated_async_flag if self.requested_async_flag is None else self.requested_async_flag

    async def run_extra_handler(self, stage: ExtraHandlerType, ctx: Context, actor: Actor):
        extra_handler = None
        if stage == ExtraHandlerType.BEFORE:
            extra_handler = self.before_handler
        if stage == ExtraHandlerType.AFTER:
            extra_handler = self.after_handler
        if extra_handler is None:
            return
        try:
            extra_handler_result = await extra_handler(ctx, actor, self._get_runtime_info(ctx))
            if extra_handler.asynchronous and isinstance(extra_handler_result, Awaitable):
                await extra_handler_result
        except asyncio.TimeoutError:
            logger.warning(f"{type(self).__name__} '{self.name}' {extra_handler.stage.name} extra handler timed out!")

    @abc.abstractmethod
    async def _run(self, ctx: Context, actor: Optional[Actor] = None) -> Optional[Context]:
        """
        A method for running pipeline component, it is overridden in all its children.
        This method is run after the component's timeout is set (if needed).

        :param ctx: current dialog Context
        :param actor: this Pipeline Actor or None if this is a service, that wraps Actor
        :return: Context if this is a synchronous service or None, asynchronous services shouldn't modify Context
        """
        raise NotImplementedError

    async def __call__(self, ctx: Context, actor: Optional[Actor] = None) -> Optional[Union[Context, Awaitable]]:
        """
        A method for calling pipeline components.
        It sets up timeout if this component is asynchronous and executes it using :py:meth:`~_run` method.

        :param ctx: current dialog Context
        :param actor: this Pipeline Actor or None if this is a service, that wraps Actor
        :return: Context if this is a synchronous service or :py:class:`~typing.const.Awaitable`,
            asynchronous services shouldn't modify Context
        """
        if self.asynchronous:
            task = asyncio.create_task(self._run(ctx, actor))
            return asyncio.wait_for(task, timeout=self.timeout)
        else:
            return await self._run(ctx, actor)

    def add_extra_handler(self, global_extra_handler_type: GlobalExtraHandlerType, extra_handler: ExtraHandlerFunction):
        """
        Method for adding a global extra handler to this particular component.

        :param global_extra_handler_type: a type of extra handler to add
        :param extra_handler: a GlobalExtraHandlerType to add to the component as an extra handler
        :return: None
        """
        target = (
            self.before_handler if global_extra_handler_type is GlobalExtraHandlerType.BEFORE else self.after_handler
        )
        target.functions.append(extra_handler)

    def _get_runtime_info(self, ctx: Context) -> ServiceRuntimeInfo:
        """
        Method for retrieving runtime info about this component.

        :param ctx: current dialog Context
        :return: :py:class:`~dff.core.engine.typing.ServiceRuntimeInfo`
            dict where all not set fields are replaced with ``[None]``.
        """
        return {
            "name": self.name if self.name is not None else "[None]",
            "path": self.path if self.path is not None else "[None]",
            "timeout": self.timeout,
            "asynchronous": self.asynchronous,
            "execution_state": copy.deepcopy(ctx.framework_states[PIPELINE_STATE_KEY]),
        }

    @property
    def info_dict(self) -> dict:
        """
        Property for retrieving info dictionary about this component.
        All not set fields there are replaced with ``[None]``.

        :return: info dict, containing most important component public fields as well as its type
        """
        return {
            "type": type(self).__name__,
            "name": self.name,
            "path": self.path if self.path is not None else "[None]",
            "asynchronous": self.asynchronous,
            "start_condition": self.start_condition.__name__,
            "extra_handlers": {
                "before": self.before_handler.info_dict,
                "after": self.after_handler.info_dict,
            },
        }
