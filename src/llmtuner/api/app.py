import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Sequence

from pydantic import BaseModel

from ..chat import ChatModel
from ..data import Role as DataRole
from ..extras.misc import torch_gc
from ..extras.packages import is_fastapi_availble, is_starlette_available, is_uvicorn_available
from .protocol import (
    ChatCompletionMessage,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseStreamChoice,
    ChatCompletionResponseUsage,
    ChatCompletionStreamResponse,
    Finish,
    Function,
    FunctionCall,
    ModelCard,
    ModelList,
    Role,
    ScoreEvaluationRequest,
    ScoreEvaluationResponse,
)


if is_fastapi_availble():
    from fastapi import FastAPI, HTTPException, status
    from fastapi.middleware.cors import CORSMiddleware


if is_starlette_available():
    from sse_starlette import EventSourceResponse


if is_uvicorn_available():
    import uvicorn


@asynccontextmanager
async def lifespan(app: "FastAPI"):  # collects GPU memory
    """
    Asynchronous context manager to manage the lifespan of a FastAPI application.

    Parameters
    ----------
    app : FastAPI
        The FastAPI application whose lifespan is being managed.

    Yields
    ------
    None
        The context manager yields control back to the caller after executing its block.

    Notes
    -----
    This function collects GPU memory using `torch_gc()` upon exiting the context manager.
    """
    yield
    torch_gc()


def dictify(data: "BaseModel") -> Dict[str, Any]:
    """
    Convert a Pydantic BaseModel instance to a dictionary.

    Parameters
    ----------
    data : BaseModel
        The Pydantic BaseModel instance to convert.

    Returns
    -------
    dict
        A dictionary representation of the Pydantic BaseModel.

    Notes
    -----
    This function utilizes `model_dump()` or `dict()` method of the BaseModel instance based on Pydantic version.
    """
    try:  # pydantic v2
        return data.model_dump(exclude_unset=True)
    except AttributeError:  # pydantic v1
        return data.dict(exclude_unset=True)


def jsonify(data: "BaseModel") -> str:
    """
    Convert a Pydantic BaseModel instance to a JSON string.

    Parameters
    ----------
    data : BaseModel
        The Pydantic BaseModel instance to convert.

    Returns
    -------
    str
        A JSON string representation of the Pydantic BaseModel.

    Notes
    -----
    This function utilizes `model_dump()` or `json()` method of the BaseModel instance based on Pydantic version.
    """
    try:  # pydantic v2
        return json.dumps(data.model_dump(exclude_unset=True), ensure_ascii=False)
    except AttributeError:  # pydantic v1
        return data.json(exclude_unset=True, ensure_ascii=False)


def create_app(chat_model: "ChatModel") -> "FastAPI":
    """
    Create a FastAPI application with endpoints for chat-related functionalities.

    Parameters
    ----------
    chat_model : ChatModel
        The chat model to be used by the application.

    Returns
    -------
    FastAPI
        A FastAPI application instance configured with chat-related endpoints.

    Notes
    -----
    This function sets up endpoints for listing models, creating chat completions, streaming chat completions, and scoring evaluations.
    """
    app = FastAPI(lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    semaphore = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT", 1)))
    role_mapping = {
        Role.USER: DataRole.USER,
        Role.ASSISTANT: DataRole.ASSISTANT,
        Role.SYSTEM: DataRole.SYSTEM,
        Role.FUNCTION: DataRole.FUNCTION,
        Role.TOOL: DataRole.OBSERVATION,
    }

    @app.get("/v1/models", response_model=ModelList)
    async def list_models():
        """
        Retrieve a list of available models.

        Parameters
        ----------
        None

        Returns
        -------
        ModelList
            A list of available models wrapped in a ModelList object.

        Notes
        -----
        This endpoint returns a list of available models, currently including a single model card with the ID "gpt-3.5-turbo".
        """
        model_card = ModelCard(id="gpt-3.5-turbo")
        return ModelList(data=[model_card])

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse, status_code=status.HTTP_200_OK)
    async def create_chat_completion(request: ChatCompletionRequest):
        """
        Generate chat completions based on user input.

        Parameters
        ----------
        request : ChatCompletionRequest
            The request object containing user messages and configuration parameters.

        Returns
        -------
        ChatCompletionResponse
            A response object containing chat completions and usage statistics.

        Raises
        ------
        HTTPException
            If the request is invalid or if chat generation is not allowed.

        Notes
        -----
        This endpoint generates chat completions based on user messages, system input, and tool usage.
        """
        if not chat_model.can_generate:
            raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Not allowed")

        if len(request.messages) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid length")

        if role_mapping[request.messages[0].role] == DataRole.SYSTEM:
            system = request.messages.pop(0).content
        else:
            system = ""

        if len(request.messages) % 2 == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only supports u/a/u/a/u...")

        input_messages = []
        for i, message in enumerate(request.messages):
            input_messages.append({"role": role_mapping[message.role], "content": message.content})
            if i % 2 == 0 and input_messages[i]["role"] not in [DataRole.USER, DataRole.OBSERVATION]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
            elif i % 2 == 1 and input_messages[i]["role"] not in [DataRole.ASSISTANT, DataRole.FUNCTION]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

        tool_list = request.tools
        if isinstance(tool_list, list) and len(tool_list):
            try:
                tools = json.dumps([tool["function"] for tool in tool_list], ensure_ascii=False)
            except Exception:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid tools")
        else:
            tools = ""

        async with semaphore:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, chat_completion, input_messages, system, tools, request)

    def chat_completion(messages: Sequence[Dict[str, str]], system: str, tools: str, request: ChatCompletionRequest):
        """
        Generate chat completions based on user input, system input, and tools.

        Parameters
        ----------
        messages : Sequence[Dict[str, str]]
            The sequence of user messages and their corresponding roles.

        system : str
            The system input message, if provided.

        tools : str
            The JSON-encoded list of tool functions, if provided.

        request : ChatCompletionRequest
            The request object containing configuration parameters.

        Returns
        -------
        ChatCompletionResponse
            A response object containing chat completions and usage statistics.

        Notes
        -----
        This function generates chat completions based on user messages, system input, and tool usage.
        """
        if request.stream:
            if tools:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot stream function calls.")

            generate = stream_chat_completion(messages, system, tools, request)
            return EventSourceResponse(generate, media_type="text/event-stream")

        responses = chat_model.chat(
            messages,
            system,
            tools,
            do_sample=request.do_sample,
            temperature=request.temperature,
            top_p=request.top_p,
            max_new_tokens=request.max_tokens,
            num_return_sequences=request.n,
        )

        prompt_length, response_length = 0, 0
        choices = []
        for i, response in enumerate(responses):
            if tools:
                result = chat_model.template.format_tools.extract(response.response_text)
            else:
                result = response.response_text

            if isinstance(result, tuple):
                name, arguments = result
                function = Function(name=name, arguments=arguments)
                response_message = ChatCompletionMessage(
                    role=Role.ASSISTANT, tool_calls=[FunctionCall(function=function)]
                )
                finish_reason = Finish.TOOL
            else:
                response_message = ChatCompletionMessage(role=Role.ASSISTANT, content=result)
                finish_reason = Finish.STOP if response.finish_reason == "stop" else Finish.LENGTH

            choices.append(
                ChatCompletionResponseChoice(index=i, message=response_message, finish_reason=finish_reason)
            )
            prompt_length = response.prompt_length
            response_length += response.response_length

        usage = ChatCompletionResponseUsage(
            prompt_tokens=prompt_length,
            completion_tokens=response_length,
            total_tokens=prompt_length + response_length,
        )

        return ChatCompletionResponse(model=request.model, choices=choices, usage=usage)

    def stream_chat_completion(
        messages: Sequence[Dict[str, str]], system: str, tools: str, request: ChatCompletionRequest
    ):
        """
        Stream chat completions based on user input, system input, and tools.

        Parameters
        ----------
        messages : Sequence[Dict[str, str]]
            The sequence of user messages and their corresponding roles.

        system : str
            The system input message, if provided.

        tools : str
            The JSON-encoded list of tool functions, if provided.

        request : ChatCompletionRequest
            The request object containing configuration parameters.

        Yields
        ------
        str
            A JSON string representation of each chat completion chunk.

        Notes
        -----
        This function streams chat completions based on user messages, system input, and tool usage.
        """
        choice_data = ChatCompletionResponseStreamChoice(
            index=0, delta=ChatCompletionMessage(role=Role.ASSISTANT, content=""), finish_reason=None
        )
        chunk = ChatCompletionStreamResponse(model=request.model, choices=[choice_data])
        yield jsonify(chunk)

        for new_text in chat_model.stream_chat(
            messages,
            system,
            tools,
            do_sample=request.do_sample,
            temperature=request.temperature,
            top_p=request.top_p,
            max_new_tokens=request.max_tokens,
        ):
            if len(new_text) == 0:
                continue

            choice_data = ChatCompletionResponseStreamChoice(
                index=0, delta=ChatCompletionMessage(content=new_text), finish_reason=None
            )
            chunk = ChatCompletionStreamResponse(model=request.model, choices=[choice_data])
            yield jsonify(chunk)

        choice_data = ChatCompletionResponseStreamChoice(
            index=0, delta=ChatCompletionMessage(), finish_reason=Finish.STOP
        )
        chunk = ChatCompletionStreamResponse(model=request.model, choices=[choice_data])
        yield jsonify(chunk)
        yield "[DONE]"

    @app.post("/v1/score/evaluation", response_model=ScoreEvaluationResponse, status_code=status.HTTP_200_OK)
    async def create_score_evaluation(request: ScoreEvaluationRequest):
        """
        Evaluate the scores of a chat model based on user input.

        Parameters
        ----------
        request : ScoreEvaluationRequest
            The request object containing user messages and configuration parameters.

        Returns
        -------
        ScoreEvaluationResponse
            A response object containing evaluation scores.

        Raises
        ------
        HTTPException
            If the request is invalid or if chat generation is allowed.

        Notes
        -----
        This endpoint evaluates the scores of a chat model based on user input, using the `get_scores()` method of the chat model.
        """
        if chat_model.can_generate:
            raise HTTPException(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Not allowed")

        if len(request.messages) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")

        async with semaphore:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, get_score, request)

    def get_score(request: ScoreEvaluationRequest):
        """
        Get the scores of a chat model based on user input.

        Parameters
        ----------
        request : ScoreEvaluationRequest
            The request object containing user messages and configuration parameters.

        Returns
        -------
        ScoreEvaluationResponse
            A response object containing evaluation scores.

        Notes
        -----
        This function retrieves the scores of a chat model based on user input, using the `get_scores()` method of the chat model.
        """
        scores = chat_model.get_scores(request.messages, max_length=request.max_length)
        return ScoreEvaluationResponse(model=request.model, scores=scores)

    return app


if __name__ == "__main__":
    chat_model = ChatModel()
    app = create_app(chat_model)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("API_PORT", 8000)), workers=1)
