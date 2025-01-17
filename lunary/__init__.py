import warnings, traceback, logging, copy, time, chevron, hashlib, aiohttp, copy

from pkg_resources import parse_version
from importlib.metadata import version, PackageNotFoundError
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional
import jsonpickle

from .parsers import default_input_parser, default_output_parser, filter_params
from .openai_utils import OpenAIUtils
from .event_queue import EventQueue
from .thread import Thread
from .utils import clean_nones, create_uuid_from_string
from .config import get_config, set_config
from .run_manager import RunManager

from .users import (
    user_ctx,
    user_props_ctx,
    identify,
)  # DO NOT REMOVE `identify`` import
from .tags import tags_ctx, tags  # DO NOT REMOVE `tags` import
from .parent import parent_ctx, parent, get_parent  # DO NOT REMOVE `parent` import
from .project import project_ctx  # DO NOT REMOVE `project` import

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


event_queue_ctx = ContextVar("event_queue_ctx")
event_queue_ctx.set(EventQueue())
queue = event_queue_ctx.get()

run_manager = RunManager()

from contextvars import ContextVar

run_ctx = ContextVar("run_ctx", default=None)


class RunContextManager:
    def __init__(self, run_id: str):
        run_ctx.set(run_id)

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, exc_tb):
        run_ctx.set(None)


def run_context(id: str) -> RunContextManager:
    return RunContextManager(id)


class LunaryException(Exception):
    pass


def get_parent():
    parent = parent_ctx.get()
    if parent and parent.get("retrieved", False) == False:
        parent_ctx.set({"message_id": parent["message_id"], "retrieved": True})
        return parent.get("message_id", None)
    return None


def config(
    app_id: str | None = None,
    verbose: str | None = None,
    api_url: str | None = None,
    disable_ssl_verify: bool | None = None,
):
    set_config(app_id, verbose, api_url, disable_ssl_verify)


def get_parent_run_id(parent_run_id: str, run_type: str, app_id: str, run_id: str):
    if parent_run_id == "None":
        parent_run_id = None

    parent_run = run_ctx.get()
    if parent_run and parent_run != run_id:
        run_ctx.set(None)
        print(parent_run)
        return str(create_uuid_from_string(str(parent_run) + str(app_id)))

    parent_from_ctx = get_parent()
    if parent_from_ctx and run_type != "thread":
        return str(create_uuid_from_string(str(parent_from_ctx) + str(app_id)))

    if parent_run_id is not None:
        return str(create_uuid_from_string(str(parent_run_id) + str(app_id)))


def track_event(
    run_type,
    event_name,
    run_id: str,
    parent_run_id=None,
    name=None,
    input=None,
    output=None,
    message=None,
    error=None,
    token_usage=None,
    user_id=None,
    user_props=None,
    tags=None,
    timestamp=None,
    thread_tags=None,
    feedback=None,
    template_id=None,
    metadata=None,
    params=None,
    runtime=None,
    app_id=None,
    api_url=None,
    callback_queue=None,
    is_openai=False,
):
    try:
        config = get_config()
        app_id = app_id or config.app_id
        api_url = api_url or config.api_url

        if not app_id:
            return warnings.warn("LUNARY_PUBLIC_KEY is not set, not sending events")

        parent_run_id = get_parent_run_id(
            parent_run_id, run_type, app_id=app_id, run_id=run_id
        )

        # We need to generate a UUID that is unique by run_id / project_id pair in case of multiple concurrent callback handler use
        run_id = str(create_uuid_from_string(str(run_id) + str(app_id)))

        event = {
            "event": event_name,
            "type": run_type,
            "name": name,
            "userId": user_id or user_ctx.get(),
            "userProps": user_props or user_props_ctx.get(),
            "tags": tags or tags_ctx.get(),
            "threadTags": thread_tags,
            "runId": run_id,
            "parentRunId": parent_run_id,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "message": message,
            "input": input,
            "output": output,
            "error": error,
            "feedback": feedback,
            "runtime": runtime or "lunary-py",
            "tokensUsage": token_usage,
            "metadata": metadata,
            "params": params,
            "templateId": template_id,
            "appId": app_id,
        }

        if callback_queue is not None:
            callback_queue.append(event)
        else:
            queue.append(event)

        if config.verbose:
            event_copy = clean_nones(copy.deepcopy(event))
            logger.info(
                f"\nAdd event: {jsonpickle.encode(event_copy, unpicklable=False, indent=4)}\n"
            )

    except Exception as e:
        logger.exception("Error in `track_event`", e)


def stream_handler(fn, run_id, name, type, *args, **kwargs):
    try:
        stream = fn(*args, **kwargs)

        choices = []
        tokens = 0

        for chunk in stream:
            tokens += 1
            if not chunk.choices:
                # Azure
                continue

            choice = chunk.choices[0]
            index = choice.index

            content = choice.delta.content
            role = choice.delta.role
            function_call = choice.delta.function_call
            tool_calls = choice.delta.tool_calls

            if len(choices) <= index:
                choices.append(
                    {
                        "message": {
                            "role": role,
                            "content": content or "",
                            "function_call": {},
                            "tool_calls": [],
                        }
                    }
                )

            if content:
                choices[index]["message"]["content"] += content

            if role:
                choices[index]["message"]["role"] = role

            if hasattr(function_call, "name"):
                choices[index]["message"]["function_call"]["name"] = function_call.name

            if hasattr(function_call, "arguments"):
                choices[index]["message"]["function_call"].setdefault("arguments", "")
                choices[index]["message"]["function_call"][
                    "arguments"
                ] += function_call.arguments

            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    existing_call_index = next(
                        (
                            index
                            for (index, tc) in enumerate(
                                choices[index]["message"]["tool_calls"]
                            )
                            if tc.index == tool_call.index
                        ),
                        -1,
                    )

                if existing_call_index == -1:
                    choices[index]["message"]["tool_calls"].append(tool_call)

                else:
                    existing_call = choices[index]["message"]["tool_calls"][
                        existing_call_index
                    ]
                    if hasattr(tool_call, "function") and hasattr(
                        tool_call.function, "arguments"
                    ):
                        existing_call.function.arguments += tool_call.function.arguments

            yield chunk
    finally:
        stream.close()

    output = OpenAIUtils.parse_message(choices[0]["message"])
    track_event(
        type,
        "end",
        run_id,
        name=name,
        output=output,
        token_usage={"completion": tokens, "prompt": None},
    )
    return


async def async_stream_handler(fn, run_id, name, type, *args, **kwargs):
    stream = await fn(*args, **kwargs)

    choices = []
    tokens = 0

    async for chunk in stream:
        tokens += 1
        if not chunk.choices:
            # Happens with Azure
            continue

        choice = chunk.choices[0]
        index = choice.index

        content = choice.delta.content
        role = choice.delta.role
        function_call = choice.delta.function_call
        tool_calls = choice.delta.tool_calls

        if len(choices) <= index:
            choices.append(
                {
                    "message": {
                        "role": role,
                        "content": content or "",
                        "function_call": {},
                        "tool_calls": [],
                    }
                }
            )

        if content:
            choices[index]["message"]["content"] += content

        if role:
            choices[index]["message"]["role"] = role

        if hasattr(function_call, "name"):
            choices[index]["message"]["function_call"]["name"] = function_call.name

        if hasattr(function_call, "arguments"):
            choices[index]["message"]["function_call"].setdefault("arguments", "")
            choices[index]["message"]["function_call"][
                "arguments"
            ] += function_call.arguments

        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                existing_call_index = next(
                    (
                        index
                        for (index, tc) in enumerate(
                            choices[index]["message"]["tool_calls"]
                        )
                        if tc.index == tool_call.index
                    ),
                    -1,
                )

            if existing_call_index == -1:
                choices[index]["message"]["tool_calls"].append(tool_call)

            else:
                existing_call = choices[index]["message"]["tool_calls"][
                    existing_call_index
                ]
                if hasattr(tool_call, "function") and hasattr(
                    tool_call.function, "arguments"
                ):
                    existing_call.function.arguments += tool_call.function.arguments

        yield chunk

    output = OpenAIUtils.parse_message(choices[0]["message"])
    track_event(
        type,
        "end",
        run_id,
        name=name,
        output=output,
        token_usage={"completion": tokens, "prompt": None},
    )
    return


def wrap(
    fn,
    type=None,
    run_id=None,
    name=None,
    user_id=None,
    user_props=None,
    tags=None,
    input_parser=default_input_parser,
    output_parser=default_output_parser,
):
    def sync_wrapper(*args, **kwargs):
        output = None

        parent_run_id = kwargs.pop("parent", None)
        run = run_manager.start_run(run_id, parent_run_id)

        with run_context(run.id):
            try:
                try:
                    params = filter_params(kwargs)
                    metadata = kwargs.pop("metadata", None)
                    parsed_input = input_parser(*args, **kwargs)

                    track_event(
                        type,
                        "start",
                        run_id=run.id,
                        parent_run_id=parent_run_id,
                        input=parsed_input["input"],
                        name=name or parsed_input["name"],
                        user_id=kwargs.pop("user_id", None)
                        or user_ctx.get()
                        or user_id,
                        user_props=kwargs.pop("user_props", None)
                        or user_props
                        or user_props_ctx.get(),
                        params=params,
                        metadata=metadata,
                        tags=kwargs.pop("tags", None) or tags or tags_ctx.get(),
                        template_id=kwargs.get("extra_headers", {}).get(
                            "Template-Id", None
                        ),
                        is_openai=True,
                    )
                except Exception as e:
                    logging.exception(e)

                if kwargs.get("stream") == True:
                    return stream_handler(
                        fn, run.id, name or parsed_input["name"], type, *args, **kwargs
                    )

                try:
                    output = fn(*args, **kwargs)

                except Exception as e:
                    track_event(
                        type,
                        "error",
                        run.id,
                        error={"message": str(e), "stack": traceback.format_exc()},
                    )

                    # rethrow error
                    raise e

                try:
                    parsed_output = output_parser(output, kwargs.get("stream", False))

                    track_event(
                        type,
                        "end",
                        run.id,
                        name=name
                        or parsed_input[
                            "name"
                        ],  # Need name in case need to compute tokens usage server side
                        output=parsed_output["output"],
                        token_usage=parsed_output["tokensUsage"],
                    )
                    return output
                except Exception as e:
                    logger.exception(e)(e)
                finally:
                    return output
            finally:
                run_manager.end_run(run.id)

    return sync_wrapper


def async_wrap(
    fn,
    type=None,
    name=None,
    user_id=None,
    user_props=None,
    tags=None,
    input_parser=default_input_parser,
    output_parser=default_output_parser,
):
    async def wrapper(*args, **kwargs):
        async def async_wrapper(*args, **kwargs):
            output = None

            parent_run_id = kwargs.pop("parent", None)
            run = run_manager.start_run(parent_run_id=parent_run_id)

            try:
                try:
                    params = filter_params(kwargs)
                    metadata = kwargs.pop("metadata", None)
                    parsed_input = input_parser(*args, **kwargs)

                    track_event(
                        type,
                        "start",
                        run_id=run.id,
                        parent_run_id=parent_run_id,
                        input=parsed_input["input"],
                        name=name or parsed_input["name"],
                        user_id=kwargs.pop("user_id", None)
                        or user_ctx.get()
                        or user_id,
                        user_props=kwargs.pop("user_props", None)
                        or user_props
                        or user_props_ctx.get(),
                        params=params,
                        metadata=metadata,
                        tags=kwargs.pop("tags", None) or tags or tags_ctx.get(),
                        template_id=kwargs.get("extra_headers", {}).get(
                            "Template-Id", None
                        ),
                    )
                except Exception as e:
                    logger.exception(e)

                try:
                    output = await fn(*args, **kwargs)

                except Exception as e:
                    track_event(
                        type,
                        "error",
                        run.id,
                        error={"message": str(e), "stack": traceback.format_exc()},
                    )

                    # rethrow error
                    raise e

                try:
                    parsed_output = output_parser(output, kwargs.get("stream", False))

                    track_event(
                        type,
                        "end",
                        run.id,
                        name=name
                        or parsed_input[
                            "name"
                        ],  # Need name in case need to compute tokens usage server side
                        output=parsed_output["output"],
                        token_usage=parsed_output["tokensUsage"],
                    )
                    return output
                except Exception as e:
                    logger.exception(e)(e)
                finally:
                    return output
            finally:
                run_manager.end_run(run.id)

        def async_stream_wrapper(*args, **kwargs):
            parent_run_id = kwargs.pop("parent", None)
            run = run_manager.start_run(parent_run_id=parent_run_id)

            try:
                try:
                    params = filter_params(kwargs)
                    metadata = kwargs.pop("metadata", None)
                    parsed_input = input_parser(*args, **kwargs)

                    track_event(
                        type,
                        "start",
                        run_id=run.id,
                        parent_run_id=parent_run_id,
                        input=parsed_input["input"],
                        name=name or parsed_input["name"],
                        user_id=kwargs.pop("user_id", None)
                        or user_ctx.get()
                        or user_id,
                        user_props=kwargs.pop("user_props", None)
                        or user_props
                        or user_props_ctx.get(),
                        tags=kwargs.pop("tags", None) or tags or tags_ctx.get(),
                        params=params,
                        metadata=metadata,
                        template_id=kwargs.get("extra_headers", {}).get(
                            "Template-Id", None
                        ),
                    )
                except Exception as e:
                    logger.exception(e)

                return async_stream_handler(
                    fn, run.id, name or parsed_input["name"], type, *args, **kwargs
                )
            finally:
                run_manager.end_run(run.id)

        if kwargs.get("stream") == True:
            return async_stream_wrapper(*args, **kwargs)
        else:
            return await async_wrapper(*args, **kwargs)

    return wrapper


def monitor(object):
    try:
        openai_version = parse_version(version("openai"))
        name = getattr(object, "__name__", getattr(type(object), "__name__", None))

        if openai_version >= parse_version("1.0.0") and openai_version < parse_version(
            "2.0.0"
        ):
            name = getattr(type(object), "__name__", None)
            if name == "openai" or name == "OpenAI" or name == "AzureOpenAI":
                try:
                    object.chat.completions.create = wrap(
                        object.chat.completions.create,
                        "llm",
                        input_parser=OpenAIUtils.parse_input,
                        output_parser=OpenAIUtils.parse_output,
                    )
                except Exception as e:
                    logging.info(
                        "Please use `lunary.monitor(openai)` or `lunary.monitor(client)` after setting the OpenAI api key"
                    )

            elif name == "AsyncOpenAI" or name == "AsyncAzureOpenAI":
                object.chat.completions.create = async_wrap(
                    object.chat.completions.create,
                    "llm",
                    input_parser=OpenAIUtils.parse_input,
                    output_parser=OpenAIUtils.parse_output,
                )
            else:
                logging.info(
                    "Unknown OpenAI client. You can only use `lunary.monitor(openai)` or `lunary.monitor(client)`"
                )
        elif openai_version < parse_version("1.0.0"):
            object.ChatCompletion.create = wrap(
                object.ChatCompletion.create,
                "llm",
                input_parser=OpenAIUtils.parse_input,
                output_parser=OpenAIUtils.parse_output,
            )

            object.ChatCompletion.acreate = wrap(
                object.ChatCompletion.acreate,
                "llm",
                input_parser=OpenAIUtils.parse_input,
                output_parser=OpenAIUtils.parse_output,
            )

    except PackageNotFoundError:
        logging.info("The `openai` package is not installed")


def agent(name=None, user_id=None, user_props=None, tags=None):
    def decorator(fn):
        return wrap(
            fn,
            "agent",
            name=name or fn.__name__,
            user_id=user_id,
            user_props=user_props,
            tags=tags,
            input_parser=default_input_parser,
        )

    return decorator


def chain(name=None, user_id=None, user_props=None, tags=None):
    def decorator(fn):
        return wrap(
            fn,
            "chain",
            name=name or fn.__name__,
            user_id=user_id,
            user_props=user_props,
            tags=tags,
            input_parser=default_input_parser,
        )

    return decorator


def tool(name=None, user_id=None, user_props=None, tags=None):
    def decorator(fn):
        return wrap(
            fn,
            "tool",
            name=name or fn.__name__,
            user_id=user_id,
            user_props=user_props,
            tags=tags,
            input_parser=default_input_parser,
        )

    return decorator


try:
    import importlib.metadata
    import logging
    import os
    import traceback
    import warnings
    from contextvars import ContextVar
    from typing import Any, Dict, List, Union, cast, Sequence, Optional
    from uuid import UUID

    import requests
    from langchain_core.agents import AgentFinish
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.messages import BaseMessage, BaseMessageChunk, ToolMessage
    from langchain_core.documents import Document
    from langchain_core.outputs import LLMResult
    from langchain_core.load import dumps
    from packaging.version import parse

    logger = logging.getLogger(__name__)

    DEFAULT_API_URL = "https://api.lunary.ai"

    user_ctx = ContextVar[Union[str, None]]("user_ctx", default=None)
    user_props_ctx = ContextVar[Union[str, None]]("user_props_ctx", default=None)

    spans: Dict[str, Any] = {}

    PARAMS_TO_CAPTURE = [
        "temperature",
        "top_p",
        "top_k",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "function_call",
        "functions",
        "tools",
        "tool_choice",
        "response_format",
        "max_tokens",
        "logit_bias",
    ]

    class UserContextManager:
        """Context manager for Lunary user context."""

        def __init__(self, user_id: str, user_props: Any = None) -> None:
            user_ctx.set(user_id)
            user_props_ctx.set(user_props)

        def __enter__(self) -> Any:
            pass

        def __exit__(self, exc_type: Any, exc_value: Any, exc_tb: Any) -> Any:
            user_ctx.set(None)
            user_props_ctx.set(None)

    def identify(user_id: str, user_props: Any = None) -> UserContextManager:
        """Builds a Lunary UserContextManager

        Parameters:
            - `user_id`: The user id.
            - `user_props`: The user properties.

        Returns:
            A context manager that sets the user context.
        """
        return UserContextManager(user_id, user_props)

    def _serialize(data: Any):
        if not data:
            return None

        if hasattr(data, "messages"):
            return _serialize(data.messages)
        if isinstance(data, BaseMessage) or isinstance(data, BaseMessageChunk):
            return _parse_lc_message(data)
        elif isinstance(data, dict):
            return {key: _serialize(value) for key, value in data.items()}
        elif isinstance(data, list):
            if len(data) == 1:
                return _serialize(data[0])
            else:
                return [_serialize(item) for item in data]
        elif isinstance(data, (str, int, float, bool)):
            return data
        else:
            return dumps(data)

    def _parse_input(raw_input: Any) -> Any:
        serialized = _serialize(raw_input)
        if isinstance(serialized, dict):
            if serialized.get("input"):
                return serialized["input"]

        return serialized

    def _parse_output(raw_output: dict) -> Any:
        serialized = _serialize(raw_output)
        if isinstance(serialized, dict):
            if serialized.get("output"):
                return serialized["output"]

        return serialized

    def _parse_lc_role(
        role: str,
    ) -> str:
        if role == "human":
            return "user"
        elif role == "ai":
            return "assistant"
        else:
            return role

    def _get_user_id(metadata: Any) -> Any:
        if user_ctx.get() is not None:
            return user_ctx.get()

        metadata = metadata or {}
        user_id = metadata.get("user_id")
        return user_id

    def _get_user_props(metadata: Any) -> Any:
        if user_props_ctx.get() is not None:
            return user_props_ctx.get()

        metadata = metadata or {}
        return metadata.get("user_props", None)

    def _parse_tool_call(tool_call: Dict[str, Any]):
        tool_call = {
            "id": tool_call.get("id"),
            "type": "function",
            "function": {
                "name": tool_call.get("name"),
                "arguments": str(tool_call.get("args")),
            },
        }
        return tool_call

    def _parse_tool_message(tool_message: ToolMessage):
        tool_message = {
            "role": "tool",
            "content": getattr(tool_message, "content", None),
            "name": getattr(tool_message, "name", None),
            "tool_call_id": getattr(tool_message, "tool_call_id", None),
        }
        return tool_message

    def _parse_lc_message(message: BaseMessage) -> Dict[str, Any]:
        if message.type == "tool":
            return _parse_tool_message(message)

        parsed = {"content": message.content, "role": _parse_lc_role(message.type)}

        # For tool calls in input
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            parsed["tool_calls"] = [
                _parse_tool_call(tool_call) for tool_call in tool_calls
            ]

        # For tool calls in output
        keys = ["function_call", "tool_calls", "tool_call_id", "name"]
        parsed.update(
            {
                key: cast(Any, message.additional_kwargs.get(key))
                for key in keys
                if message.additional_kwargs.get(key) is not None
            }
        )

        return parsed

    def _parse_lc_messages(
        messages: Union[List[BaseMessage], Any]
    ) -> List[Dict[str, Any]]:
        return [_parse_lc_message(message) for message in messages]

    class LunaryCallbackHandler(BaseCallbackHandler):
        """Callback Handler for Lunary`.

        #### Parameters:
            - `app_id`: The app id of the app you want to report to. Defaults to
            `None`, which means that `LUNARY_PUBLIC_KEY` will be used.
            - `api_url`: The url of the Lunary API. Defaults to `None`,
            which means that either `LUNARY_API_URL` environment variable
            or `https://api.lunary.ai` will be used.

        #### Raises:
            - `ValueError`: if `app_id` is not provided either as an
            argument or as an environment variable.
            - `ConnectionError`: if the connection to the API fails.


        #### Example:
        ```python
        from langchain_openai.chat_models import ChatOpenAI
        from lunary import LunaryCallbackHandler

        handler = LunaryCallbackHandler()
        llm = ChatOpenAI(callbacks=[handler],
                    metadata={"userId": "user-123"})
        llm.predict("Hello, how are you?")
        ```
        """

        __app_id: str
        __api_url: str

        def __init__(
            self,
            app_id: Union[str, None] = None,
            api_url: Union[str, None] = None,
        ) -> None:
            super().__init__()
            config = get_config()
            try:
                import lunary

                self.__lunary_version = importlib.metadata.version("lunary")
                self.__track_event = lunary.track_event

            except ImportError:
                logger.warning(
                    """To use the Lunary callback handler you need to 
                    have the `lunary` Python package installed. Please install it 
                    with `pip install lunary`"""
                )
                self.__has_valid_config = False
                return

            if parse(self.__lunary_version) < parse("0.0.32"):
                logger.warning(
                    f"""The installed `lunary` version is
                    {self.__lunary_version}
                    but `LunaryCallbackHandler` requires at least version 0.1.1
                    upgrade `lunary` with `pip install --upgrade lunary`"""
                )
                self.__has_valid_config = False

            self.__has_valid_config = True

            self.__app_id = app_id or config.app_id
            if self.__app_id is None:
                logger.warning(
                    """app_id must be provided either as an argument or 
                    as an environment variable"""
                )
                self.__has_valid_config = False

            self.__api_url = api_url or config.api_url or None

            self.queue = queue

            if self.__has_valid_config is False:
                return None

        def on_llm_start(
            self,
            serialized: Dict[str, Any],
            prompts: List[str],
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            tags: Union[List[str], None] = None,
            metadata: Union[Dict[str, Any], None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                user_id = _get_user_id(metadata)
                user_props = _get_user_props(metadata)

                params = kwargs.get("invocation_params", {})
                params.update(
                    serialized.get("kwargs", {})
                )  # Sometimes, for example with ChatAnthropic, `invocation_params` is empty

                name = (
                    params.get("model")
                    or params.get("model_name")
                    or params.get("model_id")
                    or params.get("deployment_name")
                    or params.get("azure_deployment")
                )

                if not name and "anthropic" in params.get("_type"):
                    name = "claude-2"

                params = filter_params(params)
                input = _parse_input(prompts)

                self.__track_event(
                    "llm",
                    "start",
                    user_id=user_id,
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    name=name,
                    input=input,
                    tags=tags,
                    metadata=metadata,
                    params=params,
                    user_props=user_props,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_llm_start`: {e}")

        def on_chat_model_start(
            self,
            serialized: Dict[str, Any],
            messages: List[List[BaseMessage]],
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            tags: Union[List[str], None] = None,
            metadata: Union[Dict[str, Any], None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                user_id = _get_user_id(metadata)
                user_props = _get_user_props(metadata)

                params = kwargs.get("invocation_params", {})
                params.update(
                    serialized.get("kwargs", {})
                )  # Sometimes, for example with ChatAnthropic, `invocation_params` is empty

                name = (
                    params.get("model")
                    or params.get("model_name")
                    or params.get("model_id")
                    or params.get("deployment_name")
                    or params.get("azure_deployment")
                )

                if not name and "anthropic" in params.get("_type"):
                    name = "claude-2"

                params = filter_params(params)
                input = _parse_lc_messages(messages[0])

                self.__track_event(
                    "llm",
                    "start",
                    user_id=user_id,
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    name=name,
                    input=input,
                    tags=tags,
                    metadata=metadata,
                    params=params,
                    user_props=user_props,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_chat_model_start`: {e}")

        def on_llm_end(
            self,
            response: LLMResult,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run_id = run_manager.end_run(run_id)

                token_usage = (response.llm_output or {}).get("token_usage", {})
                parsed_output: Any = [
                    (
                        _parse_lc_message(generation.message)
                        if hasattr(generation, "message")
                        else generation.text
                    )
                    for generation in response.generations[0]
                ]

                # if it's an array of 1, just parse the first element
                if len(parsed_output) == 1:
                    parsed_output = parsed_output[0]

                self.__track_event(
                    "llm",
                    "end",
                    run_id=run_id,
                    output=parsed_output,
                    token_usage={
                        "prompt": token_usage.get("prompt_tokens"),
                        "completion": token_usage.get("completion_tokens"),
                    },
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_llm_end`: {e}")

        def on_tool_start(
            self,
            serialized: Dict[str, Any],
            input_str: str,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            tags: Union[List[str], None] = None,
            metadata: Union[Dict[str, Any], None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                user_id = _get_user_id(metadata)
                user_props = _get_user_props(metadata)
                name = serialized.get("name")

                self.__track_event(
                    "tool",
                    "start",
                    user_id=user_id,
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    name=name,
                    input=input_str,
                    tags=tags,
                    metadata=metadata,
                    user_props=user_props,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_tool_start`: {e}")

        def on_tool_end(
            self,
            output: str,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            tags: Union[List[str], None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run_id = run_manager.end_run(run_id)
                self.__track_event(
                    "tool",
                    "end",
                    run_id=run_id,
                    output=output,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_tool_end`: {e}")

        def on_chain_start(
            self,
            serialized: Dict[str, Any],
            inputs: Dict[str, Any],
            *args,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            tags: Union[List[str], None] = None,
            metadata: Union[Dict[str, Any], None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                name = (
                    serialized.get("id", [None, None, None, None])[3]
                    if len(serialized.get("id", [])) > 3
                    else None
                )
                type = "chain"
                metadata = metadata or {}

                agentName = metadata.get("agent_name")
                if agentName is None:
                    agentName = metadata.get("agentName")

                if name == "AgentExecutor" or name == "PlanAndExecute":
                    type = "agent"
                if agentName is not None:
                    type = "agent"
                    name = agentName
                if parent_run_id is not None:
                    type = "chain"
                    name = kwargs.get("name")

                user_id = _get_user_id(metadata)
                user_props = _get_user_props(metadata)
                input = _parse_input(inputs)

                self.__track_event(
                    type,
                    "start",
                    user_id=user_id,
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    name=name,
                    input=input,
                    tags=tags,
                    metadata=metadata,
                    user_props=user_props,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_chain_start`: {e}")

        def on_chain_end(
            self,
            outputs: Dict[str, Any],
            *args,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run_id = run_manager.end_run(run_id)

                output = _parse_output(outputs)

                self.__track_event(
                    "chain",
                    "end",
                    run_id=run_id,
                    output=output,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_chain_end`: {e}")

        def on_agent_finish(
            self,
            finish: AgentFinish,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run_id = run_manager.end_run(run_id)

                output = _parse_output(finish.return_values)

                self.__track_event(
                    "agent",
                    "end",
                    run_id=run_id,
                    output=output,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_agent_finish`: {e}")

        def on_chain_error(
            self,
            error: BaseException,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run_id = run_manager.end_run(run_id)

                self.__track_event(
                    "chain",
                    "error",
                    run_id=run_id,
                    error={"message": str(error), "stack": traceback.format_exc()},
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_chain_error`: {e}")

        def on_tool_error(
            self,
            error: BaseException,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run_id = run_manager.end_run(run_id)

                self.__track_event(
                    "tool",
                    "error",
                    run_id=run_id,
                    error={"message": str(error), "stack": traceback.format_exc()},
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_tool_error`: {e}")

        def on_llm_error(
            self,
            error: BaseException,
            *,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                run_id = run_manager.end_run(run_id)

                self.__track_event(
                    "llm",
                    "error",
                    run_id=run_id,
                    error={"message": str(error), "stack": traceback.format_exc()},
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_llm_error`: {e}")

        def on_retriever_start(
            self,
            serialized: Dict[str, Any],
            query: str,
            run_id: Optional[UUID] = None,
            parent_run_id: Optional[UUID] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                user_id = _get_user_id(kwargs.get("metadata"))
                user_props = _get_user_props(kwargs.get("metadata"))

                name = serialized.get("name")

                self.__track_event(
                    "retriever",
                    "start",
                    user_id=user_id,
                    user_props=user_props,
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    name=name,
                    input=query,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_retriever_start`: {e}")

        def on_retriever_end(
            self,
            documents: Sequence[Document],
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run = run_manager.start_run(run_id, parent_run_id)

                # only report the metadata
                doc_metadata = [
                    (
                        doc.metadata
                        if doc.metadata
                        else {"summary": doc.page_content[:100]}
                    )
                    for doc in documents
                ]

                self.__track_event(
                    "retriever",
                    "end",
                    run_id=run.id,
                    parent_run_id=run.parent_run_id,
                    output=doc_metadata,
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_retriever_end`: {e}")

        def on_retriever_error(
            self,
            error: BaseException,
            run_id: UUID,
            parent_run_id: Union[UUID, None] = None,
            **kwargs: Any,
        ) -> None:
            try:
                run_id = run_manager.end_run(run_id)

                self.__track_event(
                    "retriever",
                    "error",
                    run_id=run_id,
                    error={"message": str(error), "stack": traceback.format_exc()},
                    app_id=self.__app_id,
                    api_url=self.__api_url,
                    callback_queue=self.queue,
                    runtime="langchain-py",
                )
            except Exception as e:
                logger.exception(f"An error occurred in `on_retriever_error`: {e}")

except Exception as e:
    # Do not raise or print error for users that do not have Langchain installed
    pass


def open_thread(id: Optional[str] = None, tags: Optional[List[str]] = None):
    return Thread(track_event=track_event, id=id, tags=tags)


def track_feedback(run_id: str, feedback: Dict[str, Any]):
    if not run_id or not isinstance(run_id, str):
        logger.exception("No message ID provided to track feedback")
        return

    if not isinstance(feedback, dict):
        logger.exception("Invalid feedback provided. Pass a valid object")
        return

    track_event(None, "feedback", run_id=run_id, feedback=feedback)


templateCache = {}


def get_raw_template(slug: str, app_id: str | None = None, api_url: str | None = None):
    config = get_config()
    token = app_id or config.app_id
    api_url = api_url or config.api_url

    global templateCache
    now = time.time() * 1000
    cache_entry = templateCache.get(slug)

    if cache_entry and now - cache_entry["timestamp"] < 60000:
        return cache_entry["data"]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    response = requests.get(
        f"{api_url}/v1/template_versions/latest?slug={slug}",
        headers=headers,
        verify=config.ssl_verify,
    )
    if not response.ok:
        logger.exception(
            f"Error fetching template: {response.status_code} - {response.text}"
        )

    data = response.json()
    templateCache[slug] = {"timestamp": now, "data": data}
    return data


async def get_raw_template_async(
    slug: str, app_id: str | None = None, api_url: str | None = None
):
    config = get_config()
    token = app_id or config.app_id
    api_url = api_url or config.api_url

    global templateCache
    now = time.time() * 1000
    cache_entry = templateCache.get(slug)

    if cache_entry and now - cache_entry["timestamp"] < 60000:
        return cache_entry["data"]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{api_url}/v1/template_versions/latest?slug={slug}", headers=headers
        ) as response:
            if not response.ok:
                raise Exception(
                    f"Lunary: Error fetching template: {response.status} - {await response.text()}"
                )

            data = await response.json()

    templateCache[slug] = {"timestamp": now, "data": data}
    return data


def render_template(
    slug: str, data={}, app_id: str | None = None, api_url: str | None = None
):
    try:
        raw_template = get_raw_template(slug, app_id, api_url)

        if (
            raw_template.get("message")
            == "Template not found, is the project ID correct?"
        ):
            raise Exception("Template not found, are the project ID and slug correct?")

        template_id = copy.deepcopy(raw_template["id"])
        content = copy.deepcopy(raw_template["content"])
        extra = copy.deepcopy(raw_template["extra"])

        text_mode = isinstance(content, str)

        # extra_headers is safe with OpenAI to be used to pass value
        extra_headers = {"Template-Id": str(template_id)}

        result = None
        if text_mode:
            rendered = chevron.render(content, data)
            result = {"text": rendered, "extra_headers": extra_headers, **extra}
            return result
        else:
            messages = []
            for message in content:
                message["content"] = chevron.render(message["content"], data)
                messages.append(message)
            result = {"messages": messages, "extra_headers": extra_headers, **extra}

            return result
    except Exception as e:
        logging.exception(f"Error rendering template {e}")


async def render_template_async(
    slug: str, data={}, app_id: str | None = None, api_url: str | None = None
):
    try:
        raw_template = await get_raw_template_async(slug, app_id, api_url)

        if (
            raw_template.get("message")
            == "Template not found, is the project ID correct?"
        ):
            raise Exception("Template not found, are the project ID and slug correct?")

        template_id = copy.deepcopy(raw_template["id"])
        content = copy.deepcopy(raw_template["content"])
        extra = copy.deepcopy(raw_template["extra"])

        text_mode = isinstance(content, str)

        # extra_headers is safe with OpenAI to be used to pass value
        extra_headers = {"Template-Id": str(template_id)}

        result = None
        if text_mode:
            rendered = chevron.render(content, data)
            result = {"text": rendered, "extra_headers": extra_headers, **extra}
            return result
        else:
            messages = []
            for message in content:
                message["content"] = chevron.render(message["content"], data)
                messages.append(message)
            result = {"messages": messages, "extra_headers": extra_headers, **extra}

            return result
    except Exception as e:
        logging.exception(f"Error rendering template {e}")


def get_langchain_template(
    slug: str, app_id: str | None = None, api_url: str | None = None
):
    try:
        from langchain_core.prompts import ChatPromptTemplate, PromptTemplate

        raw_template = get_raw_template(slug, app_id, api_url)

        if (
            raw_template.get("message")
            == "Template not found, is the project ID correct?"
        ):
            raise Exception("Template not found, are the project ID and slug correct?")

        content = copy.deepcopy(raw_template["content"])

        def replace_double_braces(text):
            return text.replace("{{", "{").replace("}}", "}")

        text_mode = isinstance(content, str)

        if text_mode:
            # replace {{ variables }} with { variables }
            rendered = replace_double_braces(content)
            template = PromptTemplate.from_template(rendered)
            return template

        else:
            messages = []

            # Return array of messages:
            #  [
            #     ("system", "You are a helpful AI bot. Your name is {name}."),
            #     ("human", "Hello, how are you doing?"),
            #     ("ai", "I'm doing well, thanks!"),
            #     ("human", "{user_input}"),
            # ]
            for message in content:
                messages.append(
                    (
                        message["role"]
                        .replace("assistant", "ai")
                        .replace("user", "human"),
                        replace_double_braces(message["content"]),
                    )
                )

            template = ChatPromptTemplate.from_messages(messages)

            return template

    except Exception as e:
        logger.exception(f"Error fetching template: {e}")


async def get_langchain_template_async(
    slug, app_id: str | None = None, api_url: str | None = None
):
    try:
        from langchain_core.prompts import ChatPromptTemplate, PromptTemplate

        raw_template = await get_raw_template_async(slug, app_id, api_url)

        if (
            raw_template.get("message")
            == "Template not found, is the project ID correct?"
        ):
            raise Exception("Template not found, are the project ID and slug correct?")

        content = copy.deepcopy(raw_template["content"])

        def replace_double_braces(text):
            return text.replace("{{", "{").replace("}}", "}")

        text_mode = isinstance(content, str)

        if text_mode:
            # replace {{ variables }} with { variables }
            rendered = replace_double_braces(content)

            template = PromptTemplate.from_template(rendered)

            return template

        else:
            messages = []

            # Return array of messages like that:
            #  [
            #     ("system", "You are a helpful AI bot. Your name is {name}."),
            #     ("human", "Hello, how are you doing?"),
            #     ("ai", "I'm doing well, thanks!"),
            #     ("human", "{user_input}"),
            # ]

            for message in content:
                messages.append(
                    (
                        message["role"]
                        .replace("assistant", "ai")
                        .replace("user", "human"),
                        replace_double_braces(message["content"]),
                    )
                )

            template = ChatPromptTemplate.from_messages(messages)

            return template

    except Exception as e:
        logger.exception(f"Error fetching template: {e}")


def get_live_templates(app_id: str | None = None, api_url: str | None = None):
    try:
        config = get_config()
        token = app_id or config.app_id
        api_url = api_url or config.api_url

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        response = requests.get(
            url=f"{api_url}/v1/templates/latest",
            headers=headers,
            verify=config.ssl_verify,
        )
        if not response.ok:
            logger.exception(
                f"Error fetching template: {response.status_code} - {response.text}"
            )

        templates = response.json()
        return templates
    except Exception as e:
        raise LunaryException(f"An error occurred fetching templates: {str(e)}") from e


import humps


class DatasetItem:
    def __init__(self, d=None):
        if d is not None:
            for key, value in d.items():
                setattr(self, key, value)


def get_dataset(slug: str, app_id: str | None = None, api_url: str | None = None):
    config = get_config()
    token = app_id or config.app_id
    api_url = api_url or config.api_url

    try:
        url = f"{api_url}/v1/datasets/{slug}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        response = requests.get(url, headers=headers, verify=config.ssl_verify)
        if response.ok:
            dataset = response.json()
            dataset = humps.decamelize(dataset)
            items_data = dataset.get("items", [])
            items = [DatasetItem(d=item) for item in items_data]

            return items
        else:
            raise Exception(f"Status code: {response.status_code}")

    except Exception as e:
        logger.exception(f"Error fetching dataset {e}")


def evaluate(
    checklist,
    input,
    output,
    ideal_output=None,
    context=None,
    model=None,
    duration=None,
    tags=None,
    app_id: str | None = None,
    api_url: str | None = None,
):
    config = get_config()
    token = app_id or config.app_id
    api_url = api_url or config.api_url

    try:
        url = f"{api_url}/v1/evaluations/run"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        data = {"checklist": checklist, "input": input, "output": output}
        if ideal_output is not None:
            data["idealOutput"] = ideal_output
        if context is not None:
            data["context"] = context
        if model is not None:
            data["model"] = model
        if duration is not None:
            data["duration"] = duration
        if tags is not None:
            data["tags"] = tags

        response = requests.post(
            url, headers=headers, json=data, verify=config.ssl_verify
        )
        if response.status_code == 500:
            error_message = response.json().get("message")
            raise Exception(f"Evaluation error: {error_message}")

        data = humps.decamelize(response.json())
        passed = data["passed"]
        results = data["results"]

        return passed, results

    except Exception as e:
        logger.exception(
            "Error evaluating result. Please contact support@lunary.ai if the problem persists."
        )
        raise e
