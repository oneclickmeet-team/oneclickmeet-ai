"""실제 LLM 어댑터. SDK는 쓸 때만 import 하므로 설치하지 않은 제공자는 무시됩니다.

모델 이름은 코드에 박지 않고 환경변수 PLACEFLOW_LLM_MODEL(또는 --model)로 받습니다.
제공자 SDK의 세부 API는 버전에 따라 바뀔 수 있으니, 오류가 나면 각 SDK 문서를 확인하세요.
"""
from __future__ import annotations

import json
import os

from .base import ImageInput


MAX_OUTPUT_TOKENS = 4096  # 응답이 끝없이 길어지는 경우 (실측 약 140초 후 JSON 잘림) 를 빨리 끊음


class OpenAIProvider:
    name = "openai"
    supports_vision = True

    def __init__(self, model: str):
        from openai import OpenAI  # pip install openai

        self.client = OpenAI()  # OPENAI_API_KEY
        self.model = model

    def extract(self, system, user_text, images: list[ImageInput], schema):
        content = [{"type": "text", "text": user_text}]
        for img in images:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{img.mime_type};base64,{img.b64()}"}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        kwargs = {
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "extraction_result", "schema": schema, "strict": False}},
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        # 모델마다 지원 옵션이 달라서, 거절된 옵션을 빼며 최대 3번 시도합니다.
        #  - temperature 를 기본값만 허용하는 모델 → temperature 제거
        #  - json_schema 응답 형식을 지원하지 않는 모델 → json_object + 스키마를 프롬프트에 포함
        last_err = None
        for _ in range(3):
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
                break
            except Exception as e:
                msg = str(e).lower()
                last_err = e
                if "max_tokens" in msg and "max_tokens" in kwargs:  # 최신 모델은 max_completion_tokens 만 받음
                    kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                elif "temperature" in msg and "temperature" in kwargs:
                    kwargs.pop("temperature")
                elif ("response_format" in msg or "json_schema" in msg) and \
                        kwargs["response_format"]["type"] == "json_schema":
                    kwargs["response_format"] = {"type": "json_object"}
                    messages[0] = {"role": "system", "content": system + "\n\n## 출력 JSON 스키마\n"
                                   + json.dumps(schema, ensure_ascii=False)}
                else:
                    raise
        else:
            raise last_err
        if resp.choices[0].finish_reason == "length":
            raise RuntimeError("응답이 너무 길어 잘렸습니다 (finish_reason=length)")
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError(f"모델이 응답을 거절했습니다: {msg.refusal}")
        if not msg.content:
            raise RuntimeError(f"빈 응답 (finish_reason={resp.choices[0].finish_reason})")
        return json.loads(msg.content)


class AnthropicProvider:
    name = "anthropic"
    supports_vision = True

    def __init__(self, model: str):
        import anthropic  # pip install anthropic

        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY
        self.model = model

    def extract(self, system, user_text, images: list[ImageInput], schema):
        content = [{"type": "image", "source": {"type": "base64", "media_type": i.mime_type, "data": i.b64()}}
                   for i in images]
        content.append({"type": "text", "text": user_text})
        # 도구 호출을 강제해서 스키마에 맞는 JSON 을 받습니다.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=[{"name": "save_places", "description": "추출한 장소 정보를 저장", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "save_places"},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError("Anthropic 응답에 tool_use 블록이 없습니다.")


class GeminiProvider:
    name = "gemini"
    supports_vision = True

    def __init__(self, model: str):
        from google import genai  # pip install google-genai

        self.client = genai.Client()  # GEMINI_API_KEY
        self.model = model

    def extract(self, system, user_text, images: list[ImageInput], schema):
        from google.genai import types

        parts = [types.Part.from_bytes(data=i.data, mime_type=i.mime_type) for i in images]
        parts.append(user_text)
        resp = self.client.models.generate_content(
            model=self.model,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
                temperature=0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        return json.loads(resp.text)


def get_provider(name: str, model: str | None = None):
    name = name.lower()
    if name in ("heuristic", "rule", "none"):
        from ..heuristic import HeuristicExtractor

        return HeuristicExtractor()
    model = model or os.environ.get("PLACEFLOW_LLM_MODEL")
    if not model:
        raise ValueError(f"{name} 제공자는 모델 이름이 필요합니다 — 화면의 '모델 이름' 칸(또는 --model, .env 의 PLACEFLOW_LLM_MODEL)에 입력하세요.")
    cls = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "claude": AnthropicProvider,
           "gemini": GeminiProvider, "google": GeminiProvider}.get(name)
    if not cls:
        raise ValueError(f"알 수 없는 LLM 제공자: {name}")
    return cls(model)
